"""Dispatch: main task dispatch lifecycle."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from .agent import run_agent, run_verify
from .backends import AgentBackend, discover_backends
from .config import Config
from .constants import (
    TRUNCATE_ERROR_MESSAGE,
    TRUNCATE_REASON,
    TRUNCATE_REVIEW_OUTPUT,
)
from .db import Database
from .dispatch_helpers import (
    _mark_issue_failed,
    _mark_issue_no_changes,
    _pick_fallback_agent,
    _pick_rate_limit_fallback,
    _pick_retry_model,
    pick_retry_agent_and_model,
    sync_agent_credentials,
)
from .git_ops import (
    _get_repo_lock,
    cleanup_task_artifacts,
    clone_or_fetch,
    ensure_repo_permissions,
    make_branch_name,
    setup_worktree,
)
from .github import comment_on_issue, repo_full_name_from_url
from .repo_intel import detect_and_store_stack, record_learning
from .review import create_pr

log = logging.getLogger("backporcher.dispatch")


async def dispatch_task(
    task: dict,
    config: Config,
    db: Database,
    backends: dict[str, AgentBackend] | None = None,
):
    """Full lifecycle: fetch -> worktree -> agent -> PR."""
    task_id = task["id"]

    # Resolve backend registry (lazily discover if not passed)
    if backends is None:
        backends = discover_backends(config)

    try:
        repo = await db.get_repo(task["repo_id"])
        if not repo:
            raise ValueError(f"Repo {task['repo_id']} not found")

        # Serialize git operations per-repo (fetch + worktree creation)
        repo_lock = _get_repo_lock(repo["id"])
        async with repo_lock:
            await db.add_log(task_id, "Fetching repository...")
            repo_path = await clone_or_fetch(repo, config)
            await ensure_repo_permissions(repo_path, config)
            await detect_and_store_stack(repo, db)

            branch = make_branch_name(task_id, task["prompt"])
            await db.update_task(task_id, branch_name=branch)
            await db.add_log(task_id, f"Creating worktree on branch {branch}")
            worktree_path = await setup_worktree(
                repo_path,
                task_id,
                branch,
                repo["default_branch"],
            )
            await db.update_task(task_id, worktree_path=str(worktree_path))

        # Ensure agent credentials are fresh before launching
        await sync_agent_credentials(config)

        # Resolve the backend for this task (fall back to default if unknown)
        task_agent = task.get("agent", config.default_agent)
        backend = backends.get(task_agent) or backends.get(config.default_agent)

        # Record agent start timing and model info.
        # Each backend's display_model() returns a human-readable string
        # (e.g. "gemini/auto", "opencode/qwen3.5-9b", or just "sonnet" for Claude).
        effective_model = backend.display_model(task["model"]) if backend else task["model"]
        agent_start_now = datetime.now(timezone.utc).isoformat()
        await db.update_task(
            task_id,
            agent_started_at=agent_start_now,
            initial_model=task["model"],
            model_used=effective_model,
        )
        await db.record_metric(
            "agent_start",
            task_id=task_id,
            repo=task.get("repo_name"),
            model=task["model"],
        )

        # Run agent
        await db.add_log(task_id, "Running agent...")
        exit_code, summary = await run_agent(task, worktree_path, config, db, backend=backend)

        # Record agent finish timing
        agent_finish_now = datetime.now(timezone.utc).isoformat()
        await db.update_task(
            task_id,
            exit_code=exit_code,
            output_summary=summary[:TRUNCATE_REVIEW_OUTPUT] if summary else None,
            agent_finished_at=agent_finish_now,
            model_used=effective_model,
        )

        if exit_code != 0:
            retry_count = task.get("retry_count", 0)
            max_retries = config.max_task_retries
            is_rate_limited = task.get("_rate_limited", False)

            # Rate-limited agents skip straight to agent fallback —
            # don't burn retry slots on an exhausted API quota.
            if is_rate_limited:
                fallback_count = task.get("agent_fallback_count", 0) or 0
                next_agent = _pick_rate_limit_fallback(task, config, backends)
                if next_agent:
                    new_fallback = fallback_count + 1
                    await db.update_task(
                        task_id,
                        status="queued",
                        error_message=None,
                        started_at=None,
                        branch_name=None,
                        worktree_path=None,
                        retry_count=0,
                        agent=next_agent,
                        agent_fallback_count=new_fallback,
                        model=task.get("model", config.default_model),
                    )
                    await db.add_log(
                        task_id,
                        f"Rate limit on {task_agent} — immediate fallback to {next_agent} (fallback {new_fallback})",
                        level="warn",
                    )
                    log.info(
                        "Task %d: rate limit on %s, immediate fallback to %s (fallback %d)",
                        task_id,
                        task_agent,
                        next_agent,
                        new_fallback,
                    )
                    await db.record_metric(
                        "rate_limit_fallback",
                        task_id=task_id,
                        repo=task.get("repo_name"),
                        model=config.default_model,
                    )
                    await cleanup_task_artifacts(task, db)
                    return
                # No fallback available — fall through to normal retry logic

            if retry_count < max_retries:
                new_count = retry_count + 1
                new_agent, new_model = pick_retry_agent_and_model(task, new_count, config, backends)
                await db.update_task(
                    task_id,
                    status="queued",
                    error_message=None,
                    started_at=None,
                    branch_name=None,
                    worktree_path=None,
                    retry_count=new_count,
                    model=new_model,
                    agent=new_agent,
                )
                reason = f"exit {exit_code}"
                await db.add_log(
                    task_id,
                    f"Agent failed ({reason}), retry {new_count}/{max_retries} (agent={new_agent}, model={new_model})",
                    level="warn",
                )
                log.info(
                    "Task %d: agent failed (%s), retry %d/%d (agent=%s, model=%s)",
                    task_id,
                    reason,
                    new_count,
                    max_retries,
                    new_agent,
                    new_model,
                )
                await db.record_metric(
                    "retry_agent",
                    task_id=task_id,
                    repo=task.get("repo_name"),
                    model=new_model,
                )
                return

            # Retries exhausted -- try agent fallback before permanent failure
            fallback_count = task.get("agent_fallback_count", 0) or 0
            next_agent = _pick_fallback_agent(task, config)
            if next_agent and next_agent in backends:
                new_fallback = fallback_count + 1
                await db.update_task(
                    task_id,
                    status="queued",
                    error_message=None,
                    started_at=None,
                    branch_name=None,
                    worktree_path=None,
                    retry_count=0,
                    agent=next_agent,
                    agent_fallback_count=new_fallback,
                    model=config.default_model,
                )
                await db.add_log(
                    task_id,
                    f"Agent fallback: {task_agent} -> {next_agent} (fallback {new_fallback})",
                    level="warn",
                )
                log.info(
                    "Task %d: agent fallback %s -> %s (fallback %d)",
                    task_id,
                    task_agent,
                    next_agent,
                    new_fallback,
                )
                await db.record_metric(
                    "agent_fallback",
                    task_id=task_id,
                    repo=task.get("repo_name"),
                    model=config.default_model,
                )
                return

            # Max retries + fallback exhausted -- permanent failure
            now = datetime.now(timezone.utc).isoformat()
            await db.update_task(
                task_id,
                status="failed",
                error_message=f"Agent exited with code {exit_code} (retries exhausted)",
                completed_at=now,
            )
            await db.add_log(task_id, f"Agent failed (exit {exit_code}), retries exhausted", level="error")
            await record_learning(
                db,
                task["repo_id"],
                task_id,
                "agent_failure",
                f"Agent failed (exit {exit_code}) on: {task['prompt'][:TRUNCATE_REASON]}",
            )
            await _mark_issue_failed(
                task,
                db,
                f"Agent failed with exit code {exit_code} (retries exhausted).",
            )
            await cleanup_task_artifacts(task, db)
            cascaded = await db.handle_dependency_failure(task_id)
            if cascaded:
                log.info("Task #%d failure cascaded to tasks: %s", task_id, cascaded)
            return

        # Build verification loop
        verify_command = repo.get("verify_command")
        if verify_command:
            for attempt in range(1, config.max_verify_retries + 2):  # +2: initial + retries
                passed, verify_output = await run_verify(
                    worktree_path,
                    verify_command,
                    task_id,
                    db,
                    config,
                )
                if passed:
                    break

                if attempt > config.max_verify_retries:
                    now = datetime.now(timezone.utc).isoformat()
                    await db.update_task(
                        task_id,
                        status="failed",
                        error_message=f"Verify failed after {config.max_verify_retries} fix attempts",
                        completed_at=now,
                    )
                    await db.add_log(
                        task_id,
                        f"Verify failed after {config.max_verify_retries} retries, giving up",
                        level="error",
                    )
                    await record_learning(
                        db,
                        task["repo_id"],
                        task_id,
                        "verify_failure",
                        f"Build verification failed ({verify_command}) on: {task['prompt'][:TRUNCATE_REASON]}",
                    )
                    await _mark_issue_failed(
                        task,
                        db,
                        f"Build verification failed after {config.max_verify_retries} fix attempts.",
                    )
                    await cleanup_task_artifacts(task, db)
                    cascaded = await db.handle_dependency_failure(task_id)
                    if cascaded:
                        log.info("Task #%d failure cascaded to tasks: %s", task_id, cascaded)
                    return

                # Re-run agent with verify failure context
                await db.add_log(
                    task_id,
                    f"Verify fix attempt {attempt}/{config.max_verify_retries}",
                )
                fix_prompt = (
                    f"{task['prompt']}\n\n"
                    f"---\n"
                    f"IMPORTANT: Your previous changes failed the build/test verification.\n"
                    f"The verify command `{verify_command}` failed with this output:\n\n"
                    f"```\n{verify_output}\n```\n\n"
                    f"Fix the errors and make sure the build passes."
                )
                fix_task = dict(task)
                fix_task["prompt"] = fix_prompt
                exit_code, summary = await run_agent(fix_task, worktree_path, config, db, backend=backend)
                if exit_code != 0:
                    # Let the outer exception handler deal with retry/escalation
                    raise RuntimeError(f"Verify fix agent exited with code {exit_code}")

        # Create PR
        await db.add_log(task_id, "Creating pull request...")
        # Re-read task to get branch_name
        task = await db.get_task(task_id)
        pr_url = await create_pr(worktree_path, task, repo, db)
        now = datetime.now(timezone.utc).isoformat()

        if pr_url:
            await db.update_task(
                task_id,
                status="pr_created",
                pr_url=pr_url,
                completed_at=now,
            )
            await db.add_log(task_id, f"PR created: {pr_url}")

            # Comment on the GitHub issue if this task came from one
            issue_num = task.get("github_issue_number")
            if issue_num:
                repo_full = repo_full_name_from_url(repo["github_url"])
                await comment_on_issue(
                    repo_full,
                    issue_num,
                    f"PR created: {pr_url}\n\nAwaiting CI checks.",
                )
        else:
            await db.update_task(
                task_id,
                status="completed",
                completed_at=now,
            )
            await db.add_log(task_id, "Completed (no changes)")
            await _mark_issue_no_changes(task, db)
            await cleanup_task_artifacts(task, db)

    except Exception as e:
        log.exception("Task %d failed", task_id)
        err_str = str(e)[:TRUNCATE_ERROR_MESSAGE]
        now = datetime.now(timezone.utc).isoformat()

        retry_count = task.get("retry_count", 0)
        if retry_count < config.max_task_retries:
            new_count = retry_count + 1
            new_model = _pick_retry_model(task.get("model", "sonnet"), new_count)
            await db.update_task(
                task_id,
                status="queued",
                error_message=None,
                started_at=None,
                branch_name=None,
                worktree_path=None,
                retry_count=new_count,
                model=new_model,
            )
            await db.add_log(
                task_id,
                f"Error, retry {new_count}/{config.max_task_retries} (model={new_model}): {err_str[:TRUNCATE_REASON]}",
                level="warn",
            )
            log.info(
                "Task %d: error, retry %d/%d (model=%s)",
                task_id,
                new_count,
                config.max_task_retries,
                new_model,
            )
            await db.record_metric(
                "retry_agent",
                task_id=task_id,
                repo=task.get("repo_name"),
                model=new_model,
            )
        else:
            await db.update_task(
                task_id,
                status="failed",
                error_message=err_str,
                completed_at=now,
            )
            await db.add_log(task_id, f"Fatal error (retries exhausted): {e}", level="error")
            await _mark_issue_failed(
                task,
                db,
                f"Task failed with error: {err_str[:TRUNCATE_REASON]}",
            )
            await cleanup_task_artifacts(task, db)
            cascaded = await db.handle_dependency_failure(task_id)
            if cascaded:
                log.info("Task #%d failure cascaded to tasks: %s", task_id, cascaded)

"""Dispatch helpers: failure handling, credential sync, retry logic, agent fallback."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

from .agent import run_agent
from .config import Config
from .constants import (
    CREDENTIAL_FILE_MODE,
    TIMEOUT_COMMIT_PUSH,
)
from .db import Database
from .git_ops import _get_repo_lock, cleanup_task_artifacts, run_cmd
from .github import comment_on_issue, repo_full_name_from_url, update_issue_labels

log = logging.getLogger("backporcher.dispatch")


async def _mark_issue_failed(task: dict, db: Database, reason: str):
    """Update GitHub labels on the source issue when a task permanently fails.

    Moves from backporcher-in-progress -> backporcher-failed and posts a comment.
    No-op if the task didn't originate from a GitHub issue.
    """
    issue_num = task.get("github_issue_number")
    if not issue_num:
        return
    repo = await db.get_repo(task["repo_id"])
    if not repo:
        return
    repo_full = repo_full_name_from_url(repo["github_url"])
    await update_issue_labels(
        repo_full,
        issue_num,
        add=["backporcher-failed"],
        remove=["backporcher-in-progress"],
    )
    await comment_on_issue(
        repo_full,
        issue_num,
        f"{reason}\n\nRe-add the `backporcher` label to retry.",
    )


async def sync_agent_credentials(config: Config):
    """Copy admin's Claude credentials to agent user if they're newer."""
    if not config.agent_user:
        return
    admin_cred = Path.home() / ".claude" / ".credentials.json"
    agent_cred = Path(f"/home/{config.agent_user}") / ".claude" / ".credentials.json"
    if not admin_cred.exists():
        return
    # Use sudo stat to check agent cred mtime (file is 600 owned by agent user)
    need_sync = True
    rc, out, _ = await run_cmd("sudo", "stat", "-c", "%Y", str(agent_cred))
    if rc == 0:
        try:
            agent_mtime = float(out.strip())
            need_sync = admin_cred.stat().st_mtime > agent_mtime
        except (ValueError, OSError):
            pass

    if need_sync:
        log.info("Syncing Claude credentials to %s", config.agent_user)
        rc, _, err = await run_cmd(
            "sudo",
            "install",
            "-m",
            f"{CREDENTIAL_FILE_MODE:o}",
            "-o",
            config.agent_user,
            "-g",
            "backporcher",
            str(admin_cred),
            str(agent_cred),
        )
        if rc != 0:
            log.warning("Failed to sync credentials: %s", err.strip())


def _pick_retry_model(current_model: str, retry_count: int) -> str:
    """Escalate model on retry. Sonnet -> opus after first attempt."""
    if current_model == "sonnet" and retry_count >= 1:
        log.info("Model escalation: sonnet -> opus (retry %d)", retry_count)
        return "opus"
    return current_model


def pick_retry_agent_and_model(task: dict, retry_count: int, config: "Config", backends: dict) -> tuple[str, str]:
    """Pick agent + model for a retry attempt.

    Strategy:
      retry 1: escalate model (sonnet→opus), keep same agent
      retry 2: fall back to next agent in chain
      retry 3: fall back again (or stay on last agent with opus)
    """
    current_agent = task.get("agent", config.default_agent)
    current_model = task.get("model", config.default_model)

    if retry_count <= 1:
        # First retry: just escalate model, keep agent
        return current_agent, _pick_retry_model(current_model, retry_count)

    # Later retries: try next agent in fallback chain
    next_agent = _pick_fallback_agent(task, config)
    if next_agent and next_agent in backends:
        log.info(
            "Agent fallback: %s -> %s (retry %d)",
            current_agent,
            next_agent,
            retry_count,
        )
        return next_agent, current_model
    # No more agents — stay on current with escalated model
    return current_agent, _pick_retry_model(current_model, retry_count)


def _pick_fallback_agent(task: dict, config: Config) -> str | None:
    """Return the next agent in the fallback chain, or None if exhausted."""
    chain = config.fallback_chain
    current = task.get("agent", "claude")
    try:
        idx = chain.index(current)
        if idx + 1 < len(chain):
            return chain[idx + 1]
    except ValueError:
        pass
    return None


async def retry_with_ci_context(
    task: dict,
    ci_logs: str,
    config: Config,
    db: Database,
):
    """Re-run the agent with CI failure context on the existing branch."""
    task_id = task["id"]
    repo = await db.get_repo(task["repo_id"])
    if not repo:
        raise ValueError(f"Repo {task['repo_id']} not found")

    worktree_path = Path(task["worktree_path"])
    if not worktree_path.exists():
        raise RuntimeError(f"Worktree missing for task {task_id}: {worktree_path}")

    # Pull latest on the branch
    repo_lock = _get_repo_lock(repo["id"])
    async with repo_lock:
        rc, _, err = await run_cmd("git", "pull", "--rebase", cwd=worktree_path)
        if rc != 0:
            log.warning("git pull failed for retry task %d: %s", task_id, err)

    # Build augmented prompt
    augmented_prompt = (
        f"{task['prompt']}\n\n"
        f"---\n"
        f"IMPORTANT: The previous attempt created a PR but CI checks failed. "
        f"Please fix the issues shown in the CI logs below and commit the fixes.\n\n"
        f"CI FAILURE LOGS:\n```\n{ci_logs}\n```"
    )

    # Temporarily patch the task's prompt for the agent run
    patched_task = dict(task)
    patched_task["prompt"] = augmented_prompt

    await db.add_log(task_id, f"Retry #{task['retry_count']}: running agent with CI context")
    exit_code, summary = await run_agent(patched_task, worktree_path, config, db)

    if exit_code != 0:
        now = datetime.now(timezone.utc).isoformat()
        await db.update_task(
            task_id,
            status="failed",
            error_message=f"Retry agent exited with code {exit_code}",
            completed_at=now,
        )
        await _mark_issue_failed(
            task,
            db,
            f"CI retry agent failed with exit code {exit_code}.",
        )
        await cleanup_task_artifacts(task, db)
        return

    # Push fixes (force-with-lease since we're updating the same branch)
    branch = task["branch_name"]
    rc, _, err = await run_cmd(
        "git",
        "push",
        "--force-with-lease",
        "origin",
        branch,
        cwd=worktree_path,
        timeout=TIMEOUT_COMMIT_PUSH,
    )
    if rc != 0:
        await db.add_log(task_id, f"Force push failed on retry: {err}", level="error")
        raise RuntimeError(f"git push failed on retry: {err}")

    # Back to pr_created -- CI monitor will check again
    await db.update_task(task_id, status="pr_created")
    await db.add_log(task_id, f"Retry #{task['retry_count']}: pushed fixes, awaiting CI")

"""CI monitor loop body: check CI status, retry failures, cleanup artifacts."""

import logging

from . import notifications
from .config import Config
from .db import Database
from .dispatcher import (
    _mark_issue_failed,
    cleanup_task_artifacts,
    record_learning,
    retry_with_ci_context,
)
from .github import (
    close_pr,
    comment_on_issue,
    comment_on_pr,
    get_ci_failure_logs,
    get_pr_ci_status,
    is_pr_conflicting,
    repo_full_name_from_url,
    update_issue_labels,
)
from .worker_merge import merge_approved_task, try_merge

log = logging.getLogger("backporcher.worker")


async def monitor_ci(db: Database, config: Config) -> None:
    """One iteration of CI monitoring across all PR tasks."""
    # Check reviewed tasks for CI status
    pr_tasks = await db.list_pr_tasks()
    for task in pr_tasks:
        pr_number = task.get("pr_number")
        if not pr_number:
            continue

        repo_full = repo_full_name_from_url(task["github_url"])
        ci = await get_pr_ci_status(repo_full, pr_number)

        if ci.state == "pending":
            continue

        if ci.state == "success":
            await handle_ci_passed(db, config, task, repo_full)
        elif ci.state == "no_checks":
            await _handle_no_checks(db, task, pr_number, config=config)
        elif ci.state == "failure":
            await handle_ci_failure(db, config, task, repo_full, ci)

    # Sweep ci_passed tasks stuck due to merge conflict
    stuck_tasks = await db.list_tasks_by_status("ci_passed")
    for task in stuck_tasks:
        pr_number = task.get("pr_number")
        if not pr_number:
            continue
        repo_full = repo_full_name_from_url(task["github_url"])
        conflicting = await is_pr_conflicting(repo_full, pr_number)
        if conflicting:
            task_id = task["id"]
            log.warning("Task #%d: PR #%d still conflicting, re-queuing", task_id, pr_number)
            await db.add_log(
                task_id,
                f"PR #{pr_number} has merge conflicts — closing and re-queuing",
                level="warn",
            )
            await close_pr(
                repo_full,
                pr_number,
                comment="Merge conflict detected. Closing PR and re-running agent from latest main.",
            )
            await db.update_task(
                task_id,
                status="queued",
                branch_name=None,
                worktree_path=None,
                pr_url=None,
                pr_number=None,
                review_summary=None,
                started_at=None,
                completed_at=None,
            )

    # Sweep approved tasks: ci_passed with hold cleared -> merge
    approved_tasks = await db.list_tasks_by_status("ci_passed")
    for task in approved_tasks:
        if task.get("hold"):
            continue
        pr_number = task.get("pr_number")
        if not pr_number:
            continue
        repo_full = repo_full_name_from_url(task["github_url"])
        await merge_approved_task(db, task, repo_full)

    # Process retrying tasks
    retry_tasks = await db.list_retrying_tasks()
    for task in retry_tasks:
        await process_retry(db, config, task)


async def _handle_no_checks(db: Database, task: dict, pr_number: int, config: Config | None = None) -> None:
    """Handle PRs with no CI checks configured.

    In full-auto mode, treat no-CI as passed and proceed to merge.
    Otherwise, log a warning and block.
    """
    task_id = task["id"]

    # In full-auto mode, skip CI requirement — merge immediately.
    if config and config.approval_mode == "full-auto":
        await db.add_log(
            task_id,
            "No CI checks configured — auto-merging (full-auto mode)",
        )
        log.info("Task #%d: no CI checks, auto-merging (full-auto)", task_id)
        repo_full = repo_full_name_from_url(task["github_url"])
        await handle_ci_passed(db, config, task, repo_full)
        return

    logs = await db.get_logs(task_id, limit=50)
    already_warned = any("no CI checks configured" in (entry.get("message", "")) for entry in logs)
    if not already_warned:
        await db.add_log(
            task_id,
            "No CI checks configured for this repo. "
            "PR blocked from auto-merge. Add a GitHub Actions "
            "workflow and push to trigger checks.",
            level="warn",
        )
        log.warning(
            "Task #%d: PR #%d has no CI checks, blocking merge",
            task_id,
            pr_number,
        )


async def handle_ci_passed(db: Database, config: Config, task: dict, repo_full: str) -> None:
    """CI passed — auto-merge PR (or hold for approval), mark completed, update labels."""
    task_id = task["id"]
    pr_number = task.get("pr_number")

    await db.update_task(task_id, status="ci_passed")
    await db.add_log(task_id, "CI checks passed")
    log.info("Task #%d: CI passed", task_id)

    # Merge gate: in non-full-auto modes, hold for approval
    if config.approval_mode != "full-auto":
        await db.set_hold(task_id, "merge_approval")
        await db.add_log(task_id, "Held for merge approval — run `backporcher approve %d` or use dashboard" % task_id)
        log.info("Task #%d: held for merge approval", task_id)
        if pr_number:
            await comment_on_pr(
                repo_full,
                pr_number,
                f"CI passed. Awaiting merge approval.\n\n"
                f"Run `backporcher approve {task_id}` or use the dashboard to merge.",
            )
        title = task.get("prompt", "")[:80]
        await notifications.notify_hold(task_id, title, "merge_approval")
        return

    # Auto-merge the PR
    if pr_number:
        await try_merge(db, task, task_id, pr_number, repo_full)


async def handle_ci_failure(db: Database, config: Config, task: dict, repo_full: str, ci) -> None:
    """CI failed — retry or mark failed."""
    task_id = task["id"]
    retry_count = task.get("retry_count", 0)

    if retry_count < config.max_ci_retries:
        new_count = retry_count + 1
        await db.update_task(
            task_id,
            status="retrying",
            retry_count=new_count,
        )
        await db.add_log(
            task_id,
            f"CI failed ({', '.join(ci.failed_checks[:3])}). Retry {new_count}/{config.max_ci_retries}",
            level="warn",
        )
        log.info("Task #%d: CI failed, retry %d/%d", task_id, new_count, config.max_ci_retries)
        await db.record_metric(
            "retry_ci",
            task_id=task_id,
            repo=task.get("repo_name"),
            model=task.get("model"),
        )

        issue_num = task.get("github_issue_number")
        if issue_num:
            await comment_on_issue(
                repo_full,
                issue_num,
                f"CI failed: {', '.join(ci.failed_checks[:3])}\n\n"
                f"Auto-retrying ({new_count}/{config.max_ci_retries})...",
            )
    else:
        await db.update_task(task_id, status="failed")
        await db.add_log(
            task_id,
            f"CI failed after {config.max_ci_retries} retries: {', '.join(ci.failed_checks[:3])}",
            level="error",
        )
        log.warning("Task #%d: CI failed, max retries exhausted", task_id)
        await record_learning(
            db,
            task["repo_id"],
            task_id,
            "ci_failure",
            f"CI failed ({', '.join(ci.failed_checks[:3])}) on: {task.get('prompt', '')[:200]}",
        )

        issue_num = task.get("github_issue_number")
        if issue_num:
            await update_issue_labels(
                repo_full,
                issue_num,
                add=["backporcher-failed"],
                remove=["backporcher-in-progress"],
            )
            await comment_on_issue(
                repo_full,
                issue_num,
                f"CI failed after {config.max_ci_retries} retries. "
                f"Failed checks: {', '.join(ci.failed_checks[:5])}\n\n"
                f"Marking as failed. Re-add the `backporcher` label to retry.",
            )
        await cleanup_task_artifacts(task, db)

        title = task.get("prompt", "")[:80]
        await notifications.notify_failed(task_id, title, f"CI failed after {config.max_ci_retries} retries")


async def process_retry(db: Database, config: Config, task: dict) -> None:
    """Fetch CI logs and re-run agent with context."""
    task_id = task["id"]
    branch = task.get("branch_name")
    if not branch:
        await db.update_task(task_id, status="failed", error_message="No branch for retry")
        await _mark_issue_failed(task, db, "CI retry failed: no branch available.")
        await cleanup_task_artifacts(task, db)
        return

    repo_full = repo_full_name_from_url(task["github_url"])
    log.info("Task #%d: fetching CI logs for retry", task_id)

    ci_logs = await get_ci_failure_logs(repo_full, branch)

    try:
        await retry_with_ci_context(task, ci_logs, config, db)
    except Exception as e:
        log.exception("Retry failed for task %d", task_id)
        await db.update_task(
            task_id,
            status="failed",
            error_message=f"Retry error: {str(e)[:500]}",
        )
        await db.add_log(task_id, f"Retry error: {e}", level="error")
        await _mark_issue_failed(
            task,
            db,
            f"CI retry failed with error: {str(e)[:300]}",
        )
        await cleanup_task_artifacts(task, db)


async def cleanup_terminal_tasks(db: Database, min_age_minutes: int, running: bool) -> None:
    """One iteration of artifact cleanup for terminal tasks."""
    tasks = await db.list_cleanable_tasks(min_age_minutes=min_age_minutes)
    if tasks:
        log.info("Cleanup loop: found %d tasks to clean", len(tasks))
    for task in tasks:
        if not running:
            break
        try:
            await cleanup_task_artifacts(task, db)
        except OSError:
            log.exception("Cleanup failed for task #%d", task["id"])

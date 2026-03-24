"""Merge and post-merge bookkeeping for the CI monitor loop."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from . import notifications
from .db import Database
from .dispatcher import (
    cleanup_task_artifacts,
    record_learning,
)
from .github import (
    close_issue,
    close_pr,
    comment_on_issue,
    is_pr_conflicting,
    merge_pr,
    update_issue_labels,
)

log = logging.getLogger("backporcher.worker")


def compute_duration(start: str | None, end: str | None) -> float | None:
    """Compute duration in seconds between two ISO timestamps."""
    if not start or not end:
        return None
    try:
        s = datetime.fromisoformat(start.replace("Z", "+00:00"))
        e = datetime.fromisoformat(end.replace("Z", "+00:00"))
        # SQLite datetime('now') produces naive timestamps (no tzinfo).
        # Treat naive timestamps as UTC to avoid offset-naive vs offset-aware errors.
        if s.tzinfo is None:
            s = s.replace(tzinfo=timezone.utc)
        if e.tzinfo is None:
            e = e.replace(tzinfo=timezone.utc)
        return (e - s).total_seconds()
    except (ValueError, AttributeError):
        return None


async def try_merge(
    db: Database,
    task: dict,
    task_id: int,
    pr_number: int,
    repo_full: str,
    *,
    clear_hold: bool = False,
) -> None:
    """Attempt to merge a PR, handling success, conflict, and unknown failure."""
    merged = await merge_pr(repo_full, pr_number)
    if merged:
        await finalize_merge(db, task, task_id, pr_number, repo_full)
        return

    # Merge failed — check if it's a conflict
    conflicting = await is_pr_conflicting(repo_full, pr_number)
    if conflicting:
        await db.add_log(
            task_id,
            f"PR #{pr_number} has merge conflicts — closing and re-queuing",
            level="warn",
        )
        log.warning("Task #%d: PR #%d has merge conflicts, re-queuing", task_id, pr_number)
        await close_pr(
            repo_full,
            pr_number,
            comment="Merge conflict detected. Closing PR and re-running agent from latest main.",
        )
        requeue_fields: dict = {
            "status": "queued",
            "branch_name": None,
            "worktree_path": None,
            "pr_url": None,
            "pr_number": None,
            "review_summary": None,
            "started_at": None,
            "completed_at": None,
        }
        if clear_hold:
            requeue_fields["hold"] = None
        await db.update_task(task_id, **requeue_fields)
    else:
        await db.update_task(
            task_id,
            status="failed",
            error_message=f"Merge failed for PR #{pr_number} (no conflict detected)",
        )
        await db.add_log(task_id, f"Failed to merge PR #{pr_number} (no conflict)", level="error")
        log.warning("Task #%d: merge failed for PR #%d (no conflict), marking failed", task_id, pr_number)

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
                f"Merge failed for PR #{pr_number} (reason unknown, not a conflict).\n\n"
                f"Re-add the `backporcher` label to retry.",
            )
        await cleanup_task_artifacts(task, db)


async def finalize_merge(db: Database, task: dict, task_id: int, pr_number: int, repo_full: str) -> None:
    """Post-merge bookkeeping: update DB, labels, issue, metrics, webhook."""
    now = datetime.now(timezone.utc).isoformat()
    await db.update_task(task_id, status="completed", completed_at=now)
    await db.add_log(task_id, f"PR #{pr_number} merged (squash)")
    log.info("Task #%d: PR #%d merged", task_id, pr_number)
    await record_learning(
        db,
        task["repo_id"],
        task_id,
        "success",
        f"Successfully merged: {task.get('prompt', '')[:200]}",
    )

    # Record merge metric
    try:
        merge_duration = compute_duration(task.get("created_at"), now)
        model = task.get("model_used") or task.get("model")
        await db.record_metric(
            "merge",
            task_id=task_id,
            repo=task.get("repo_name"),
            model=model,
            value=merge_duration,
        )
    except Exception:
        log.warning("Failed to record merge metric for task %d", task_id, exc_info=True)

    issue_num = task.get("github_issue_number")
    if issue_num:
        await update_issue_labels(
            repo_full,
            issue_num,
            add=["backporcher-done"],
            remove=["backporcher-in-progress"],
        )
        await comment_on_issue(
            repo_full,
            issue_num,
            "CI passed. PR has been merged. Closing issue.",
        )
        await close_issue(repo_full, issue_num)
    await cleanup_task_artifacts(task, db)

    # Webhook: completed
    title = task.get("prompt", "")[:80]
    dur = compute_duration(task.get("created_at"), now)
    dur_str = f"{int(dur // 60)}m" if dur else "?"
    model = task.get("model_used") or task.get("model") or "?"
    await notifications.notify_completed(task_id, title, dur_str, model)


async def merge_approved_task(db: Database, task: dict, repo_full: str) -> None:
    """Merge a task that has been approved (ci_passed, hold cleared)."""
    task_id = task["id"]
    pr_number = task.get("pr_number")

    log.info("Task #%d: merge approved, proceeding", task_id)
    await db.add_log(task_id, "Merge approved, proceeding to merge")

    await try_merge(db, task, task_id, pr_number, repo_full, clear_hold=True)

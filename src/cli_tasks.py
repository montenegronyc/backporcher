"""CLI task commands: fleet, status, cancel, cleanup."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from datetime import datetime, timezone

from .cli_repo import cleanup_single_task
from .config import load_config
from .db_sync import SyncDatabase


def _get_db() -> SyncDatabase:
    config = load_config()
    db = SyncDatabase(config.db_path)
    db.connect()
    return db


def _status_badge(status: str, hold: str | None = None) -> str:
    if hold == "merge_approval":
        return "APRV"
    elif hold == "dispatch_approval":
        return "GATE"
    elif hold == "user_hold":
        return "HOLD"
    elif hold == "conflict_hold":
        return "CNFL"
    return {
        "queued": "WAIT",
        "working": " RUN",
        "pr_created": "  PR",
        "reviewing": " REV",
        "reviewed": "RVWD",
        "ci_passed": "  OK",
        "retrying": " RTY",
        "completed": "DONE",
        "failed": "FAIL",
        "cancelled": " CXL",
    }.get(status, status[:4].upper())


def cmd_fleet(args):
    """Dashboard showing all active work across the fleet."""
    db = _get_db()
    tasks = db.list_tasks(limit=50)

    if not tasks:
        print("No tasks. Create a GitHub issue with label 'backporcher' to dispatch work.")
        db.close()
        return

    # Count by status
    counts = {}
    held_count = 0
    for t in tasks:
        counts[t["status"]] = counts.get(t["status"], 0) + 1
        if t.get("hold"):
            held_count += 1

    # Check global pause
    paused = db.is_queue_paused()

    # Header
    parts = []
    if paused:
        parts.append("PAUSED")
    for status, label in [
        ("working", "running"),
        ("queued", "queued"),
        ("pr_created", "awaiting review"),
        ("reviewing", "reviewing"),
        ("reviewed", "awaiting CI"),
        ("retrying", "retrying"),
        ("ci_passed", "CI passed"),
    ]:
        if counts.get(status, 0) > 0:
            parts.append(f"{counts[status]} {label}")
    if held_count > 0:
        parts.append(f"{held_count} awaiting approval")

    if parts:
        print(f"Fleet: {', '.join(parts)}")
    else:
        print("Fleet: idle")
    print()

    # Active tasks detail
    active_statuses = {"queued", "working", "pr_created", "reviewing", "reviewed", "retrying", "ci_passed"}
    active = [t for t in tasks if t["status"] in active_statuses]

    if active:
        print("Active:")
        for t in active:
            badge = _status_badge(t["status"], t.get("hold"))
            issue = f" (#{t['github_issue_number']})" if t.get("github_issue_number") else ""
            retry = f" [retry {t['retry_count']}]" if t.get("retry_count", 0) > 0 else ""
            pri = f" P{t['priority']}" if t.get("priority") is not None and t["priority"] != 100 else ""
            dep = f" blocked-by:#{t['depends_on_task_id']}" if t.get("depends_on_task_id") else ""
            line = f"  #{t['id']:3d} [{badge}] {t['repo_name']:<15s}{issue}{pri}{dep}{retry} {t['prompt'][:50]}"
            if t.get("pr_url"):
                line += f"  {t['pr_url']}"
            print(line)
        print()

    # Recent completed
    done = [t for t in tasks if t["status"] not in active_statuses][:10]
    if done:
        print("Recent:")
        for t in done:
            badge = _status_badge(t["status"], t.get("hold"))
            issue = f" (#{t['github_issue_number']})" if t.get("github_issue_number") else ""
            line = f"  #{t['id']:3d} [{badge}] {t['repo_name']:<15s}{issue} {t['prompt'][:50]}"
            if t.get("pr_url"):
                line += f"  {t['pr_url']}"
            print(line)

    db.close()


def cmd_status(args):
    db = _get_db()

    if args.task_id:
        # Single task detail
        task = db.get_task(int(args.task_id))
        if not task:
            print(f"Task #{args.task_id} not found")
            sys.exit(1)

        print(f"Task #{task['id']}  [{task['status']}]")
        print(f"  Repo:    {task['repo_name']}")
        print(f"  Model:   {task['model']}")
        print(f"  Prompt:  {task['prompt'][:100]}")
        if task.get("github_issue_number"):
            print(f"  Issue:   #{task['github_issue_number']}")
        if task.get("priority") is not None and task["priority"] != 100:
            print(f"  Priority: {task['priority']}")
        if task.get("depends_on_task_id"):
            print(f"  Depends: task #{task['depends_on_task_id']}")
        if task.get("hold"):
            print(f"  Hold:    {task['hold']}")
        if task.get("branch_name"):
            print(f"  Branch:  {task['branch_name']}")
        if task.get("pr_url"):
            print(f"  PR:      {task['pr_url']}")
        if task.get("retry_count", 0) > 0:
            print(f"  Retries: {task['retry_count']}")
        if task.get("review_summary"):
            print(f"  Review:  {task['review_summary'][:200]}")
        if task.get("error_message"):
            print(f"  Error:   {task['error_message'][:200]}")
        if task.get("started_at"):
            print(f"  Started: {task['started_at']}")
        if task.get("completed_at"):
            print(f"  Done:    {task['completed_at']}")

        # Show last N log lines
        logs = db.get_logs(task["id"], limit=20)
        if logs:
            print(f"\n  --- Last {len(logs)} log entries ---")
            for entry in logs:
                ts = (
                    entry["created_at"].split("T")[-1].split(".")[0]
                    if "T" in entry["created_at"]
                    else entry["created_at"][-8:]
                )
                lvl = entry["level"].upper()[:4]
                print(f"  {ts} [{lvl}] {entry['message'][:120]}")
    else:
        # Overview of all tasks
        active = db.count_active()
        queued = db.count_queued()
        print(f"Workers: {active} active, {queued} queued\n")

        tasks = db.list_tasks(limit=20)
        if not tasks:
            print("No tasks. Create a GitHub issue with label 'backporcher' to dispatch work.")
            return

        for t in tasks:
            badge = _status_badge(t["status"], t.get("hold"))
            issue = f" #{t['github_issue_number']}" if t.get("github_issue_number") else ""
            line = f"  #{t['id']:3d} [{badge}]{issue} {t['repo_name']:<15s} {t['prompt'][:50]}"
            if t.get("pr_url"):
                line += f"  {t['pr_url']}"
            print(line)

    db.close()


def cmd_cancel(args):
    db = _get_db()
    task = db.get_task(int(args.task_id))
    if not task:
        print(f"Task #{args.task_id} not found")
        sys.exit(1)

    if task["status"] not in ("queued", "working", "reviewing", "retrying"):
        print(f"Cannot cancel task #{args.task_id} (status={task['status']})")
        sys.exit(1)

    now = datetime.now(timezone.utc).isoformat()

    if task["status"] == "queued":
        db.update_task(task["id"], status="cancelled", completed_at=now)
        db.add_log(task["id"], "Cancelled (was queued)")
        print(f"Cancelled queued task #{task['id']}")
    else:
        pid = task.get("agent_pid")
        if pid:
            try:
                os.killpg(pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                try:
                    os.kill(pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
        db.update_task(task["id"], status="cancelled", completed_at=now)
        db.add_log(task["id"], "Cancelled by user", level="warn")
        print(f"Cancelled running task #{task['id']} (pid={pid})")

    # Cascade failure to dependent tasks
    cascaded = db.handle_dependency_failure(task["id"])
    if cascaded:
        print(f"  Cascaded failure to {len(cascaded)} dependent task(s): {cascaded}")

    # Restore GitHub labels if this task came from an issue
    issue_num = task.get("github_issue_number")
    if issue_num:
        repo = db.get_repo_by_name(task["repo_name"])
        if repo:
            from .github import repo_full_name_from_url

            repo_full = repo_full_name_from_url(repo["github_url"])
            subprocess.run(
                [
                    "gh",
                    "issue",
                    "edit",
                    "--repo",
                    repo_full,
                    str(issue_num),
                    "--add-label",
                    "backporcher",
                    "--remove-label",
                    "backporcher-in-progress",
                ],
                capture_output=True,
            )
            print(f"  Restored 'backporcher' label on issue #{issue_num}")

    db.close()


def cmd_cleanup(args):
    load_config()
    db = _get_db()

    if args.task_id:
        task = db.get_task(int(args.task_id))
        if not task:
            print(f"Task #{args.task_id} not found")
            sys.exit(1)

        wt_removed, br_deleted = cleanup_single_task(task, db)
        parts = []
        if wt_removed:
            parts.append("worktree")
        if br_deleted:
            parts.append("remote branch")
        if parts:
            print(f"Cleaned task #{task['id']}: removed {', '.join(parts)}")
        else:
            print(f"Nothing to clean for task #{task['id']}")
    else:
        # Clean all completed/failed/cancelled worktrees and branches
        worktrees_removed = 0
        branches_deleted = 0
        for status in ("reviewed", "ci_passed", "completed", "failed", "cancelled"):
            tasks = db.list_tasks(status=status, limit=500)
            for t in tasks:
                if not t.get("worktree_path") and not t.get("branch_name"):
                    continue
                wt, br = cleanup_single_task(t, db)
                if wt:
                    worktrees_removed += 1
                if br:
                    branches_deleted += 1
        print(f"Cleaned up {worktrees_removed} worktrees, {branches_deleted} remote branches")

    db.close()

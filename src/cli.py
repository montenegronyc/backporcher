"""CLI entry point: backporcher {status,cancel,cleanup,fleet,repo,worker}."""

import argparse
import os
import signal
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from .config import load_config
from .db import SyncDatabase
from .dispatcher import validate_github_url, repo_name_from_url


def get_db() -> SyncDatabase:
    config = load_config()
    db = SyncDatabase(config.db_path)
    db.connect()
    return db


# --- repo commands ---

def cmd_repo_add(args):
    config = load_config()
    db = get_db()

    url = args.url.strip().rstrip("/")
    url = validate_github_url(url, config)
    name = repo_name_from_url(url)

    # Check if already exists
    existing = db.get_repo_by_name(name)
    if existing:
        print(f"Repo '{name}' already exists (id={existing['id']})")
        return

    local_path = str(config.repos_dir / name)
    branch = getattr(args, "branch", "main") or "main"
    repo_id = db.add_repo(name, url, local_path, branch)
    print(f"Added repo '{name}' (id={repo_id})")
    db.close()


def cmd_repo_list(args):
    db = get_db()
    repos = db.list_repos()
    if not repos:
        print("No repos configured. Use: backporcher repo add <url>")
        return
    for r in repos:
        verify = f"  verify: {r['verify_command']}" if r.get("verify_command") else ""
        print(f"  {r['id']:3d}  {r['name']:<20s}  {r['github_url']}{verify}")
    db.close()


def cmd_repo_verify(args):
    db = get_db()
    repo = db.get_repo_by_name(args.name)
    if not repo:
        print(f"Repo '{args.name}' not found")
        sys.exit(1)

    command = " ".join(args.verify_cmd) if args.verify_cmd else None
    db.update_repo(repo["id"], verify_command=command)

    if command:
        print(f"Set verify command for '{args.name}': {command}")
    else:
        print(f"Cleared verify command for '{args.name}'")
    db.close()


# --- fleet ---

def cmd_fleet(args):
    """Dashboard showing all active work across the fleet."""
    db = get_db()
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


# --- status ---

def cmd_status(args):
    db = get_db()

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
                ts = entry["created_at"].split("T")[-1].split(".")[0] if "T" in entry["created_at"] else entry["created_at"][-8:]
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


# --- cancel ---

def cmd_cancel(args):
    db = get_db()
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
                ["gh", "issue", "edit", "--repo", repo_full, str(issue_num),
                 "--add-label", "backporcher", "--remove-label", "backporcher-in-progress"],
                capture_output=True,
            )
            print(f"  Restored 'backporcher' label on issue #{issue_num}")

    db.close()


# --- cleanup ---

def _cleanup_single_task(task: dict, db: SyncDatabase):
    """Clean up worktree and remote branch for a single task. Returns (worktree_removed, branch_deleted)."""
    wt_removed = False
    br_deleted = False
    repo = db.get_repo_by_name(task["repo_name"])
    if not repo:
        return wt_removed, br_deleted

    repo_path = repo["local_path"]

    # Remove worktree
    wt = task.get("worktree_path")
    if wt and Path(wt).exists():
        rc = subprocess.run(
            ["git", "worktree", "remove", "--force", wt],
            cwd=repo_path, capture_output=True,
        )
        if rc.returncode == 0:
            wt_removed = True
        else:
            # Force-remove directory if git command failed
            import shutil
            shutil.rmtree(wt, ignore_errors=True)
            wt_removed = Path(wt).exists() is False

    # Prune stale worktree refs
    if wt_removed:
        subprocess.run(
            ["git", "worktree", "prune"],
            cwd=repo_path, capture_output=True,
        )

    # Delete remote branch
    branch = task.get("branch_name")
    if branch:
        rc = subprocess.run(
            ["git", "push", "origin", "--delete", branch],
            cwd=repo_path, capture_output=True, timeout=30,
        )
        if rc.returncode == 0:
            br_deleted = True

    # Clear paths in DB
    if wt_removed or br_deleted:
        db.update_task(
            task["id"],
            worktree_path=None,
            branch_name=None,
        )

    return wt_removed, br_deleted


def cmd_cleanup(args):
    config = load_config()
    db = get_db()

    if args.task_id:
        task = db.get_task(int(args.task_id))
        if not task:
            print(f"Task #{args.task_id} not found")
            sys.exit(1)

        wt_removed, br_deleted = _cleanup_single_task(task, db)
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
                wt, br = _cleanup_single_task(t, db)
                if wt:
                    worktrees_removed += 1
                if br:
                    branches_deleted += 1
        print(f"Cleaned up {worktrees_removed} worktrees, {branches_deleted} remote branches")

    db.close()


# --- approve / hold / release / pause / resume ---

def cmd_approve(args):
    db = get_db()
    task = db.get_task(int(args.task_id))
    if not task:
        print(f"Task #{args.task_id} not found")
        sys.exit(1)

    hold = task.get("hold")
    if not hold:
        print(f"Task #{args.task_id} has no hold to clear")
        sys.exit(1)

    db.clear_hold(task["id"])
    db.add_log(task["id"], f"Hold '{hold}' cleared via CLI approve")

    if hold == "merge_approval":
        print(f"Approved task #{task['id']} for merge. Will merge on next CI check cycle (~60s).")
    elif hold == "dispatch_approval":
        print(f"Approved task #{task['id']} for dispatch. Will be dispatched on next executor cycle (~5s).")
    else:
        print(f"Cleared hold '{hold}' on task #{task['id']}.")
    db.close()


def cmd_hold(args):
    db = get_db()
    task = db.get_task(int(args.task_id))
    if not task:
        print(f"Task #{args.task_id} not found")
        sys.exit(1)

    if task["status"] in ("completed", "failed", "cancelled"):
        print(f"Cannot hold task #{args.task_id} (status={task['status']})")
        sys.exit(1)

    db.set_hold(task["id"], "user_hold")
    db.add_log(task["id"], "User hold set via CLI")
    print(f"Held task #{task['id']}. Use 'backporcher approve {task['id']}' to release.")
    db.close()


def cmd_release(args):
    db = get_db()
    task = db.get_task(int(args.task_id))
    if not task:
        print(f"Task #{args.task_id} not found")
        sys.exit(1)

    if task.get("hold") != "user_hold":
        print(f"Task #{args.task_id} does not have a user hold (hold={task.get('hold')})")
        print(f"Use 'backporcher approve {args.task_id}' to clear any hold type.")
        sys.exit(1)

    db.clear_hold(task["id"])
    db.add_log(task["id"], "User hold released via CLI")
    print(f"Released user hold on task #{task['id']}.")
    db.close()


def cmd_pause(args):
    db = get_db()
    db.set_queue_paused(True)
    active = db.count_active()
    queued = db.count_queued()
    print(f"Queue paused. {active} task(s) still in-flight (will finish). {queued} queued task(s) on hold.")
    db.close()


def cmd_resume(args):
    db = get_db()
    db.set_queue_paused(False)
    queued = db.count_queued()
    print(f"Queue resumed. {queued} queued task(s) now eligible for dispatch.")
    db.close()


# --- stats ---

def cmd_stats(args):
    """Print pipeline performance stats."""
    db = get_db()

    # Total tasks (exclude cancelled)
    all_tasks = db.list_tasks(limit=10000)
    tasks = [t for t in all_tasks if t["status"] != "cancelled"]

    if not tasks:
        print("No tasks yet. Run some work through the pipeline first.")
        db.close()
        return

    completed = [t for t in tasks if t["status"] == "completed"]
    failed = [t for t in tasks if t["status"] == "failed"]
    total = len(tasks)
    n_completed = len(completed)
    n_failed = len(failed)

    # Compute durations
    def _parse_iso(s):
        if not s:
            return None
        try:
            from datetime import datetime as _dt, timezone as _tz
            dt = _dt.fromisoformat(s.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=_tz.utc)
            return dt
        except Exception:
            return None

    def _fmt_duration(seconds):
        if seconds is None:
            return "-"
        m, s = divmod(int(seconds), 60)
        h, m = divmod(m, 60)
        if h > 0:
            return f"{h}h {m:02d}m {s:02d}s"
        return f"{m}m {s:02d}s"

    # Issue→merge times (created_at → completed_at for completed tasks)
    merge_times = []
    for t in completed:
        start = _parse_iso(t.get("created_at"))
        end = _parse_iso(t.get("completed_at"))
        if start and end:
            merge_times.append((end - start).total_seconds())

    # Agent runtimes (agent_started_at → agent_finished_at)
    agent_runtimes = []
    for t in completed:
        start = _parse_iso(t.get("agent_started_at"))
        end = _parse_iso(t.get("agent_finished_at"))
        if start and end:
            agent_runtimes.append((end - start).total_seconds())

    avg_merge = sum(merge_times) / len(merge_times) if merge_times else None
    avg_agent = sum(agent_runtimes) / len(agent_runtimes) if agent_runtimes else None

    # Total retries
    total_retries = sum(t.get("retry_count", 0) for t in tasks)
    retry_rate = (total_retries / total * 100) if total > 0 else 0

    # Model breakdown
    model_counts = {}
    for t in tasks:
        m = t.get("model_used") or t.get("model") or "unknown"
        model_counts[m] = model_counts.get(m, 0) + 1

    # Escalations: tasks where initial_model != model_used
    escalations = 0
    for t in tasks:
        initial = t.get("initial_model")
        used = t.get("model_used")
        if initial and used and initial != used:
            escalations += 1

    # Last 7 days
    now = datetime.now(timezone.utc)
    seven_days_ago = now - __import__("datetime").timedelta(days=7)
    recent = [t for t in tasks if _parse_iso(t.get("created_at")) and _parse_iso(t["created_at"]) >= seven_days_ago]
    recent_completed = [t for t in recent if t["status"] == "completed"]
    recent_failed = [t for t in recent if t["status"] == "failed"]
    recent_merge_times = []
    for t in recent_completed:
        start = _parse_iso(t.get("created_at"))
        end = _parse_iso(t.get("completed_at"))
        if start and end:
            recent_merge_times.append((end - start).total_seconds())
    recent_avg_merge = sum(recent_merge_times) / len(recent_merge_times) if recent_merge_times else None

    # Per-repo breakdown
    repo_stats = {}
    for t in tasks:
        rn = t.get("repo_name", "unknown")
        if rn not in repo_stats:
            repo_stats[rn] = {"total": 0, "failed": 0}
        repo_stats[rn]["total"] += 1
        if t["status"] == "failed":
            repo_stats[rn]["failed"] += 1

    # Print
    pct_completed = (n_completed / total * 100) if total > 0 else 0
    pct_failed = (n_failed / total * 100) if total > 0 else 0

    print("Backporcher Stats")
    print("\u2550" * 39)
    print()
    print("Pipeline")
    print(f"  Total tasks:          {total}")
    print(f"  Completed:            {n_completed} ({pct_completed:.1f}%)")
    print(f"  Failed:               {n_failed} ({pct_failed:.1f}%)")
    print(f"  Avg issue\u2192merge:      {_fmt_duration(avg_merge)}")
    print(f"  Avg agent runtime:    {_fmt_duration(avg_agent)}")
    print(f"  Retry rate:           {retry_rate:.1f}% ({total_retries} retries across {total} tasks)")

    print()
    print("Models")
    for m, count in sorted(model_counts.items(), key=lambda x: -x[1]):
        pct = count / total * 100
        print(f"  {m:<20s} {count} tasks ({pct:.0f}%)")
    print(f"  Escalations:          {escalations}")

    print()
    print("Activity (last 7 days)")
    print(f"  Tasks completed:      {len(recent_completed)}")
    print(f"  Tasks failed:         {len(recent_failed)}")
    print(f"  Avg issue\u2192merge:      {_fmt_duration(recent_avg_merge)}")

    if repo_stats:
        print()
        print("Repos")
        for rn, rs in sorted(repo_stats.items()):
            failed_part = f" ({rs['failed']} failed)" if rs["failed"] else ""
            print(f"  {rn:<20s} {rs['total']} tasks{failed_part}")

    db.close()


# --- worker ---

def cmd_worker(args):
    from .worker import run_worker
    run_worker()


# --- main ---

def main():
    parser = argparse.ArgumentParser(
        prog="backporcher",
        description="Parallel Claude Code agent dispatcher — GitHub Issues as task queue",
    )
    sub = parser.add_subparsers(dest="command")

    # repo
    repo_parser = sub.add_parser("repo", help="Manage repos")
    repo_sub = repo_parser.add_subparsers(dest="repo_command")

    repo_add = repo_sub.add_parser("add", help="Add a repo")
    repo_add.add_argument("url", help="GitHub repo URL")
    repo_add.add_argument("--branch", default="main", help="Default branch")

    repo_sub.add_parser("list", help="List repos")

    repo_verify = repo_sub.add_parser("verify", help="Set build/test verify command")
    repo_verify.add_argument("name", help="Repo name")
    repo_verify.add_argument("verify_cmd", nargs="*", help="Verify command (omit to clear)")

    # fleet
    sub.add_parser("fleet", help="Fleet dashboard — active work overview")

    # status
    status_parser = sub.add_parser("status", help="Check task status")
    status_parser.add_argument("task_id", nargs="?", help="Task ID for detail view")

    # cancel
    cancel_parser = sub.add_parser("cancel", help="Cancel a task")
    cancel_parser.add_argument("task_id", help="Task ID")

    # cleanup
    cleanup_parser = sub.add_parser("cleanup", help="Remove worktrees")
    cleanup_parser.add_argument("task_id", nargs="?", help="Task ID (or all)")

    # approve / hold / release / pause / resume
    approve_parser = sub.add_parser("approve", help="Approve a held task (merge or dispatch)")
    approve_parser.add_argument("task_id", help="Task ID")

    hold_parser = sub.add_parser("hold", help="Set user hold on a task")
    hold_parser.add_argument("task_id", help="Task ID")

    release_parser = sub.add_parser("release", help="Release a user hold")
    release_parser.add_argument("task_id", help="Task ID")

    sub.add_parser("pause", help="Pause the dispatch queue")
    sub.add_parser("resume", help="Resume the dispatch queue")

    # stats
    sub.add_parser("stats", help="Pipeline performance stats")

    # worker
    sub.add_parser("worker", help="Run worker daemon (foreground)")

    args = parser.parse_args()

    if args.command == "repo":
        if args.repo_command == "add":
            cmd_repo_add(args)
        elif args.repo_command == "list":
            cmd_repo_list(args)
        elif args.repo_command == "verify":
            cmd_repo_verify(args)
        else:
            repo_parser.print_help()
    elif args.command == "fleet":
        cmd_fleet(args)
    elif args.command == "status":
        cmd_status(args)
    elif args.command == "cancel":
        cmd_cancel(args)
    elif args.command == "cleanup":
        cmd_cleanup(args)
    elif args.command == "approve":
        cmd_approve(args)
    elif args.command == "hold":
        cmd_hold(args)
    elif args.command == "release":
        cmd_release(args)
    elif args.command == "pause":
        cmd_pause(args)
    elif args.command == "resume":
        cmd_resume(args)
    elif args.command == "stats":
        cmd_stats(args)
    elif args.command == "worker":
        cmd_worker(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()

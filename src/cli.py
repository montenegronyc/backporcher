"""CLI entry point: voltron {dispatch,status,cancel,retry,cleanup,repo,worker}."""

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
        print("No repos configured. Use: voltron repo add <url>")
        return
    for r in repos:
        print(f"  {r['id']:3d}  {r['name']:<20s}  {r['github_url']}")
    db.close()


# --- dispatch ---

def cmd_dispatch(args):
    config = load_config()
    db = get_db()

    # Resolve repo by name (case-insensitive)
    repo = db.get_repo_by_name(args.repo)
    if not repo:
        print(f"Error: repo '{args.repo}' not found. Use: voltron repo list")
        sys.exit(1)

    model = args.model or config.default_model
    if model not in config.allowed_models:
        print(f"Error: model must be one of {config.allowed_models}")
        sys.exit(1)

    prompt = args.prompt
    if not prompt.strip():
        print("Error: prompt cannot be empty")
        sys.exit(1)

    task_id = db.create_task(repo["id"], prompt, model)
    db.add_log(task_id, f"Task queued (model={model})")
    print(f"Queued task #{task_id} for {repo['name']} (model={model})")
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
        if task.get("branch_name"):
            print(f"  Branch:  {task['branch_name']}")
        if task.get("pr_url"):
            print(f"  PR:      {task['pr_url']}")
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
            print("No tasks. Use: voltron dispatch <repo> \"<prompt>\"")
            return

        for t in tasks:
            status = t["status"]
            badge = {
                "queued": "WAIT",
                "working": " RUN",
                "pr_created": "  PR",
                "completed": "DONE",
                "failed": "FAIL",
                "cancelled": " CXL",
            }.get(status, status[:4].upper())

            line = f"  #{t['id']:3d} [{badge}] {t['repo_name']:<15s} {t['prompt'][:50]}"
            if t.get("pr_url"):
                line += f"  {t['pr_url']}"
            print(line)

    db.close()


# --- cancel ---

def cmd_cancel(args):
    db = get_db()
    task = db.get_task(int(args.task_id))
    if not task:
        print(f"Task #{args.task_id} not found")
        sys.exit(1)

    if task["status"] not in ("queued", "working"):
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
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        db.update_task(task["id"], status="cancelled", completed_at=now)
        db.add_log(task["id"], "Cancelled by user", level="warn")
        print(f"Cancelled running task #{task['id']} (pid={pid})")

    db.close()


# --- retry ---

def cmd_retry(args):
    db = get_db()
    task = db.get_task(int(args.task_id))
    if not task:
        print(f"Task #{args.task_id} not found")
        sys.exit(1)

    if task["status"] not in ("failed", "cancelled"):
        print(f"Can only retry failed/cancelled tasks (status={task['status']})")
        sys.exit(1)

    new_id = db.create_task(task["repo_id"], task["prompt"], task["model"])
    db.add_log(new_id, f"Retry of task #{task['id']}")
    print(f"Re-queued as task #{new_id}")
    db.close()


# --- cleanup ---

def cmd_cleanup(args):
    config = load_config()
    db = get_db()

    if args.task_id:
        task = db.get_task(int(args.task_id))
        if not task:
            print(f"Task #{args.task_id} not found")
            sys.exit(1)
        wt = task.get("worktree_path")
        if wt and Path(wt).exists():
            repo_path = Path(db.get_repo_by_name(task["repo_name"])["local_path"])
            rc = subprocess.run(
                ["git", "worktree", "remove", "--force", wt],
                cwd=str(repo_path), capture_output=True,
            )
            if rc.returncode == 0:
                print(f"Removed worktree for task #{task['id']}")
            else:
                print(f"Failed to remove worktree: {rc.stderr.decode()}")
        else:
            print(f"No worktree found for task #{task['id']}")
    else:
        # Clean all completed/failed/cancelled worktrees
        removed = 0
        for status in ("pr_created", "completed", "failed", "cancelled"):
            tasks = db.list_tasks(status=status, limit=200)
            for t in tasks:
                wt = t.get("worktree_path")
                if wt and Path(wt).exists():
                    repo = db.get_repo_by_name(t["repo_name"])
                    if repo:
                        subprocess.run(
                            ["git", "worktree", "remove", "--force", wt],
                            cwd=repo["local_path"], capture_output=True,
                        )
                        removed += 1
        print(f"Cleaned up {removed} worktrees")

    db.close()


# --- worker ---

def cmd_worker(args):
    from .worker import run_worker
    run_worker()


# --- main ---

def main():
    parser = argparse.ArgumentParser(
        prog="voltron",
        description="Parallel Claude Code agent dispatcher",
    )
    sub = parser.add_subparsers(dest="command")

    # repo
    repo_parser = sub.add_parser("repo", help="Manage repos")
    repo_sub = repo_parser.add_subparsers(dest="repo_command")

    repo_add = repo_sub.add_parser("add", help="Add a repo")
    repo_add.add_argument("url", help="GitHub repo URL")
    repo_add.add_argument("--branch", default="main", help="Default branch")

    repo_sub.add_parser("list", help="List repos")

    # dispatch
    dispatch_parser = sub.add_parser("dispatch", help="Dispatch a task")
    dispatch_parser.add_argument("repo", help="Repo name")
    dispatch_parser.add_argument("prompt", help="Task prompt")
    dispatch_parser.add_argument("--model", default=None, help="Model (sonnet/opus/haiku)")

    # status
    status_parser = sub.add_parser("status", help="Check task status")
    status_parser.add_argument("task_id", nargs="?", help="Task ID for detail view")

    # cancel
    cancel_parser = sub.add_parser("cancel", help="Cancel a task")
    cancel_parser.add_argument("task_id", help="Task ID")

    # retry
    retry_parser = sub.add_parser("retry", help="Retry a failed task")
    retry_parser.add_argument("task_id", help="Task ID")

    # cleanup
    cleanup_parser = sub.add_parser("cleanup", help="Remove worktrees")
    cleanup_parser.add_argument("task_id", nargs="?", help="Task ID (or all)")

    # worker
    sub.add_parser("worker", help="Run worker daemon (foreground)")

    args = parser.parse_args()

    if args.command == "repo":
        if args.repo_command == "add":
            cmd_repo_add(args)
        elif args.repo_command == "list":
            cmd_repo_list(args)
        else:
            repo_parser.print_help()
    elif args.command == "dispatch":
        cmd_dispatch(args)
    elif args.command == "status":
        cmd_status(args)
    elif args.command == "cancel":
        cmd_cancel(args)
    elif args.command == "retry":
        cmd_retry(args)
    elif args.command == "cleanup":
        cmd_cleanup(args)
    elif args.command == "worker":
        cmd_worker(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()

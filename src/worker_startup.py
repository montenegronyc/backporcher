"""Worker daemon startup logic: PID lock, stale task recovery, preflight checks."""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from .config import Config
from .db import Database
from .dispatcher import clone_or_fetch, detect_and_store_stack, sync_agent_credentials

log = logging.getLogger("backporcher.worker")


def _get_boot_id() -> str:
    """Return a unique identifier for the current boot/container lifecycle.

    Tries /proc/sys/kernel/random/boot_id first (unique per boot and per
    container PID namespace), then falls back to /proc/1/sched cgroup inode
    as a last resort.  Returns "" if nothing works — the caller treats a
    mismatch (including "" vs any saved value) as a stale lock.
    """
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        return ""


def acquire_pid_lock(config: Config) -> Path | None:
    """Write PID file, check for stale locks.

    Returns the pid_file Path on success, or None if another worker is running.

    The PID file stores ``pid:boot_id``.  When the PID file lives on a bind
    mount that survives container restarts, a raw ``os.kill(pid, 0)`` check
    is not enough — PID 1 is always alive inside every new container.  By
    recording the kernel boot_id we can detect that the lock belongs to a
    previous container and safely reclaim it.
    """
    pid_file = config.base_dir / "data" / "backporcher.pid"
    pid_file.parent.mkdir(parents=True, exist_ok=True)

    current_boot_id = _get_boot_id()

    if pid_file.exists():
        raw = pid_file.read_text().strip()
        # Parse "pid:boot_id" (new format) or bare "pid" (old format)
        if ":" in raw:
            old_pid_s, old_boot_id = raw.split(":", 1)
        else:
            old_pid_s, old_boot_id = raw, ""

        old_pid = int(old_pid_s)

        # Different boot_id means the lock is from a previous container/boot
        if old_boot_id and current_boot_id and old_boot_id != current_boot_id:
            log.warning(
                "Removing stale PID file from previous boot (pid=%d, old_boot=%s)",
                old_pid,
                old_boot_id[:12],
            )
            pid_file.unlink()
        else:
            try:
                os.kill(old_pid, 0)  # Check if process is alive (signal 0 = no-op)
                log.error("Another worker is already running (pid=%d). Exiting.", old_pid)
                return None
            except ProcessLookupError:
                log.warning("Removing stale PID file (pid=%d no longer running)", old_pid)
                pid_file.unlink()
            except PermissionError:
                log.error("Another worker is running as a different user (pid=%d). Exiting.", old_pid)
                return None

    pid_file.write_text(f"{os.getpid()}:{current_boot_id}")
    return pid_file


async def recover_stale_tasks(db: Database) -> None:
    """Reset stale working/reviewing tasks from previous crash."""
    async with db._write_lock:
        # Reviewing → pr_created (re-review)
        async with db.db.execute(
            "UPDATE tasks SET status = 'pr_created', review_summary = NULL WHERE status = 'reviewing' RETURNING id"
        ) as cur:
            recovered_reviewing = [r[0] for r in await cur.fetchall()]
        # Working tasks — check if agent PID is still alive before resetting
        async with db.db.execute("SELECT id, agent_pid FROM tasks WHERE status = 'working'") as cur:
            working_tasks = await cur.fetchall()

        recovered_working = []
        for task_row in working_tasks:
            task_id, agent_pid = task_row[0], task_row[1]
            pid_alive = False
            if agent_pid:
                try:
                    os.kill(agent_pid, 0)
                    pid_alive = True
                except (ProcessLookupError, PermissionError):
                    pass

            if pid_alive:
                log.info("Task #%d agent still running (pid=%d), leaving as working", task_id, agent_pid)
            else:
                await db.db.execute(
                    "UPDATE tasks SET status = 'queued', started_at = NULL, "
                    "error_message = NULL, agent_pid = NULL, branch_name = NULL, "
                    "worktree_path = NULL WHERE id = ?",
                    (task_id,),
                )
                recovered_working.append(task_id)
        await db.db.commit()
    if recovered_reviewing:
        log.info("Recovered %d stale reviewing tasks: %s", len(recovered_reviewing), recovered_reviewing)
    if recovered_working:
        log.info("Recovered %d stale working tasks: %s", len(recovered_working), recovered_working)


async def run_preflight(db: Database, config: Config) -> bool:
    """Sync repos, detect stacks, build graphs, check agent user.

    Returns True if all checks passed, False otherwise.
    """
    log.info("Running preflight checks...")
    preflight_ok = True

    # Sync all repos — clone if missing, fetch latest if present
    repos = await db.list_repos()
    if repos:
        log.info("Syncing %d repo(s)...", len(repos))
        for repo in repos:
            try:
                await clone_or_fetch(repo, config)
                await detect_and_store_stack(repo, db)
                # Pre-build code graph so navigation context is ready for first dispatch
                try:
                    from .graph import ensure_graph

                    repo_path = Path(repo["local_path"])
                    if repo_path.exists():
                        await ensure_graph(repo_path)
                except Exception:
                    log.warning("Failed to build code graph for %s (non-fatal)", repo["name"], exc_info=True)
                log.info("Repo synced: %s", repo["name"])
            except Exception as e:
                log.error("Failed to sync repo %s: %s", repo["name"], e)
                preflight_ok = False
    else:
        log.warning("No repos registered — nothing to sync")

    # Check agent user can access repos
    if config.agent_user:
        proc = await asyncio.create_subprocess_exec(
            "sudo",
            "-u",
            config.agent_user,
            "test",
            "-r",
            str(config.repos_dir),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()
        if proc.returncode != 0:
            log.error("PREFLIGHT FAIL: %s cannot read %s", config.agent_user, config.repos_dir)
            preflight_ok = False
        else:
            log.info("Preflight OK: agent user can access repos")

        # Sync credentials if needed
        await sync_agent_credentials(config)

    if not preflight_ok:
        log.error("Preflight checks failed — starting anyway but tasks may fail")

    return preflight_ok

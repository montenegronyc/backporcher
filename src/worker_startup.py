"""Worker daemon startup logic: PID lock, stale task recovery, preflight checks."""

import asyncio
import logging
import os
from pathlib import Path

from .config import Config
from .db import Database
from .dispatcher import clone_or_fetch, detect_and_store_stack, sync_agent_credentials

log = logging.getLogger("backporcher.worker")


def _get_container_id() -> str:
    """Return a unique identifier for the current container lifecycle.

    Uses the hostname, which Docker sets to the container ID (short form) by
    default.  Falls back to reading the container ID from /proc/self/cgroup
    (cgroup v1) or /proc/self/mountinfo (cgroup v2).  Returns "" if running
    outside a container or detection fails.
    """
    import socket

    hostname = socket.gethostname()
    # Docker short container IDs are 12 hex chars
    if len(hostname) == 12 and all(c in "0123456789abcdef" for c in hostname):
        return hostname

    # Fallback: parse container ID from cgroup (v1 format)
    try:
        for line in Path("/proc/self/cgroup").read_text().splitlines():
            # e.g. "12:memory:/docker/abc123def456..."
            parts = line.split("/")
            for part in reversed(parts):
                if len(part) >= 12 and all(c in "0123456789abcdef" for c in part[:64]):
                    return part[:12]
    except OSError:
        pass

    return ""


def _get_proc_starttime(pid: int) -> str:
    """Return the start time (in clock ticks since boot) for a process.

    Field 22 of /proc/<pid>/stat is the process start time in clock ticks.
    Two processes with the same PID but different start times are different
    processes (PID was recycled).  Returns "" on failure.
    """
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        # Fields are space-separated, but field 2 (comm) may contain spaces
        # and is enclosed in parentheses.  Skip past it.
        close_paren = stat.rfind(")")
        fields_after_comm = stat[close_paren + 2 :].split()
        # starttime is field 22 (1-indexed), which is index 19 after comm
        # (fields 1=pid, 2=comm already consumed, so 22 - 3 = 19)
        return fields_after_comm[19]
    except (OSError, IndexError):
        return ""


def acquire_pid_lock(config: Config) -> Path | None:
    """Write PID file, check for stale locks.

    Returns the pid_file Path on success, or None if another worker is running.

    The PID file stores ``pid:container_id:starttime``.  This handles two
    failure modes that the previous boot_id approach could not:

    1. **Container ID** — Docker sets the hostname to the container's short ID.
       On bind-mounted data dirs, a new container gets a new hostname, so a
       lock from a previous container is detected even though boot_id (read
       from the host kernel) stays the same.

    2. **Process start time** — field 22 of /proc/<pid>/stat gives the time
       the process started (in clock ticks since boot).  Even if the PID and
       container ID match (e.g. PID 1 in the same container after exec), a
       different start time means the lock holder was replaced.

    Backward compatible: parses old ``pid:boot_id`` and bare ``pid`` formats.
    """
    pid_file = config.base_dir / "data" / "backporcher.pid"
    pid_file.parent.mkdir(parents=True, exist_ok=True)

    current_container_id = _get_container_id()
    current_pid = os.getpid()
    current_starttime = _get_proc_starttime(current_pid)

    if pid_file.exists():
        raw = pid_file.read_text().strip()
        parts = raw.split(":")
        old_pid = int(parts[0])
        old_container_id = parts[1] if len(parts) > 1 else ""
        old_starttime = parts[2] if len(parts) > 2 else ""

        stale = False

        # Check 1: different container ID means different container lifecycle
        if old_container_id and current_container_id and old_container_id != current_container_id:
            log.warning(
                "Removing stale PID file from previous container (pid=%d, old=%s, new=%s)",
                old_pid,
                old_container_id[:12],
                current_container_id[:12],
            )
            stale = True

        # Check 2: same container (or no container ID) — verify process is alive
        if not stale:
            try:
                os.kill(old_pid, 0)
            except ProcessLookupError:
                log.warning("Removing stale PID file (pid=%d no longer running)", old_pid)
                stale = True
            except PermissionError:
                # Process exists but owned by different user.  In containers
                # this is common: PID 1 is root's init process, but the worker
                # runs as a non-root user.  Check starttime before giving up.
                pass
            # PID is alive (or PermissionError) — but is it the same process
            # that wrote the lock?
            if not stale and old_starttime:
                live_starttime = _get_proc_starttime(old_pid)
                if live_starttime and live_starttime != old_starttime:
                    log.warning(
                        "Removing stale PID file (pid=%d recycled: starttime %s→%s)",
                        old_pid,
                        old_starttime,
                        live_starttime,
                    )
                    stale = True

            if not stale:
                log.error("Another worker is already running (pid=%d). Exiting.", old_pid)
                return None

        if stale:
            pid_file.unlink()

    pid_file.write_text(f"{current_pid}:{current_container_id}:{current_starttime}")
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

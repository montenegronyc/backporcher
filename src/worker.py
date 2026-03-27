"""Background worker daemon: 5 core loops + optional dashboard.

Issue poller, task executor, coordinator reviewer, CI monitor, artifact
cleanup.
"""

import asyncio
import logging
import signal

from . import notifications
from .backends import discover_backends
from .config import Config, load_config
from .db import Database
from .dispatcher import dispatch_task
from .worker_loops import (
    cleanup_terminal_tasks,
    compute_duration,
    monitor_ci,
    poll_issues,
    review_pending_tasks,
    try_claim_and_dispatch,
)
from .worker_startup import acquire_pid_lock, recover_stale_tasks, run_preflight

log = logging.getLogger("backporcher.worker")

# Task executor poll interval (separate from issue poller)
EXECUTOR_POLL_SECONDS = 5
# Coordinator review loop interval
COORDINATOR_POLL_SECONDS = 15
# Artifact cleanup loop interval
CLEANUP_POLL_SECONDS = 300  # 5 minutes
# Minimum age before cleaning up terminal task artifacts
CLEANUP_MIN_AGE_MINUTES = 10


class WorkerDaemon:
    def __init__(self, config: Config, db: Database):
        self.config = config
        self.db = db
        self.semaphore = asyncio.Semaphore(config.max_workers)
        self._running = False
        self._tasks: set[asyncio.Task] = set()
        self.backends = discover_backends(config)

        # Auto-populate enabled_agents from discovered backends when not
        # explicitly configured.  This ensures triage and batch orchestration
        # see all available backends without requiring BACKPORCHER_ENABLED_AGENTS.
        if not config.enabled_agents and self.backends:
            discovered = tuple(self.backends.keys())
            # frozen dataclass — replace via object.__setattr__
            object.__setattr__(config, "enabled_agents", discovered)
            log.info("Auto-enabled agents from discovery: %s", list(discovered))
        # Guarantee at least "claude" so triage always has a valid choice.
        if not config.enabled_agents:
            object.__setattr__(config, "enabled_agents", ("claude",))

    async def run(self):
        """Launch concurrent loops (4 core + optional dashboard)."""
        self._running = True
        log.info(
            "Worker daemon started (max_concurrency=%d, issue_poll=%ds, "
            "ci_poll=%ds, coordinator_model=%s, approval_mode=%s)",
            self.config.max_workers,
            self.config.poll_interval_seconds,
            self.config.ci_check_interval_seconds,
            self.config.coordinator_model,
            self.config.approval_mode,
        )

        # Log tasks with holds so the user knows what's waiting
        held = await self.db.list_held_tasks()
        if held:
            log.info("Tasks awaiting approval on startup: %s", ", ".join(f"#{t['id']}({t['hold']})" for t in held))

        loops = [
            self._issue_poller_loop(),
            self._task_executor_loop(),
            self._coordinator_review_loop(),
            self._ci_monitor_loop(),
            self._cleanup_loop(),
        ]

        if self.config.dashboard_password:
            from .dashboard import set_embedded_mode, start_dashboard

            set_embedded_mode()
            loops.append(start_dashboard(self.db, self.config))
            log.info("Dashboard enabled on port %d (embedded mode)", self.config.dashboard_port)
        else:
            log.info("Dashboard disabled (no BACKPORCHER_DASHBOARD_PASSWORD set)")

        await asyncio.gather(*loops)

    # --- Loop 1: Issue Poller ---

    async def _issue_poller_loop(self):
        allowed_users = set(self.config.allowed_github_users)
        while self._running:
            try:
                await poll_issues(self.db, self.config, allowed_users)
            except Exception:
                log.exception("Error in issue poller loop")
            await asyncio.sleep(self.config.poll_interval_seconds)

    # --- Loop 2: Task Executor ---

    async def _task_executor_loop(self):
        while self._running:
            try:
                if await self.db.is_queue_paused():
                    await asyncio.sleep(EXECUTOR_POLL_SECONDS)
                    continue

                if self.semaphore._value > 0:
                    task = await try_claim_and_dispatch(self.db, self.config)
                    if task:
                        atask = asyncio.create_task(self._run_with_semaphore(task))
                        self._tasks.add(atask)
                        atask.add_done_callback(self._tasks.discard)
                        continue  # Check for more work immediately
            except Exception:
                log.exception("Error in task executor loop")

            await asyncio.sleep(EXECUTOR_POLL_SECONDS)

    async def _run_with_semaphore(self, task: dict):
        async with self.semaphore:
            try:
                await dispatch_task(task, self.config, self.db, backends=self.backends)
            except Exception:
                log.exception("Unhandled error dispatching task %d", task["id"])
            finally:
                try:
                    fresh = await self.db.get_task(task["id"])
                    if fresh:
                        await self._record_terminal_metric(fresh)
                except Exception:
                    log.warning("Failed to record terminal metric for task %d", task["id"], exc_info=True)

    async def _record_terminal_metric(self, task: dict):
        """Record metrics for task completion or failure."""
        status = task["status"]
        task_id = task["id"]
        repo = task.get("repo_name")
        model = task.get("model_used") or task.get("model")

        if status == "completed":
            duration = compute_duration(task.get("agent_started_at"), task.get("agent_finished_at"))
            await self.db.record_metric("task_completed", task_id=task_id, repo=repo, model=model, value=duration)
        elif status == "failed":
            await self.db.record_metric("task_failed", task_id=task_id, repo=repo, model=model)

    # --- Loop 3: Coordinator Review ---

    async def _coordinator_review_loop(self):
        while self._running:
            try:
                await review_pending_tasks(self.db, self.config, self._running)
            except Exception:
                log.exception("Error in coordinator review loop")
            await asyncio.sleep(COORDINATOR_POLL_SECONDS)

    # --- Loop 4: CI Monitor ---

    async def _ci_monitor_loop(self):
        while self._running:
            try:
                await monitor_ci(self.db, self.config)
            except Exception:
                log.exception("Error in CI monitor loop")
            await asyncio.sleep(self.config.ci_check_interval_seconds)

    # --- Loop 5: Periodic Cleanup ---

    async def _cleanup_loop(self):
        await asyncio.sleep(60)  # Let startup settle
        while self._running:
            try:
                await cleanup_terminal_tasks(self.db, CLEANUP_MIN_AGE_MINUTES, self._running)
            except Exception:
                log.exception("Error in cleanup loop")
            await asyncio.sleep(CLEANUP_POLL_SECONDS)

    async def shutdown(self):
        """Graceful shutdown: stop polling, wait for active tasks."""
        log.info("Shutting down worker daemon...")
        self._running = False

        if self._tasks:
            log.info("Waiting for %d active tasks to finish...", len(self._tasks))
            _, pending = await asyncio.wait(self._tasks, timeout=30)
            for t in pending:
                log.warning("Force-cancelling task")
                t.cancel()

        log.info("Worker daemon stopped")


async def _run_worker():
    """Entry point for the worker daemon."""
    config = load_config()

    pid_file = acquire_pid_lock(config)
    if pid_file is None:
        return

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    config.repos_dir.mkdir(parents=True, exist_ok=True)
    config.logs_dir.mkdir(parents=True, exist_ok=True)

    db = Database(config.db_path)
    await db.connect()
    log.info("Database connected: %s", config.db_path)

    await recover_stale_tasks(db)
    await run_preflight(db, config)

    # Initialize webhook notifications
    notifications.init(config)
    if config.webhook_url:
        log.info("Webhooks enabled: %s (events: %s)", config.webhook_url, ",".join(config.webhook_events))

    daemon = WorkerDaemon(config, db)

    loop = asyncio.get_running_loop()

    def _request_shutdown():
        log.info("Received shutdown signal")
        asyncio.create_task(daemon.shutdown())

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _request_shutdown)

    try:
        await daemon.run()
    finally:
        try:
            pid_file.unlink(missing_ok=True)
        except OSError:
            pass
        await db.close()
        log.info("Worker shutdown complete")


def run_worker():
    """Synchronous entry point for CLI / systemd."""
    asyncio.run(_run_worker())

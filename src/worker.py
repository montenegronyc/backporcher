"""Asyncio worker pool with semaphore-based concurrency control."""

import asyncio
import logging
import signal

from .config import Config
from .db import Database
from .dispatcher import dispatch_task

log = logging.getLogger("compound.worker")


class WorkerPool:
    def __init__(self, config: Config, db: Database):
        self.config = config
        self.db = db
        self.semaphore = asyncio.Semaphore(config.max_workers)
        self._running = False
        self._tasks: set[asyncio.Task] = set()
        self._poll_task: asyncio.Task | None = None

    @property
    def active_count(self) -> int:
        return self.config.max_workers - self.semaphore._value

    @property
    def max_workers(self) -> int:
        return self.config.max_workers

    async def start(self):
        """Start the worker poll loop."""
        self._running = True
        self._poll_task = asyncio.create_task(self._poll_loop())
        log.info("Worker pool started (max_workers=%d)", self.config.max_workers)

    async def stop(self):
        """Gracefully stop the worker pool."""
        log.info("Stopping worker pool...")
        self._running = False

        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass

        # Wait for active tasks to finish (with timeout)
        if self._tasks:
            log.info("Waiting for %d active tasks...", len(self._tasks))
            done, pending = await asyncio.wait(
                self._tasks, timeout=30
            )
            for t in pending:
                t.cancel()

        log.info("Worker pool stopped")

    async def _poll_loop(self):
        """Poll for queued tasks and dispatch them."""
        while self._running:
            try:
                # Only check if we have capacity
                if self.semaphore._value > 0:
                    task = await self.db.claim_next_queued()
                    if task:
                        log.info(
                            "Claimed task %d (repo=%d): %s",
                            task["id"], task["repo_id"],
                            task["prompt"][:80],
                        )
                        atask = asyncio.create_task(
                            self._run_with_semaphore(task)
                        )
                        self._tasks.add(atask)
                        atask.add_done_callback(self._tasks.discard)
                        # Check again immediately for more work
                        continue

            except Exception:
                log.exception("Error in poll loop")

            await asyncio.sleep(2)

    async def _run_with_semaphore(self, task: dict):
        """Run a task within the semaphore."""
        async with self.semaphore:
            try:
                await dispatch_task(task, self.config, self.db)
            except Exception:
                log.exception("Unhandled error dispatching task %d", task["id"])

    async def cancel_task(self, task_id: int) -> bool:
        """Cancel a running task by killing its agent process."""
        task = await self.db.get_task(task_id)
        if not task:
            return False
        if task["status"] not in ("queued", "working"):
            return False

        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()

        if task["status"] == "queued":
            await self.db.update_task(
                task_id, status="cancelled", completed_at=now
            )
            await self.db.add_log(task_id, "Cancelled (was queued)")
            return True

        # Kill running agent
        pid = task.get("agent_pid")
        if pid:
            try:
                import os
                os.kill(pid, signal.SIGTERM)
                await asyncio.sleep(2)
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            except ProcessLookupError:
                pass

        await self.db.update_task(
            task_id, status="cancelled", completed_at=now
        )
        await self.db.add_log(task_id, "Cancelled by user", level="warn")
        return True

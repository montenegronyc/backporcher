"""Background worker daemon: polls SQLite, dispatches agents."""

import asyncio
import logging
import os
import signal

from .config import Config, load_config
from .db import Database
from .dispatcher import dispatch_task

log = logging.getLogger("voltron.worker")


class WorkerDaemon:
    def __init__(self, config: Config, db: Database):
        self.config = config
        self.db = db
        self.semaphore = asyncio.Semaphore(config.max_workers)
        self._running = False
        self._tasks: set[asyncio.Task] = set()

    async def run(self):
        """Main loop: poll for queued tasks, dispatch up to max_workers."""
        self._running = True
        log.info(
            "Worker daemon started (max_concurrency=%d, poll=%ds)",
            self.config.max_workers,
            self.config.poll_interval_seconds,
        )

        while self._running:
            try:
                if self.semaphore._value > 0:
                    task = await self.db.claim_next_queued()
                    if task:
                        log.info(
                            "Claimed task #%d: %s",
                            task["id"], task["prompt"][:80],
                        )
                        atask = asyncio.create_task(
                            self._run_with_semaphore(task)
                        )
                        self._tasks.add(atask)
                        atask.add_done_callback(self._tasks.discard)
                        continue  # Check for more work immediately
            except Exception:
                log.exception("Error in poll loop")

            await asyncio.sleep(self.config.poll_interval_seconds)

    async def _run_with_semaphore(self, task: dict):
        async with self.semaphore:
            try:
                await dispatch_task(task, self.config, self.db)
            except Exception:
                log.exception("Unhandled error dispatching task %d", task["id"])

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

        await self.db.close()
        log.info("Worker daemon stopped")


async def _run_worker():
    """Entry point for the worker daemon."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    config = load_config()
    config.repos_dir.mkdir(parents=True, exist_ok=True)
    config.logs_dir.mkdir(parents=True, exist_ok=True)

    db = Database(config.db_path)
    await db.connect()
    log.info("Database connected: %s", config.db_path)

    daemon = WorkerDaemon(config, db)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, lambda: asyncio.create_task(daemon.shutdown()))

    await daemon.run()


def run_worker():
    """Synchronous entry point for CLI / systemd."""
    asyncio.run(_run_worker())

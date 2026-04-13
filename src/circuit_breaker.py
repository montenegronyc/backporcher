"""Circuit breaker: per-repo failure rate protection.

Tracks recent task outcomes per repo.  When the failure rate exceeds a
threshold within a time window, the circuit "opens" and new tasks for
that repo are held instead of dispatched.  This prevents burning API
credits on a repo that's in a broken state (e.g. main is broken, CI
is misconfigured, or credentials have expired).

The circuit closes automatically after a cooldown period, or when a
task for the repo succeeds.

States:
  CLOSED  — normal operation, tasks dispatch freely
  OPEN    — too many recent failures, new tasks are held
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from .db import Database

log = logging.getLogger("backporcher.circuit_breaker")

# Default configuration — can be overridden via Config in the future
WINDOW_SECONDS = 3600  # Look at tasks from the last hour
FAILURE_THRESHOLD = 0.7  # Open circuit when >= 70% of recent tasks failed
MIN_TASKS_FOR_TRIP = 3  # Don't trip on 1-of-1 failure; need at least 3 tasks
COOLDOWN_SECONDS = 1800  # 30 minutes before allowing new tasks after trip

# Hold reason used for circuit-broken tasks
HOLD_CIRCUIT_BREAKER = "circuit_breaker"


async def check_circuit(repo_id: int, db: Database) -> bool:
    """Check if the circuit is healthy (closed) for a repo.

    Returns True if tasks should proceed, False if the circuit is open
    and new tasks should be held.
    """
    now = datetime.now(timezone.utc)

    # Query recent tasks for this repo within the window
    async with db.db.execute(
        "SELECT status, completed_at FROM tasks "
        "WHERE repo_id = ? "
        "  AND completed_at IS NOT NULL "
        "  AND completed_at > datetime('now', ?) "
        "ORDER BY completed_at DESC",
        (repo_id, f"-{WINDOW_SECONDS} seconds"),
    ) as cur:
        rows = await cur.fetchall()

    if len(rows) < MIN_TASKS_FOR_TRIP:
        return True  # Not enough data to trip

    total = len(rows)
    failed = sum(1 for r in rows if r[0] == "failed")
    rate = failed / total

    if rate < FAILURE_THRESHOLD:
        return True  # Healthy

    # Check cooldown: if the most recent failure was long enough ago,
    # allow one task through (half-open state)
    most_recent = rows[0]
    if most_recent[1]:
        try:
            last_time = datetime.fromisoformat(most_recent[1])
            if last_time.tzinfo is None:
                last_time = last_time.replace(tzinfo=timezone.utc)
            elapsed = (now - last_time).total_seconds()
            if elapsed >= COOLDOWN_SECONDS:
                log.info(
                    "Repo %d: circuit half-open after %ds cooldown (%.0f%% failure rate, %d tasks)",
                    repo_id,
                    int(elapsed),
                    rate * 100,
                    total,
                )
                return True  # Half-open: let one through
        except (ValueError, TypeError):
            pass

    log.warning(
        "Repo %d: circuit OPEN — %.0f%% failure rate (%d/%d tasks in last %ds)",
        repo_id,
        rate * 100,
        failed,
        total,
        WINDOW_SECONDS,
    )
    return False


async def apply_circuit_breaker(task: dict, db: Database) -> bool:
    """Check circuit and hold the task if open.

    Returns True if the task was held (caller should skip dispatch),
    False if the task can proceed normally.
    """
    repo_id = task["repo_id"]
    is_healthy = await check_circuit(repo_id, db)

    if is_healthy:
        return False

    # Hold the task
    task_id = task["id"]
    await db.set_hold(task_id, HOLD_CIRCUIT_BREAKER)
    await db.update_task(task_id, status="queued", started_at=None)
    await db.add_log(
        task_id,
        "Circuit breaker tripped: too many recent failures for this repo. "
        "Task held until failure rate drops or cooldown expires.",
        level="warn",
    )
    log.info("Task #%d: held by circuit breaker (repo %d)", task_id, repo_id)
    return True

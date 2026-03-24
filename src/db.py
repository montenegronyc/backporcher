"""SQLite database layer with WAL mode and parameterized queries."""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite

from .db_migrations import _init_and_migrate_sync


class Database:
    """Async SQLite database wrapper with write serialization."""

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._db: aiosqlite.Connection | None = None
        self._write_lock = asyncio.Lock()

    async def connect(self):
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        # Run migration using a sync connection first (avoids threading issues)
        _init_and_migrate_sync(self.db_path)

        self._db = await aiosqlite.connect(str(self.db_path))
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA foreign_keys=ON")
        await self._db.execute("PRAGMA busy_timeout=5000")

    async def close(self):
        if self._db:
            await self._db.close()
            self._db = None

    @property
    def db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("Database not connected")
        return self._db

    # --- Repos ---

    async def list_repos(self) -> list[dict]:
        async with self.db.execute("SELECT * FROM repos ORDER BY name") as cur:
            return [dict(r) for r in await cur.fetchall()]

    async def get_repo(self, repo_id: int) -> dict | None:
        async with self.db.execute("SELECT * FROM repos WHERE id = ?", (repo_id,)) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

    async def get_repo_by_name(self, name: str) -> dict | None:
        """Lookup by name (case-insensitive)."""
        async with self.db.execute("SELECT * FROM repos WHERE LOWER(name) = LOWER(?)", (name,)) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

    async def add_repo(
        self,
        name: str,
        github_url: str,
        local_path: str,
        default_branch: str = "main",
    ) -> int:
        async with self._write_lock:
            async with self.db.execute(
                "INSERT INTO repos (name, github_url, local_path, default_branch) VALUES (?, ?, ?, ?)",
                (name, github_url, local_path, default_branch),
            ) as cur:
                await self.db.commit()
                return cur.lastrowid

    async def update_repo(self, repo_id: int, **fields):
        allowed = {"verify_command", "default_branch", "stack_info"}
        fields = {k: v for k, v in fields.items() if k in allowed}
        if not fields:
            return
        async with self._write_lock:
            sets = ", ".join(f"{k} = ?" for k in fields)
            vals = list(fields.values()) + [repo_id]
            await self.db.execute(f"UPDATE repos SET {sets} WHERE id = ?", vals)
            await self.db.commit()

    # --- Tasks ---

    async def list_tasks(
        self,
        status: str | None = None,
        limit: int = 50,
    ) -> list[dict]:
        if status:
            query = (
                "SELECT t.*, r.name as repo_name FROM tasks t "
                "JOIN repos r ON t.repo_id = r.id "
                "WHERE t.status = ? ORDER BY t.created_at DESC LIMIT ?"
            )
            params = (status, limit)
        else:
            query = (
                "SELECT t.*, r.name as repo_name FROM tasks t "
                "JOIN repos r ON t.repo_id = r.id "
                "ORDER BY t.created_at DESC LIMIT ?"
            )
            params = (limit,)
        async with self.db.execute(query, params) as cur:
            return [dict(r) for r in await cur.fetchall()]

    async def get_task(self, task_id: int) -> dict | None:
        async with self.db.execute(
            "SELECT t.*, r.name as repo_name FROM tasks t JOIN repos r ON t.repo_id = r.id WHERE t.id = ?",
            (task_id,),
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

    async def create_task(
        self,
        repo_id: int,
        prompt: str,
        model: str = "sonnet",
        agent: str = "claude",
    ) -> int:
        async with self._write_lock:
            async with self.db.execute(
                "INSERT INTO tasks (repo_id, prompt, model, agent) VALUES (?, ?, ?, ?)",
                (repo_id, prompt, model, agent),
            ) as cur:
                await self.db.commit()
                return cur.lastrowid

    async def create_task_from_issue(
        self,
        repo_id: int,
        prompt: str,
        model: str,
        issue_number: int,
        issue_url: str,
        priority: int = 100,
        depends_on_task_id: int | None = None,
        agent: str = "claude",
    ) -> int:
        """Create a task linked to a GitHub issue."""
        async with self._write_lock:
            async with self.db.execute(
                "INSERT INTO tasks (repo_id, prompt, model, github_issue_number, github_issue_url, priority, depends_on_task_id, agent) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (repo_id, prompt, model, issue_number, issue_url, priority, depends_on_task_id, agent),
            ) as cur:
                await self.db.commit()
                return cur.lastrowid

    async def get_task_by_issue(self, repo_id: int, issue_number: int) -> dict | None:
        """Check if an issue already has a task (dedup).
        Excludes cancelled tasks and completed no-op tasks (no PR created)."""
        async with self.db.execute(
            "SELECT t.*, r.name as repo_name FROM tasks t "
            "JOIN repos r ON t.repo_id = r.id "
            "WHERE t.repo_id = ? AND t.github_issue_number = ? "
            "AND t.status != 'cancelled' "
            "AND NOT (t.status = 'completed' AND (t.pr_url IS NULL OR t.pr_url = '')) "
            "LIMIT 1",
            (repo_id, issue_number),
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

    async def list_pr_tasks(self) -> list[dict]:
        """Tasks awaiting CI results (status=reviewed, after coordinator approval)."""
        async with self.db.execute(
            "SELECT t.*, r.name as repo_name, r.github_url FROM tasks t "
            "JOIN repos r ON t.repo_id = r.id "
            "WHERE t.status = 'reviewed'"
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]

    async def list_retrying_tasks(self) -> list[dict]:
        """Tasks queued for CI retry (status=retrying)."""
        async with self.db.execute(
            "SELECT t.*, r.name as repo_name, r.github_url FROM tasks t "
            "JOIN repos r ON t.repo_id = r.id "
            "WHERE t.status = 'retrying'"
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]

    async def list_tasks_by_status(self, status: str) -> list[dict]:
        """List tasks with a specific status."""
        async with self.db.execute(
            "SELECT t.*, r.name as repo_name, r.github_url FROM tasks t "
            "JOIN repos r ON t.repo_id = r.id "
            "WHERE t.status = ?",
            (status,),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]

    async def list_cleanable_tasks(self, min_age_minutes: int = 10) -> list[dict]:
        """Find terminal tasks with worktrees or branches that are old enough to clean up."""
        async with self.db.execute(
            "SELECT t.*, r.name as repo_name, r.github_url FROM tasks t "
            "JOIN repos r ON t.repo_id = r.id "
            "WHERE t.status IN ('completed', 'failed', 'cancelled') "
            "  AND (t.worktree_path IS NOT NULL OR t.branch_name IS NOT NULL) "
            "  AND t.completed_at IS NOT NULL "
            "  AND t.completed_at < datetime('now', ?)",
            (f"-{min_age_minutes} minutes",),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]

    async def claim_next_queued(self) -> dict | None:
        """Atomically claim the highest-priority queued task whose dependencies are met."""
        async with self._write_lock:
            now = datetime.now(timezone.utc).isoformat()
            async with self.db.execute(
                "UPDATE tasks SET status = 'working', started_at = ? "
                "WHERE id = ("
                "  SELECT t.id FROM tasks t"
                "  WHERE t.status = 'queued'"
                "    AND t.hold IS NULL"
                "    AND ("
                "      t.depends_on_task_id IS NULL"
                "      OR EXISTS ("
                "        SELECT 1 FROM tasks dep"
                "        WHERE dep.id = t.depends_on_task_id"
                "          AND dep.status = 'completed'"
                "      )"
                "    )"
                "  ORDER BY t.priority ASC, t.created_at ASC"
                "  LIMIT 1"
                ") RETURNING *",
                (now,),
            ) as cur:
                row = await cur.fetchone()
                await self.db.commit()
                return dict(row) if row else None

    async def list_pending_review(self) -> list[dict]:
        """Tasks awaiting coordinator review (status=pr_created)."""
        async with self.db.execute(
            "SELECT t.*, r.name as repo_name, r.github_url FROM tasks t "
            "JOIN repos r ON t.repo_id = r.id "
            "WHERE t.status = 'pr_created'"
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]

    async def record_metric(
        self,
        event: str,
        task_id: int | None = None,
        repo: str | None = None,
        model: str | None = None,
        value: float | None = None,
    ):
        """Append a metric event. Never raises — logs and continues on failure."""
        try:
            now = datetime.now(timezone.utc).isoformat()
            async with self._write_lock:
                await self.db.execute(
                    "INSERT INTO metrics (event, task_id, repo, model, value, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (event, task_id, repo, model, value, now),
                )
                await self.db.commit()
        except (sqlite3.OperationalError, sqlite3.IntegrityError):
            import logging

            logging.getLogger("backporcher.db").warning(
                "Failed to record metric %s for task %s",
                event,
                task_id,
                exc_info=True,
            )

    async def update_task(self, task_id: int, **fields):
        if not fields:
            return
        allowed = {
            "status",
            "branch_name",
            "worktree_path",
            "pr_url",
            "agent_pid",
            "exit_code",
            "error_message",
            "output_summary",
            "review_summary",
            "prompt",
            "model",
            "started_at",
            "completed_at",
            "pr_number",
            "github_issue_number",
            "github_issue_url",
            "retry_count",
            "priority",
            "depends_on_task_id",
            "hold",
            "agent_started_at",
            "agent_finished_at",
            "model_used",
            "initial_model",
            "agent",
            "agent_fallback_count",
        }
        fields = {k: v for k, v in fields.items() if k in allowed}
        if not fields:
            return
        async with self._write_lock:
            sets = ", ".join(f"{k} = ?" for k in fields)
            vals = list(fields.values()) + [task_id]
            await self.db.execute(f"UPDATE tasks SET {sets} WHERE id = ?", vals)
            await self.db.commit()

    async def handle_dependency_failure(self, failed_task_id: int) -> list[int]:
        """Cascade failure to queued tasks that depend on the failed task."""
        async with self._write_lock:
            now = datetime.now(timezone.utc).isoformat()
            async with self.db.execute(
                "UPDATE tasks SET status = 'failed', "
                "error_message = 'Dependency task #' || ? || ' failed', "
                "completed_at = ? "
                "WHERE depends_on_task_id = ? AND status = 'queued' "
                "RETURNING id",
                (str(failed_task_id), now, failed_task_id),
            ) as cur:
                rows = await cur.fetchall()
                await self.db.commit()
                cascaded_ids = [r[0] for r in rows]

        # Recursively cascade to dependents of dependents
        all_cascaded = list(cascaded_ids)
        for cid in cascaded_ids:
            sub = await self.handle_dependency_failure(cid)
            all_cascaded.extend(sub)
        return all_cascaded

    # --- Hold / Pause ---

    async def set_hold(self, task_id: int, hold_reason: str):
        """Set a hold on a task (prevents claiming/merging)."""
        async with self._write_lock:
            await self.db.execute(
                "UPDATE tasks SET hold = ? WHERE id = ?",
                (hold_reason, task_id),
            )
            await self.db.commit()

    async def clear_hold(self, task_id: int):
        """Clear hold on a task."""
        async with self._write_lock:
            await self.db.execute(
                "UPDATE tasks SET hold = NULL WHERE id = ?",
                (task_id,),
            )
            await self.db.commit()

    async def list_held_tasks(self) -> list[dict]:
        """List all tasks with a hold set."""
        async with self.db.execute(
            "SELECT t.*, r.name as repo_name FROM tasks t "
            "JOIN repos r ON t.repo_id = r.id "
            "WHERE t.hold IS NOT NULL "
            "ORDER BY t.created_at DESC"
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]

    async def is_queue_paused(self) -> bool:
        """Check if the global queue is paused."""
        async with self.db.execute("SELECT value FROM system_state WHERE key = 'queue_paused'") as cur:
            row = await cur.fetchone()
            return row is not None and row[0] == "true"

    async def set_queue_paused(self, paused: bool):
        """Set or clear the global queue pause."""
        async with self._write_lock:
            now = datetime.now(timezone.utc).isoformat()
            await self.db.execute(
                "INSERT INTO system_state (key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                ("queue_paused", "true" if paused else "false", now),
            )
            await self.db.commit()

    async def list_inflight_tasks_for_repo(self, repo_id: int) -> list[dict]:
        """List tasks in active statuses for a given repo (for conflict checking)."""
        async with self.db.execute(
            "SELECT t.*, r.name as repo_name FROM tasks t "
            "JOIN repos r ON t.repo_id = r.id "
            "WHERE t.repo_id = ? AND t.status IN ('working', 'pr_created', 'reviewing', 'reviewed') "
            "ORDER BY t.created_at ASC",
            (repo_id,),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]

    async def count_active(self) -> int:
        async with self.db.execute("SELECT COUNT(*) FROM tasks WHERE status = 'working'") as cur:
            row = await cur.fetchone()
            return row[0]

    async def count_queued(self) -> int:
        async with self.db.execute("SELECT COUNT(*) FROM tasks WHERE status = 'queued'") as cur:
            row = await cur.fetchone()
            return row[0]

    # --- Task Logs ---

    async def add_log(self, task_id: int, message: str, level: str = "info"):
        async with self._write_lock:
            await self.db.execute(
                "INSERT INTO task_logs (task_id, level, message) VALUES (?, ?, ?)",
                (task_id, level, message),
            )
            await self.db.commit()

    async def get_logs(
        self,
        task_id: int,
        limit: int = 20,
        offset: int = 0,
    ) -> list[dict]:
        async with self.db.execute(
            "SELECT * FROM task_logs WHERE task_id = ? ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (task_id, limit, offset),
        ) as cur:
            rows = [dict(r) for r in await cur.fetchall()]
            rows.reverse()  # Oldest first for display
            return rows

    # --- Learnings ---

    async def add_learning(
        self,
        repo_id: int,
        learning_type: str,
        content: str,
        task_id: int | None = None,
    ):
        async with self._write_lock:
            await self.db.execute(
                "INSERT INTO repo_learnings (repo_id, task_id, learning_type, content) VALUES (?, ?, ?, ?)",
                (repo_id, task_id, learning_type, content),
            )
            await self.db.commit()

    async def get_learnings(self, repo_id: int, limit: int = 10) -> list[dict]:
        async with self.db.execute(
            "SELECT * FROM repo_learnings WHERE repo_id = ? ORDER BY created_at DESC LIMIT ?",
            (repo_id, limit),
        ) as cur:
            rows = [dict(r) for r in await cur.fetchall()]
            rows.reverse()  # Oldest first
            return rows

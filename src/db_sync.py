"""Synchronous SQLite database wrapper for CLI commands."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .db_migrations import _migrate_sync


class SyncDatabase:
    """Synchronous SQLite wrapper for CLI commands (no event loop needed)."""

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._db: sqlite3.Connection | None = None

    def connect(self):
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(self.db_path))
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA foreign_keys=ON")
        self._db.execute("PRAGMA busy_timeout=5000")

        # Ensure base tables exist before migration
        self._db.executescript("""
CREATE TABLE IF NOT EXISTS repos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    github_url TEXT NOT NULL,
    local_path TEXT NOT NULL,
    default_branch TEXT DEFAULT 'main',
    verify_command TEXT,
    stack_info TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS task_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL,
    level TEXT DEFAULT 'info',
    message TEXT NOT NULL,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_task_logs_task_id ON task_logs(task_id);
""")
        self._db.commit()
        _migrate_sync(self._db)

    def close(self):
        if self._db:
            self._db.close()
            self._db = None

    @property
    def db(self) -> sqlite3.Connection:
        if self._db is None:
            raise RuntimeError("Database not connected")
        return self._db

    def list_repos(self) -> list[dict]:
        cur = self.db.execute("SELECT * FROM repos ORDER BY name")
        return [dict(r) for r in cur.fetchall()]

    def get_repo_by_name(self, name: str) -> dict | None:
        cur = self.db.execute("SELECT * FROM repos WHERE LOWER(name) = LOWER(?)", (name,))
        row = cur.fetchone()
        return dict(row) if row else None

    def add_repo(
        self,
        name: str,
        github_url: str,
        local_path: str,
        default_branch: str = "main",
    ) -> int:
        cur = self.db.execute(
            "INSERT INTO repos (name, github_url, local_path, default_branch) VALUES (?, ?, ?, ?)",
            (name, github_url, local_path, default_branch),
        )
        self.db.commit()
        return cur.lastrowid

    def update_repo(self, repo_id: int, **fields):
        allowed = {"verify_command", "default_branch", "stack_info"}
        fields = {k: v for k, v in fields.items() if k in allowed}
        if not fields:
            return
        sets = ", ".join(f"{k} = ?" for k in fields)
        vals = list(fields.values()) + [repo_id]
        self.db.execute(f"UPDATE repos SET {sets} WHERE id = ?", vals)
        self.db.commit()

    def create_task(self, repo_id: int, prompt: str, model: str = "sonnet") -> int:
        cur = self.db.execute(
            "INSERT INTO tasks (repo_id, prompt, model) VALUES (?, ?, ?)",
            (repo_id, prompt, model),
        )
        self.db.commit()
        return cur.lastrowid

    def add_log(self, task_id: int, message: str, level: str = "info"):
        self.db.execute(
            "INSERT INTO task_logs (task_id, level, message) VALUES (?, ?, ?)",
            (task_id, level, message),
        )
        self.db.commit()

    def get_task(self, task_id: int) -> dict | None:
        cur = self.db.execute(
            "SELECT t.*, r.name as repo_name FROM tasks t JOIN repos r ON t.repo_id = r.id WHERE t.id = ?",
            (task_id,),
        )
        row = cur.fetchone()
        return dict(row) if row else None

    def list_tasks(self, status: str | None = None, limit: int = 50) -> list[dict]:
        if status:
            cur = self.db.execute(
                "SELECT t.*, r.name as repo_name FROM tasks t "
                "JOIN repos r ON t.repo_id = r.id "
                "WHERE t.status = ? ORDER BY t.created_at DESC LIMIT ?",
                (status, limit),
            )
        else:
            cur = self.db.execute(
                "SELECT t.*, r.name as repo_name FROM tasks t "
                "JOIN repos r ON t.repo_id = r.id "
                "ORDER BY t.created_at DESC LIMIT ?",
                (limit,),
            )
        return [dict(r) for r in cur.fetchall()]

    def get_logs(self, task_id: int, limit: int = 20) -> list[dict]:
        cur = self.db.execute(
            "SELECT * FROM task_logs WHERE task_id = ? ORDER BY created_at DESC LIMIT ?",
            (task_id, limit),
        )
        rows = [dict(r) for r in cur.fetchall()]
        rows.reverse()
        return rows

    def record_metric(
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
            self.db.execute(
                "INSERT INTO metrics (event, task_id, repo, model, value, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (event, task_id, repo, model, value, now),
            )
            self.db.commit()
        except (sqlite3.OperationalError, sqlite3.IntegrityError):
            import logging

            logging.getLogger("backporcher.db").warning(
                "Failed to record metric %s for task %s",
                event,
                task_id,
                exc_info=True,
            )

    def update_task(self, task_id: int, **fields):
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
        }
        fields = {k: v for k, v in fields.items() if k in allowed}
        if not fields:
            return
        sets = ", ".join(f"{k} = ?" for k in fields)
        vals = list(fields.values()) + [task_id]
        self.db.execute(f"UPDATE tasks SET {sets} WHERE id = ?", vals)
        self.db.commit()

    def handle_dependency_failure(self, failed_task_id: int) -> list[int]:
        """Cascade failure to queued tasks that depend on the failed task."""
        now = datetime.now(timezone.utc).isoformat()
        cur = self.db.execute(
            "UPDATE tasks SET status = 'failed', "
            "error_message = 'Dependency task #' || ? || ' failed', "
            "completed_at = ? "
            "WHERE depends_on_task_id = ? AND status = 'queued' "
            "RETURNING id",
            (str(failed_task_id), now, failed_task_id),
        )
        rows = cur.fetchall()
        self.db.commit()
        cascaded_ids = [r[0] for r in rows]

        all_cascaded = list(cascaded_ids)
        for cid in cascaded_ids:
            sub = self.handle_dependency_failure(cid)
            all_cascaded.extend(sub)
        return all_cascaded

    # --- Hold / Pause ---

    def set_hold(self, task_id: int, hold_reason: str):
        self.db.execute("UPDATE tasks SET hold = ? WHERE id = ?", (hold_reason, task_id))
        self.db.commit()

    def clear_hold(self, task_id: int):
        self.db.execute("UPDATE tasks SET hold = NULL WHERE id = ?", (task_id,))
        self.db.commit()

    def list_held_tasks(self) -> list[dict]:
        cur = self.db.execute(
            "SELECT t.*, r.name as repo_name FROM tasks t "
            "JOIN repos r ON t.repo_id = r.id "
            "WHERE t.hold IS NOT NULL "
            "ORDER BY t.created_at DESC"
        )
        return [dict(r) for r in cur.fetchall()]

    def is_queue_paused(self) -> bool:
        cur = self.db.execute("SELECT value FROM system_state WHERE key = 'queue_paused'")
        row = cur.fetchone()
        return row is not None and row[0] == "true"

    def set_queue_paused(self, paused: bool):
        now = datetime.now(timezone.utc).isoformat()
        self.db.execute(
            "INSERT INTO system_state (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            ("queue_paused", "true" if paused else "false", now),
        )
        self.db.commit()

    def count_active(self) -> int:
        cur = self.db.execute("SELECT COUNT(*) FROM tasks WHERE status = 'working'")
        return cur.fetchone()[0]

    def count_queued(self) -> int:
        cur = self.db.execute("SELECT COUNT(*) FROM tasks WHERE status = 'queued'")
        return cur.fetchone()[0]

    # --- Learnings ---

    def get_learnings(self, repo_id: int, limit: int = 20) -> list[dict]:
        cur = self.db.execute(
            "SELECT * FROM repo_learnings WHERE repo_id = ? ORDER BY created_at DESC LIMIT ?",
            (repo_id, limit),
        )
        rows = [dict(r) for r in cur.fetchall()]
        rows.reverse()  # Oldest first
        return rows

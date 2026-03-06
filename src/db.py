"""SQLite database layer with WAL mode and parameterized queries."""

import aiosqlite
from pathlib import Path
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS repos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    github_url TEXT NOT NULL UNIQUE,
    local_path TEXT,
    default_branch TEXT DEFAULT 'main',
    last_fetched_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_id INTEGER NOT NULL REFERENCES repos(id),
    prompt TEXT NOT NULL,
    branch_name TEXT,
    worktree_path TEXT,
    status TEXT NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued','working','pr_created','completed','failed','cancelled')),
    pr_url TEXT,
    model TEXT NOT NULL DEFAULT 'sonnet',
    max_budget_usd REAL NOT NULL DEFAULT 5.0,
    agent_pid INTEGER,
    exit_code INTEGER,
    error_message TEXT,
    output_summary TEXT,
    cost_usd REAL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    started_at TEXT,
    completed_at TEXT
);

CREATE TABLE IF NOT EXISTS task_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL REFERENCES tasks(id),
    level TEXT NOT NULL DEFAULT 'info',
    message TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_repo_id ON tasks(repo_id);
CREATE INDEX IF NOT EXISTS idx_task_logs_task_id ON task_logs(task_id);
"""


class Database:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._db: aiosqlite.Connection | None = None

    async def connect(self):
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(str(self.db_path))
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA foreign_keys=ON")
        await self._db.execute("PRAGMA busy_timeout=5000")
        await self._db.executescript(SCHEMA)
        await self._db.commit()

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
        async with self.db.execute(
            "SELECT * FROM repos WHERE id = ?", (repo_id,)
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

    async def add_repo(
        self, name: str, github_url: str, local_path: str, default_branch: str = "main"
    ) -> int:
        async with self.db.execute(
            "INSERT INTO repos (name, github_url, local_path, default_branch) "
            "VALUES (?, ?, ?, ?)",
            (name, github_url, local_path, default_branch),
        ) as cur:
            await self.db.commit()
            return cur.lastrowid

    async def update_repo_fetched(self, repo_id: int):
        now = datetime.now(timezone.utc).isoformat()
        await self.db.execute(
            "UPDATE repos SET last_fetched_at = ? WHERE id = ?", (now, repo_id)
        )
        await self.db.commit()

    # --- Tasks ---

    async def list_tasks(
        self, status: str | None = None, limit: int = 100
    ) -> list[dict]:
        if status:
            query = "SELECT * FROM tasks WHERE status = ? ORDER BY created_at DESC LIMIT ?"
            params = (status, limit)
        else:
            query = "SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?"
            params = (limit,)
        async with self.db.execute(query, params) as cur:
            return [dict(r) for r in await cur.fetchall()]

    async def get_task(self, task_id: int) -> dict | None:
        async with self.db.execute(
            "SELECT * FROM tasks WHERE id = ?", (task_id,)
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

    async def create_task(
        self,
        repo_id: int,
        prompt: str,
        model: str = "sonnet",
        max_budget_usd: float = 5.0,
    ) -> int:
        async with self.db.execute(
            "INSERT INTO tasks (repo_id, prompt, model, max_budget_usd) "
            "VALUES (?, ?, ?, ?)",
            (repo_id, prompt, model, max_budget_usd),
        ) as cur:
            await self.db.commit()
            return cur.lastrowid

    async def claim_next_queued(self) -> dict | None:
        """Atomically claim the next queued task."""
        now = datetime.now(timezone.utc).isoformat()
        async with self.db.execute(
            "UPDATE tasks SET status = 'working', started_at = ? "
            "WHERE id = (SELECT id FROM tasks WHERE status = 'queued' "
            "ORDER BY created_at ASC LIMIT 1) RETURNING *",
            (now,),
        ) as cur:
            row = await cur.fetchone()
            await self.db.commit()
            return dict(row) if row else None

    async def update_task(self, task_id: int, **fields):
        if not fields:
            return
        allowed = {
            "status", "branch_name", "worktree_path", "pr_url",
            "agent_pid", "exit_code", "error_message", "output_summary",
            "cost_usd", "started_at", "completed_at",
        }
        fields = {k: v for k, v in fields.items() if k in allowed}
        if not fields:
            return
        sets = ", ".join(f"{k} = ?" for k in fields)
        vals = list(fields.values()) + [task_id]
        await self.db.execute(
            f"UPDATE tasks SET {sets} WHERE id = ?", vals
        )
        await self.db.commit()

    async def count_active(self) -> int:
        async with self.db.execute(
            "SELECT COUNT(*) FROM tasks WHERE status = 'working'"
        ) as cur:
            row = await cur.fetchone()
            return row[0]

    # --- Task Logs ---

    async def add_log(self, task_id: int, message: str, level: str = "info"):
        await self.db.execute(
            "INSERT INTO task_logs (task_id, level, message) VALUES (?, ?, ?)",
            (task_id, level, message),
        )
        await self.db.commit()

    async def get_logs(
        self, task_id: int, limit: int = 500, offset: int = 0
    ) -> list[dict]:
        async with self.db.execute(
            "SELECT * FROM task_logs WHERE task_id = ? "
            "ORDER BY created_at ASC LIMIT ? OFFSET ?",
            (task_id, limit, offset),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]

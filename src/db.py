"""SQLite database layer with WAL mode and parameterized queries."""

import asyncio
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite

SCHEMA_VERSION = 7

SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS repos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    github_url TEXT NOT NULL,
    local_path TEXT NOT NULL,
    default_branch TEXT DEFAULT 'main',
    created_at TEXT DEFAULT (datetime('now'))
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
    model TEXT DEFAULT 'sonnet',
    agent_pid INTEGER,
    exit_code INTEGER,
    error_message TEXT,
    output_summary TEXT,
    started_at TEXT,
    completed_at TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS task_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL REFERENCES tasks(id),
    level TEXT DEFAULT 'info',
    message TEXT NOT NULL,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_repo_id ON tasks(repo_id);
CREATE INDEX IF NOT EXISTS idx_task_logs_task_id ON task_logs(task_id);
"""

VALID_STATUSES = (
    "queued",
    "working",
    "pr_created",
    "reviewing",
    "reviewed",
    "ci_passed",
    "retrying",
    "completed",
    "failed",
    "cancelled",
)

SCHEMA_V3_TASKS = """
CREATE TABLE tasks_v3 (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_id INTEGER NOT NULL REFERENCES repos(id),
    prompt TEXT NOT NULL,
    branch_name TEXT,
    worktree_path TEXT,
    status TEXT NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued','working','pr_created','reviewing','reviewed','ci_passed','retrying','completed','failed','cancelled')),
    pr_url TEXT,
    model TEXT DEFAULT 'sonnet',
    agent_pid INTEGER,
    exit_code INTEGER,
    error_message TEXT,
    output_summary TEXT,
    review_summary TEXT,
    github_issue_number INTEGER,
    github_issue_url TEXT,
    pr_number INTEGER,
    retry_count INTEGER DEFAULT 0,
    started_at TEXT,
    completed_at TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);
"""

SCHEMA_V2_TASKS = """
CREATE TABLE tasks_v2 (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_id INTEGER NOT NULL REFERENCES repos(id),
    prompt TEXT NOT NULL,
    branch_name TEXT,
    worktree_path TEXT,
    status TEXT NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued','working','pr_created','ci_passed','retrying','completed','failed','cancelled')),
    pr_url TEXT,
    model TEXT DEFAULT 'sonnet',
    agent_pid INTEGER,
    exit_code INTEGER,
    error_message TEXT,
    output_summary TEXT,
    github_issue_number INTEGER,
    github_issue_url TEXT,
    pr_number INTEGER,
    retry_count INTEGER DEFAULT 0,
    started_at TEXT,
    completed_at TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);
"""


def _get_schema_version(conn) -> int:
    """Get current schema version. Returns 1 if tasks table exists but no version table."""
    try:
        cur = conn.execute("SELECT version FROM schema_version ORDER BY version DESC LIMIT 1")
        row = cur.fetchone()
        return row[0] if row else 1
    except Exception:
        # No schema_version table — check if tasks table exists (v1)
        cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='tasks'")
        return 1 if cur.fetchone() else 0


def _migrate_sync(conn):
    """Run schema migrations (sync, works for both sync and async via raw conn)."""
    version = _get_schema_version(conn)

    if version == 0:
        # Fresh database — create current tasks table directly (repos/task_logs already created)
        conn.executescript("""
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_id INTEGER NOT NULL REFERENCES repos(id),
    prompt TEXT NOT NULL,
    branch_name TEXT,
    worktree_path TEXT,
    status TEXT NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued','working','pr_created','reviewing','reviewed','ci_passed','retrying','completed','failed','cancelled')),
    pr_url TEXT,
    model TEXT DEFAULT 'sonnet',
    agent_pid INTEGER,
    exit_code INTEGER,
    error_message TEXT,
    output_summary TEXT,
    review_summary TEXT,
    github_issue_number INTEGER,
    github_issue_url TEXT,
    pr_number INTEGER,
    retry_count INTEGER DEFAULT 0,
    priority INTEGER DEFAULT 100,
    depends_on_task_id INTEGER,
    hold TEXT,
    agent_started_at TEXT,
    agent_finished_at TEXT,
    model_used TEXT,
    initial_model TEXT,
    started_at TEXT,
    completed_at TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_repo_id ON tasks(repo_id);
CREATE INDEX IF NOT EXISTS idx_tasks_priority ON tasks(priority);

CREATE TABLE IF NOT EXISTS system_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event TEXT NOT NULL,
    task_id INTEGER,
    repo TEXT,
    model TEXT,
    value REAL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_metrics_event ON metrics(event);
CREATE INDEX IF NOT EXISTS idx_metrics_created_at ON metrics(created_at);
""")
        conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY)")
        conn.execute("INSERT OR REPLACE INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,))
        conn.commit()
        return

    if version < 2:
        # Migrate from v1 to v2: recreate tasks table with new columns + statuses
        # Disable FK checks during migration (task_logs references tasks)
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("DROP TABLE IF EXISTS tasks_v2")
        conn.executescript(SCHEMA_V2_TASKS)
        conn.execute("""
            INSERT INTO tasks_v2 (
                id, repo_id, prompt, branch_name, worktree_path, status,
                pr_url, model, agent_pid, exit_code, error_message,
                output_summary, started_at, completed_at, created_at,
                github_issue_number, github_issue_url, pr_number, retry_count
            )
            SELECT
                id, repo_id, prompt, branch_name, worktree_path, status,
                pr_url, model, agent_pid, exit_code, error_message,
                output_summary, started_at, completed_at, created_at,
                NULL, NULL, NULL, 0
            FROM tasks
        """)
        conn.execute("DROP TABLE tasks")
        conn.execute("ALTER TABLE tasks_v2 RENAME TO tasks")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_repo_id ON tasks(repo_id)")
        conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY)")
        conn.execute("INSERT OR REPLACE INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,))
        conn.execute("PRAGMA foreign_keys=ON")
        conn.commit()

    if version < 3:
        # Migrate to v3: add reviewing/reviewed statuses + review_summary column
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("DROP TABLE IF EXISTS tasks_v3")
        conn.executescript(SCHEMA_V3_TASKS)
        conn.execute("""
            INSERT INTO tasks_v3 (
                id, repo_id, prompt, branch_name, worktree_path, status,
                pr_url, model, agent_pid, exit_code, error_message,
                output_summary, review_summary,
                github_issue_number, github_issue_url, pr_number, retry_count,
                started_at, completed_at, created_at
            )
            SELECT
                id, repo_id, prompt, branch_name, worktree_path, status,
                pr_url, model, agent_pid, exit_code, error_message,
                output_summary, NULL,
                github_issue_number, github_issue_url, pr_number, retry_count,
                started_at, completed_at, created_at
            FROM tasks
        """)
        conn.execute("DROP TABLE tasks")
        conn.execute("ALTER TABLE tasks_v3 RENAME TO tasks")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_repo_id ON tasks(repo_id)")
        conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY)")
        conn.execute("INSERT OR REPLACE INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,))
        conn.execute("PRAGMA foreign_keys=ON")
        conn.commit()

    if version < 4:
        # v4: add verify_command to repos
        try:
            conn.execute("ALTER TABLE repos ADD COLUMN verify_command TEXT")
        except Exception:
            pass  # Column already exists
        conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY)")
        conn.execute("INSERT OR REPLACE INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,))
        conn.commit()

    if version < 5:
        # v5: add priority and depends_on_task_id to tasks
        try:
            conn.execute("ALTER TABLE tasks ADD COLUMN priority INTEGER DEFAULT 100")
        except Exception:
            pass  # Column already exists
        try:
            conn.execute("ALTER TABLE tasks ADD COLUMN depends_on_task_id INTEGER")
        except Exception:
            pass  # Column already exists
        conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_priority ON tasks(priority)")
        conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY)")
        conn.execute("INSERT OR REPLACE INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,))
        conn.commit()

    if version < 6:
        # v6: add hold column to tasks + system_state table
        try:
            conn.execute("ALTER TABLE tasks ADD COLUMN hold TEXT")
        except Exception:
            pass  # Column already exists
        conn.execute("""
            CREATE TABLE IF NOT EXISTS system_state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT DEFAULT (datetime('now'))
            )
        """)
        conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY)")
        conn.execute("INSERT OR REPLACE INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,))
        conn.commit()

    if version < 7:
        # v7: add metrics columns to tasks + metrics table
        for col in [
            "ALTER TABLE tasks ADD COLUMN agent_started_at TEXT",
            "ALTER TABLE tasks ADD COLUMN agent_finished_at TEXT",
            "ALTER TABLE tasks ADD COLUMN model_used TEXT",
            "ALTER TABLE tasks ADD COLUMN initial_model TEXT",
        ]:
            try:
                conn.execute(col)
            except Exception:
                pass  # Column already exists
        conn.execute("""
            CREATE TABLE IF NOT EXISTS metrics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event TEXT NOT NULL,
                task_id INTEGER,
                repo TEXT,
                model TEXT,
                value REAL,
                created_at TEXT NOT NULL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_metrics_event ON metrics(event)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_metrics_created_at ON metrics(created_at)")
        conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY)")
        conn.execute("INSERT OR REPLACE INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,))
        conn.commit()


def _init_and_migrate_sync(db_path: Path):
    """Initialize base tables and run migration using a sync connection."""
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.executescript("""
CREATE TABLE IF NOT EXISTS repos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    github_url TEXT NOT NULL,
    local_path TEXT NOT NULL,
    default_branch TEXT DEFAULT 'main',
    verify_command TEXT,
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
    conn.commit()
    _migrate_sync(conn)
    conn.close()


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
        allowed = {"verify_command", "default_branch"}
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
    ) -> int:
        async with self._write_lock:
            async with self.db.execute(
                "INSERT INTO tasks (repo_id, prompt, model) VALUES (?, ?, ?)",
                (repo_id, prompt, model),
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
    ) -> int:
        """Create a task linked to a GitHub issue."""
        async with self._write_lock:
            async with self.db.execute(
                "INSERT INTO tasks (repo_id, prompt, model, github_issue_number, github_issue_url, priority, depends_on_task_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (repo_id, prompt, model, issue_number, issue_url, priority, depends_on_task_id),
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
        except Exception:
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
        allowed = {"verify_command", "default_branch"}
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
        except Exception:
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

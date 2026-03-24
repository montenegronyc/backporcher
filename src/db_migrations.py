"""Database migration logic: sync migration runner and initialization."""

import sqlite3
from pathlib import Path

from .db_schema import (
    SCHEMA_V2_TASKS,
    SCHEMA_V3_TASKS,
    SCHEMA_VERSION,
    _get_schema_version,
)


def _migrate_sync(conn):
    """Run schema migrations (sync, works for both sync and async via raw conn)."""
    version = _get_schema_version(conn)

    if version == 0:
        # Fresh database -- create current tasks table directly (repos/task_logs already created)
        conn.executescript("""
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_id INTEGER NOT NULL REFERENCES repos(id),
    prompt TEXT NOT NULL,
    branch_name TEXT,
    worktree_path TEXT,
    status TEXT NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued','working','pr_created','reviewing',
                          'reviewed','ci_passed','retrying','completed',
                          'failed','cancelled')),
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
    agent TEXT NOT NULL DEFAULT 'claude',
    agent_fallback_count INTEGER NOT NULL DEFAULT 0,
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

CREATE TABLE IF NOT EXISTS repo_learnings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_id INTEGER NOT NULL REFERENCES repos(id),
    task_id INTEGER,
    learning_type TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_repo_learnings_repo ON repo_learnings(repo_id);
""")
        conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY)")
        conn.execute("INSERT OR REPLACE INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,))
        conn.commit()
        return

    if version < 2:
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
        try:
            conn.execute("ALTER TABLE repos ADD COLUMN verify_command TEXT")
        except sqlite3.OperationalError:
            pass
        conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY)")
        conn.execute("INSERT OR REPLACE INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,))
        conn.commit()

    if version < 5:
        try:
            conn.execute("ALTER TABLE tasks ADD COLUMN priority INTEGER DEFAULT 100")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE tasks ADD COLUMN depends_on_task_id INTEGER")
        except sqlite3.OperationalError:
            pass
        conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_priority ON tasks(priority)")
        conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY)")
        conn.execute("INSERT OR REPLACE INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,))
        conn.commit()

    if version < 6:
        try:
            conn.execute("ALTER TABLE tasks ADD COLUMN hold TEXT")
        except sqlite3.OperationalError:
            pass
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
        for col in [
            "ALTER TABLE tasks ADD COLUMN agent_started_at TEXT",
            "ALTER TABLE tasks ADD COLUMN agent_finished_at TEXT",
            "ALTER TABLE tasks ADD COLUMN model_used TEXT",
            "ALTER TABLE tasks ADD COLUMN initial_model TEXT",
        ]:
            try:
                conn.execute(col)
            except sqlite3.OperationalError:
                pass
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

    if version < 8:
        try:
            conn.execute("ALTER TABLE repos ADD COLUMN stack_info TEXT")
        except sqlite3.OperationalError:
            pass
        conn.execute("""
            CREATE TABLE IF NOT EXISTS repo_learnings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                repo_id INTEGER NOT NULL REFERENCES repos(id),
                task_id INTEGER,
                learning_type TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT DEFAULT (datetime('now'))
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_repo_learnings_repo ON repo_learnings(repo_id)")
        conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY)")
        conn.execute("INSERT OR REPLACE INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,))
        conn.commit()

    if version < 9:
        cur = conn.cursor()
        cur.execute("ALTER TABLE tasks ADD COLUMN agent TEXT NOT NULL DEFAULT 'claude'")
        cur.execute("ALTER TABLE tasks ADD COLUMN agent_fallback_count INTEGER NOT NULL DEFAULT 0")
        cur.execute("UPDATE schema_version SET version = 9")
        version = 9
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
    conn.commit()
    _migrate_sync(conn)
    conn.close()

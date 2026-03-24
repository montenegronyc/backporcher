"""Database schema definitions for backporcher."""

import sqlite3

SCHEMA_VERSION = 9

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
    except sqlite3.OperationalError:
        # No schema_version table -- check if tasks table exists (v1)
        cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='tasks'")
        return 1 if cur.fetchone() else 0

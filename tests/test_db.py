"""Tests for database layer — schema migration, CRUD, new methods."""

import sqlite3
from pathlib import Path

import pytest
import pytest_asyncio

from src.db import SCHEMA_V1, Database, SyncDatabase, _get_schema_version

# --- Helpers ---


def make_v1_db(path: Path):
    """Create a v1 database (original schema, no schema_version table)."""
    conn = sqlite3.connect(str(path))
    conn.executescript(SCHEMA_V1)
    conn.execute(
        "INSERT INTO repos (name, github_url, local_path) VALUES (?, ?, ?)",
        ("testrepo", "https://github.com/test/repo", "/tmp/repo"),
    )
    conn.execute(
        "INSERT INTO tasks (repo_id, prompt, model, status) VALUES (?, ?, ?, ?)",
        (1, "test prompt", "sonnet", "queued"),
    )
    conn.execute(
        "INSERT INTO tasks (repo_id, prompt, model, status, pr_url) VALUES (?, ?, ?, ?, ?)",
        (1, "done task", "opus", "pr_created", "https://github.com/test/repo/pull/1"),
    )
    conn.execute(
        "INSERT INTO task_logs (task_id, message) VALUES (?, ?)",
        (1, "test log"),
    )
    conn.commit()
    conn.close()


# --- Schema Migration Tests ---


class TestSchemaMigration:
    def test_fresh_database(self, tmp_path):
        """Fresh DB gets latest schema."""
        db_path = tmp_path / "fresh.db"
        db = SyncDatabase(db_path)
        db.connect()

        # Should be latest version
        cur = db.db.execute("SELECT version FROM schema_version")
        assert cur.fetchone()[0] == 7

        # Should have all columns across all migrations
        cur = db.db.execute("PRAGMA table_info(tasks)")
        columns = {row[1] for row in cur.fetchall()}
        assert "github_issue_number" in columns
        assert "github_issue_url" in columns
        assert "pr_number" in columns
        assert "retry_count" in columns
        assert "priority" in columns
        assert "depends_on_task_id" in columns
        assert "hold" in columns
        assert "agent_started_at" in columns
        assert "agent_finished_at" in columns
        assert "model_used" in columns
        assert "initial_model" in columns

        # Should have system_state table
        cur = db.db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='system_state'")
        assert cur.fetchone() is not None

        # Should have metrics table
        cur = db.db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='metrics'")
        assert cur.fetchone() is not None

        db.close()

    def test_v1_to_latest_migration(self, tmp_path):
        """Migrate v1 database all the way to latest, preserving data."""
        db_path = tmp_path / "v1.db"
        make_v1_db(db_path)

        db = SyncDatabase(db_path)
        db.connect()

        # Version upgraded to latest
        cur = db.db.execute("SELECT version FROM schema_version")
        assert cur.fetchone()[0] == 7

        # Data preserved
        cur = db.db.execute("SELECT COUNT(*) FROM tasks")
        assert cur.fetchone()[0] == 2

        cur = db.db.execute("SELECT COUNT(*) FROM repos")
        assert cur.fetchone()[0] == 1

        cur = db.db.execute("SELECT COUNT(*) FROM task_logs")
        assert cur.fetchone()[0] == 1

        # New columns have defaults
        cur = db.db.execute("SELECT github_issue_number, retry_count FROM tasks WHERE id = 1")
        row = cur.fetchone()
        assert row[0] is None
        assert row[1] == 0

        # New statuses accepted
        db.db.execute("UPDATE tasks SET status = 'ci_passed' WHERE id = 1")
        db.db.execute("UPDATE tasks SET status = 'retrying' WHERE id = 2")
        db.db.commit()

        db.close()

    def test_migration_idempotent(self, tmp_path):
        """Running migration twice doesn't break anything."""
        db_path = tmp_path / "idem.db"
        make_v1_db(db_path)

        # First migration
        db = SyncDatabase(db_path)
        db.connect()
        db.close()

        # Second migration
        db = SyncDatabase(db_path)
        db.connect()

        cur = db.db.execute("SELECT version FROM schema_version")
        assert cur.fetchone()[0] == 7

        cur = db.db.execute("SELECT COUNT(*) FROM tasks")
        assert cur.fetchone()[0] == 2

        db.close()

    def test_version_detection_v1(self, tmp_path):
        """Detect v1 schema (tasks exists, no schema_version)."""
        db_path = tmp_path / "v1det.db"
        make_v1_db(db_path)

        conn = sqlite3.connect(str(db_path))
        assert _get_schema_version(conn) == 1
        conn.close()

    def test_version_detection_empty(self, tmp_path):
        """Detect empty database (nothing exists)."""
        db_path = tmp_path / "empty.db"
        conn = sqlite3.connect(str(db_path))
        assert _get_schema_version(conn) == 0
        conn.close()


# --- SyncDatabase CRUD Tests ---


class TestSyncDatabase:
    @pytest.fixture
    def db(self, tmp_path):
        db = SyncDatabase(tmp_path / "test.db")
        db.connect()
        yield db
        db.close()

    def test_repo_crud(self, db):
        repo_id = db.add_repo("myrepo", "https://github.com/test/myrepo", "/tmp/myrepo")
        assert repo_id > 0

        repo = db.get_repo_by_name("myrepo")
        assert repo["name"] == "myrepo"
        assert repo["github_url"] == "https://github.com/test/myrepo"

        # Case-insensitive
        repo2 = db.get_repo_by_name("MyRepo")
        assert repo2["id"] == repo["id"]

        repos = db.list_repos()
        assert len(repos) == 1

    def test_task_crud(self, db):
        db.add_repo("r", "https://github.com/t/r", "/tmp/r")
        task_id = db.create_task(1, "do something", "sonnet")
        assert task_id > 0

        task = db.get_task(task_id)
        assert task["prompt"] == "do something"
        assert task["status"] == "queued"
        assert task["retry_count"] == 0
        assert task["github_issue_number"] is None

    def test_task_update_new_fields(self, db):
        db.add_repo("r", "https://github.com/t/r", "/tmp/r")
        tid = db.create_task(1, "test", "sonnet")

        db.update_task(tid, pr_number=42, github_issue_number=7, retry_count=1)
        task = db.get_task(tid)
        assert task["pr_number"] == 42
        assert task["github_issue_number"] == 7
        assert task["retry_count"] == 1

    def test_task_update_rejects_unknown_fields(self, db):
        db.add_repo("r", "https://github.com/t/r", "/tmp/r")
        tid = db.create_task(1, "test", "sonnet")

        # Should silently ignore unknown fields
        db.update_task(tid, unknown_field="bad", status="working")
        task = db.get_task(tid)
        assert task["status"] == "working"

    def test_new_statuses(self, db):
        db.add_repo("r", "https://github.com/t/r", "/tmp/r")
        tid = db.create_task(1, "test", "sonnet")

        for status in ("working", "pr_created", "ci_passed", "retrying", "completed", "failed", "cancelled"):
            db.update_task(tid, status=status)
            task = db.get_task(tid)
            assert task["status"] == status

    def test_list_tasks_by_status(self, db):
        db.add_repo("r", "https://github.com/t/r", "/tmp/r")
        db.create_task(1, "a", "sonnet")
        db.create_task(1, "b", "sonnet")
        tid3 = db.create_task(1, "c", "sonnet")
        db.update_task(tid3, status="working")

        queued = db.list_tasks(status="queued")
        assert len(queued) == 2

        working = db.list_tasks(status="working")
        assert len(working) == 1

    def test_count_methods(self, db):
        db.add_repo("r", "https://github.com/t/r", "/tmp/r")
        db.create_task(1, "a", "sonnet")
        db.create_task(1, "b", "sonnet")
        tid = db.create_task(1, "c", "sonnet")
        db.update_task(tid, status="working")

        assert db.count_queued() == 2
        assert db.count_active() == 1

    def test_logs(self, db):
        db.add_repo("r", "https://github.com/t/r", "/tmp/r")
        tid = db.create_task(1, "test", "sonnet")

        db.add_log(tid, "first")
        db.add_log(tid, "second", level="error")

        logs = db.get_logs(tid)
        assert len(logs) == 2
        messages = {log["message"] for log in logs}
        assert "first" in messages
        assert "second" in messages
        # Verify error level is present
        levels = {log["level"] for log in logs}
        assert "error" in levels


# --- Async Database Tests ---


class TestAsyncDatabase:
    @pytest_asyncio.fixture
    async def db(self, tmp_path):
        db = Database(tmp_path / "async_test.db")
        await db.connect()
        yield db
        await db.close()

    @pytest.mark.asyncio
    async def test_create_task_from_issue(self, db):
        await db.add_repo("r", "https://github.com/t/r", "/tmp/r")
        tid = await db.create_task_from_issue(
            1,
            "fix the bug",
            "sonnet",
            42,
            "https://github.com/t/r/issues/42",
        )
        task = await db.get_task(tid)
        assert task["github_issue_number"] == 42
        assert task["github_issue_url"] == "https://github.com/t/r/issues/42"
        assert task["status"] == "queued"

    @pytest.mark.asyncio
    async def test_get_task_by_issue_dedup(self, db):
        await db.add_repo("r", "https://github.com/t/r", "/tmp/r")

        # Create active task for issue #5
        tid = await db.create_task_from_issue(1, "fix", "sonnet", 5, "url")

        # Should find it
        found = await db.get_task_by_issue(1, 5)
        assert found is not None
        assert found["id"] == tid

        # Should not find non-existent issue
        not_found = await db.get_task_by_issue(1, 999)
        assert not_found is None

    @pytest.mark.asyncio
    async def test_get_task_by_issue_excludes_cancelled(self, db):
        await db.add_repo("r", "https://github.com/t/r", "/tmp/r")
        tid = await db.create_task_from_issue(1, "fix", "sonnet", 5, "url")
        await db.update_task(tid, status="cancelled")

        # Should NOT find cancelled task (allows re-pickup)
        found = await db.get_task_by_issue(1, 5)
        assert found is None

    @pytest.mark.asyncio
    async def test_get_task_by_issue_includes_failed(self, db):
        """Failed tasks are still found (prevents duplicate pickup of same issue)."""
        await db.add_repo("r", "https://github.com/t/r", "/tmp/r")
        tid = await db.create_task_from_issue(1, "fix", "sonnet", 5, "url")
        await db.update_task(tid, status="failed")

        found = await db.get_task_by_issue(1, 5)
        assert found is not None
        assert found["id"] == tid

    @pytest.mark.asyncio
    async def test_list_pr_tasks(self, db):
        """list_pr_tasks returns tasks with status=reviewed (awaiting CI)."""
        await db.add_repo("r", "https://github.com/t/r", "/tmp/r")
        tid1 = await db.create_task(1, "a", "sonnet")
        tid2 = await db.create_task(1, "b", "sonnet")
        await db.update_task(tid1, status="reviewed")
        await db.update_task(tid2, status="working")

        pr_tasks = await db.list_pr_tasks()
        assert len(pr_tasks) == 1
        assert pr_tasks[0]["id"] == tid1

    @pytest.mark.asyncio
    async def test_list_retrying_tasks(self, db):
        await db.add_repo("r", "https://github.com/t/r", "/tmp/r")
        tid = await db.create_task(1, "a", "sonnet")
        await db.update_task(tid, status="retrying", retry_count=1)

        retrying = await db.list_retrying_tasks()
        assert len(retrying) == 1
        assert retrying[0]["retry_count"] == 1

    @pytest.mark.asyncio
    async def test_claim_next_queued(self, db):
        await db.add_repo("r", "https://github.com/t/r", "/tmp/r")
        tid1 = await db.create_task(1, "first", "sonnet")
        tid2 = await db.create_task(1, "second", "sonnet")

        claimed = await db.claim_next_queued()
        assert claimed["id"] == tid1
        assert claimed["status"] == "working"

        # Second claim gets second task
        claimed2 = await db.claim_next_queued()
        assert claimed2["id"] == tid2

        # No more
        assert await db.claim_next_queued() is None

    @pytest.mark.asyncio
    async def test_async_migration_on_v1(self, tmp_path):
        """Async Database.connect() migrates v1 schema."""
        db_path = tmp_path / "asyncv1.db"
        make_v1_db(db_path)

        db = Database(db_path)
        await db.connect()

        task = await db.get_task(1)
        assert task is not None
        assert task["retry_count"] == 0
        assert task["github_issue_number"] is None

        await db.close()

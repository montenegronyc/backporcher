"""Tests for CLI — command registration, fleet output, cancel with labels."""

import subprocess
import sys
from pathlib import Path


def backporcher(*args):
    """Run backporcher CLI and return (returncode, stdout, stderr)."""
    result = subprocess.run(
        [sys.executable, "-m", "src.cli", *args],
        capture_output=True,
        text=True,
        cwd=str(Path(__file__).resolve().parent.parent),
    )
    return result.returncode, result.stdout, result.stderr


class TestCLICommands:
    def test_help_shows_fleet(self):
        rc, out, _ = backporcher("--help")
        assert rc == 0
        assert "fleet" in out

    def test_help_no_dispatch_command(self):
        rc, out, _ = backporcher("--help")
        # "dispatch" should not appear as a subcommand in the choices
        assert "{" in out  # The choices line like {repo,fleet,...}
        # Extract the choices portion
        for line in out.splitlines():
            if line.strip().startswith("{"):
                assert "dispatch" not in line
                break

    def test_help_no_retry(self):
        rc, out, _ = backporcher("--help")
        assert "retry" not in out

    def test_dispatch_removed(self):
        rc, out, err = backporcher("dispatch", "repo", "prompt")
        assert rc != 0
        assert "invalid choice" in err

    def test_fleet_runs(self):
        rc, out, _ = backporcher("fleet")
        assert rc == 0
        # Should show some output (header at minimum)
        assert "Fleet" in out or "No tasks" in out

    def test_status_runs(self):
        rc, out, _ = backporcher("status")
        assert rc == 0

    def test_status_nonexistent_task(self):
        rc, out, _ = backporcher("status", "99999")
        assert rc != 0
        assert "not found" in out

    def test_cancel_nonexistent_task(self):
        rc, out, _ = backporcher("cancel", "99999")
        assert rc != 0
        assert "not found" in out

    def test_repo_list(self):
        rc, out, _ = backporcher("repo", "list")
        assert rc == 0

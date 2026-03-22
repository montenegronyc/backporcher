"""Tests for PID lock acquisition in worker_startup."""

import os
import socket
from unittest.mock import patch

import pytest

from src.worker_startup import _get_container_id, _get_proc_starttime, acquire_pid_lock


@pytest.fixture
def config(tmp_path):
    """Minimal config with a temp base_dir."""

    class _Config:
        base_dir = tmp_path

    return _Config()


def pid_file(config):
    return config.base_dir / "data" / "backporcher.pid"


# --- _get_container_id ---


def test_get_container_id_docker_hostname():
    with patch.object(socket, "gethostname", return_value="abc123def456"):
        assert _get_container_id() == "abc123def456"


def test_get_container_id_non_docker_hostname():
    with patch.object(socket, "gethostname", return_value="my-server"):
        # Falls through to cgroup parsing — may or may not find anything,
        # but should not raise
        result = _get_container_id()
        assert isinstance(result, str)


# --- _get_proc_starttime ---


def test_get_proc_starttime_self():
    """Current process should have a parseable start time."""
    st = _get_proc_starttime(os.getpid())
    assert st != ""
    assert st.isdigit()


def test_get_proc_starttime_nonexistent():
    assert _get_proc_starttime(999999999) == ""


# --- acquire_pid_lock: fresh start ---


def test_acquire_fresh(config):
    result = acquire_pid_lock(config)
    assert result is not None
    assert pid_file(config).exists()
    content = pid_file(config).read_text()
    parts = content.split(":")
    assert parts[0] == str(os.getpid())
    assert len(parts) == 3  # pid:container_id:starttime


# --- acquire_pid_lock: stale PID (process dead) ---


def test_acquire_stale_dead_pid(config):
    pf = pid_file(config)
    pf.parent.mkdir(parents=True, exist_ok=True)
    # PID 999999999 should not exist
    pf.write_text("999999999:abc123def456:12345")

    result = acquire_pid_lock(config)
    assert result is not None
    # Lock file should now contain our PID
    assert pid_file(config).read_text().startswith(str(os.getpid()))


# --- acquire_pid_lock: different container ID ---


def test_acquire_different_container(config):
    """Lock from a different container should be reclaimed even if PID 1 is alive."""
    pf = pid_file(config)
    pf.parent.mkdir(parents=True, exist_ok=True)
    # PID 1 is always alive, but container ID differs
    pf.write_text("1:oldcontainer1:99999")

    with patch("src.worker_startup._get_container_id", return_value="newcontainer2"):
        result = acquire_pid_lock(config)

    assert result is not None


# --- acquire_pid_lock: same container, PID alive but starttime changed ---


def test_acquire_pid_recycled(config):
    """If PID is alive but starttime differs, lock is stale (PID was recycled)."""
    pf = pid_file(config)
    pf.parent.mkdir(parents=True, exist_ok=True)
    # Use PID 1 (always alive) with a fake old starttime
    real_starttime = _get_proc_starttime(1)
    fake_old_starttime = "1"  # Will differ from real starttime
    if real_starttime == fake_old_starttime:
        fake_old_starttime = "2"

    container_id = "same_container"
    pf.write_text(f"1:{container_id}:{fake_old_starttime}")

    with patch("src.worker_startup._get_container_id", return_value=container_id):
        result = acquire_pid_lock(config)

    assert result is not None


# --- acquire_pid_lock: same container, same PID, same starttime ---


def test_acquire_same_process_blocks(config):
    """If PID is alive with matching starttime, lock is valid — should block."""
    pf = pid_file(config)
    pf.parent.mkdir(parents=True, exist_ok=True)
    my_pid = os.getpid()
    my_starttime = _get_proc_starttime(my_pid)
    container_id = "same_container"
    pf.write_text(f"{my_pid}:{container_id}:{my_starttime}")

    with patch("src.worker_startup._get_container_id", return_value=container_id):
        result = acquire_pid_lock(config)

    assert result is None  # Should block — same process still holds lock


# --- acquire_pid_lock: backward compat with old formats ---


def test_acquire_old_boot_id_format(config):
    """Old pid:boot_id format should parse without crashing."""
    pf = pid_file(config)
    pf.parent.mkdir(parents=True, exist_ok=True)
    # Dead PID with old boot_id format (no starttime field)
    pf.write_text("999999999:6d5b854a-c5cf-4d46-b2c8-520316a2a16d")

    result = acquire_pid_lock(config)
    assert result is not None


def test_acquire_bare_pid_format(config):
    """Oldest bare-pid format should parse without crashing."""
    pf = pid_file(config)
    pf.parent.mkdir(parents=True, exist_ok=True)
    pf.write_text("999999999")

    result = acquire_pid_lock(config)
    assert result is not None


# --- acquire_pid_lock: no container ID available ---


def test_acquire_no_container_id_falls_through_to_pid_check(config):
    """When container ID is empty, should fall through to PID liveness check."""
    pf = pid_file(config)
    pf.parent.mkdir(parents=True, exist_ok=True)
    # Dead PID, no container ID
    pf.write_text("999999999::")

    result = acquire_pid_lock(config)
    assert result is not None

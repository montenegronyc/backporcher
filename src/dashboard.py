"""Real-time web dashboard for Backporcher — aiohttp + SSE, no frontend build step."""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import secrets
import sys
from pathlib import Path

from aiohttp import web

from .config import Config
from .db import Database

log = logging.getLogger("backporcher.dashboard")


# --- Worker process management ---

_worker_proc: asyncio.subprocess.Process | None = None
_worker_log_lines: list[str] = []  # last N lines of worker output
_WORKER_LOG_MAX = 200

# When True the dashboard is running inside the worker process (container mode).
# The worker is always alive — start/stop controls are disabled.
_embedded_mode: bool = False


def set_embedded_mode():
    """Mark that the dashboard is running inside the worker process."""
    global _embedded_mode
    _embedded_mode = True


def _is_worker_alive() -> bool:
    if _embedded_mode:
        return True
    return _worker_proc is not None and _worker_proc.returncode is None


async def _start_worker(config: Config):
    """Launch the backporcher worker as a subprocess."""
    global _worker_proc, _worker_log_lines
    if _is_worker_alive():
        return False, "Worker already running"

    _worker_log_lines = []

    # Build env with all BACKPORCHER_ vars from current env + config overrides
    env = dict(os.environ)
    env["BACKPORCHER_BASE_DIR"] = str(config.base_dir)
    if config.github_owner:
        env["BACKPORCHER_GITHUB_OWNER"] = config.github_owner
    if config.agent_user:
        env["BACKPORCHER_AGENT_USER"] = config.agent_user
    if config.dashboard_password:
        # Don't let the child start its own dashboard
        env.pop("BACKPORCHER_DASHBOARD_PASSWORD", None)

    # Use the installed CLI entry point, or fall back to python -m
    import shutil

    worker_cmd = shutil.which("backporcher")
    if worker_cmd:
        cmd = [worker_cmd, "worker"]
    else:
        cmd = [sys.executable, "-c", "from src.worker import run_worker; run_worker()"]

    _worker_proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=str(config.base_dir),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env=env,
    )
    log.info("Worker started (pid=%d)", _worker_proc.pid)

    # Stream output in background
    asyncio.create_task(_read_worker_output())
    return True, f"Worker started (pid={_worker_proc.pid})"


async def _read_worker_output():
    """Read worker stdout/stderr and buffer last N lines."""
    global _worker_proc
    if not _worker_proc or not _worker_proc.stdout:
        return
    try:
        async for raw in _worker_proc.stdout:
            line = raw.decode(errors="replace").rstrip()
            if line:
                _worker_log_lines.append(line)
                if len(_worker_log_lines) > _WORKER_LOG_MAX:
                    _worker_log_lines.pop(0)
    except (OSError, UnicodeDecodeError) as exc:
        log.debug("Worker output stream ended: %s", exc)
    # Process exited
    if _worker_proc:
        await _worker_proc.wait()
        code = _worker_proc.returncode
        if code and code < 0:
            _worker_log_lines.append(f"[Worker stopped (signal {-code})]")
        elif code:
            _worker_log_lines.append(f"[Worker exited with code {code}]")
        else:
            _worker_log_lines.append("[Worker stopped cleanly]")
        log.info("Worker exited (code=%s)", code)


async def _stop_worker():
    """Stop the worker subprocess gracefully."""
    global _worker_proc
    if not _is_worker_alive():
        return False, "Worker not running"

    pid = _worker_proc.pid
    try:
        _worker_proc.terminate()
        try:
            await asyncio.wait_for(_worker_proc.wait(), timeout=15)
        except asyncio.TimeoutError:
            log.warning("Worker didn't stop after SIGTERM, sending SIGKILL")
            _worker_proc.kill()
            await _worker_proc.wait()
    except ProcessLookupError:
        pass

    log.info("Worker stopped (pid=%d)", pid)
    _worker_proc = None
    return True, f"Worker stopped (pid={pid})"


# Status badge mapping (matches CLI fleet command)
BADGE = {
    "queued": "WAIT",
    "working": " RUN",
    "pr_created": "  PR",
    "reviewing": " REV",
    "reviewed": "RVWD",
    "ci_passed": "  OK",
    "retrying": " RTY",
    "completed": "DONE",
    "failed": "FAIL",
    "cancelled": " CXL",
}

BADGE_CLASS = {
    "queued": "wait",
    "working": "run",
    "pr_created": "pr",
    "reviewing": "rev",
    "reviewed": "rvwd",
    "ci_passed": "ok",
    "retrying": "rty",
    "completed": "done",
    "failed": "fail",
    "cancelled": "cxl",
}


def _check_auth(request: web.Request, password: str) -> bool:
    """Validate HTTP Basic Auth. Any username accepted, only password matters."""
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(auth_header[6:]).decode("utf-8")
        _, _, pwd = decoded.partition(":")
        return secrets.compare_digest(pwd, password)
    except (ValueError, UnicodeDecodeError):
        return False


def auth_middleware(password: str | None):
    """Create auth middleware. If no password, dashboard is disabled (caller checks)."""

    @web.middleware
    async def middleware(request: web.Request, handler):
        if password and not _check_auth(request, password):
            return web.Response(
                status=401,
                headers={"WWW-Authenticate": 'Basic realm="Backporcher Dashboard"'},
                text="Unauthorized",
            )
        return await handler(request)

    return middleware


_THEME_CSS_PATH = Path(__file__).resolve().parent.parent / "backporcher-theme.css"
_TEMPLATE_PATH = Path(__file__).resolve().parent.parent / "static" / "index.html"
DASHBOARD_HTML = _TEMPLATE_PATH.read_text()

# Track in-flight single-dispatch tasks so we don't double-dispatch
_dispatching: set[int] = set()


async def theme_css_handler(request: web.Request) -> web.Response:
    """Serve the theme CSS file from disk (editable without restart)."""
    try:
        css = _THEME_CSS_PATH.read_text()
    except FileNotFoundError:
        return web.Response(status=404, text="theme.css not found")
    return web.Response(
        text=css,
        content_type="text/css",
        headers={"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache"},
    )


async def index_handler(request: web.Request) -> web.Response:
    """Serve the main dashboard HTML page."""
    return web.Response(
        text=DASHBOARD_HTML,
        content_type="text/html",
        headers={"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache"},
    )


async def start_dashboard(db: Database, config: Config):
    """Start the dashboard web server. Runs until cancelled."""
    from .dashboard_actions import (
        approve_handler,
        edit_task_handler,
        escalate_task_handler,
        hold_handler,
        reject_handler,
        requeue_task_handler,
    )
    from .dashboard_api import (
        create_task_handler,
        dispatch_single_handler,
        pause_handler,
        resume_handler,
        worker_start_handler,
        worker_status_handler,
        worker_stop_handler,
    )
    from .dashboard_sse import (
        sse_handler,
        stats_handler,
        status_handler,
        task_detail_handler,
        tasks_handler,
    )

    password = None if config.dashboard_skip_auth else config.dashboard_password
    app = web.Application(middlewares=[auth_middleware(password)])
    app["db"] = db
    app["config"] = config
    app["github_owner"] = config.github_owner

    app.router.add_get("/", index_handler)
    app.router.add_get("/theme.css", theme_css_handler)
    app.router.add_get("/api/status", status_handler)
    app.router.add_get("/api/stats", stats_handler)
    app.router.add_get("/api/tasks", tasks_handler)
    app.router.add_get("/api/tasks/{id}", task_detail_handler)
    app.router.add_get("/events", sse_handler)
    app.router.add_post("/api/tasks/{id}/approve", approve_handler)
    app.router.add_post("/api/tasks/{id}/hold", hold_handler)
    app.router.add_post("/api/tasks/{id}/reject", reject_handler)
    app.router.add_post("/api/tasks/{id}/edit", edit_task_handler)
    app.router.add_post("/api/tasks/{id}/requeue", requeue_task_handler)
    app.router.add_post("/api/tasks/{id}/escalate", escalate_task_handler)
    app.router.add_post("/api/tasks/{id}/dispatch", dispatch_single_handler)
    app.router.add_post("/api/pause", pause_handler)
    app.router.add_post("/api/resume", resume_handler)
    app.router.add_post("/api/tasks/create", create_task_handler)
    app.router.add_post("/api/worker/start", worker_start_handler)
    app.router.add_post("/api/worker/stop", worker_stop_handler)
    app.router.add_get("/api/worker/status", worker_status_handler)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, config.dashboard_host, config.dashboard_port)
    await site.start()
    log.info("Dashboard running on port %d", config.dashboard_port)

    try:
        while True:
            await asyncio.sleep(3600)
    except asyncio.CancelledError:
        pass
    finally:
        await runner.cleanup()

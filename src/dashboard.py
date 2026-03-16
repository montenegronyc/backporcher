"""Real-time web dashboard for Backporcher — aiohttp + SSE, no frontend build step."""

import asyncio
import base64
import json
import logging
import os
import secrets
import signal
import sys
from datetime import datetime, timezone

from aiohttp import web

from .config import Config
from .db import Database

log = logging.getLogger("backporcher.dashboard")


# --- Worker process management ---

_worker_proc: asyncio.subprocess.Process | None = None
_worker_log_lines: list[str] = []  # last N lines of worker output
_WORKER_LOG_MAX = 200


def _is_worker_alive() -> bool:
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
    except Exception:
        pass
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
    except Exception:
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


async def _build_status(db: Database) -> dict:
    """Build full status payload from the database."""
    async with db.db.execute(
        "SELECT t.id, t.status, t.model, t.github_issue_number, t.started_at, "
        "t.completed_at, t.pr_url, t.pr_number, t.priority, t.depends_on_task_id, "
        "t.retry_count, t.error_message, t.branch_name, t.hold, "
        "t.agent_started_at, t.agent_finished_at, t.model_used, t.initial_model, "
        "t.repo_id, "
        "r.name as repo_name, "
        "substr(t.prompt, 1, 120) as title "
        "FROM tasks t JOIN repos r ON t.repo_id = r.id "
        "ORDER BY t.created_at DESC LIMIT 100"
    ) as cur:
        rows = [dict(r) for r in await cur.fetchall()]

    # Summary counts
    counts = {}
    repo_counts = {}
    for row in rows:
        s = row["status"]
        counts[s] = counts.get(s, 0) + 1
        rn = row["repo_name"]
        if rn not in repo_counts:
            repo_counts[rn] = {}
        repo_counts[rn][s] = repo_counts[rn].get(s, 0) + 1

    # Elapsed time for working tasks
    now = datetime.now(timezone.utc)
    for row in rows:
        if row["status"] == "working" and row["started_at"]:
            try:
                started = datetime.fromisoformat(row["started_at"].replace("Z", "+00:00"))
                if started.tzinfo is None:
                    started = started.replace(tzinfo=timezone.utc)
                elapsed = (now - started).total_seconds()
                row["elapsed_seconds"] = int(elapsed)
                mins, secs = divmod(int(elapsed), 60)
                row["elapsed"] = f"{mins}m{secs:02d}s"
            except Exception:
                row["elapsed"] = "?"
                row["elapsed_seconds"] = 0
        else:
            row["elapsed"] = None
            row["elapsed_seconds"] = 0

    # Check global pause
    queue_paused = await db.is_queue_paused()

    # Count held tasks
    held_count = sum(1 for row in rows if row.get("hold"))

    return {
        "counts": counts,
        "repo_counts": repo_counts,
        "tasks": rows,
        "timestamp": now.isoformat(),
        "queue_paused": queue_paused,
        "held_count": held_count,
    }


async def index_handler(request: web.Request) -> web.Response:
    """Serve the main dashboard HTML page."""
    return web.Response(text=DASHBOARD_HTML, content_type="text/html")


async def status_handler(request: web.Request) -> web.Response:
    """JSON summary counts."""
    db = request.app["db"]
    data = await _build_status(db)
    return web.json_response({"counts": data["counts"], "repo_counts": data["repo_counts"]})


async def tasks_handler(request: web.Request) -> web.Response:
    """JSON task list, filterable by ?status= and ?repo=."""
    db = request.app["db"]
    data = await _build_status(db)
    tasks = data["tasks"]

    status_filter = request.query.get("status")
    repo_filter = request.query.get("repo")
    if status_filter:
        tasks = [t for t in tasks if t["status"] == status_filter]
    if repo_filter:
        tasks = [t for t in tasks if t["repo_name"] == repo_filter]

    return web.json_response({"tasks": tasks})


async def task_detail_handler(request: web.Request) -> web.Response:
    """Single task detail with recent logs."""
    db = request.app["db"]
    task_id = int(request.match_info["id"])
    task = await db.get_task(task_id)
    if not task:
        return web.json_response({"error": "not found"}, status=404)

    logs = await db.get_logs(task_id, limit=30)
    return web.json_response({"task": task, "logs": logs})


async def sse_handler(request: web.Request) -> web.StreamResponse:
    """Server-Sent Events — push status every 5 seconds."""
    response = web.StreamResponse(
        headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        }
    )
    await response.prepare(request)
    db = request.app["db"]

    github_owner = request.app.get("github_owner", "")

    try:
        while True:
            data = await _build_status(db)
            data["github_owner"] = github_owner
            data["worker_running"] = _is_worker_alive()
            data["worker_pid"] = _worker_proc.pid if _worker_proc and _is_worker_alive() else None
            payload = f"data: {json.dumps(data)}\n\n"
            await response.write(payload.encode())
            await asyncio.sleep(5)
    except (asyncio.CancelledError, ConnectionResetError):
        pass
    return response


DASHBOARD_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Backporcher Dashboard</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Share+Tech+Mono&family=Rajdhani:wght@400;600;700&display=swap" rel="stylesheet">
<style>
:root {
  --bg-base: #060e0e;
  --bg-surface: #0a1614;
  --bg-elevated: #0d1e1a;
  --bg-overlay: rgba(6, 14, 14, 0.92);

  --c-primary: #20dfa0;
  --c-primary-bright: #50f0b8;
  --c-primary-dim: #148060;
  --c-primary-muted: rgba(32, 223, 160, 0.08);
  --c-primary-glow: rgba(32, 223, 160, 0.30);

  --c-accent: #20dfa0;
  --c-accent-bright: #50f0b8;
  --c-accent-muted: rgba(32, 223, 160, 0.10);

  --c-danger: #ff1744;
  --c-danger-bright: #ff5252;
  --c-danger-muted: rgba(255, 23, 68, 0.08);

  --c-success: #20dfa0;
  --c-success-bright: #50f0b8;
  --c-success-dim: #148060;
  --c-success-muted: rgba(32, 223, 160, 0.08);

  --text-1: #c8e0d8;
  --text-2: #6a9080;
  --text-3: #3a5a4e;

  --border: rgba(32, 223, 160, 0.12);
  --border-active: rgba(32, 223, 160, 0.30);
  --border-subtle: rgba(32, 223, 160, 0.05);

  --font-mono: 'Share Tech Mono', 'JetBrains Mono', 'SF Mono', monospace;
  --font-display: 'Rajdhani', 'Share Tech Mono', monospace;

  --s-xs: 4px;
  --s-sm: 8px;
  --s-md: 12px;
  --s-lg: 16px;
  --s-xl: 24px;
  --s-xxl: 32px;

  --corner-size: 12px;
  --corner-weight: 1px;
}

*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

html, body {
  height: 100%;
  overflow: hidden;
}
body {
  background: var(--bg-base);
  color: var(--text-1);
  font-family: var(--font-mono);
  font-size: 13px;
  line-height: 1.5;
  -webkit-font-smoothing: antialiased;
  display: flex;
  flex-direction: column;
}

/* Scan-line overlay */
body::after {
  content: '';
  position: fixed;
  inset: 0;
  background: repeating-linear-gradient(
    0deg,
    transparent,
    transparent 2px,
    rgba(0, 0, 0, 0.03) 2px,
    rgba(0, 0, 0, 0.03) 4px
  );
  pointer-events: none;
  z-index: 9999;
}

/* --- HEADER --- */

.header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  height: 48px;
  padding: 0 var(--s-xl);
  border-bottom: 1px solid var(--border);
  background: var(--bg-surface);
}

.header-title {
  font-family: var(--font-display);
  font-size: 18px;
  font-weight: 700;
  letter-spacing: 0.12em;
  text-transform: uppercase;
  color: var(--c-primary);
}

.header-status {
  display: flex;
  align-items: center;
  gap: var(--s-lg);
  font-size: 11px;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: var(--text-2);
}

.status-live {
  display: flex;
  align-items: center;
  gap: var(--s-xs);
  color: var(--c-success);
}

.status-live::before {
  content: '';
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--c-success);
  animation: pulse-dot 2s ease-in-out infinite;
}

.status-paused { color: var(--c-danger); font-weight: 600; }

@keyframes pulse-dot {
  0%, 100% { opacity: 1; box-shadow: 0 0 4px var(--c-success); }
  50% { opacity: 0.4; box-shadow: none; }
}

/* --- PANELS (corner bracket style) --- */

.panel {
  position: relative;
  background: var(--bg-surface);
  padding: var(--s-lg);
  display: flex;
  flex-direction: column;
  min-height: 0;
}
.row-fleet .panel { overflow: hidden; }
.panel-scroll { flex: 1; min-height: 0; overflow-y: auto; }

.panel::before,
.panel::after {
  content: '';
  position: absolute;
  width: var(--corner-size);
  height: var(--corner-size);
  border-color: var(--c-primary);
  border-style: solid;
  border-width: 0;
  opacity: 0.5;
}

.panel::before {
  top: 0; left: 0;
  border-top-width: var(--corner-weight);
  border-left-width: var(--corner-weight);
}

.panel::after {
  top: 0; right: 0;
  border-top-width: var(--corner-weight);
  border-right-width: var(--corner-weight);
}

.panel-corners::before,
.panel-corners::after {
  content: '';
  position: absolute;
  width: var(--corner-size);
  height: var(--corner-size);
  border-color: var(--c-primary);
  border-style: solid;
  border-width: 0;
  opacity: 0.5;
}

.panel-corners::before {
  bottom: 0; left: 0;
  border-bottom-width: var(--corner-weight);
  border-left-width: var(--corner-weight);
}

.panel-corners::after {
  bottom: 0; right: 0;
  border-bottom-width: var(--corner-weight);
  border-right-width: var(--corner-weight);
}

.panel-header {
  font-family: var(--font-display);
  font-size: 11px;
  font-weight: 600;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: var(--c-primary-dim);
  padding-bottom: var(--s-sm);
  border-bottom: 1px solid var(--border);
  margin-bottom: var(--s-md);
}

/* --- METRICS --- */

.metrics-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: var(--s-sm);
}

.metric { text-align: center; padding: var(--s-sm); }

.metric-value {
  font-family: var(--font-display);
  font-size: 22px;
  font-weight: 700;
  color: var(--c-primary);
  line-height: 1.1;
}

.metric-value.accent { color: var(--c-accent); }
.metric-value.success { color: var(--c-success); }
.metric-value.danger { color: var(--c-danger); }

.metric-label {
  font-size: 10px;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: var(--text-3);
  margin-top: var(--s-xs);
}

/* --- FLEET TABLE --- */

.fleet-table { width: 100%; border-collapse: collapse; }

.fleet-table thead th {
  font-family: var(--font-display);
  font-size: 10px;
  font-weight: 600;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: var(--text-3);
  text-align: left;
  padding: var(--s-sm) var(--s-md);
  border-bottom: 1px solid var(--border);
}

.fleet-table tbody td {
  font-size: 12px;
  padding: var(--s-sm) var(--s-md);
  border-bottom: 1px solid var(--border-subtle);
  vertical-align: middle;
}

.fleet-table tbody tr:hover { background: var(--c-primary-muted); }

.fleet-table tbody tr.selected {
  background: var(--c-primary-muted);
  box-shadow: inset 2px 0 0 var(--c-primary);
}

.col-id, .col-time { font-variant-numeric: tabular-nums; color: var(--text-2); }
.col-issue { color: var(--text-1); max-width: 300px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.col-repo { color: var(--text-2); }
.col-model { color: var(--text-3); font-size: 10px; text-transform: uppercase; letter-spacing: 0.06em; }

/* --- BADGES --- */

.badge {
  display: inline-block;
  font-family: var(--font-mono);
  font-size: 10px;
  font-weight: 600;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  padding: 2px 8px;
  border: 1px solid transparent;
  line-height: 1.4;
}

.badge-wait { color: var(--text-3); border-color: var(--text-3); }
.badge-run  { color: var(--c-primary); border-color: var(--c-primary); background: var(--c-primary-muted); animation: badge-pulse 2s ease-in-out infinite; box-shadow: 0 0 6px var(--c-primary-glow); }
.badge-pr   { color: var(--c-primary-dim); border-color: var(--c-primary-dim); }
.badge-rev  { color: var(--c-primary-dim); border-color: var(--c-primary-dim); animation: badge-pulse 2.5s ease-in-out infinite; }
.badge-rvwd { color: var(--c-primary); border-color: var(--c-primary); background: var(--c-primary-muted); }
.badge-ok   { color: var(--c-primary); border-color: var(--c-primary); background: var(--c-primary-muted); }
.badge-aprv { color: var(--c-primary-bright); border-color: var(--c-primary-bright); background: var(--c-primary-muted); animation: badge-pulse 1.5s ease-in-out infinite; box-shadow: 0 0 8px var(--c-primary-glow); }
.badge-gate { color: var(--c-primary-dim); border-color: var(--c-primary-dim); background: var(--c-primary-muted); }
.badge-hold { color: var(--text-2); border-color: var(--text-2); }
.badge-rty  { color: var(--c-primary); border-color: var(--c-primary); background: var(--c-primary-muted); animation: badge-pulse 1s ease-in-out infinite; }
.badge-done { color: var(--c-primary-dim); border-color: rgba(32, 223, 160, 0.15); }
.badge-fail { color: var(--c-danger); border-color: var(--c-danger); background: var(--c-danger-muted); }
.badge-cxl  { color: var(--text-3); text-decoration: line-through; }

@keyframes badge-pulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.55; }
}

/* --- BUTTONS --- */

.btn {
  font-family: var(--font-display);
  font-size: 11px;
  font-weight: 600;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  padding: 5px 14px;
  border: 1px solid;
  background: transparent;
  cursor: pointer;
  transition: all 0.15s ease;
  outline: none;
}

.btn:active { transform: scale(0.97); }

.btn-approve { border-color: var(--c-primary); color: var(--c-primary); }
.btn-approve:hover { background: var(--c-primary); color: var(--bg-base); box-shadow: 0 0 12px var(--c-primary-glow); }

.btn-hold { border-color: var(--text-2); color: var(--text-2); }
.btn-hold:hover { background: var(--text-2); color: var(--bg-base); }

.btn-reject { border-color: var(--c-danger); color: var(--c-danger); }
.btn-reject:hover { background: var(--c-danger); color: var(--bg-base); box-shadow: 0 0 12px rgba(255, 23, 68, 0.35); }

.btn-ghost { border-color: transparent; color: var(--text-2); }
.btn-ghost:hover { color: var(--text-1); }

.btn-pause { border-color: var(--c-danger); color: var(--c-danger); }
.btn-pause:hover { background: var(--c-danger); color: var(--bg-base); }

.btn-resume { border-color: var(--c-success); color: var(--c-success); }
.btn-resume:hover { background: var(--c-success); color: var(--bg-base); }

.btn-group { display: flex; gap: var(--s-sm); }

/* --- TASK DETAIL --- */

.task-detail-title {
  font-family: var(--font-display);
  font-size: 15px;
  font-weight: 600;
  color: var(--text-1);
}

.task-detail-meta {
  font-size: 11px;
  color: var(--text-2);
  margin-top: var(--s-xs);
}

.task-detail-status {
  font-size: 12px;
  margin-top: var(--s-md);
  padding: var(--s-sm) var(--s-md);
  border-left: 2px solid var(--c-primary);
  background: var(--c-primary-muted);
  color: var(--text-1);
}
.task-detail-status.error {
  border-left-color: var(--c-danger);
  background: var(--c-danger-muted);
  color: var(--c-danger);
}

/* --- TIMELINE --- */

.timeline { margin-top: var(--s-lg); }

.timeline-header {
  font-size: 10px;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: var(--text-3);
  margin-bottom: var(--s-sm);
}

.timeline-entry {
  display: flex;
  gap: var(--s-md);
  padding: 3px 0;
  font-size: 12px;
}

.timeline-time {
  color: var(--text-3);
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
  min-width: 70px;
}

.timeline-event { color: var(--text-2); }
.timeline-entry:last-child .timeline-event { color: var(--text-1); }

/* --- LAYOUT --- */

.dashboard {
  padding: var(--s-md) var(--s-xl);
  display: flex;
  flex-direction: column;
  gap: var(--s-md);
  flex: 1;
  min-height: 0;
  overflow: hidden;
}

.row { display: grid; gap: var(--s-md); flex-shrink: 0; }
.row-2 { grid-template-columns: 240px 1fr; }
.row-equal { grid-template-columns: 1fr 1fr; }
.row-top { grid-template-columns: 1fr 1fr 1fr; }
.row-fleet { grid-template-columns: 1fr 1fr; flex: 1; min-height: 0; flex-shrink: 1; }

@media (max-width: 900px) {
  .row-2, .row-equal, .row-fleet, .row-top { grid-template-columns: 1fr; }
  .row-fleet { flex: none; }
}

::-webkit-scrollbar { width: 6px; height: 6px; }
::-webkit-scrollbar-track { background: var(--bg-base); }
::-webkit-scrollbar-thumb { background: var(--text-3); }
::-webkit-scrollbar-thumb:hover { background: var(--text-2); }

.text-primary { color: var(--text-1); }
.text-secondary { color: var(--text-2); }
.text-muted { color: var(--text-3); }
.text-cyan { color: var(--c-primary); }
.text-amber { color: var(--c-primary); }
.text-red { color: var(--c-danger); }
.text-green { color: var(--c-success); }
.uppercase { text-transform: uppercase; letter-spacing: 0.06em; }
.mono { font-family: var(--font-mono); }
.display { font-family: var(--font-display); }
.glow { text-shadow: 0 0 8px var(--c-primary-glow); }

/* Additional dashboard-specific styles */
a { color: var(--c-primary); text-decoration: none; }
a:hover { text-decoration: underline; }
.empty { color: var(--text-3); font-style: italic; padding: var(--s-lg); text-align: center; }
.hidden { display: none; }
.modal-overlay {
  display: none; position: fixed; inset: 0;
  background: var(--bg-overlay); z-index: 100;
  justify-content: center; align-items: flex-start; padding-top: 60px;
}
.modal-overlay.open { display: flex; }
.modal {
  background: var(--bg-base); border: 1px solid var(--border);
  width: 700px; max-width: 95vw; max-height: 85vh; overflow-y: auto;
  padding: var(--s-xl); position: relative;
}
.modal .panel-corners::before { bottom: 0; left: 0; }
.modal .panel-corners::after { bottom: 0; right: 0; }
.modal-close {
  position: absolute; top: var(--s-md); right: var(--s-md);
  background: none; border: none; color: var(--text-3);
  font-size: 18px; cursor: pointer; font-family: var(--font-mono);
}
.modal-close:hover { color: var(--text-1); }
.edit-form { margin-top: var(--s-md); }
.edit-form label {
  display: block; font-family: var(--font-display); font-size: 10px;
  letter-spacing: 0.06em; text-transform: uppercase; color: var(--text-3);
  margin-bottom: var(--s-xs); margin-top: var(--s-md);
}
.edit-form textarea, .edit-form select, .edit-form input {
  width: 100%; background: var(--bg-elevated); border: 1px solid var(--border);
  color: var(--text-1); padding: var(--s-sm); font-family: var(--font-mono);
  font-size: 12px; outline: none;
}
.edit-form textarea:focus, .edit-form select:focus, .edit-form input:focus {
  border-color: var(--border-active);
}
.edit-form textarea { min-height: 80px; resize: vertical; }
.edit-form select { width: auto; min-width: 100px; }
.edit-form .form-row { display: flex; gap: var(--s-md); align-items: flex-end; }
.pipeline-count { display: flex; align-items: center; gap: var(--s-sm); padding: 4px 0; font-size: 12px; }
.pipeline-count .dot { width: 6px; height: 6px; border-radius: 50%; flex-shrink: 0; }
.pipeline-count .cnt { font-family: var(--font-display); font-weight: 700; min-width: 20px; }
.pipeline-count .lbl { color: var(--text-3); font-size: 11px; text-transform: uppercase; letter-spacing: 0.06em; }
.new-row { animation: row-flash 1s ease-out; }
@keyframes row-flash { from { box-shadow: inset 2px 0 0 var(--c-primary-bright); } to { box-shadow: none; } }
.filter-bar { display: flex; gap: var(--s-xs); align-items: center; }
.filter-btn {
  font-family: var(--font-display); font-size: 10px; font-weight: 600;
  letter-spacing: 0.06em; text-transform: uppercase;
  padding: 3px 10px; border: 1px solid var(--border); background: transparent;
  color: var(--text-3); cursor: pointer; transition: all 0.15s ease; outline: none;
}
.filter-btn:hover { color: var(--text-1); border-color: var(--border-active); }
.filter-btn.active { color: var(--c-primary); border-color: var(--c-primary); background: var(--c-primary-muted); }
.filter-btn.active-fail { color: var(--c-danger); border-color: var(--c-danger); background: var(--c-danger-muted); }
.filter-btn.active-run { color: var(--c-primary-bright); border-color: var(--c-primary-bright); background: var(--c-primary-muted); box-shadow: 0 0 6px var(--c-primary-glow); }
.filter-btn.active-wait { color: var(--c-primary-dim); border-color: var(--c-primary-dim); background: var(--c-primary-muted); }
.filter-sep { width: 1px; height: 16px; background: var(--border); margin: 0 var(--s-xs); }
.inline-edit {
  margin-top: var(--s-md); padding: var(--s-md);
  border: 1px solid var(--border); background: var(--bg-elevated);
}
.inline-edit label {
  display: block; font-family: var(--font-display); font-size: 10px;
  letter-spacing: 0.06em; text-transform: uppercase; color: var(--text-3);
  margin-bottom: var(--s-xs); margin-top: var(--s-md);
}
.inline-edit label:first-child { margin-top: 0; }
.inline-edit textarea, .inline-edit select, .inline-edit input[type=number] {
  width: 100%; background: var(--bg-base); border: 1px solid var(--border);
  color: var(--text-1); padding: var(--s-sm); font-family: var(--font-mono);
  font-size: 12px; outline: none;
}
.inline-edit textarea:focus, .inline-edit select:focus, .inline-edit input:focus {
  border-color: var(--border-active);
}
.inline-edit textarea { min-height: 100px; resize: vertical; }
.inline-edit select { width: auto; min-width: 100px; }
.inline-edit .form-row { display: flex; gap: var(--s-md); align-items: flex-end; margin-top: var(--s-md); }
.fleet-count { font-size: 10px; color: var(--text-3); margin-left: var(--s-sm); }

/* Progress bar */
.progress-bar { height: 4px; background: var(--bg-elevated); margin-top: var(--s-xs); overflow: hidden; }
.progress-fill { height: 100%; transition: width 0.6s ease; }
.progress-fill.green { background: linear-gradient(90deg, var(--c-primary-dim), var(--c-primary)); }
.progress-fill.red { background: linear-gradient(90deg, var(--c-danger), var(--c-danger-bright)); }

/* Mini bar chart */
.bar-chart { display: flex; align-items: flex-end; gap: 3px; height: 40px; padding-top: var(--s-sm); }
.bar-col { display: flex; flex-direction: column; align-items: center; flex: 1; gap: 2px; }
.bar-fill { width: 100%; min-width: 16px; max-width: 48px; background: var(--c-primary); transition: height 0.4s ease; opacity: 0.7; }
.bar-fill.fail-portion { background: var(--c-danger); opacity: 0.9; }
.bar-label { font-size: 8px; color: var(--text-3); text-transform: uppercase; letter-spacing: 0.04em; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 60px; }

/* Pipeline stage visualization */
.stage-flow { display: flex; align-items: center; gap: 2px; padding: var(--s-sm) 0; }
.stage-node {
  width: 28px; height: 28px; border: 1px solid var(--border);
  display: flex; align-items: center; justify-content: center;
  font-family: var(--font-display); font-size: 11px; font-weight: 700;
  color: var(--text-3); position: relative; transition: all 0.3s ease;
}
.stage-node.active { border-color: var(--c-primary); color: var(--c-primary); background: var(--c-primary-muted); box-shadow: 0 0 8px var(--c-primary-glow); }
.stage-node.has-tasks { border-color: var(--c-primary-dim); color: var(--c-primary-dim); }
.stage-arrow { color: var(--text-3); font-size: 10px; padding: 0 1px; }
.stage-label { font-size: 8px; color: var(--text-3); text-align: center; margin-top: 2px; letter-spacing: 0.04em; }
.stage-col { display: flex; flex-direction: column; align-items: center; }

/* 3D Agent visualizer */
.agent-viz {
  perspective: 400px;
  display: flex; gap: var(--s-sm); justify-content: center;
  padding: var(--s-sm) 0; min-height: 60px;
}
.agent-cube {
  width: 56px; height: 56px; position: relative;
  transform-style: preserve-3d;
  animation: cube-rotate 8s linear infinite;
}
.agent-cube.idle { animation: cube-idle 4s ease-in-out infinite; opacity: 0.3; }
.agent-cube .face {
  position: absolute; width: 56px; height: 56px;
  border: 1px solid var(--c-primary); background: var(--c-primary-muted);
  display: flex; flex-direction: column; align-items: center; justify-content: center;
  font-family: var(--font-display); font-size: 9px; color: var(--c-primary);
  text-transform: uppercase; letter-spacing: 0.04em; backface-visibility: hidden;
}
.agent-cube .face .agent-id { font-size: 13px; font-weight: 700; }
.agent-cube .face .agent-model { font-size: 8px; color: var(--c-primary-dim); }
.agent-cube .face .agent-time { font-size: 8px; color: var(--text-2); margin-top: 1px; }
.agent-cube .front  { transform: translateZ(28px); }
.agent-cube .back   { transform: rotateY(180deg) translateZ(28px); }
.agent-cube .right  { transform: rotateY(90deg) translateZ(28px); }
.agent-cube .left   { transform: rotateY(-90deg) translateZ(28px); }
.agent-cube .top    { transform: rotateX(90deg) translateZ(28px); }
.agent-cube .bottom { transform: rotateX(-90deg) translateZ(28px); }
.agent-cube.fail .face { border-color: var(--c-danger); background: var(--c-danger-muted); color: var(--c-danger); }
.agent-cube.fail .face .agent-model { color: var(--c-danger); }
@keyframes cube-rotate {
  0% { transform: rotateX(-15deg) rotateY(0deg); }
  100% { transform: rotateX(-15deg) rotateY(360deg); }
}
@keyframes cube-idle {
  0%, 100% { transform: rotateX(-10deg) rotateY(0deg); }
  50% { transform: rotateX(-10deg) rotateY(20deg); }
}
.agent-viz-label { text-align: center; font-size: 9px; color: var(--text-3); text-transform: uppercase; letter-spacing: 0.06em; margin-top: var(--s-xs); }
.agent-slot { display: flex; flex-direction: column; align-items: center; }
</style>
</head>
<body>

<div class="header">
  <span class="header-title">BACKPORCHER</span>
  <div class="header-status">
    <span class="status-live" id="sse-status">LIVE</span>
    <span id="worker-status" class="text-muted">WORKER: OFF</span>
    <button class="btn btn-approve" id="worker-btn" onclick="toggleWorker()">START FLEET</button>
    <span id="held-indicator" class="text-amber" style="display:none"></span>
    <span id="paused-indicator" class="status-paused" style="display:none">PAUSED</span>
    <button class="btn btn-pause" id="pause-btn">PAUSE</button>
    <button class="btn btn-ghost" id="new-task-btn" onclick="openNewTaskModal()">+ TASK</button>
  </div>
</div>

<div class="dashboard">
  <!-- Top row: Agents | Metrics | Repos -->
  <div class="row row-top">
    <div class="panel">
      <div class="panel-header">Agents</div>
      <div id="agent-viz" class="agent-viz"></div>
      <div id="stage-flow-container"></div>
      <div class="panel-corners"></div>
    </div>
    <div class="panel">
      <div class="panel-header">Metrics</div>
      <div class="metrics-grid" id="metrics-grid">
        <div class="metric">
          <div class="metric-value" id="m-merged">-</div>
          <div class="metric-label">Merged</div>
          <div class="progress-bar"><div class="progress-fill green" id="m-merged-bar" style="width:0%"></div></div>
        </div>
        <div class="metric">
          <div class="metric-value" id="m-rate">-</div>
          <div class="metric-label">Success Rate</div>
          <div class="progress-bar"><div class="progress-fill green" id="m-rate-bar" style="width:0%"></div></div>
        </div>
        <div class="metric">
          <div class="metric-value" id="m-time">-</div>
          <div class="metric-label">Avg Time</div>
        </div>
        <div class="metric">
          <div class="metric-value" id="m-retry">-</div>
          <div class="metric-label">Retry Rate</div>
          <div class="progress-bar"><div class="progress-fill red" id="m-retry-bar" style="width:0%"></div></div>
        </div>
      </div>
      <div class="panel-corners"></div>
    </div>
    <div class="panel">
      <div class="panel-header">Repos</div>
      <div id="repo-chart" class="bar-chart"></div>
      <div class="panel-corners"></div>
    </div>
  </div>

  <!-- Fleet + Detail side by side -->
  <div class="row row-fleet">
    <div class="panel">
      <div class="panel-header" style="display:flex;justify-content:space-between;align-items:center">
        <span>Fleet</span>
        <div class="filter-bar" id="filter-bar"></div>
      </div>
      <div id="fleet-content" class="panel-scroll"></div>
      <div class="panel-corners"></div>
    </div>
    <div class="panel" id="task-detail-panel">
      <div class="panel-header">Task Detail</div>
      <div id="task-detail-content" class="panel-scroll"><div class="empty">Select a task</div></div>
      <div class="panel-corners"></div>
    </div>
  </div>
</div>

<!-- Task Edit Modal -->
<div class="modal-overlay" id="task-modal" onclick="if(event.target===this)closeModal()">
  <div class="modal panel">
    <button class="modal-close" onclick="closeModal()">&times;</button>
    <div class="panel-header" id="modal-title">Task</div>
    <div id="modal-body"></div>
    <div class="panel-corners"></div>
  </div>
</div>

<script>
/* ═══ Constants ═══ */
const BADGE_MAP = {
  queued:'wait', working:'run', pr_created:'pr', reviewing:'rev',
  reviewed:'rvwd', ci_passed:'ok', retrying:'rty',
  completed:'done', failed:'fail', cancelled:'cxl'
};
const BADGE_LABEL = {
  queued:'WAIT', working:'RUN', pr_created:'PR', reviewing:'REV',
  reviewed:'RVWD', ci_passed:'OK', retrying:'RTY',
  completed:'DONE', failed:'FAIL', cancelled:'CXL'
};
const HOLD_MAP = {
  merge_approval: {cls:'aprv', label:'APRV'},
  dispatch_approval: {cls:'gate', label:'GATE'},
  user_hold: {cls:'hold', label:'HOLD'},
  conflict_hold: {cls:'hold', label:'CNFL'},
};

let _paused = false, _owner = '', _tasks = [], _selectedId = null, _prevIds = new Set();
let _filter = 'all'; // 'all', 'running', 'failed', 'waiting', or a repo name
let _repoFilter = null; // null or repo name string

/* ═══ Helpers ═══ */
function esc(s) { return (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }
function fmtDur(secs) {
  if (!secs && secs !== 0) return '\\u2014';
  const s = Math.round(secs), m = Math.floor(s/60), h = Math.floor(m/60);
  if (h > 0) return h+'h '+String(m%60).padStart(2,'0')+'m';
  return m+'m '+String(s%60).padStart(2,'0')+'s';
}

function badge(status, hold) {
  if (hold && HOLD_MAP[hold]) {
    const h = HOLD_MAP[hold];
    return `<span class="badge badge-${h.cls}">${h.label}</span>`;
  }
  const cls = BADGE_MAP[status] || 'wait';
  const label = BADGE_LABEL[status] || status.toUpperCase();
  return `<span class="badge badge-${cls}">${label}</span>`;
}

function issueLink(t) {
  if (!t.github_issue_number) return '\\u2014';
  return `<a href="https://github.com/${_owner}/${t.repo_name||''}/issues/${t.github_issue_number}" target="_blank" onclick="event.stopPropagation()">#${t.github_issue_number}</a>`;
}
function prLink(t) {
  if (!t.pr_url) return '\\u2014';
  return `<a href="${t.pr_url}" target="_blank" onclick="event.stopPropagation()">PR#${t.pr_number||'?'}</a>`;
}

function actionBtns(t) {
  let b = '';
  const hold = t.hold, st = t.status;
  const term = new Set(['completed','failed','cancelled']);
  // Run: dispatch a single queued or failed task
  if (st === 'queued' || st === 'failed')
    b += `<button class="btn btn-approve" onclick="event.stopPropagation();dispatchSingle(${t.id},this)">RUN</button> `;
  if (hold === 'merge_approval' || hold === 'dispatch_approval')
    b += `<button class="btn btn-approve" data-id="${t.id}" data-action="approve">APPROVE</button> `;
  if (!term.has(st) && !hold && st !== 'queued')
    b += `<button class="btn btn-hold" data-id="${t.id}" data-action="hold">HOLD</button> `;
  if (['working','pr_created','reviewing','reviewed','ci_passed'].includes(st))
    b += `<button class="btn btn-reject" data-id="${t.id}" data-action="reject">REJECT</button>`;
  return b;
}

/* ═══ Agent Visualizer ═══ */
function renderAgentViz(tasks) {
  const el = document.getElementById('agent-viz');
  const working = tasks.filter(t => t.status === 'working');
  // Show max 6 slots
  const maxSlots = Math.max(2, working.length);
  let html = '';
  for (let i = 0; i < maxSlots && i < 6; i++) {
    const t = working[i];
    if (t) {
      const fail = t.retry_count > 0 ? ' fail' : '';
      html += `<div class="agent-slot">
        <div class="agent-cube${fail}" style="animation-delay:${i*-2}s">
          <div class="face front"><span class="agent-id">#${t.id}</span><span class="agent-model">${esc(t.model||'')}</span><span class="agent-time">${t.elapsed||''}</span></div>
          <div class="face back"><span class="agent-id">#${t.id}</span><span class="agent-model">${esc(t.repo_name||'')}</span></div>
          <div class="face right"><span class="agent-id">#${t.id}</span></div>
          <div class="face left"><span class="agent-id">#${t.id}</span></div>
          <div class="face top"></div>
          <div class="face bottom"></div>
        </div>
        <div class="agent-viz-label">${esc(t.repo_name||'')} #${t.github_issue_number||'?'}</div>
      </div>`;
    } else {
      html += `<div class="agent-slot">
        <div class="agent-cube idle">
          <div class="face front"><span class="agent-id">--</span><span class="agent-model">IDLE</span></div>
          <div class="face back"></div><div class="face right"></div><div class="face left"></div><div class="face top"></div><div class="face bottom"></div>
        </div>
        <div class="agent-viz-label">Slot ${i+1}</div>
      </div>`;
    }
  }
  el.innerHTML = html;
}

function renderStageFlow(counts) {
  const el = document.getElementById('stage-flow-container');
  const stages = [
    {key:'queued', label:'Queue', cnt: counts.queued||0},
    {key:'working', label:'Agent', cnt: counts.working||0},
    {key:'pr_created', label:'PR', cnt: counts.pr_created||0},
    {key:'reviewing', label:'Review', cnt: (counts.reviewing||0)+(counts.reviewed||0)},
    {key:'ci_passed', label:'CI', cnt: (counts.ci_passed||0)+(counts.retrying||0)},
    {key:'completed', label:'Done', cnt: counts.completed||0},
  ];
  let html = '<div class="stage-flow">';
  stages.forEach((s, i) => {
    const active = s.cnt > 0 ? (s.key === 'working' ? ' active' : ' has-tasks') : '';
    html += `<div class="stage-col"><div class="stage-node${active}">${s.cnt}</div><div class="stage-label">${s.label}</div></div>`;
    if (i < stages.length - 1) html += '<span class="stage-arrow">&#x25B8;</span>';
  });
  html += '</div>';
  el.innerHTML = html;
}

/* ═══ Repo Bar Chart ═══ */
function renderRepoChart(tasks) {
  const el = document.getElementById('repo-chart');
  const repos = {};
  for (const t of tasks) {
    if (t.status === 'cancelled') continue;
    const r = t.repo_name || '?';
    if (!repos[r]) repos[r] = {total: 0, failed: 0};
    repos[r].total++;
    if (t.status === 'failed') repos[r].failed++;
  }
  const entries = Object.entries(repos).sort((a,b) => b[1].total - a[1].total);
  if (!entries.length) { el.innerHTML = '<div class="empty">No data</div>'; return; }
  const maxVal = Math.max(...entries.map(([,v]) => v.total), 1);
  el.innerHTML = entries.map(([name, v]) => {
    const h = Math.max(4, Math.round(v.total / maxVal * 36));
    const fh = v.failed > 0 ? Math.max(2, Math.round(v.failed / maxVal * 36)) : 0;
    const okH = h - fh;
    return `<div class="bar-col">
      <div style="display:flex;flex-direction:column;align-items:center;gap:0">
        ${fh > 0 ? `<div class="bar-fill fail-portion" style="height:${fh}px"></div>` : ''}
        <div class="bar-fill" style="height:${okH}px"></div>
      </div>
      <div class="bar-label">${esc(name)}</div>
    </div>`;
  }).join('');
}

/* ═══ Filters ═══ */
const FILTER_GROUPS = {
  all: null,
  running: new Set(['working','reviewing','retrying']),
  failed: new Set(['failed']),
  waiting: new Set(['queued','ci_passed']),
  pipeline: new Set(['pr_created','reviewed']),
  done: new Set(['completed','cancelled']),
};

function setFilter(f) {
  _filter = f;
  _repoFilter = null;
  renderFilterBar();
  renderFleet(_tasks);
}

function setRepoFilter(repo) {
  if (_repoFilter === repo) { _repoFilter = null; } else { _repoFilter = repo; }
  renderFilterBar();
  renderFleet(_tasks);
}

function renderFilterBar() {
  const bar = document.getElementById('filter-bar');
  const repos = [...new Set(_tasks.map(t => t.repo_name))].sort();

  // Count per filter
  const counts = {};
  for (const [k, set] of Object.entries(FILTER_GROUPS)) {
    counts[k] = set ? _tasks.filter(t => set.has(t.status)).length : _tasks.length;
  }

  let html = '';
  const filters = [
    {key:'all', label:'ALL'},
    {key:'running', label:'RUNNING', activeCls:'active-run'},
    {key:'waiting', label:'WAITING', activeCls:'active-wait'},
    {key:'failed', label:'FAILED', activeCls:'active-fail'},
    {key:'pipeline', label:'PIPELINE'},
    {key:'done', label:'DONE'},
  ];
  for (const f of filters) {
    const cls = _filter === f.key ? (f.activeCls || 'active') : '';
    const cnt = counts[f.key] || 0;
    html += `<button class="filter-btn ${cls}" onclick="setFilter('${f.key}')">${f.label}<span class="fleet-count">${cnt}</span></button>`;
  }

  if (repos.length > 1) {
    html += '<span class="filter-sep"></span>';
    for (const r of repos) {
      const cls = _repoFilter === r ? 'active' : '';
      html += `<button class="filter-btn ${cls}" onclick="setRepoFilter('${esc(r)}')">${esc(r)}</button>`;
    }
  }

  bar.innerHTML = html;
}

function applyFilters(tasks) {
  let filtered = tasks;
  const group = FILTER_GROUPS[_filter];
  if (group) filtered = filtered.filter(t => group.has(t.status));
  if (_repoFilter) filtered = filtered.filter(t => t.repo_name === _repoFilter);
  return filtered;
}

/* ═══ Fleet Table ═══ */
function renderFleet(tasks) {
  const el = document.getElementById('fleet-content');
  const filtered = applyFilters(tasks);

  if (!filtered.length) {
    const msg = _filter !== 'all' || _repoFilter ? 'No matching tasks' : 'No tasks';
    el.innerHTML = `<div class="empty">${msg}</div>`;
    return;
  }

  const newIds = new Set(filtered.map(t => t.id));

  let html = '<table class="fleet-table"><thead><tr>';
  html += '<th>ID</th><th>Status</th><th>Repo</th><th>Issue</th><th>PR</th><th>Model</th><th>Time</th><th>Actions</th>';
  html += '</tr></thead><tbody>';
  for (const t of filtered) {
    const sel = t.id === _selectedId ? ' selected' : '';
    const isNew = !_prevIds.has(t.id) && _prevIds.size > 0 ? ' new-row' : '';
    const time = t.elapsed ? `<span class="text-cyan">${t.elapsed}</span>` : '\\u2014';
    html += `<tr class="${sel}${isNew}" data-task-id="${t.id}" style="cursor:pointer">`;
    html += `<td class="col-id">#${t.id}</td>`;
    html += `<td>${badge(t.status, t.hold)}</td>`;
    html += `<td class="col-repo">${esc(t.repo_name)}</td>`;
    html += `<td class="col-issue">${issueLink(t)}</td>`;
    html += `<td>${prLink(t)}</td>`;
    html += `<td class="col-model">${esc(t.model||'')}</td>`;
    html += `<td class="col-time">${time}</td>`;
    html += `<td>${actionBtns(t)}</td>`;
    html += '</tr>';
  }
  html += '</tbody></table>';
  el.innerHTML = html;

  _prevIds = newIds;
}

/* ═══ Metrics ═══ */
function updateMetrics(tasks) {
  const nonCancelled = tasks.filter(t => t.status !== 'cancelled');
  const completed = nonCancelled.filter(t => t.status === 'completed');
  const total = nonCancelled.length;

  document.getElementById('m-merged').textContent = completed.length;
  const mergedBar = document.getElementById('m-merged-bar');
  if (mergedBar) mergedBar.style.width = total > 0 ? Math.round(completed.length/total*100)+'%' : '0%';

  if (total > 0) {
    const rate = Math.round(completed.length / total * 100);
    document.getElementById('m-rate').textContent = rate + '%';
    const rateBar = document.getElementById('m-rate-bar');
    if (rateBar) rateBar.style.width = rate + '%';
  } else {
    document.getElementById('m-rate').textContent = '\\u2014';
  }

  let mergeTimes = [];
  for (const t of completed) {
    if (t.completed_at && t.started_at) {
      try {
        const s = new Date(t.started_at), e = new Date(t.completed_at);
        if (!isNaN(s) && !isNaN(e)) mergeTimes.push((e - s) / 1000);
      } catch(_) {}
    }
  }
  document.getElementById('m-time').textContent = mergeTimes.length ? fmtDur(mergeTimes.reduce((a,b)=>a+b,0)/mergeTimes.length) : '\\u2014';

  const retries = nonCancelled.reduce((a,t) => a + (t.retry_count||0), 0);
  const retryPct = total > 0 ? Math.round(retries/total*100) : 0;
  document.getElementById('m-retry').textContent = total > 0 ? retryPct + '%' : '\\u2014';
  const retryBar = document.getElementById('m-retry-bar');
  if (retryBar) retryBar.style.width = retryPct + '%';
}

/* ═══ Task Detail Panel ═══ */
function showTaskDetail(taskId) {
  _selectedId = taskId;
  const panel = document.getElementById('task-detail-panel');
  const content = document.getElementById('task-detail-content');
  const t = _tasks.find(x => x.id == taskId);
  if (!t) { content.innerHTML = '<div class="empty">Task not found</div>'; return; }

  const terminal = new Set(['completed','failed','cancelled']);
  const escalatable = (t.status === 'queued' || t.status === 'working') && t.model !== 'opus';
  const requeueable = terminal.has(t.status);
  const editable = t.status === 'queued' || t.status === 'failed' || t.hold;

  let html = `<div class="task-detail-title">${esc(t.title)}</div>`;
  html += `<div class="task-detail-meta">${esc(t.repo_name)} &middot; ${esc(t.model||'')} ${t.elapsed ? '&middot; '+t.elapsed : ''}</div>`;

  // Status explanation
  let statusText = '', isError = false;
  if (t.hold === 'merge_approval') statusText = 'CI PASSED \\u2014 awaiting merge approval';
  else if (t.hold === 'dispatch_approval') statusText = 'Queued \\u2014 awaiting dispatch approval';
  else if (t.hold === 'user_hold') statusText = 'User hold \\u2014 manually paused';
  else if (t.error_message) { statusText = esc(t.error_message.substring(0,200)); isError = true; }
  if (statusText) html += `<div class="task-detail-status${isError ? ' error' : ''}">${statusText}</div>`;

  // Buttons
  html += '<div class="btn-group" style="margin-top:var(--s-md)">';
  html += actionBtns(t);
  if (t.status === 'queued' || t.status === 'failed')
    html += ` <button class="btn btn-approve" onclick="dispatchSingle(${t.id},this)">RUN</button>`;
  if (escalatable) html += ` <button class="btn btn-approve" onclick="escalateTask(${t.id})">ESCALATE</button>`;
  if (requeueable && !(editable || requeueable)) html += ` <button class="btn btn-approve" onclick="requeueTask(${t.id})">RE-QUEUE</button>`;
  html += '</div>';

  // Inline edit + requeue form for failed/editable tasks
  if (editable || requeueable) {
    html += `<div class="inline-edit" id="inline-edit-${t.id}">
      <label>Prompt (edit to refine before re-queuing)</label>
      <textarea id="ie-prompt-${t.id}">Loading full prompt...</textarea>
      <div class="form-row">
        <div><label>Model</label>
          <select id="ie-model-${t.id}">
            <option value="sonnet" ${t.model==='sonnet'?'selected':''}>sonnet</option>
            <option value="opus" ${t.model==='opus'?'selected':''}>opus</option>
            <option value="haiku" ${t.model==='haiku'?'selected':''}>haiku</option>
          </select>
        </div>
        <div><label>Priority</label>
          <input type="number" id="ie-pri-${t.id}" value="${t.priority||100}" style="width:80px">
        </div>
        <div>
          <button class="btn btn-approve" onclick="inlineSubmit(${t.id})" id="ie-btn-${t.id}">
            ${requeueable ? 'SAVE & RE-QUEUE' : 'SAVE & UPDATE'}
          </button>
        </div>
      </div>
    </div>`;
  }

  // Timeline
  html += `<div class="timeline" id="timeline-${t.id}"><div class="timeline-header">Timeline</div><div class="empty">Loading...</div></div>`;
  content.innerHTML = html;

  // Fetch full task data (for prompt + logs)
  fetch('/api/tasks/' + t.id).then(r => r.json()).then(d => {
    if (!d.task) return;
    // Fill in the full prompt
    const promptEl = document.getElementById('ie-prompt-' + t.id);
    if (promptEl) promptEl.value = d.task.prompt || '';
    // Render timeline
    const logs = d.logs || [];
    const tl = document.getElementById('timeline-' + t.id);
    if (!tl) return;
    if (!logs.length) { tl.innerHTML = '<div class="timeline-header">Timeline</div><div class="empty">No logs</div>'; return; }
    let lhtml = '<div class="timeline-header">Timeline</div>';
    for (const log of logs) {
      const ts = (log.created_at||'').split('T').pop().split('.')[0] || '';
      const cls = log.level === 'error' ? 'text-red' : log.level === 'warn' ? 'text-amber' : '';
      lhtml += `<div class="timeline-entry"><span class="timeline-time">${ts}</span><span class="timeline-event ${cls}">${esc(log.message.substring(0,200))}</span></div>`;
    }
    tl.innerHTML = lhtml;
  }).catch(() => {});

  // Highlight selected row
  document.querySelectorAll('.fleet-table tbody tr').forEach(r => r.classList.remove('selected'));
  const row = document.querySelector(`.fleet-table tbody tr[data-task-id="${taskId}"]`);
  if (row) row.classList.add('selected');
}

/* ═══ Edit Modal ═══ */
function openEditModal(id) {
  const t = _tasks.find(x => x.id == id);
  if (!t) return;
  // Fetch full task data
  fetch('/api/tasks/' + id).then(r => r.json()).then(d => {
    if (!d.task) return;
    const task = d.task;
    const terminal = new Set(['completed','failed','cancelled']);
    const isTerminal = terminal.has(task.status);

    let html = '';
    html += `<div class="edit-form">`;
    html += `<label>Prompt</label><textarea id="edit-prompt">${esc(task.prompt)}</textarea>`;
    html += `<div class="form-row">`;
    html += `<div><label>Model</label><select id="edit-model">
      <option value="sonnet" ${task.model==='sonnet'?'selected':''}>sonnet</option>
      <option value="opus" ${task.model==='opus'?'selected':''}>opus</option>
      <option value="haiku" ${task.model==='haiku'?'selected':''}>haiku</option>
    </select></div>`;
    html += `<div><label>Priority</label><input type="number" id="edit-priority" value="${task.priority||100}" style="width:80px"></div>`;
    html += `<div style="padding-bottom:2px"><button class="btn btn-approve" onclick="submitEdit(${id})" style="margin-top:18px">SAVE &amp; ${isTerminal ? 'RE-QUEUE' : 'UPDATE'}</button></div>`;
    html += `</div></div>`;

    document.getElementById('modal-title').textContent = 'Edit Task #' + id;
    document.getElementById('modal-body').innerHTML = html;
    document.getElementById('task-modal').classList.add('open');
  }).catch(() => {});
}

function closeModal() { document.getElementById('task-modal').classList.remove('open'); }
document.addEventListener('keydown', e => { if (e.key === 'Escape') closeModal(); });

async function submitEdit(id) {
  const prompt = document.getElementById('edit-prompt').value;
  const model = document.getElementById('edit-model').value;
  const priority = parseInt(document.getElementById('edit-priority').value) || 100;
  const t = _tasks.find(x => x.id == id);
  const terminal = new Set(['completed','failed','cancelled']);
  const isTerminal = t && terminal.has(t.status);
  const url = isTerminal ? '/api/tasks/'+id+'/requeue' : '/api/tasks/'+id+'/edit';
  try {
    const res = await fetch(url, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({prompt,model,priority})});
    const d = await res.json();
    if (d.ok) { closeModal(); showTaskDetail(id); } else { alert('Failed: '+(d.error||'')); }
  } catch(e) { alert('Error: '+e); }
}

async function inlineSubmit(id) {
  const prompt = document.getElementById('ie-prompt-' + id)?.value;
  const model = document.getElementById('ie-model-' + id)?.value;
  const priority = parseInt(document.getElementById('ie-pri-' + id)?.value) || 100;
  const btn = document.getElementById('ie-btn-' + id);
  if (btn) { btn.textContent = '...'; btn.disabled = true; }

  const t = _tasks.find(x => x.id == id);
  const terminal = new Set(['completed','failed','cancelled']);
  const isTerminal = t && terminal.has(t.status);
  const url = isTerminal ? '/api/tasks/'+id+'/requeue' : '/api/tasks/'+id+'/edit';

  try {
    const res = await fetch(url, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({prompt,model,priority})});
    const d = await res.json();
    if (d.ok) {
      if (btn) { btn.textContent = 'DONE'; btn.style.borderColor = 'var(--c-success)'; btn.style.color = 'var(--c-success)'; }
      setTimeout(() => showTaskDetail(id), 1000);
    } else {
      alert('Failed: '+(d.error||''));
      if (btn) { btn.textContent = isTerminal ? 'SAVE & RE-QUEUE' : 'SAVE & UPDATE'; btn.disabled = false; }
    }
  } catch(e) {
    alert('Error: '+e);
    if (btn) { btn.textContent = isTerminal ? 'SAVE & RE-QUEUE' : 'SAVE & UPDATE'; btn.disabled = false; }
  }
}

async function dispatchSingle(id, btn) {
  if (btn) { btn.textContent = '...'; btn.disabled = true; }
  try {
    const res = await fetch('/api/tasks/' + id + '/dispatch', {method:'POST', credentials:'include'});
    const d = await res.json();
    if (!d.ok) {
      alert('Dispatch failed: ' + (d.error || ''));
      if (btn) { btn.textContent = 'RUN'; btn.disabled = false; }
    }
    // SSE will update the row to show WORKING status
  } catch(e) {
    alert('Error: ' + e);
    if (btn) { btn.textContent = 'RUN'; btn.disabled = false; }
  }
}

async function escalateTask(id) {
  try {
    const res = await fetch('/api/tasks/'+id+'/escalate', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({model:'opus'})});
    const d = await res.json();
    if (!d.ok) alert('Escalate failed: '+(d.error||''));
  } catch(e) { alert('Error: '+e); }
}

async function requeueTask(id) {
  if (!confirm('Re-queue task #'+id+'?')) return;
  try {
    const res = await fetch('/api/tasks/'+id+'/requeue', {method:'POST', headers:{'Content-Type':'application/json'}, body:'{}'});
    const d = await res.json();
    if (!d.ok) alert('Requeue failed: '+(d.error||''));
  } catch(e) { alert('Error: '+e); }
}

/* ═══ Event Delegation ═══ */
document.addEventListener('click', async (e) => {
  const btn = e.target.closest('.btn[data-action]');
  if (btn) {
    e.stopPropagation();
    const id = btn.dataset.id, action = btn.dataset.action;
    if (action === 'reject' && !confirm('Reject task #'+id+'?')) return;
    const orig = btn.textContent;
    btn.textContent = '...'; btn.disabled = true;
    try { await fetch('/api/tasks/'+id+'/'+action, {method:'POST', credentials:'include'}); }
    catch(err) { console.error('Action failed:', err); }
    setTimeout(() => { btn.textContent = orig; btn.disabled = false; }, 3000);
    return;
  }

  const pauseBtn = e.target.closest('#pause-btn');
  if (pauseBtn) {
    const action = _paused ? 'resume' : 'pause';
    pauseBtn.textContent = '...'; pauseBtn.disabled = true;
    try { await fetch('/api/'+action, {method:'POST', credentials:'include'}); }
    catch(err) { console.error('Pause/resume failed:', err); }
    setTimeout(() => { pauseBtn.disabled = false; }, 3000);
    return;
  }

  const row = e.target.closest('.fleet-table tbody tr');
  if (row && !e.target.closest('.btn') && !e.target.closest('a')) {
    const taskId = parseInt(row.dataset.taskId);
    if (taskId) showTaskDetail(taskId);
  }
});

/* ═══ SSE ═══ */
function update(data) {
  const c = data.counts || {};
  _paused = !!data.queue_paused;
  _owner = data.github_owner || '';
  _tasks = data.tasks || [];

  // Header
  const pauseBtn = document.getElementById('pause-btn');
  const pausedInd = document.getElementById('paused-indicator');
  const heldInd = document.getElementById('held-indicator');
  if (_paused) {
    pausedInd.style.display = ''; pauseBtn.textContent = 'RESUME'; pauseBtn.className = 'btn btn-resume';
  } else {
    pausedInd.style.display = 'none'; pauseBtn.textContent = 'PAUSE'; pauseBtn.className = 'btn btn-pause';
  }
  if (data.held_count > 0) {
    heldInd.style.display = ''; heldInd.textContent = data.held_count + ' AWAITING APPROVAL';
  } else {
    heldInd.style.display = 'none';
  }

  // Worker status
  const workerSt = document.getElementById('worker-status');
  const workerBtn = document.getElementById('worker-btn');
  _workerRunning = !!data.worker_running;
  if (_workerRunning) {
    workerSt.textContent = 'WORKER: PID ' + (data.worker_pid||'?');
    workerSt.className = 'text-green';
    workerBtn.textContent = 'STOP FLEET';
    workerBtn.className = 'btn btn-reject';
  } else {
    workerSt.textContent = 'WORKER: OFF';
    workerSt.className = 'text-muted';
    workerBtn.textContent = 'START FLEET';
    workerBtn.className = 'btn btn-approve';
  }

  renderAgentViz(_tasks);
  renderStageFlow(c);
  renderFilterBar();
  renderFleet(_tasks);
  updateMetrics(_tasks);
  renderRepoChart(_tasks);

  // Refresh detail if visible
  if (_selectedId) {
    const t = _tasks.find(x => x.id === _selectedId);
    if (t) showTaskDetail(_selectedId);
  }
}

/* ═══ Worker Control ═══ */
let _workerRunning = false;

async function toggleWorker() {
  const btn = document.getElementById('worker-btn');
  const action = _workerRunning ? 'stop' : 'start';
  if (_workerRunning && !confirm('Stop the fleet worker?')) return;
  btn.textContent = '...'; btn.disabled = true;
  try {
    const res = await fetch('/api/worker/' + action, {method:'POST', credentials:'include'});
    const d = await res.json();
    if (!d.ok) alert(d.message || 'Failed');
  } catch(e) { alert('Error: ' + e); }
  setTimeout(() => { btn.disabled = false; }, 2000);
}

/* ═══ New Task Modal ═══ */
async function openNewTaskModal() {
  // Fetch repos for the dropdown
  let repos = [];
  try {
    // Extract repo names from current tasks
    repos = [...new Set(_tasks.map(t => t.repo_name))].sort();
  } catch(_) {}

  let repoOpts = repos.map(r => `<option value="${esc(r)}">${esc(r)}</option>`).join('');
  if (!repoOpts) repoOpts = '<option value="">No repos available</option>';

  const html = `<div class="edit-form">
    <label>Repo</label>
    <select id="new-repo">${repoOpts}</select>
    <label>Prompt / Issue Description</label>
    <textarea id="new-prompt" placeholder="Describe the task for the agent..."></textarea>
    <div class="form-row">
      <div><label>Model</label>
        <select id="new-model">
          <option value="sonnet">sonnet</option>
          <option value="opus">opus</option>
          <option value="haiku">haiku</option>
        </select>
      </div>
      <div><label>Priority</label>
        <input type="number" id="new-priority" value="100" style="width:80px">
      </div>
      <div>
        <button class="btn btn-approve" onclick="submitNewTask()" style="margin-top:18px">CREATE TASK</button>
      </div>
    </div>
  </div>`;

  document.getElementById('modal-title').textContent = 'New Task';
  document.getElementById('modal-body').innerHTML = html;
  document.getElementById('task-modal').classList.add('open');
}

async function submitNewTask() {
  const repo = document.getElementById('new-repo').value;
  const prompt = document.getElementById('new-prompt').value.trim();
  const model = document.getElementById('new-model').value;
  const priority = parseInt(document.getElementById('new-priority').value) || 100;

  if (!prompt) { alert('Prompt is required'); return; }

  try {
    const res = await fetch('/api/tasks/create', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({repo, prompt, model, priority})
    });
    const d = await res.json();
    if (d.ok) {
      closeModal();
      // Select the new task after next SSE update
      setTimeout(() => { if (d.task_id) showTaskDetail(d.task_id); }, 2000);
    } else {
      alert('Failed: ' + (d.error || ''));
    }
  } catch(e) { alert('Error: ' + e); }
}

function connect() {
  const statusEl = document.getElementById('sse-status');
  const es = new EventSource('/events');
  es.onopen = () => { statusEl.textContent = 'LIVE'; statusEl.className = 'status-live'; };
  es.onmessage = (e) => { try { update(JSON.parse(e.data)); } catch(err) { console.error('SSE parse error', err); } };
  es.onerror = () => {
    statusEl.textContent = 'RECONNECTING'; statusEl.className = 'text-red';
    es.close(); setTimeout(connect, 3000);
  };
}
connect();
</script>
</body>
</html>
"""


async def stats_handler(request: web.Request) -> web.Response:
    """JSON stats endpoint — mirrors CLI stats but server-side computed."""
    db = request.app["db"]

    now = datetime.now(timezone.utc)

    # All tasks (exclude cancelled) — use direct SQL for efficiency
    async with db.db.execute(
        "SELECT t.status, t.model, t.created_at, t.completed_at, "
        "t.agent_started_at, t.agent_finished_at, t.model_used, t.initial_model, "
        "t.retry_count, r.name as repo_name "
        "FROM tasks t JOIN repos r ON t.repo_id = r.id "
        "WHERE t.status != 'cancelled'"
    ) as cur:
        tasks = [dict(r) for r in await cur.fetchall()]

    if not tasks:
        return web.json_response({"stats": None})

    def _parse_iso(s):
        if not s:
            return None
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except Exception:
            return None

    completed = [t for t in tasks if t["status"] == "completed"]
    failed = [t for t in tasks if t["status"] == "failed"]
    total = len(tasks)
    n_completed = len(completed)
    n_failed = len(failed)

    # Issue→merge times
    merge_times = []
    for t in completed:
        s, e = _parse_iso(t["created_at"]), _parse_iso(t["completed_at"])
        if s and e:
            merge_times.append((e - s).total_seconds())

    # Agent runtimes
    agent_runtimes = []
    for t in completed:
        s, e = _parse_iso(t.get("agent_started_at")), _parse_iso(t.get("agent_finished_at"))
        if s and e:
            agent_runtimes.append((e - s).total_seconds())

    avg_merge = sum(merge_times) / len(merge_times) if merge_times else None
    avg_agent = sum(agent_runtimes) / len(agent_runtimes) if agent_runtimes else None

    total_retries = sum(t.get("retry_count") or 0 for t in tasks)
    retry_rate = (total_retries / total * 100) if total > 0 else 0

    # Model breakdown
    model_counts = {}
    for t in tasks:
        m = t.get("model_used") or t.get("model") or "unknown"
        model_counts[m] = model_counts.get(m, 0) + 1

    # Escalations
    escalations = sum(
        1 for t in tasks
        if t.get("initial_model") and t.get("model_used")
        and t["initial_model"] != t["model_used"]
    )

    # Last 7 days
    from datetime import timedelta
    seven_ago = now - timedelta(days=7)
    recent = [t for t in tasks if (_parse_iso(t["created_at"]) or now) >= seven_ago]
    recent_completed = [t for t in recent if t["status"] == "completed"]
    recent_failed = [t for t in recent if t["status"] == "failed"]
    recent_merge = []
    for t in recent_completed:
        s, e = _parse_iso(t["created_at"]), _parse_iso(t["completed_at"])
        if s and e:
            recent_merge.append((e - s).total_seconds())
    recent_avg_merge = sum(recent_merge) / len(recent_merge) if recent_merge else None

    # Per-repo
    repo_stats = {}
    for t in tasks:
        rn = t.get("repo_name", "unknown")
        if rn not in repo_stats:
            repo_stats[rn] = {"total": 0, "failed": 0}
        repo_stats[rn]["total"] += 1
        if t["status"] == "failed":
            repo_stats[rn]["failed"] += 1

    return web.json_response({"stats": {
        "total": total,
        "completed": n_completed,
        "failed": n_failed,
        "pct_completed": round(n_completed / total * 100, 1) if total else 0,
        "pct_failed": round(n_failed / total * 100, 1) if total else 0,
        "avg_merge_seconds": round(avg_merge) if avg_merge else None,
        "avg_agent_seconds": round(avg_agent) if avg_agent else None,
        "total_retries": total_retries,
        "retry_rate": round(retry_rate, 1),
        "models": model_counts,
        "escalations": escalations,
        "recent_7d": {
            "completed": len(recent_completed),
            "failed": len(recent_failed),
            "avg_merge_seconds": round(recent_avg_merge) if recent_avg_merge else None,
        },
        "repos": repo_stats,
    }})


async def edit_task_handler(request: web.Request) -> web.Response:
    """Edit a task's prompt and/or model and re-queue it.

    Accepts JSON body: {"prompt": "...", "model": "...", "priority": N}
    All fields optional. Only works on queued, failed, or held tasks.
    If task is failed, resets it to queued automatically.
    """
    db = request.app["db"]
    task_id = int(request.match_info["id"])
    task = await db.get_task(task_id)
    if not task:
        return web.json_response({"ok": False, "error": "not found"}, status=404)

    editable_statuses = {"queued", "failed"}
    has_hold = bool(task.get("hold"))
    if task["status"] not in editable_statuses and not has_hold:
        return web.json_response({
            "ok": False,
            "error": f"cannot edit task in status={task['status']} (must be queued, failed, or held)"
        }, status=400)

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "invalid JSON body"}, status=400)

    updates = {}
    if "prompt" in body and body["prompt"]:
        updates["prompt"] = str(body["prompt"])[:10000]
    if "model" in body and body["model"] in ("sonnet", "opus", "haiku"):
        updates["model"] = body["model"]
    if "priority" in body:
        try:
            updates["priority"] = int(body["priority"])
        except (ValueError, TypeError):
            pass

    if not updates:
        return web.json_response({"ok": False, "error": "no valid fields to update"}, status=400)

    # If task was failed, reset to queued
    if task["status"] == "failed":
        updates["status"] = "queued"
        updates["error_message"] = None
        updates["started_at"] = None
        updates["completed_at"] = None
        updates["branch_name"] = None
        updates["worktree_path"] = None
        updates["pr_url"] = None
        updates["pr_number"] = None
        updates["review_summary"] = None

    await db.update_task(task_id, **updates)
    action_desc = ", ".join(f"{k}={v!r:.60}" for k, v in updates.items() if k not in (
        "error_message", "started_at", "completed_at", "branch_name",
        "worktree_path", "pr_url", "pr_number", "review_summary",
    ))
    await db.add_log(task_id, f"Edited via dashboard: {action_desc}")

    return web.json_response({"ok": True, "task_id": task_id, "action": "edit", "updates": {k: str(v)[:100] for k, v in updates.items()}})


async def requeue_task_handler(request: web.Request) -> web.Response:
    """Re-queue a failed or completed task for another run.

    Resets the task to queued, clearing all execution state.
    Optionally accepts JSON body: {"model": "opus", "prompt": "..."} to override.
    """
    db = request.app["db"]
    task_id = int(request.match_info["id"])
    task = await db.get_task(task_id)
    if not task:
        return web.json_response({"ok": False, "error": "not found"}, status=404)

    requeueable = {"failed", "completed", "cancelled"}
    if task["status"] not in requeueable:
        return web.json_response({
            "ok": False,
            "error": f"cannot requeue task in status={task['status']} (must be failed, completed, or cancelled)"
        }, status=400)

    try:
        body = await request.json()
    except Exception:
        body = {}

    updates = {
        "status": "queued",
        "error_message": None,
        "started_at": None,
        "completed_at": None,
        "branch_name": None,
        "worktree_path": None,
        "pr_url": None,
        "pr_number": None,
        "review_summary": None,
        "exit_code": None,
        "agent_pid": None,
        "output_summary": None,
        "hold": None,
        "agent_started_at": None,
        "agent_finished_at": None,
    }

    if "model" in body and body["model"] in ("sonnet", "opus", "haiku"):
        updates["model"] = body["model"]
    if "prompt" in body and body["prompt"]:
        updates["prompt"] = str(body["prompt"])[:10000]

    await db.update_task(task_id, **updates)
    model = updates.get("model", task["model"])
    await db.add_log(task_id, f"Re-queued via dashboard (model={model})")

    return web.json_response({"ok": True, "task_id": task_id, "action": "requeue"})


async def escalate_task_handler(request: web.Request) -> web.Response:
    """Escalate a task's model (e.g., sonnet -> opus).

    Works on queued or working tasks. For working tasks, this sets the model
    for the next retry — it doesn't interrupt the current run.
    """
    db = request.app["db"]
    task_id = int(request.match_info["id"])
    task = await db.get_task(task_id)
    if not task:
        return web.json_response({"ok": False, "error": "not found"}, status=404)

    if task["status"] not in ("queued", "working"):
        return web.json_response({
            "ok": False,
            "error": f"cannot escalate task in status={task['status']} (must be queued or working)"
        }, status=400)

    try:
        body = await request.json()
    except Exception:
        body = {}

    target_model = body.get("model", "opus")
    if target_model not in ("sonnet", "opus", "haiku"):
        return web.json_response({"ok": False, "error": f"invalid model: {target_model}"}, status=400)

    if task["model"] == target_model:
        return web.json_response({"ok": False, "error": f"task already uses {target_model}"}, status=400)

    old_model = task["model"]
    await db.update_task(task_id, model=target_model)
    await db.add_log(task_id, f"Model escalated: {old_model} -> {target_model} via dashboard")

    return web.json_response({"ok": True, "task_id": task_id, "action": "escalate", "from": old_model, "to": target_model})


async def approve_handler(request: web.Request) -> web.Response:
    """Clear hold on a task, allowing it to proceed."""
    db = request.app["db"]
    task_id = int(request.match_info["id"])
    task = await db.get_task(task_id)
    if not task:
        return web.json_response({"ok": False, "error": "not found"}, status=404)
    if not task.get("hold"):
        return web.json_response({"ok": False, "error": "no hold on this task"}, status=400)

    await db.clear_hold(task_id)
    await db.add_log(task_id, f"Hold '{task['hold']}' cleared via dashboard")
    return web.json_response({"ok": True, "task_id": task_id, "action": "approve"})


async def hold_handler(request: web.Request) -> web.Response:
    """Set a user hold on a task."""
    db = request.app["db"]
    task_id = int(request.match_info["id"])
    task = await db.get_task(task_id)
    if not task:
        return web.json_response({"ok": False, "error": "not found"}, status=404)
    if task["status"] in ("completed", "failed", "cancelled"):
        return web.json_response({"ok": False, "error": f"cannot hold terminal task (status={task['status']})"}, status=400)
    if task.get("hold"):
        return web.json_response({"ok": False, "error": f"task already has hold: {task['hold']}"}, status=400)

    await db.set_hold(task_id, "user_hold")
    await db.add_log(task_id, "User hold set via dashboard")
    return web.json_response({"ok": True, "task_id": task_id, "action": "hold"})


async def reject_handler(request: web.Request) -> web.Response:
    """Cancel/reject a task — mirrors CLI cancel logic."""
    import os as _os
    import signal as _signal

    db = request.app["db"]
    task_id = int(request.match_info["id"])
    task = await db.get_task(task_id)
    if not task:
        return web.json_response({"ok": False, "error": "not found"}, status=404)

    rejectable = {"working", "pr_created", "reviewing", "reviewed", "ci_passed"}
    if task["status"] not in rejectable:
        return web.json_response({"ok": False, "error": f"cannot reject task in status={task['status']}"}, status=400)

    now = datetime.now(timezone.utc).isoformat()

    # Kill agent process if running
    pid = task.get("agent_pid")
    if pid and task["status"] == "working":
        try:
            _os.killpg(pid, _signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            try:
                _os.kill(pid, _signal.SIGTERM)
            except ProcessLookupError:
                pass

    await db.update_task(task_id, status="cancelled", completed_at=now, hold=None)
    await db.add_log(task_id, "Cancelled/rejected via dashboard", level="warn")

    # Cascade failure to dependent tasks
    await db.handle_dependency_failure(task_id)

    # Restore GitHub labels if this task came from an issue
    issue_num = task.get("github_issue_number")
    if issue_num:
        repo = await db.get_repo(task["repo_id"])
        if repo:
            import subprocess
            from .github import repo_full_name_from_url
            repo_full = repo_full_name_from_url(repo["github_url"])
            subprocess.run(
                ["gh", "issue", "edit", "--repo", repo_full, str(issue_num),
                 "--add-label", "backporcher", "--remove-label", "backporcher-in-progress"],
                capture_output=True,
            )

    return web.json_response({"ok": True, "task_id": task_id, "action": "reject"})


# Track in-flight single-dispatch tasks so we don't double-dispatch
_dispatching: set[int] = set()


async def dispatch_single_handler(request: web.Request) -> web.Response:
    """Dispatch a single task immediately — runs agent in background without needing the full worker."""
    db = request.app["db"]
    config = request.app["config"]
    task_id = int(request.match_info["id"])
    task = await db.get_task(task_id)
    if not task:
        return web.json_response({"ok": False, "error": "not found"}, status=404)

    if task_id in _dispatching:
        return web.json_response({"ok": False, "error": "already dispatching"}, status=409)

    # Allow dispatching queued or failed tasks
    if task["status"] not in ("queued", "failed"):
        return web.json_response({"ok": False, "error": f"cannot dispatch task in status={task['status']}"}, status=400)

    # If failed, reset to queued first
    if task["status"] == "failed":
        now = datetime.now(timezone.utc).isoformat()
        await db.update_task(task_id,
            status="queued", error_message=None, started_at=None,
            completed_at=None, branch_name=None, worktree_path=None,
            pr_url=None, pr_number=None, review_summary=None,
            exit_code=None, agent_pid=None, output_summary=None, hold=None,
            agent_started_at=None, agent_finished_at=None,
        )
        await db.add_log(task_id, "Reset to queued for single dispatch via dashboard")

    # Claim it
    now = datetime.now(timezone.utc).isoformat()
    await db.update_task(task_id, status="working", started_at=now)
    await db.add_log(task_id, "Dispatched via dashboard (single task)")

    _dispatching.add(task_id)

    # Run in background
    async def _run():
        try:
            from .dispatcher import dispatch_task
            fresh = await db.get_task(task_id)
            if fresh:
                await dispatch_task(fresh, config, db)
        except Exception:
            log.exception("Single dispatch failed for task %d", task_id)
            try:
                await db.update_task(task_id, status="failed",
                    error_message="Single dispatch error", completed_at=datetime.now(timezone.utc).isoformat())
                await db.add_log(task_id, "Single dispatch failed", level="error")
            except Exception:
                pass
        finally:
            _dispatching.discard(task_id)

    asyncio.create_task(_run())

    return web.json_response({"ok": True, "task_id": task_id, "action": "dispatch"})


async def create_task_handler(request: web.Request) -> web.Response:
    """Create a new task manually from the dashboard.

    Accepts JSON: {"repo": "name", "prompt": "...", "model": "sonnet", "priority": 100}
    """
    db = request.app["db"]
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "invalid JSON"}, status=400)

    repo_name = body.get("repo")
    prompt = body.get("prompt", "").strip()
    model = body.get("model", "sonnet")
    priority = int(body.get("priority", 100))

    if not repo_name:
        return web.json_response({"ok": False, "error": "repo is required"}, status=400)
    if not prompt:
        return web.json_response({"ok": False, "error": "prompt is required"}, status=400)
    if model not in ("sonnet", "opus", "haiku"):
        return web.json_response({"ok": False, "error": f"invalid model: {model}"}, status=400)

    repo = await db.get_repo_by_name(repo_name)
    if not repo:
        return web.json_response({"ok": False, "error": f"repo '{repo_name}' not found"}, status=404)

    task_id = await db.create_task(repo["id"], prompt, model)
    if priority != 100:
        await db.update_task(task_id, priority=priority)
    await db.add_log(task_id, f"Created manually via dashboard (model={model})")

    return web.json_response({"ok": True, "task_id": task_id})


async def worker_start_handler(request: web.Request) -> web.Response:
    """Start the worker daemon subprocess."""
    config = request.app["config"]
    ok, msg = await _start_worker(config)
    return web.json_response({"ok": ok, "message": msg})


async def worker_stop_handler(request: web.Request) -> web.Response:
    """Stop the worker daemon subprocess."""
    ok, msg = await _stop_worker()
    return web.json_response({"ok": ok, "message": msg})


async def worker_status_handler(request: web.Request) -> web.Response:
    """Worker status and recent log lines."""
    alive = _is_worker_alive()
    pid = _worker_proc.pid if _worker_proc and alive else None
    return web.json_response({
        "running": alive,
        "pid": pid,
        "log": _worker_log_lines[-50:],
    })


async def pause_handler(request: web.Request) -> web.Response:
    """Pause the dispatch queue."""
    db = request.app["db"]
    await db.set_queue_paused(True)

    # Webhook: paused
    try:
        from . import notifications
        active = await db.count_active()
        queued = await db.count_queued()
        await notifications.notify_paused(active, queued)
    except Exception:
        pass

    return web.json_response({"ok": True, "queue_paused": True})


async def resume_handler(request: web.Request) -> web.Response:
    """Resume the dispatch queue."""
    db = request.app["db"]
    await db.set_queue_paused(False)
    return web.json_response({"ok": True, "queue_paused": False})


async def start_dashboard(db: Database, config: Config):
    """Start the dashboard web server. Runs until cancelled."""
    app = web.Application(middlewares=[auth_middleware(config.dashboard_password)])
    app["db"] = db
    app["config"] = config
    app["github_owner"] = config.github_owner

    app.router.add_get("/", index_handler)
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
    site = web.TCPSite(runner, "0.0.0.0", config.dashboard_port)
    await site.start()
    log.info("Dashboard running on port %d", config.dashboard_port)

    try:
        while True:
            await asyncio.sleep(3600)
    except asyncio.CancelledError:
        pass
    finally:
        await runner.cleanup()
        log.info("Dashboard stopped")

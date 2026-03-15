"""Real-time web dashboard for Backporcher — aiohttp + SSE, no frontend build step."""

import asyncio
import base64
import json
import logging
import secrets
from datetime import datetime, timezone

from aiohttp import web

from .config import Config
from .db import Database

log = logging.getLogger("backporcher.dashboard")

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
<style>
  :root {
    --bg: #0d1117; --bg2: #161b22; --border: #30363d;
    --fg: #c9d1d9; --fg2: #8b949e; --fg3: #58a6ff;
    --green: #3fb950; --red: #f85149; --yellow: #d29922;
    --blue: #58a6ff; --purple: #bc8cff; --cyan: #39d353;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    background: var(--bg); color: var(--fg);
    font-family: 'SF Mono', 'Cascadia Code', 'Fira Code', monospace;
    font-size: 13px; line-height: 1.5; padding: 16px;
  }
  h1 { font-size: 18px; color: var(--fg3); margin-bottom: 16px; }
  h2 { font-size: 14px; color: var(--fg2); margin-bottom: 8px; }

  .header {
    display: flex; align-items: center; gap: 24px;
    padding: 12px 16px; background: var(--bg2);
    border: 1px solid var(--border); border-radius: 6px;
    margin-bottom: 16px; flex-wrap: wrap;
  }
  .header .title { font-size: 16px; font-weight: bold; color: var(--fg3); }
  .header .stat {
    display: flex; flex-direction: column; align-items: center;
    min-width: 60px;
  }
  .header .stat .value { font-size: 24px; font-weight: bold; }
  .header .stat .label { font-size: 11px; color: var(--fg2); text-transform: uppercase; }
  .header .stat.active .value { color: var(--green); }
  .header .stat.queued .value { color: var(--yellow); }
  .header .stat.done .value { color: var(--fg2); }
  .header .stat.failed .value { color: var(--red); }

  .status-dot {
    display: inline-block; width: 8px; height: 8px;
    border-radius: 50%; margin-right: 4px;
  }
  .status-dot.connected { background: var(--green); }
  .status-dot.disconnected { background: var(--red); }

  .repos {
    display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
    gap: 12px; margin-bottom: 16px;
  }
  .repo-card {
    background: var(--bg2); border: 1px solid var(--border);
    border-radius: 6px; padding: 12px;
  }
  .repo-card h3 {
    font-size: 13px; color: var(--fg3); margin-bottom: 8px;
    border-bottom: 1px solid var(--border); padding-bottom: 4px;
  }
  .repo-card .breakdown { font-size: 12px; }
  .repo-card .breakdown div {
    display: flex; justify-content: space-between; padding: 1px 0;
  }

  table {
    width: 100%; border-collapse: collapse;
    background: var(--bg2); border: 1px solid var(--border);
    border-radius: 6px; overflow: hidden;
  }
  th {
    text-align: left; padding: 8px 12px;
    background: var(--bg); color: var(--fg2);
    font-size: 11px; text-transform: uppercase;
    border-bottom: 1px solid var(--border);
  }
  td {
    padding: 6px 12px; border-bottom: 1px solid var(--border);
    font-size: 12px; white-space: nowrap;
  }
  tr:last-child td { border-bottom: none; }
  tr:hover { background: rgba(88,166,255,0.04); }

  .badge {
    display: inline-block; padding: 2px 6px;
    border-radius: 3px; font-size: 11px;
    font-weight: bold; letter-spacing: 0.5px;
  }
  .badge.wait { background: rgba(210,153,34,0.15); color: var(--yellow); }
  .badge.run { background: rgba(63,185,80,0.15); color: var(--green); }
  .badge.pr { background: rgba(88,166,255,0.15); color: var(--blue); }
  .badge.rev { background: rgba(188,140,255,0.15); color: var(--purple); }
  .badge.rvwd { background: rgba(188,140,255,0.15); color: var(--purple); }
  .badge.ok { background: rgba(57,211,83,0.15); color: var(--cyan); }
  .badge.rty { background: rgba(210,153,34,0.15); color: var(--yellow); }
  .badge.done { background: rgba(139,148,158,0.1); color: var(--fg2); }
  .badge.fail { background: rgba(248,81,73,0.15); color: var(--red); }
  .badge.cxl { background: rgba(139,148,158,0.1); color: var(--fg2); }
  .badge.aprv { background: rgba(210,153,34,0.2); color: var(--yellow); }
  .badge.gate { background: rgba(210,153,34,0.2); color: var(--yellow); }
  .badge.hold { background: rgba(248,81,73,0.1); color: var(--red); }

  .btn-approve {
    background: rgba(210,153,34,0.2); color: var(--yellow);
    border: 1px solid var(--yellow); border-radius: 4px;
    padding: 2px 8px; font-size: 11px; cursor: pointer;
    font-family: inherit;
  }
  .btn-approve:hover { background: rgba(210,153,34,0.4); }

  .btn-pause {
    background: rgba(248,81,73,0.15); color: var(--red);
    border: 1px solid var(--red); border-radius: 4px;
    padding: 4px 12px; font-size: 11px; cursor: pointer;
    font-family: inherit; margin-left: 8px;
  }
  .btn-pause:hover { background: rgba(248,81,73,0.3); }
  .btn-pause.resume { background: rgba(63,185,80,0.15); color: var(--green); border-color: var(--green); }
  .btn-pause.resume:hover { background: rgba(63,185,80,0.3); }

  .paused-indicator {
    background: rgba(248,81,73,0.2); color: var(--red);
    padding: 4px 12px; border-radius: 4px; font-weight: bold;
    font-size: 13px;
  }

  .model { color: var(--fg2); }
  .model.opus { color: var(--purple); }
  .model.sonnet { color: var(--blue); }
  .model.haiku { color: var(--cyan); }

  a { color: var(--fg3); text-decoration: none; }
  a:hover { text-decoration: underline; }

  .elapsed { color: var(--yellow); }
  .title-col {
    max-width: 400px; overflow: hidden;
    text-overflow: ellipsis; white-space: nowrap;
  }

  .footer {
    margin-top: 16px; font-size: 11px; color: var(--fg2);
    display: flex; justify-content: space-between; align-items: center;
  }
  .section { margin-bottom: 16px; }
  .empty { color: var(--fg2); font-style: italic; padding: 16px; text-align: center; }

  @media (max-width: 768px) {
    body { padding: 8px; font-size: 12px; }
    .header { gap: 12px; }
    td, th { padding: 4px 8px; }
    .title-col { max-width: 200px; }
  }
</style>
</head>
<body>

<div class="header">
  <span class="title">BACKPORCHER</span>
  <div class="stat active">
    <span class="value" id="cnt-active">-</span>
    <span class="label">Running</span>
  </div>
  <div class="stat queued">
    <span class="value" id="cnt-queued">-</span>
    <span class="label">Queued</span>
  </div>
  <div class="stat">
    <span class="value" id="cnt-review">-</span>
    <span class="label">Review</span>
  </div>
  <div class="stat" style="--stat-color: var(--yellow);">
    <span class="value" id="cnt-awaiting" style="color:var(--yellow)">-</span>
    <span class="label">Awaiting</span>
  </div>
  <div class="stat done">
    <span class="value" id="cnt-done">-</span>
    <span class="label">Done</span>
  </div>
  <div class="stat failed">
    <span class="value" id="cnt-failed">-</span>
    <span class="label">Failed</span>
  </div>
  <span id="paused-box" style="display:none"><span class="paused-indicator">PAUSED</span></span>
  <button id="pause-btn" class="btn-pause" onclick="togglePause()">Pause Queue</button>
  <span style="margin-left:auto; font-size:11px; color:var(--fg2)">
    <span class="status-dot disconnected" id="sse-dot"></span>
    <span id="sse-label">connecting...</span>
  </span>
</div>

<div class="section">
  <h2>Repos</h2>
  <div class="repos" id="repos"></div>
</div>

<div class="section">
  <h2>Active Agents</h2>
  <div id="active-table"></div>
</div>

<div class="section">
  <h2>Pipeline</h2>
  <div id="pipeline-table"></div>
</div>

<div class="section">
  <h2>Recent Completions / Failures</h2>
  <div id="recent-table"></div>
</div>

<div class="footer">
  <span id="last-update">-</span>
  <span>Backporcher Agent Dispatcher</span>
</div>

<script>
const BADGES = {
  queued:"WAIT", working:" RUN", pr_created:"  PR", reviewing:" REV",
  reviewed:"RVWD", ci_passed:"  OK", retrying:" RTY",
  completed:"DONE", failed:"FAIL", cancelled:" CXL"
};
const BADGE_CLS = {
  queued:"wait", working:"run", pr_created:"pr", reviewing:"rev",
  reviewed:"rvwd", ci_passed:"ok", retrying:"rty",
  completed:"done", failed:"fail", cancelled:"cxl"
};

const HOLD_BADGE = {
  merge_approval: {cls:"aprv", label:"APRV"},
  dispatch_approval: {cls:"gate", label:"GATE"},
  user_hold: {cls:"hold", label:"HOLD"},
  conflict_hold: {cls:"hold", label:"CNFL"},
};

let _queuePaused = false;
let _githubOwner = '';

function badge(status, hold) {
  if (hold && HOLD_BADGE[hold]) {
    const h = HOLD_BADGE[hold];
    return `<span class="badge ${h.cls}">${h.label}</span>`;
  }
  const cls = BADGE_CLS[status] || 'wait';
  const label = BADGES[status] || status;
  return `<span class="badge ${cls}">${label}</span>`;
}

function approveBtn(task) {
  if (!task.hold) return '';
  return ` <button class="btn-approve" onclick="approveTask(${task.id})">Approve</button>`;
}

async function approveTask(id) {
  try {
    const res = await fetch('/api/tasks/' + id + '/approve', {method:'POST'});
    if (!res.ok) {
      const d = await res.json();
      alert('Approve failed: ' + (d.error || res.statusText));
    }
  } catch(e) { alert('Approve error: ' + e); }
}

async function togglePause() {
  const url = _queuePaused ? '/api/resume' : '/api/pause';
  try {
    const res = await fetch(url, {method:'POST'});
    if (!res.ok) alert('Pause/resume failed: ' + res.statusText);
  } catch(e) { alert('Error: ' + e); }
}

function modelTag(m) {
  return `<span class="model ${m || ''}">${m || '-'}</span>`;
}

function issueLink(task) {
  if (!task.github_issue_number) return '-';
  const repo = task.repo_name || '';
  return `<a href="https://github.com/${_githubOwner}/${repo}/issues/${task.github_issue_number}" target="_blank">#${task.github_issue_number}</a>`;
}

function prLink(task) {
  if (!task.pr_url) return '-';
  return `<a href="${task.pr_url}" target="_blank">PR#${task.pr_number || '?'}</a>`;
}

function renderRepos(repoCountsMap) {
  const el = document.getElementById('repos');
  if (!Object.keys(repoCountsMap).length) {
    el.innerHTML = '<div class="empty">No repos registered</div>';
    return;
  }
  el.innerHTML = Object.entries(repoCountsMap).map(([name, counts]) => {
    const lines = Object.entries(counts)
      .sort((a,b) => b[1]-a[1])
      .map(([s,n]) => `<div>${badge(s)} <span>${n}</span></div>`)
      .join('');
    return `<div class="repo-card"><h3>${name}</h3><div class="breakdown">${lines}</div></div>`;
  }).join('');
}

function renderTable(containerId, tasks, columns) {
  const el = document.getElementById(containerId);
  if (!tasks.length) {
    el.innerHTML = '<div class="empty">None</div>';
    return;
  }
  const ths = columns.map(c => `<th>${c.label}</th>`).join('');
  const rows = tasks.map(t => {
    const tds = columns.map(c => `<td>${c.render(t)}</td>`).join('');
    return `<tr>${tds}</tr>`;
  }).join('');
  el.innerHTML = `<table><thead><tr>${ths}</tr></thead><tbody>${rows}</tbody></table>`;
}

const activeCols = [
  {label:'ID', render: t => `#${t.id}`},
  {label:'Status', render: t => badge(t.status, t.hold)},
  {label:'Repo', render: t => t.repo_name},
  {label:'Issue', render: issueLink},
  {label:'Model', render: t => modelTag(t.model)},
  {label:'Elapsed', render: t => t.elapsed ? `<span class="elapsed">${t.elapsed}</span>` : '-'},
  {label:'Title', render: t => `<span class="title-col">${(t.title||'').replace(/</g,'&lt;')}</span>`},
  {label:'Actions', render: t => approveBtn(t)},
];

const pipelineCols = [
  {label:'ID', render: t => `#${t.id}`},
  {label:'Status', render: t => badge(t.status, t.hold)},
  {label:'Repo', render: t => t.repo_name},
  {label:'Issue', render: issueLink},
  {label:'PR', render: prLink},
  {label:'Model', render: t => modelTag(t.model)},
  {label:'Retries', render: t => t.retry_count || '0'},
  {label:'Title', render: t => `<span class="title-col">${(t.title||'').replace(/</g,'&lt;')}</span>`},
  {label:'Actions', render: t => approveBtn(t)},
];

const recentCols = [
  {label:'ID', render: t => `#${t.id}`},
  {label:'Status', render: t => badge(t.status, t.hold)},
  {label:'Repo', render: t => t.repo_name},
  {label:'Issue', render: issueLink},
  {label:'PR', render: prLink},
  {label:'Error', render: t => {
    const msg = t.error_message || '';
    return `<span class="title-col">${msg.substring(0,80).replace(/</g,'&lt;')}</span>`;
  }},
];

function update(data) {
  const c = data.counts || {};
  document.getElementById('cnt-active').textContent = (c.working||0);
  document.getElementById('cnt-queued').textContent = (c.queued||0);
  document.getElementById('cnt-review').textContent =
    (c.pr_created||0) + (c.reviewing||0) + (c.reviewed||0);
  document.getElementById('cnt-awaiting').textContent = (data.held_count||0);
  document.getElementById('cnt-done').textContent = (c.completed||0);
  document.getElementById('cnt-failed').textContent = (c.failed||0);

  // Pause state
  _queuePaused = !!data.queue_paused;
  _githubOwner = data.github_owner || '';
  const pausedBox = document.getElementById('paused-box');
  const pauseBtn = document.getElementById('pause-btn');
  if (_queuePaused) {
    pausedBox.style.display = '';
    pauseBtn.textContent = 'Resume Queue';
    pauseBtn.className = 'btn-pause resume';
  } else {
    pausedBox.style.display = 'none';
    pauseBtn.textContent = 'Pause Queue';
    pauseBtn.className = 'btn-pause';
  }

  renderRepos(data.repo_counts || {});

  const tasks = data.tasks || [];
  const activeStatuses = new Set(['working','queued']);
  const pipelineStatuses = new Set(['pr_created','reviewing','reviewed','ci_passed','retrying']);
  const recentStatuses = new Set(['completed','failed','cancelled']);

  renderTable('active-table', tasks.filter(t => activeStatuses.has(t.status)), activeCols);
  renderTable('pipeline-table', tasks.filter(t => pipelineStatuses.has(t.status)), pipelineCols);
  renderTable('recent-table', tasks.filter(t => recentStatuses.has(t.status)).slice(0,20), recentCols);

  const ts = data.timestamp ? new Date(data.timestamp).toLocaleTimeString() : '-';
  document.getElementById('last-update').textContent = `Last update: ${ts}`;
}

function connect() {
  const dot = document.getElementById('sse-dot');
  const label = document.getElementById('sse-label');
  const es = new EventSource('/events');

  es.onopen = () => {
    dot.className = 'status-dot connected';
    label.textContent = 'live';
  };
  es.onmessage = (e) => {
    try { update(JSON.parse(e.data)); }
    catch(err) { console.error('SSE parse error', err); }
  };
  es.onerror = () => {
    dot.className = 'status-dot disconnected';
    label.textContent = 'reconnecting...';
    es.close();
    setTimeout(connect, 3000);
  };
}

connect();
</script>
</body>
</html>
"""


async def approve_handler(request: web.Request) -> web.Response:
    """Clear hold on a task, allowing it to proceed."""
    db = request.app["db"]
    task_id = int(request.match_info["id"])
    task = await db.get_task(task_id)
    if not task:
        return web.json_response({"error": "not found"}, status=404)
    if not task.get("hold"):
        return web.json_response({"error": "no hold on this task"}, status=400)

    await db.clear_hold(task_id)
    await db.add_log(task_id, f"Hold '{task['hold']}' cleared via dashboard")
    task = await db.get_task(task_id)
    return web.json_response({"ok": True, "task": task})


async def pause_handler(request: web.Request) -> web.Response:
    """Pause the dispatch queue."""
    db = request.app["db"]
    await db.set_queue_paused(True)
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
    app["github_owner"] = config.github_owner

    app.router.add_get("/", index_handler)
    app.router.add_get("/api/status", status_handler)
    app.router.add_get("/api/tasks", tasks_handler)
    app.router.add_get("/api/tasks/{id}", task_detail_handler)
    app.router.add_get("/events", sse_handler)
    app.router.add_post("/api/tasks/{id}/approve", approve_handler)
    app.router.add_post("/api/pause", pause_handler)
    app.router.add_post("/api/resume", resume_handler)

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

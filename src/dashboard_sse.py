"""Dashboard SSE, status, and stats handlers."""

import asyncio
import json
import logging
from datetime import datetime, timezone

from aiohttp import web

from .dashboard import _is_worker_alive, _worker_proc
from .db import Database

log = logging.getLogger("backporcher.dashboard")


async def _build_status(db: Database) -> dict:
    """Build full status payload from the database."""
    async with db.db.execute(
        "SELECT t.id, t.status, t.model, t.github_issue_number, t.started_at, "
        "t.completed_at, t.pr_url, t.pr_number, t.priority, t.depends_on_task_id, "
        "t.retry_count, t.error_message, t.branch_name, t.hold, "
        "t.agent_started_at, t.agent_finished_at, t.model_used, t.initial_model, "
        "t.agent, "
        "t.repo_id, "
        "r.name as repo_name, "
        "substr(t.prompt, 1, 120) as title "
        "FROM tasks t JOIN repos r ON t.repo_id = r.id "
        "ORDER BY t.created_at DESC"
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
            except (ValueError, TypeError, OverflowError):
                row["elapsed"] = "?"
                row["elapsed_seconds"] = 0
        else:
            row["elapsed"] = None
            row["elapsed_seconds"] = 0
        # Ensure agent field always has a value
        if not row.get("agent"):
            row["agent"] = "claude"

    # Check global pause
    queue_paused = await db.is_queue_paused()

    # Count held tasks
    held_count = sum(1 for row in rows if row.get("hold"))

    # All registered repo names (so repos with 0 tasks still show)
    all_repos = await db.list_repos()
    repo_names = [r["name"] for r in all_repos]

    return {
        "counts": counts,
        "repo_counts": repo_counts,
        "tasks": rows,
        "timestamp": now.isoformat(),
        "queue_paused": queue_paused,
        "held_count": held_count,
        "repo_names": repo_names,
    }


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


async def stats_handler(request: web.Request) -> web.Response:
    """JSON stats endpoint — mirrors CLI stats but server-side computed."""
    db = request.app["db"]

    now = datetime.now(timezone.utc)

    # All tasks (exclude cancelled) — use direct SQL for efficiency
    async with db.db.execute(
        "SELECT t.status, t.model, t.agent, t.created_at, t.completed_at, "
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
        except (ValueError, TypeError):
            return None

    completed = [t for t in tasks if t["status"] == "completed"]
    failed = [t for t in tasks if t["status"] == "failed"]
    total = len(tasks)
    n_completed = len(completed)
    n_failed = len(failed)

    # Issue->merge times
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

    # Agent/backend breakdown
    agent_counts = {}
    for t in tasks:
        a = t.get("agent") or "claude"
        agent_counts[a] = agent_counts.get(a, 0) + 1

    # Escalations
    escalations = sum(
        1 for t in tasks if t.get("initial_model") and t.get("model_used") and t["initial_model"] != t["model_used"]
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

    return web.json_response(
        {
            "stats": {
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
                "agents": agent_counts,
                "escalations": escalations,
                "recent_7d": {
                    "completed": len(recent_completed),
                    "failed": len(recent_failed),
                    "avg_merge_seconds": round(recent_avg_merge) if recent_avg_merge else None,
                },
                "repos": repo_stats,
            }
        }
    )

"""Dashboard operational handlers — dispatch, create, worker control, pause/resume."""

import asyncio
import json
import logging
from datetime import datetime, timezone

from aiohttp import web

from .dashboard import (
    _dispatching,
    _embedded_mode,
    _is_worker_alive,
    _start_worker,
    _stop_worker,
    _worker_log_lines,
    _worker_proc,
)

log = logging.getLogger("backporcher.dashboard")


async def dispatch_single_handler(request: web.Request) -> web.Response:
    """Dispatch a single task immediately — runs agent in background without needing the full worker."""
    try:
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
            return web.json_response(
                {"ok": False, "error": f"cannot dispatch task in status={task['status']}"}, status=400
            )

        # If failed, reset to queued first
        if task["status"] == "failed":
            await db.update_task(
                task_id,
                status="queued",
                error_message=None,
                started_at=None,
                completed_at=None,
                branch_name=None,
                worktree_path=None,
                pr_url=None,
                pr_number=None,
                review_summary=None,
                exit_code=None,
                agent_pid=None,
                output_summary=None,
                hold=None,
                agent_started_at=None,
                agent_finished_at=None,
            )
            await db.add_log(task_id, "Reset to queued for single dispatch via dashboard")

        # Claim it (clear any hold so the task actually runs)
        now = datetime.now(timezone.utc).isoformat()
        await db.update_task(task_id, status="working", started_at=now, hold=None)
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
                    await db.update_task(
                        task_id,
                        status="failed",
                        error_message="Single dispatch error",
                        completed_at=datetime.now(timezone.utc).isoformat(),
                    )
                    await db.add_log(task_id, "Single dispatch failed", level="error")
                except Exception:
                    log.exception("Failed to record dispatch failure for task %d", task_id)
            finally:
                _dispatching.discard(task_id)

        asyncio.create_task(_run())

        return web.json_response({"ok": True, "task_id": task_id, "action": "dispatch"})
    except Exception as e:
        log.exception("dispatch_single_handler error")
        return web.json_response({"ok": False, "error": str(e)}, status=500)


async def create_task_handler(request: web.Request) -> web.Response:
    """Create a new task manually from the dashboard.

    Accepts JSON: {"repo": "name", "prompt": "...", "model": "sonnet", "priority": 100}
    """
    db = request.app["db"]
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        return web.json_response({"ok": False, "error": "invalid JSON"}, status=400)

    repo_name = body.get("repo")
    prompt = body.get("prompt", "").strip()
    model = body.get("model", "sonnet")
    agent = body.get("agent", "claude").lower()
    try:
        priority = int(body.get("priority", 100))
    except (ValueError, TypeError):
        priority = 100

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
    updates = {}
    if priority != 100:
        updates["priority"] = priority
    if agent != "claude":
        updates["agent"] = agent
    if updates:
        await db.update_task(task_id, **updates)
    await db.add_log(task_id, f"Created manually via dashboard (agent={agent}, model={model})")

    return web.json_response({"ok": True, "task_id": task_id})


async def worker_start_handler(request: web.Request) -> web.Response:
    """Start the worker daemon subprocess."""
    if _embedded_mode:
        return web.json_response(
            {"ok": False, "message": "Worker is built-in (embedded mode) — cannot start separately"}
        )
    config = request.app["config"]
    ok, msg = await _start_worker(config)
    return web.json_response({"ok": ok, "message": msg})


async def worker_stop_handler(request: web.Request) -> web.Response:
    """Stop the worker daemon subprocess."""
    if _embedded_mode:
        return web.json_response(
            {"ok": False, "message": "Worker is built-in (embedded mode) — cannot stop separately"}
        )
    ok, msg = await _stop_worker()
    return web.json_response({"ok": ok, "message": msg})


async def worker_status_handler(request: web.Request) -> web.Response:
    """Worker status and recent log lines."""
    import os

    alive = _is_worker_alive()
    pid = os.getpid() if _embedded_mode else (_worker_proc.pid if _worker_proc and alive else None)
    return web.json_response(
        {
            "running": alive,
            "embedded": _embedded_mode,
            "pid": pid,
            "log": _worker_log_lines[-50:],
        }
    )


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
        log.warning("Failed to send pause notification", exc_info=True)

    return web.json_response({"ok": True, "queue_paused": True})


async def resume_handler(request: web.Request) -> web.Response:
    """Resume the dispatch queue."""
    db = request.app["db"]
    await db.set_queue_paused(False)
    return web.json_response({"ok": True, "queue_paused": False})

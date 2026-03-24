"""Dashboard task action handlers — approve, hold, reject, edit, requeue, escalate."""

import json
import logging
from datetime import datetime, timezone

from aiohttp import web

log = logging.getLogger("backporcher.dashboard")


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
        return web.json_response(
            {"ok": False, "error": f"cannot hold terminal task (status={task['status']})"}, status=400
        )
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
                [
                    "gh",
                    "issue",
                    "edit",
                    "--repo",
                    repo_full,
                    str(issue_num),
                    "--add-label",
                    "backporcher",
                    "--remove-label",
                    "backporcher-in-progress",
                ],
                capture_output=True,
            )

    return web.json_response({"ok": True, "task_id": task_id, "action": "reject"})


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
        return web.json_response(
            {"ok": False, "error": f"cannot edit task in status={task['status']} (must be queued, failed, or held)"},
            status=400,
        )

    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        return web.json_response({"ok": False, "error": "invalid JSON body"}, status=400)

    updates = {}
    if "prompt" in body and body["prompt"]:
        updates["prompt"] = str(body["prompt"])[:10000]
    if "model" in body and body["model"] in ("sonnet", "opus", "haiku"):
        updates["model"] = body["model"]
    if "agent" in body and body["agent"]:
        updates["agent"] = str(body["agent"]).lower()
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
    action_desc = ", ".join(
        f"{k}={v!r:.60}"
        for k, v in updates.items()
        if k
        not in (
            "error_message",
            "started_at",
            "completed_at",
            "branch_name",
            "worktree_path",
            "pr_url",
            "pr_number",
            "review_summary",
        )
    )
    await db.add_log(task_id, f"Edited via dashboard: {action_desc}")

    return web.json_response(
        {"ok": True, "task_id": task_id, "action": "edit", "updates": {k: str(v)[:100] for k, v in updates.items()}}
    )


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
        return web.json_response(
            {
                "ok": False,
                "error": f"cannot requeue task in status={task['status']} (must be failed, completed, or cancelled)",
            },
            status=400,
        )

    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
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
    if "agent" in body and body["agent"]:
        updates["agent"] = str(body["agent"]).lower()
    if "prompt" in body and body["prompt"]:
        updates["prompt"] = str(body["prompt"])[:10000]

    await db.update_task(task_id, **updates)
    model = updates.get("model", task["model"])
    agent = updates.get("agent", task.get("agent", "claude"))
    await db.add_log(task_id, f"Re-queued via dashboard (agent={agent}, model={model})")

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
        return web.json_response(
            {"ok": False, "error": f"cannot escalate task in status={task['status']} (must be queued or working)"},
            status=400,
        )

    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        body = {}

    target_model = body.get("model", "opus")
    if target_model not in ("sonnet", "opus", "haiku"):
        return web.json_response({"ok": False, "error": f"invalid model: {target_model}"}, status=400)

    if task["model"] == target_model:
        return web.json_response({"ok": False, "error": f"task already uses {target_model}"}, status=400)

    old_model = task["model"]
    await db.update_task(task_id, model=target_model)
    await db.add_log(task_id, f"Model escalated: {old_model} -> {target_model} via dashboard")

    return web.json_response(
        {"ok": True, "task_id": task_id, "action": "escalate", "from": old_model, "to": target_model}
    )

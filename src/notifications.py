"""Webhook notifications — fire-and-forget with 5s timeout."""

import asyncio
import json
import logging
import socket
from urllib.request import Request, urlopen

from .config import Config

log = logging.getLogger("backporcher.notifications")

# Module-level config cache — set once from worker startup
_config: Config | None = None


def init(config: Config):
    """Initialize the notification module with config."""
    global _config
    _config = config


async def send_webhook(event: str, payload: dict):
    """Post a webhook notification. No-op if webhook URL is not configured.

    Fire-and-forget: never blocks the pipeline, never raises.
    """
    if not _config or not _config.webhook_url:
        return
    if event not in _config.webhook_events:
        return

    try:
        # Build message text
        text = payload.get("text", "")

        # Send both text (Slack) and content (Discord) fields
        body = json.dumps({
            "text": text,
            "content": text,
        }).encode("utf-8")

        # Run in executor to avoid blocking the event loop
        loop = asyncio.get_running_loop()
        await asyncio.wait_for(
            loop.run_in_executor(None, _post_webhook, _config.webhook_url, body),
            timeout=5.0,
        )
    except asyncio.TimeoutError:
        log.warning("Webhook timed out for event=%s", event)
    except Exception:
        log.warning("Webhook failed for event=%s", event, exc_info=True)


def _post_webhook(url: str, body: bytes):
    """Synchronous POST to webhook URL."""
    req = Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    urlopen(req, timeout=5)


def _dashboard_url() -> str | None:
    """Construct dashboard URL if dashboard is active."""
    if not _config or not _config.dashboard_password:
        return None
    hostname = socket.gethostname()
    return f"http://{hostname}:{_config.dashboard_port}"


async def notify_hold(task_id: int, title: str, hold_type: str):
    """Notify that a task is awaiting approval."""
    dashboard = _dashboard_url()
    parts = [f"Task #{task_id} awaiting {hold_type.replace('_', ' ')}: *{title[:80]}*"]
    if dashboard:
        parts.append(f"[Dashboard]({dashboard})")
    parts.append(f"`backporcher approve {task_id}`")
    text = " \u2014 ".join(parts)
    await send_webhook("hold", {"text": text, "task_id": task_id})


async def notify_failed(task_id: int, title: str, reason: str):
    """Notify that a task has failed."""
    text = f"Task #{task_id} failed: *{title[:80]}* \u2014 {reason[:200]}"
    await send_webhook("failed", {"text": text, "task_id": task_id})


async def notify_completed(task_id: int, title: str, duration_str: str, model: str):
    """Notify that a task has been merged."""
    text = f"Task #{task_id} merged: *{title[:80]}* ({duration_str}, {model})"
    await send_webhook("completed", {"text": text, "task_id": task_id})


async def notify_paused(active: int, queued: int):
    """Notify that the queue has been paused."""
    text = f"Queue paused. {active} tasks in-flight, {queued} queued."
    await send_webhook("paused", {"text": text})

"""Gemini CLI agent backend."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from ..constants import SENSITIVE_ENV_VARS
from . import AgentEvent

log = logging.getLogger("backporcher.backends")

# Claude model names that should never be forwarded to the Gemini CLI.
_CLAUDE_MODELS: frozenset[str] = frozenset({"sonnet", "opus", "haiku"})


class GeminiBackend:
    """Backend that drives ``gemini -p`` (Gemini CLI) as the agent."""

    name: str = "gemini"

    def __init__(self, api_key: str = "") -> None:
        self._api_key = api_key

    def build_command(self, prompt: str, model: str, worktree_path: Path) -> list[str]:
        """Return the argv for a non-interactive Gemini CLI agent run.

        Gemini uses ``-p`` for headless mode, ``-y`` for auto-approve (yolo),
        and ``--output-format stream-json`` for structured JSONL output.
        Working directory is set via subprocess ``cwd=`` by the caller.
        """
        cmd = [
            "gemini",
            "-p",
            prompt,
            "-y",
            "--output-format",
            "stream-json",
        ]
        # Only pass explicit Gemini model IDs — skip Claude model names
        # and Gemini's own default placeholders.
        if model and model not in ("gemini", "auto") and model not in _CLAUDE_MODELS:
            cmd.extend(["-m", model])
        return cmd

    def build_env(self, base_env: dict[str, str]) -> dict[str, str]:
        """Return a cleaned copy of *base_env* with sensitive variables removed."""
        env = {k: v for k, v in base_env.items() if k not in SENSITIVE_ENV_VARS}
        if self._api_key:
            env["GEMINI_API_KEY"] = self._api_key
        return env

    def parse_output_line(self, line: str) -> AgentEvent | None:
        """Parse one JSON line from ``gemini --output-format stream-json``.

        Gemini emits these event types:

        - ``type="init"`` — session metadata (skipped)
        - ``type="message", role="user"`` — echo of the prompt (skipped)
        - ``type="message", role="assistant"`` — assistant text output
        - ``type="result"`` — final completion with stats

        Tool call events and other types are logged at DEBUG and skipped.
        """
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            log.debug("GeminiBackend.parse_output_line: non-JSON line: %.120s", line)
            return None

        etype = event.get("type", "")

        if etype == "message":
            role = event.get("role", "")
            if role == "assistant":
                content = event.get("content", "")
                if isinstance(content, str):
                    return AgentEvent(type="assistant", content=content, raw=event)
            # Skip user messages and tool calls
            return None

        if etype == "result":
            status = event.get("status", "")
            is_error = status != "success"
            # Result doesn't carry text content — just stats
            return AgentEvent(type="result", content="", is_error=is_error, raw=event)

        if etype in ("init", "tool_call", "tool_result"):
            log.debug("GeminiBackend.parse_output_line: skipping %s event", etype)
            return None

        log.debug("GeminiBackend.parse_output_line: unrecognised type %r", etype)
        return None

    def display_model(self, task_model: str) -> str:
        """Prefix with 'gemini/' for dashboard display."""
        return f"gemini/{task_model}"

    def required_env_vars(self) -> dict[str, str]:
        """Return env vars required by the Gemini CLI."""
        if self._api_key:
            return {"GEMINI_API_KEY": self._api_key}
        return {}

"""OpenAI Codex CLI agent backend."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from ..constants import SENSITIVE_ENV_VARS
from . import AgentEvent

log = logging.getLogger("backporcher.backends")


class CodexBackend:
    """Backend that drives `codex exec` (OpenAI Codex CLI) as the agent."""

    name: str = "codex"

    def __init__(self, api_key: str) -> None:
        self._api_key = api_key

    def build_command(self, prompt: str, model: str, worktree_path: Path) -> list[str]:
        """Return the argv for a non-interactive Codex agent run.

        Codex uses -C to set the working directory, so worktree_path is
        passed as a flag rather than via subprocess cwd=.
        """
        return [
            "codex",
            "exec",
            prompt,
            "--json",
            "--full-auto",
            "--skip-git-repo-check",
            "-C",
            str(worktree_path),
            "-m",
            model,
        ]

    def build_env(self, base_env: dict[str, str]) -> dict[str, str]:
        """
        Return a cleaned copy of *base_env* with sensitive variables removed
        and CODEX_API_KEY injected.
        """
        env = {k: v for k, v in base_env.items() if k not in SENSITIVE_ENV_VARS}
        env["CODEX_API_KEY"] = self._api_key
        return env

    def parse_output_line(self, line: str) -> AgentEvent | None:
        """
        Parse one JSONL line from `codex exec --json`.

        Recognised event types:
          - ``item.completed`` with ``item.type == "agent_message"`` — agent text output
          - ``turn.completed``  — final turn summary with usage info
          - ``turn.failed``     — error event

        Progress events (``thread.started``, ``turn.started``, ``item.started``)
        are logged at DEBUG level and return None.
        """
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            log.debug("CodexBackend.parse_output_line: non-JSON line: %.120s", line)
            return None

        etype = event.get("type", "")

        if etype == "item.completed":
            item = event.get("item") or {}
            if item.get("type") == "agent_message":
                text = item.get("text", "") or ""
                return AgentEvent(type="text", content=text, raw=event)
            log.debug("CodexBackend.parse_output_line: item.completed non-message item type %r", item.get("type"))
            return None

        if etype == "turn.completed":
            usage = event.get("usage") or {}
            content = f"input_tokens={usage.get('input_tokens', 0)} output_tokens={usage.get('output_tokens', 0)}"
            return AgentEvent(type="result", content=content, raw=event)

        if etype == "turn.failed":
            error = event.get("error", "") or ""
            return AgentEvent(type="error", content=error, is_error=True, raw=event)

        log.debug("CodexBackend.parse_output_line: unrecognised event type %r", etype)
        return None

    def display_model(self, task_model: str) -> str:
        """Prefix with 'codex/' for dashboard display."""
        return f"codex/{task_model}"

    def required_env_vars(self) -> dict[str, str]:
        """Return the CODEX_API_KEY required by this backend."""
        return {"CODEX_API_KEY": self._api_key}

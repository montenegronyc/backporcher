"""Kimi Code agent backend."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from ..constants import SENSITIVE_ENV_VARS
from . import AgentEvent

log = logging.getLogger("backporcher.backends")

_KIMI_DEFAULT_MODEL = "kimi"


class KimiBackend:
    """Backend that drives `kimi -p` (Kimi Code CLI) as the agent."""

    name: str = "kimi"

    def __init__(self, api_key: str) -> None:
        self._api_key = api_key

    def build_command(self, prompt: str, model: str, worktree_path: Path) -> list[str]:
        """Return the argv for a non-interactive Kimi Code agent run.

        Kimi accepts a working directory via ``-w``, so ``worktree_path`` is
        passed directly rather than relying on subprocess ``cwd=``.

        ``--print`` implies ``--yolo`` (auto-approve all tool calls).
        ``-y`` is passed explicitly for forward compatibility.
        """
        cmd = [
            "kimi",
            "-p",
            prompt,
            "--print",
            "--output-format",
            "stream-json",
            "-y",
            "-w",
            str(worktree_path),
        ]
        if model and model != _KIMI_DEFAULT_MODEL:
            cmd.extend(["-m", model])
        return cmd

    def build_env(self, base_env: dict[str, str]) -> dict[str, str]:
        """Return a cleaned copy of *base_env* with sensitive variables removed.

        Only injects KIMI_API_KEY when a real API key is configured.
        When using OAuth (file-based credentials), the CLI handles
        auth itself and injecting a placeholder key would override it.
        """
        env = {k: v for k, v in base_env.items() if k not in SENSITIVE_ENV_VARS}
        if self._api_key and not self._api_key.startswith("oauth"):
            env["KIMI_API_KEY"] = self._api_key
        return env

    def parse_output_line(self, line: str) -> AgentEvent | None:
        """Parse one JSON line from ``kimi --output-format stream-json``.

        Kimi uses a role-based schema instead of Claude's type-based schema:

        - ``role="assistant"`` with a string ``content`` — text output
        - ``role="assistant"`` with ``tool_calls`` — tool invocation (skipped)
        - ``role="tool"`` — tool result (skipped)

        The last ``{"role": "assistant", "content": "<text>"}`` line is the
        final response.  There is no explicit ``type="result"`` event.

        Unrecognised lines are logged at DEBUG level and return ``None``.
        """
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            log.debug("KimiBackend.parse_output_line: non-JSON line: %.120s", line)
            return None

        role = event.get("role", "")

        if role == "assistant":
            content = event.get("content", "")
            # When tool_calls are present, content is [] — skip those lines.
            if not isinstance(content, str):
                log.debug("KimiBackend.parse_output_line: skipping tool_calls event")
                return None
            return AgentEvent(type="assistant", content=content, raw=event)

        if role == "tool":
            # Tool result lines carry no user-visible text.
            log.debug("KimiBackend.parse_output_line: skipping tool result event")
            return None

        log.debug("KimiBackend.parse_output_line: unrecognised role %r", role)
        return None

    def display_model(self, task_model: str) -> str:
        """Prefix with 'kimi/' for dashboard display."""
        return f"kimi/{task_model}"

    def required_env_vars(self) -> dict[str, str]:
        """Return the ``KIMI_API_KEY`` required by the Kimi CLI, if any."""
        if self._api_key and not self._api_key.startswith("oauth"):
            return {"KIMI_API_KEY": self._api_key}
        return {}

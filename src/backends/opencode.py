"""OpenCode agent backend."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from ..constants import SENSITIVE_ENV_VARS
from . import AgentEvent

log = logging.getLogger("backporcher.backends")


class OpenCodeBackend:
    """Backend that drives ``opencode run`` as the agent.

    OpenCode connects to a configurable LLM endpoint (e.g. a local
    Qwen3.5-9B via OpenAI-compatible API).  The model is specified in
    ``provider/model`` format.
    """

    name: str = "opencode"

    def __init__(self, model: str = "") -> None:
        self._model = model

    def build_command(self, prompt: str, model: str, worktree_path: Path) -> list[str]:
        """Return the argv for a non-interactive OpenCode agent run.

        ``opencode run`` exits after completing the task.
        ``--format json`` gives JSONL event output.
        ``--dir`` sets the working directory.
        """
        cmd = [
            "opencode",
            "run",
            prompt,
            "--format",
            "json",
            "--dir",
            str(worktree_path),
        ]
        # Use configured model or the one passed by the dispatcher
        effective_model = self._model or model
        if effective_model and effective_model not in ("opencode", "auto"):
            cmd.extend(["--model", effective_model])
        return cmd

    def build_env(self, base_env: dict[str, str]) -> dict[str, str]:
        """Return a cleaned copy of *base_env* with sensitive variables removed."""
        return {k: v for k, v in base_env.items() if k not in SENSITIVE_ENV_VARS}

    def parse_output_line(self, line: str) -> AgentEvent | None:
        """Parse one JSON line from ``opencode run --format json``.

        OpenCode emits these event types:

        - ``type="step_start"`` — beginning of a processing step (skipped)
        - ``type="tool_use"`` — tool invocation with input/output (skipped)
        - ``type="assistant"`` — assistant message content
        - ``type="result"`` — final result with is_error, duration_ms, usage
        - ``type="error"`` — error event

        Unrecognised types are logged at DEBUG level and return None.
        """
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            log.debug("OpenCodeBackend.parse_output_line: non-JSON line: %.120s", line)
            return None

        etype = event.get("type", "")

        if etype == "assistant":
            content = event.get("content", "")
            if isinstance(content, str):
                return AgentEvent(type="assistant", content=content, raw=event)
            return None

        if etype == "result":
            is_error = bool(event.get("is_error"))
            content = event.get("result", "") or ""
            return AgentEvent(type="result", content=content, is_error=is_error, raw=event)

        if etype == "error":
            error = event.get("error", {})
            msg = error.get("data", {}).get("message", "") if isinstance(error, dict) else str(error)
            return AgentEvent(type="error", content=msg, is_error=True, raw=event)

        if etype in ("step_start", "tool_use"):
            log.debug("OpenCodeBackend.parse_output_line: skipping %s event", etype)
            return None

        log.debug("OpenCodeBackend.parse_output_line: unrecognised type %r", etype)
        return None

    def display_model(self, task_model: str) -> str:
        """Prefix with 'opencode/' and prefer the configured model if set."""
        label = self._model or task_model
        return f"opencode/{label}"

    def required_env_vars(self) -> dict[str, str]:
        """OpenCode uses its own config file for credentials — no env vars needed."""
        return {}

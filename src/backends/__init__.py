"""Agent backend abstraction: Protocol, AgentEvent, and backend discovery."""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable

log = logging.getLogger("backporcher.backends")


@dataclass
class AgentEvent:
    """Parsed event from an agent's stdout stream."""

    type: str
    content: str
    is_error: bool = False
    raw: dict = field(default_factory=dict)


@runtime_checkable
class AgentBackend(Protocol):
    """Protocol that every agent backend must satisfy."""

    name: str

    def build_command(self, prompt: str, model: str, worktree_path: Path) -> list[str]:
        """Return the subprocess argv for this backend."""
        ...

    def build_env(self, base_env: dict[str, str]) -> dict[str, str]:
        """Return a cleaned env dict suitable for the agent subprocess."""
        ...

    def parse_output_line(self, line: str) -> AgentEvent | None:
        """
        Parse one JSON line from the agent's stdout.

        Returns an AgentEvent on success, or None for lines that should be
        silently ignored (unrecognised event types are logged at DEBUG level).
        """
        ...

    def required_env_vars(self) -> dict[str, str]:
        """
        Return any env vars this backend requires that aren't already in the
        environment.  Return an empty dict if none are needed.
        """
        ...


def discover_backends(config) -> dict[str, AgentBackend]:
    """
    Return available backends as a name → backend dict.

    An agent is available if its CLI is in PATH and (for non-Claude backends)
    its API key is set in config.
    """
    from .claude import ClaudeBackend  # noqa: PLC0415

    backends: dict[str, AgentBackend] = {}

    # Claude Code — uses mounted credentials, no API key check.
    if shutil.which("claude") is not None:
        backends["claude"] = ClaudeBackend()
        log.debug("discover_backends: claude registered")

    # Kimi — needs CLI + API key.
    kimi_key = getattr(config, "kimi_api_key", "") or ""
    if kimi_key and shutil.which("kimi") is not None:
        from .kimi import KimiBackend  # noqa: PLC0415

        backends["kimi"] = KimiBackend(api_key=kimi_key)
        log.debug("discover_backends: kimi registered")

    # Codex — needs CLI + API key.
    codex_key = getattr(config, "codex_api_key", "") or ""
    if codex_key and shutil.which("codex") is not None:
        from .codex import CodexBackend  # noqa: PLC0415

        backends["codex"] = CodexBackend(api_key=codex_key)
        log.debug("discover_backends: codex registered")

    log.info("discover_backends: %d backend(s): %s", len(backends), list(backends.keys()))
    return backends

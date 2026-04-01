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

    def display_model(self, task_model: str) -> str:
        """
        Return a human-readable model string for dashboard display.

        For non-Claude backends, this typically prefixes the backend name
        (e.g. "gemini/auto", "opencode/qwen3.5-9b").  Claude returns
        the model as-is.
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

    # Kimi — needs CLI + (API key OR OAuth credentials on disk).
    kimi_key = getattr(config, "kimi_api_key", "") or ""
    kimi_has_oauth = Path.home().joinpath(".kimi", "credentials").is_dir()
    if shutil.which("kimi") is not None and (kimi_key or kimi_has_oauth):
        from .kimi import KimiBackend  # noqa: PLC0415

        backends["kimi"] = KimiBackend(api_key=kimi_key)
        log.debug(
            "discover_backends: kimi registered (auth=%s)",
            "api_key" if kimi_key and not kimi_key.startswith("oauth") else "oauth",
        )

    # Codex — needs CLI + (API key OR OAuth auth.json on disk).
    codex_key = getattr(config, "codex_api_key", "") or ""
    codex_has_oauth = Path.home().joinpath(".codex", "auth.json").is_file()
    if shutil.which("codex") is not None and (codex_key or codex_has_oauth):
        from .codex import CodexBackend  # noqa: PLC0415

        backends["codex"] = CodexBackend(api_key=codex_key)
        log.debug(
            "discover_backends: codex registered (auth=%s)",
            "api_key" if codex_key and not codex_key.startswith("oauth") else "oauth",
        )

    # Gemini CLI — needs CLI; API key optional (may use gcloud auth).
    gemini_key = getattr(config, "gemini_api_key", "") or ""
    if shutil.which("gemini") is not None:
        from .gemini import GeminiBackend  # noqa: PLC0415

        backends["gemini"] = GeminiBackend(api_key=gemini_key)
        log.debug("discover_backends: gemini registered")

    # OpenCode — needs CLI; uses its own config for LLM endpoint.
    if shutil.which("opencode") is not None:
        from .opencode import OpenCodeBackend  # noqa: PLC0415

        opencode_model = getattr(config, "opencode_model", "") or ""
        backends["opencode"] = OpenCodeBackend(model=opencode_model)
        log.debug("discover_backends: opencode registered")

    log.info("discover_backends: %d backend(s): %s", len(backends), list(backends.keys()))
    return backends

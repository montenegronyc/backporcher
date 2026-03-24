"""Claude Code agent backend."""

import json
import logging
from pathlib import Path

from ..constants import SENSITIVE_ENV_VARS
from . import AgentEvent

log = logging.getLogger("backporcher.backends")

# Additional env vars stripped beyond the shared SENSITIVE_ENV_VARS set.
# CLAUDECODE — prevents nested-session detection.
# SSH_* and GIT_ASKPASS / GIT_CREDENTIALS — credential helpers that must
# not leak into the sandboxed agent process.
_CLAUDE_STRIP_VARS: frozenset[str] = frozenset(
    {
        "CLAUDECODE",
        "SSH_AUTH_SOCK",
        "SSH_AGENT_PID",
        "GIT_ASKPASS",
        "GIT_CREDENTIALS",
    }
)


class ClaudeBackend:
    """Backend that drives `claude -p` (Claude Code CLI) as the agent."""

    name: str = "claude"

    def build_command(self, prompt: str, model: str, worktree_path: Path) -> list[str]:
        """Return the argv for a non-interactive Claude Code agent run.

        Claude uses cwd for working directory (set by the caller via
        subprocess cwd=), so worktree_path is not passed as a flag.
        """
        return [
            "claude",
            "-p",
            "--output-format",
            "stream-json",
            "--verbose",
            "--dangerously-skip-permissions",
            "--model",
            model,
            prompt,
        ]

    def build_env(self, base_env: dict[str, str]) -> dict[str, str]:
        """
        Return a cleaned copy of *base_env* with sensitive and agent-unsafe
        variables removed.

        The stripping logic mirrors what agent.py does in the non-sandboxed
        (no agent_user) code path.
        """
        strip = SENSITIVE_ENV_VARS | _CLAUDE_STRIP_VARS
        return {k: v for k, v in base_env.items() if k not in strip}

    def parse_output_line(self, line: str) -> AgentEvent | None:
        """
        Parse one JSON line from `claude -p --output-format stream-json`.

        Recognised event types:
          - ``assistant``           — message with content blocks
          - ``result``              — final result / error summary
          - ``content_block_delta`` — streaming text delta

        All other event types are logged at DEBUG level and return None so
        that callers can skip them cleanly.
        """
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            log.debug("ClaudeBackend.parse_output_line: non-JSON line: %.120s", line)
            return None

        etype = event.get("type", "")

        if etype == "assistant":
            # Extract text from message content blocks.
            msg = event.get("message") or {}
            texts: list[str] = []
            for block in msg.get("content") or []:
                if block.get("type") == "text":
                    texts.append(block["text"])
            return AgentEvent(type=etype, content="".join(texts), raw=event)

        if etype == "result":
            content = event.get("result", "") or ""
            is_error = bool(event.get("is_error"))
            return AgentEvent(type=etype, content=content, is_error=is_error, raw=event)

        if etype == "content_block_delta":
            delta = event.get("delta") or {}
            text = delta.get("text", "") if delta.get("type") == "text_delta" else ""
            return AgentEvent(type=etype, content=text, raw=event)

        log.debug("ClaudeBackend.parse_output_line: unrecognised event type %r", etype)
        return None

    def required_env_vars(self) -> dict[str, str]:
        """
        Claude Code uses mounted credentials, not env vars.
        No additional env vars are required.
        """
        return {}

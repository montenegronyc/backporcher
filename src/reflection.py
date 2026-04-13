"""Reflection: structured diagnosis before retry attempts.

After a task fails, a cheap haiku call diagnoses the root cause before
the retry agent runs.  The structured reflection output is:
  1. Fed into the retry prompt so the fix attempt is better informed
  2. Stored as a task log for observability
  3. Used to enrich repo learnings with actionable diagnosis
"""

from __future__ import annotations

import asyncio
import json
import logging
import os

from .config import Config
from .constants import (
    SENSITIVE_ENV_VARS,
    TRUNCATE_REASON,
    prlimit_args,
)
from .db import Database
from .prompts import REFLECTION_PROMPT_TEMPLATE

log = logging.getLogger("backporcher.reflection")

# Hard timeout for reflection call (should be fast — small prompt, haiku model)
TIMEOUT_REFLECTION = 60


async def run_reflection(
    task: dict,
    error_output: str,
    config: Config,
    db: Database,
) -> dict | None:
    """Run a haiku reflection call to diagnose a failure before retrying.

    Returns a structured dict on success::

        {
            "root_cause": "Missing import for datetime module",
            "error_pattern": "ModuleNotFoundError",
            "hypothesis": "Agent added datetime usage but forgot the import",
            "suggested_approach": "Add the missing import and re-run tests"
        }

    Returns None on any failure (timeout, bad JSON, process error).
    The caller should proceed with the retry regardless — reflection is
    advisory, never blocking.
    """
    task_id = task["id"]
    prompt_text = task.get("prompt", "")[:TRUNCATE_REASON]
    error_text = error_output[-3000:] if error_output else "(no error output)"

    prompt = REFLECTION_PROMPT_TEMPLATE.format(
        task_prompt=prompt_text,
        error_output=error_text,
        retry_count=task.get("retry_count", 0),
    )

    cmd = ["claude", "-p", "--output-format", "text", "--model", "haiku", prompt]

    if config.agent_user:
        cmd = [
            "sudo",
            "-u",
            config.agent_user,
            "--",
            *prlimit_args(),
            *cmd,
        ]
        agent_env = None
    else:
        _sensitive_vars = SENSITIVE_ENV_VARS | {
            "CLAUDECODE",
            "SSH_AUTH_SOCK",
            "SSH_AGENT_PID",
            "GIT_ASKPASS",
            "GIT_CREDENTIALS",
        }
        agent_env = {k: v for k, v in os.environ.items() if k not in _sensitive_vars}

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
        **({"env": agent_env} if agent_env is not None else {}),
    )

    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=TIMEOUT_REFLECTION)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        log.warning("Task #%d: reflection timed out", task_id)
        return None

    output = stdout.decode(errors="replace").strip()
    if proc.returncode != 0:
        log.warning("Task #%d: reflection failed (exit %d)", task_id, proc.returncode)
        return None

    # Strip markdown fences if present
    cleaned = output
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        cleaned = "\n".join(lines)

    try:
        result = json.loads(cleaned)
    except json.JSONDecodeError:
        log.warning("Task #%d: reflection returned invalid JSON: %s", task_id, cleaned[:TRUNCATE_REASON])
        return None

    if not isinstance(result, dict):
        log.warning("Task #%d: reflection returned non-dict: %s", task_id, type(result))
        return None

    # Validate expected keys (lenient — accept partial results)
    expected_keys = {"root_cause", "error_pattern", "hypothesis", "suggested_approach"}
    result = {k: str(v)[:500] for k, v in result.items() if k in expected_keys}

    if not result:
        log.warning("Task #%d: reflection returned no recognized keys", task_id)
        return None

    # Store reflection in task logs
    summary = (
        f"Reflection: {result.get('root_cause', 'unknown')} | "
        f"Pattern: {result.get('error_pattern', 'unknown')} | "
        f"Approach: {result.get('suggested_approach', 'unknown')}"
    )
    await db.add_log(task_id, summary[:500])
    log.info("Task #%d: reflection complete — %s", task_id, result.get("root_cause", "unknown")[:100])

    return result


def format_reflection_for_prompt(reflection: dict) -> str:
    """Format a reflection dict into a prompt section for the retry agent."""
    parts = ["## Failure Diagnosis (from previous attempt)\n"]
    if reflection.get("root_cause"):
        parts.append(f"**Root cause:** {reflection['root_cause']}")
    if reflection.get("error_pattern"):
        parts.append(f"**Error pattern:** {reflection['error_pattern']}")
    if reflection.get("hypothesis"):
        parts.append(f"**Why it failed:** {reflection['hypothesis']}")
    if reflection.get("suggested_approach"):
        parts.append(f"**Suggested fix:** {reflection['suggested_approach']}")
    parts.append("")
    return "\n".join(parts)

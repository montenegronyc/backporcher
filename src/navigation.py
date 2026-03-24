"""Navigation context generation: code graph -> sonnet -> file map for agents."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path

from .config import Config
from .constants import (
    NAV_MAX_EDGES,
    NAV_MAX_FILES,
    NAV_MAX_SYMBOLS_PER_FILE,
    TIMEOUT_NAVIGATION_MODEL,
    TRUNCATE_NAV_CONTEXT,
)
from .db import Database
from .prompts import NAVIGATION_PROMPT

log = logging.getLogger("backporcher.agent")


async def generate_navigation_context(
    task: dict,
    repo_path: Path,
    db: Database,
    config: Config,
) -> str | None:
    """Use sonnet + code graph to build a navigation map for the work agent.

    Returns a formatted prompt section, or None on any failure.
    """
    if not config.navigation_enabled:
        return None

    try:
        from .graph import build_navigation_context, ensure_graph

        store = await ensure_graph(repo_path)
        if not store:
            return None

        # Run graph query in thread (CPU-bound)
        nav_data = await asyncio.to_thread(build_navigation_context, store, task["prompt"], repo_path)
        if not nav_data or not nav_data.get("matched_files"):
            return None

        # Format graph data for the navigation model
        matched_text = "\n".join(
            f"- {f['path']}: {', '.join(f['symbols'][:NAV_MAX_SYMBOLS_PER_FILE])} ({f['match_reason']})"
            for f in nav_data["matched_files"]
        )
        related_text = (
            "\n".join(
                f"- {f['path']}: {', '.join(f['symbols'][:NAV_MAX_SYMBOLS_PER_FILE])} (via {f['relationship']})"
                for f in nav_data["related_files"]
            )
            or "(none)"
        )
        edges_text = (
            "\n".join(f"- {e['from']} --[{e['kind']}]--> {e['to']}" for e in nav_data["edges"][:NAV_MAX_EDGES])
            or "(none)"
        )

        nav_prompt = NAVIGATION_PROMPT.format(
            task_prompt=task["prompt"],
            matched_files=matched_text,
            related_files=related_text,
            edges=edges_text,
        )

        # Call sonnet for navigation (single-shot, 60s timeout)
        cmd = [
            "claude",
            "-p",
            "--output-format",
            "json",
            "--model",
            config.navigation_model,
            nav_prompt,
        ]

        # Clean env (same as agent)
        nav_env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(repo_path),
            env=nav_env,
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=TIMEOUT_NAVIGATION_MODEL)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            log.warning("Navigation context timed out for task %d", task["id"])
            return None

        if proc.returncode != 0:
            log.warning("Navigation model failed (exit %d) for task %d", proc.returncode, task["id"])
            return None

        # Parse response -- extract JSON from the output
        output = stdout.decode(errors="replace").strip()

        # claude --output-format json wraps result in {"type":"result","result":"..."}
        try:
            wrapper = json.loads(output)
            if isinstance(wrapper, dict) and "result" in wrapper:
                output = wrapper["result"]
        except json.JSONDecodeError:
            pass

        # Strip markdown fences if model wrapped them
        if output.startswith("```"):
            lines = output.splitlines()
            if lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            output = "\n".join(lines)

        try:
            files = json.loads(output)
        except json.JSONDecodeError:
            log.warning("Navigation model returned invalid JSON for task %d", task["id"])
            return None

        if not isinstance(files, list) or not files:
            return None

        # Format into prompt section (capped at ~4k chars)
        section_lines = [
            "## Navigation Context",
            "These files are most relevant to your task (from dependency analysis):",
        ]
        total_len = sum(len(line) for line in section_lines)
        for entry in files[:NAV_MAX_FILES]:
            if not isinstance(entry, dict):
                continue
            fpath = entry.get("file", "")
            symbols = entry.get("symbols", [])
            why = entry.get("why", "")
            sym_str = ", ".join(str(s) for s in symbols[:NAV_MAX_SYMBOLS_PER_FILE]) if symbols else ""
            line = f"  - {fpath}"
            if sym_str:
                line += f" — {sym_str}"
            if why:
                line += f"\n    Why: {why}"
            if total_len + len(line) > TRUNCATE_NAV_CONTEXT:
                break
            section_lines.append(line)
            total_len += len(line)

        if len(section_lines) <= 2:
            return None  # No files made it through

        return "\n".join(section_lines) + "\n\n"

    except Exception:
        log.debug("Failed to generate navigation context for task %d", task["id"], exc_info=True)
        return None

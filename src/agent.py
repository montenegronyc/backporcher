"""Agent execution: prompt building, agent runner, build verification."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from pathlib import Path

from .backends import AgentBackend, AgentEvent
from .config import Config
from .constants import (
    MAX_OUTPUT_BYTES,
    READLINE_LIMIT,
    TIMEOUT_VERIFY_AGENT,
    TRUNCATE_LOG_LINE,
    TRUNCATE_OUTPUT_TAIL,
    TRUNCATE_SUMMARY,
    TRUNCATE_VERIFY_OUTPUT,
    prlimit_args,
)
from .db import Database
from .navigation import generate_navigation_context
from .prompts import AGENT_PROMPT_TEMPLATE
from .repo_intel import get_learnings_text

log = logging.getLogger("backporcher.agent")

# Patterns in stderr that indicate a rate limit / quota exhaustion.
# These trigger immediate agent fallback without burning retry slots.
RATE_LIMIT_PATTERNS = (
    "429",
    "rate limit",
    "rate_limit",
    "quota exceeded",
    "quota_exceeded",
    "resource exhausted",
    "resource_exhausted",
    "too many requests",
    "overloaded",
)


async def run_agent(
    task: dict,
    worktree_path: Path,
    config: Config,
    db: Database,
    backend: AgentBackend | None = None,
) -> tuple[int, str | None]:
    """
    Run an agent backend in the worktree. Streams stdout to log file.
    Returns (exit_code, output_summary).

    If *backend* is None, defaults to ClaudeBackend for backward compatibility.
    """
    if backend is None:
        from .backends.claude import ClaudeBackend  # noqa: PLC0415

        backend = ClaudeBackend()

    # Build structured prompt with stack info, learnings, and navigation context
    project_context = ""
    learnings_section = ""
    navigation_section = ""
    try:
        repo = await db.get_repo(task["repo_id"])
        if repo:
            stack = repo.get("stack_info")
            if stack:
                project_context = f"## Project Context\nTech stack: {stack}\n\n"
            learnings_section = await get_learnings_text(db, task["repo_id"]) or ""
            # Generate navigation context from code graph
            repo_path = Path(repo["local_path"])
            if repo_path.exists():
                nav = await generate_navigation_context(task, repo_path, db, config)
                if nav:
                    navigation_section = nav
                    await db.add_log(task["id"], "Navigation context generated from code graph")
    except Exception:
        log.debug("Failed to fetch context for prompt", exc_info=True)

    prompt = AGENT_PROMPT_TEMPLATE.format(
        project_context=project_context,
        learnings_section=learnings_section,
        navigation_section=navigation_section,
        task_prompt=task["prompt"],
    )
    model = task["model"]
    log_file = config.logs_dir / f"{task['id']}.jsonl"

    cmd = backend.build_command(prompt, model, worktree_path)

    # Sandbox: wrap with sudo -u + prlimit when agent_user is configured
    if config.agent_user:
        # Inject backend-required env vars via --preserve-env if any
        required_vars = backend.required_env_vars()
        preserve_args: list[str] = []
        if required_vars:
            # Set the vars in current env so sudo can preserve them
            for k, v in required_vars.items():
                os.environ[k] = v
            preserve_args = [f"--preserve-env={','.join(required_vars.keys())}"]
        cmd = [
            "sudo",
            "-u",
            config.agent_user,
            *preserve_args,
            "--",
            *prlimit_args(),
            *cmd,
        ]
        agent_env = None  # Let sudo reset env to target user's defaults
    else:
        agent_env = backend.build_env(dict(os.environ))

    agent_name = backend.name
    log.info(
        "Starting agent for task %d (agent=%s, model=%s, user=%s)",
        task["id"],
        agent_name,
        model,
        config.agent_user or "self",
    )
    await db.add_log(task["id"], f"Starting agent={agent_name} with model={model}")

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=str(worktree_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
        limit=READLINE_LIMIT,
        **({"env": agent_env} if agent_env is not None else {}),
    )

    await db.update_task(task["id"], agent_pid=proc.pid)

    output_summary = None
    last_content: list[str] = []
    content_size = 0
    rate_limited = False

    async def read_stream():
        nonlocal output_summary, content_size
        fd = os.open(str(log_file), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o640)
        with os.fdopen(fd, "w") as lf:
            async for raw_line in proc.stdout:
                line = raw_line.decode(errors="replace").strip()
                if not line:
                    continue

                # Write every line to the log file
                lf.write(line + "\n")
                lf.flush()

                evt: AgentEvent | None = backend.parse_output_line(line)
                if evt is None:
                    continue

                if evt.type in ("assistant", "content_block_delta"):
                    if evt.content and content_size < MAX_OUTPUT_BYTES:
                        last_content.append(evt.content)
                        content_size += len(evt.content)

                elif evt.type in ("result", "error"):
                    raw = evt.content or ""
                    output_summary = str(raw) if not isinstance(raw, str) else raw
                    if evt.is_error:
                        await db.add_log(
                            task["id"],
                            f"Agent error: {output_summary[:TRUNCATE_SUMMARY]}",
                            level="error",
                        )

    async def read_stderr():
        nonlocal rate_limited
        async for raw_line in proc.stderr:
            line = raw_line.decode(errors="replace").strip()
            if line:
                await db.add_log(task["id"], f"stderr: {line[:TRUNCATE_LOG_LINE]}", level="warn")
                # Detect rate limit / quota exhaustion in stderr
                if not rate_limited:
                    line_lower = line.lower()
                    for pattern in RATE_LIMIT_PATTERNS:
                        if pattern in line_lower:
                            rate_limited = True
                            log.warning(
                                "Task %d: rate limit detected for agent %s: %s",
                                task["id"],
                                agent_name,
                                line[:200],
                            )
                            await db.add_log(
                                task["id"],
                                f"Rate limit detected for {agent_name}",
                                level="error",
                            )
                            break

    try:
        await asyncio.wait_for(
            asyncio.gather(read_stream(), read_stderr()),
            timeout=config.task_timeout_seconds,
        )
        await proc.wait()
    except asyncio.TimeoutError:
        log.warning("Task %d timed out after %ds", task["id"], config.task_timeout_seconds)
        await db.add_log(
            task["id"],
            f"TIMEOUT after {config.task_timeout_seconds}s",
            level="error",
        )
        try:
            os.killpg(proc.pid, signal.SIGTERM)
            await asyncio.sleep(5)
            if proc.returncode is None:
                os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            try:
                proc.kill()
            except ProcessLookupError:
                pass
        await proc.wait()

    if not output_summary and last_content:
        output_summary = "".join(last_content)[-TRUNCATE_OUTPUT_TAIL:]

    await db.add_log(
        task["id"],
        f"Agent exited with code {proc.returncode}",
    )

    # Stash rate_limited flag on the task dict so dispatch can read it
    task["_rate_limited"] = rate_limited

    return proc.returncode, output_summary


async def run_verify(
    worktree_path: Path,
    verify_command: str,
    task_id: int,
    db: Database,
    config: Config | None = None,
) -> tuple[bool, str]:
    """Run repo's verify command in the worktree. Returns (passed, output)."""
    log.info("Task #%d: running verify: %s", task_id, verify_command)
    await db.add_log(task_id, f"Running verify: {verify_command}")

    # Run as agent user when sandboxing is configured, so target/ dirs
    # are owned by the same user that runs the agent
    cmd: list[str] = ["bash", "-c", verify_command]
    if config and config.agent_user:
        cmd = [
            "sudo",
            "-u",
            config.agent_user,
            "--",
            *prlimit_args(),
            *cmd,
        ]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=str(worktree_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=TIMEOUT_VERIFY_AGENT)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return False, f"Verify command timed out after {TIMEOUT_VERIFY_AGENT}s"

    output = stdout.decode(errors="replace")
    if proc.returncode == 0:
        await db.add_log(task_id, "Verify passed")
        return True, output

    # Truncate to last 3000 chars (most relevant part)
    if len(output) > TRUNCATE_VERIFY_OUTPUT:
        output = "...(truncated)...\n" + output[-TRUNCATE_VERIFY_OUTPUT:]
    await db.add_log(task_id, f"Verify failed (exit {proc.returncode})", level="warn")
    return False, output

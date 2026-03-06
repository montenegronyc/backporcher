"""Dispatcher: worktree setup, agent execution, PR creation."""

import asyncio
import json
import logging
import os
import re
import signal
import shlex
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from .config import Config
from .db import Database

log = logging.getLogger("compound.dispatcher")

# Strict pattern for branch names — alphanumeric, hyphens, slashes only
BRANCH_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9/_-]{0,100}$")
# GitHub URL pattern
GITHUB_URL_RE = re.compile(
    r"^https://github\.com/[a-zA-Z0-9._-]+/[a-zA-Z0-9._-]+(\.git)?$"
)


def validate_github_url(url: str, config: Config) -> str:
    """Validate and normalize a GitHub repo URL."""
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise ValueError("Only HTTPS git URLs are allowed")
    if parsed.hostname not in config.allowed_git_hosts:
        raise ValueError(f"Host {parsed.hostname} not in allowed list")
    if not GITHUB_URL_RE.match(url.rstrip("/")):
        raise ValueError("Invalid GitHub URL format")
    clean = url.rstrip("/")
    if not clean.endswith(".git"):
        clean += ".git"
    return clean


def repo_name_from_url(url: str) -> str:
    """Extract 'owner/repo' from a GitHub URL."""
    parsed = urlparse(url)
    path = parsed.path.strip("/").removesuffix(".git")
    parts = path.split("/")
    if len(parts) != 2:
        raise ValueError(f"Expected owner/repo in URL path, got: {path}")
    return "/".join(parts)


def make_branch_name(task_id: int, prompt: str) -> str:
    """Generate a safe branch name from task ID and prompt."""
    slug = re.sub(r"[^a-z0-9]+", "-", prompt.lower().strip())[:40].strip("-")
    branch = f"compound/{task_id}-{slug}"
    if not BRANCH_RE.match(branch):
        branch = f"compound/{task_id}"
    return branch


async def run_cmd(
    *args: str,
    cwd: str | Path | None = None,
    timeout: int = 120,
    env: dict[str, str] | None = None,
) -> tuple[int, str, str]:
    """Run a subprocess with timeout. Returns (returncode, stdout, stderr)."""
    merged_env = {**os.environ, **(env or {})}
    proc = await asyncio.create_subprocess_exec(
        *args,
        cwd=str(cwd) if cwd else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=merged_env,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=timeout
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return -1, "", f"Command timed out after {timeout}s"

    return (
        proc.returncode,
        stdout.decode(errors="replace"),
        stderr.decode(errors="replace"),
    )


async def clone_or_fetch(
    db: Database, repo: dict, config: Config
) -> Path:
    """Clone repo if needed, otherwise fetch. Returns local path."""
    local_path = config.repos_dir / repo["name"].replace("/", "_")

    if local_path.exists() and (local_path / ".git").exists():
        log.info("Fetching %s", repo["name"])
        rc, out, err = await run_cmd(
            "git", "fetch", "--all", "--prune", cwd=local_path
        )
        if rc != 0:
            raise RuntimeError(f"git fetch failed: {err}")
    else:
        log.info("Cloning %s -> %s", repo["github_url"], local_path)
        local_path.mkdir(parents=True, exist_ok=True)
        rc, out, err = await run_cmd(
            "git", "clone", repo["github_url"], str(local_path), timeout=300
        )
        if rc != 0:
            raise RuntimeError(f"git clone failed: {err}")

    await db.update_repo_fetched(repo["id"])

    # Update local_path in DB if not set
    if not repo.get("local_path"):
        await db.db.execute(
            "UPDATE repos SET local_path = ? WHERE id = ?",
            (str(local_path), repo["id"]),
        )
        await db.db.commit()

    return local_path


async def setup_worktree(
    repo_path: Path, branch_name: str, default_branch: str
) -> Path:
    """Create a git worktree for the task. Returns worktree path."""
    worktree_path = repo_path.parent / f"wt-{branch_name.replace('/', '-')}"

    # Clean up stale worktree if exists
    if worktree_path.exists():
        await run_cmd("git", "worktree", "remove", "--force", str(worktree_path), cwd=repo_path)

    rc, out, err = await run_cmd(
        "git", "worktree", "add", "-b", branch_name,
        str(worktree_path), f"origin/{default_branch}",
        cwd=repo_path,
    )
    if rc != 0:
        raise RuntimeError(f"git worktree add failed: {err}")

    return worktree_path


async def run_agent(
    task: dict,
    worktree_path: Path,
    config: Config,
    db: Database,
) -> tuple[int, str | None, float | None]:
    """
    Run claude -p in the worktree. Streams output, logs lines.
    Returns (exit_code, output_summary, cost_usd).
    """
    prompt = task["prompt"]
    model = task["model"]
    budget = min(task["max_budget_usd"], config.max_budget_limit_usd)

    cmd = [
        "claude", "-p",
        "--output-format", "stream-json",
        "--dangerously-skip-permissions",
        "--model", model,
        "--max-budget-usd", str(budget),
        prompt,
    ]

    log.info("Starting agent for task %d: %s", task["id"], shlex.join(cmd))
    await db.add_log(task["id"], f"Starting agent with model={model}, budget=${budget}")

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=str(worktree_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    # Store PID for cancellation
    await db.update_task(task["id"], agent_pid=proc.pid)

    output_summary = None
    cost_usd = None
    last_content = []
    log_buffer = []

    async def read_stream():
        nonlocal output_summary, cost_usd
        async for raw_line in proc.stdout:
            line = raw_line.decode(errors="replace").strip()
            if not line:
                continue

            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                log_buffer.append(line)
                continue

            etype = event.get("type", "")

            if etype == "assistant" and "message" in event:
                msg = event["message"]
                if msg.get("content"):
                    for block in msg["content"]:
                        if block.get("type") == "text":
                            last_content.append(block["text"])
                # Extract cost from usage/stats
                usage = msg.get("usage", {})
                if usage:
                    log_buffer.append(
                        f"Tokens: in={usage.get('input_tokens', '?')}, "
                        f"out={usage.get('output_tokens', '?')}"
                    )

            elif etype == "result":
                output_summary = event.get("result", "")
                cost_usd = event.get("cost_usd")
                if event.get("is_error"):
                    log_buffer.append(f"Agent error: {output_summary}")

            elif etype == "content_block_delta":
                delta = event.get("delta", {})
                if delta.get("type") == "text_delta":
                    last_content.append(delta.get("text", ""))

            # Batch-flush logs periodically
            if len(log_buffer) >= 10:
                for msg in log_buffer:
                    await db.add_log(task["id"], msg[:2000])
                log_buffer.clear()

    try:
        await asyncio.wait_for(
            read_stream(), timeout=config.task_timeout_seconds
        )
        await proc.wait()
    except asyncio.TimeoutError:
        log.warning("Task %d timed out after %ds", task["id"], config.task_timeout_seconds)
        await db.add_log(task["id"], f"TIMEOUT after {config.task_timeout_seconds}s", level="error")
        try:
            proc.send_signal(signal.SIGTERM)
            await asyncio.sleep(5)
            if proc.returncode is None:
                proc.kill()
        except ProcessLookupError:
            pass
        await proc.wait()

    # Flush remaining logs
    for msg in log_buffer:
        await db.add_log(task["id"], msg[:2000])

    if not output_summary and last_content:
        output_summary = "".join(last_content)[-2000:]

    return proc.returncode, output_summary, cost_usd


async def create_pr(
    worktree_path: Path,
    task: dict,
    repo: dict,
) -> str | None:
    """Commit, push, and create a PR. Returns PR URL or None."""
    # Check if there are changes to commit
    rc, out, _ = await run_cmd("git", "status", "--porcelain", cwd=worktree_path)
    if rc != 0 or not out.strip():
        log.info("Task %d: no changes to commit", task["id"])
        return None

    # Stage all changes
    rc, _, err = await run_cmd("git", "add", "-A", cwd=worktree_path)
    if rc != 0:
        raise RuntimeError(f"git add failed: {err}")

    # Commit
    commit_msg = f"compound: {task['prompt'][:72]}\n\nTask #{task['id']} via Compound dispatcher"
    rc, _, err = await run_cmd(
        "git", "commit", "-m", commit_msg, cwd=worktree_path
    )
    if rc != 0:
        raise RuntimeError(f"git commit failed: {err}")

    # Push
    branch = task["branch_name"]
    rc, _, err = await run_cmd(
        "git", "push", "-u", "origin", branch,
        cwd=worktree_path, timeout=120,
    )
    if rc != 0:
        raise RuntimeError(f"git push failed: {err}")

    # Create PR
    pr_title = f"[Compound] {task['prompt'][:60]}"
    pr_body = (
        f"## Compound Agent Task #{task['id']}\n\n"
        f"**Prompt:** {task['prompt'][:500]}\n\n"
        f"**Model:** {task['model']}\n\n"
        f"---\n_Created by Compound dispatcher_"
    )
    rc, out, err = await run_cmd(
        "gh", "pr", "create",
        "--title", pr_title,
        "--body", pr_body,
        "--head", branch,
        "--base", repo["default_branch"],
        cwd=worktree_path, timeout=60,
    )
    if rc != 0:
        log.error("PR creation failed: %s", err)
        return None

    pr_url = out.strip()
    log.info("Created PR: %s", pr_url)
    return pr_url


async def dispatch_task(task: dict, config: Config, db: Database):
    """Full lifecycle: fetch → worktree → agent → PR."""
    task_id = task["id"]
    try:
        repo = await db.get_repo(task["repo_id"])
        if not repo:
            raise ValueError(f"Repo {task['repo_id']} not found")

        # Clone/fetch
        await db.add_log(task_id, "Fetching repository...")
        repo_path = await clone_or_fetch(db, repo, config)

        # Create worktree
        branch = make_branch_name(task_id, task["prompt"])
        await db.update_task(task_id, branch_name=branch)
        await db.add_log(task_id, f"Creating worktree on branch {branch}")
        worktree_path = await setup_worktree(
            repo_path, branch, repo["default_branch"]
        )
        await db.update_task(task_id, worktree_path=str(worktree_path))

        # Run agent
        await db.add_log(task_id, "Running agent...")
        exit_code, summary, cost = await run_agent(
            task, worktree_path, config, db
        )
        await db.update_task(
            task_id,
            exit_code=exit_code,
            output_summary=summary[:4000] if summary else None,
            cost_usd=cost,
        )

        if exit_code != 0:
            now = datetime.now(timezone.utc).isoformat()
            await db.update_task(
                task_id,
                status="failed",
                error_message=f"Agent exited with code {exit_code}",
                completed_at=now,
            )
            await db.add_log(task_id, f"Agent failed with exit code {exit_code}", level="error")
            return

        # Create PR
        await db.add_log(task_id, "Creating pull request...")
        pr_url = await create_pr(worktree_path, task, repo)
        now = datetime.now(timezone.utc).isoformat()

        if pr_url:
            await db.update_task(
                task_id, status="pr_created", pr_url=pr_url, completed_at=now
            )
            await db.add_log(task_id, f"PR created: {pr_url}")
        else:
            await db.update_task(
                task_id, status="completed", completed_at=now
            )
            await db.add_log(task_id, "Completed (no changes to PR)")

    except Exception as e:
        log.exception("Task %d failed", task_id)
        now = datetime.now(timezone.utc).isoformat()
        await db.update_task(
            task_id,
            status="failed",
            error_message=str(e)[:2000],
            completed_at=now,
        )
        await db.add_log(task_id, f"Fatal error: {e}", level="error")

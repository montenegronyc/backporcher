"""Dispatcher: worktree setup, agent execution, PR creation."""

import asyncio
import json
import logging
import os
import re
import signal
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from .config import Config
from .db import Database

log = logging.getLogger("voltron.dispatcher")

# Per-repo locks to serialize git operations (fetch/worktree) for the same repo
_repo_locks: dict[int, asyncio.Lock] = {}


def _get_repo_lock(repo_id: int) -> asyncio.Lock:
    if repo_id not in _repo_locks:
        _repo_locks[repo_id] = asyncio.Lock()
    return _repo_locks[repo_id]

BRANCH_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9/_-]{0,100}$")
GITHUB_URL_RE = re.compile(
    r"^https://github\.com/[a-zA-Z0-9._-]+/[a-zA-Z0-9._-]+(\.git)?$"
)


def validate_github_url(url: str, config: Config) -> str:
    """Validate and normalize a GitHub repo URL."""
    url = url.strip().rstrip("/")
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise ValueError("Only HTTPS git URLs are allowed")
    if parsed.hostname not in config.allowed_git_hosts:
        raise ValueError(f"Host {parsed.hostname} not in allowed list")
    if not GITHUB_URL_RE.match(url):
        raise ValueError("Invalid GitHub URL format")
    return url


def repo_name_from_url(url: str) -> str:
    """Extract repo short name (e.g. 'valibjorn') from a GitHub URL."""
    parsed = urlparse(url)
    path = parsed.path.strip("/").removesuffix(".git")
    parts = path.split("/")
    if len(parts) != 2:
        raise ValueError(f"Expected owner/repo in URL, got: {path}")
    return parts[1]  # Just the repo name, not owner/repo


def make_branch_name(task_id: int, prompt: str) -> str:
    """Generate a safe branch name from task ID and prompt."""
    slug = re.sub(r"[^a-z0-9]+", "-", prompt.lower().strip())[:40].strip("-")
    branch = f"voltron/{task_id}-{slug}"
    if not BRANCH_RE.match(branch):
        branch = f"voltron/{task_id}"
    return branch


async def run_cmd(
    *args: str,
    cwd: str | Path | None = None,
    timeout: int = 120,
) -> tuple[int, str, str]:
    """Run a subprocess with timeout. Returns (returncode, stdout, stderr)."""
    proc = await asyncio.create_subprocess_exec(
        *args,
        cwd=str(cwd) if cwd else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
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


async def clone_or_fetch(repo: dict, config: Config) -> Path:
    """Clone repo if needed, otherwise fetch. Returns local path."""
    local_path = Path(repo["local_path"])

    if local_path.exists() and (local_path / ".git").exists():
        log.info("Fetching %s", repo["name"])
        rc, _, err = await run_cmd(
            "git", "fetch", "--all", "--prune", "--force", cwd=local_path
        )
        if rc != 0:
            raise RuntimeError(f"git fetch failed: {err}")
    else:
        log.info("Cloning %s -> %s", repo["github_url"], local_path)
        local_path.mkdir(parents=True, exist_ok=True)
        rc, _, err = await run_cmd(
            "git", "clone", repo["github_url"], str(local_path), timeout=300
        )
        if rc != 0:
            raise RuntimeError(f"git clone failed: {err}")

    return local_path


async def setup_worktree(
    repo_path: Path, task_id: int, branch_name: str, default_branch: str,
) -> Path:
    """Create a git worktree for the task."""
    worktree_path = repo_path / ".worktrees" / str(task_id)

    # Clean up stale worktree if exists
    if worktree_path.exists():
        await run_cmd(
            "git", "worktree", "remove", "--force",
            str(worktree_path), cwd=repo_path,
        )

    worktree_path.parent.mkdir(parents=True, exist_ok=True)

    rc, _, err = await run_cmd(
        "git", "worktree", "add", "-b", branch_name,
        str(worktree_path), f"origin/{default_branch}",
        cwd=repo_path,
    )
    if rc != 0:
        raise RuntimeError(f"git worktree add failed: {err}")

    # Configure git identity in the worktree
    await run_cmd("git", "config", "user.name", "Voltron", cwd=worktree_path)
    await run_cmd("git", "config", "user.email", "voltron@dispatch.local", cwd=worktree_path)

    return worktree_path


async def run_agent(
    task: dict,
    worktree_path: Path,
    config: Config,
    db: Database,
) -> tuple[int, str | None]:
    """
    Run claude -p in the worktree. Streams stdout to log file.
    Returns (exit_code, output_summary).
    Uses Max subscription — no --max-budget-usd flag.
    """
    prompt = task["prompt"]
    model = task["model"]
    log_file = config.logs_dir / f"{task['id']}.jsonl"

    cmd = [
        "claude", "-p",
        "--output-format", "stream-json",
        "--verbose",
        "--dangerously-skip-permissions",
        "--model", model,
        prompt,
    ]

    log.info("Starting agent for task %d (model=%s)", task["id"], model)
    await db.add_log(task["id"], f"Starting agent with model={model}")

    # Clean env: unset CLAUDECODE to avoid nested-session detection
    agent_env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=str(worktree_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=agent_env,
    )

    await db.update_task(task["id"], agent_pid=proc.pid)

    output_summary = None
    last_content: list[str] = []

    async def read_stream():
        nonlocal output_summary
        with open(log_file, "w") as lf:
            async for raw_line in proc.stdout:
                line = raw_line.decode(errors="replace").strip()
                if not line:
                    continue

                # Write every line to the log file
                lf.write(line + "\n")
                lf.flush()

                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue

                etype = event.get("type", "")

                if etype == "assistant" and "message" in event:
                    msg = event["message"]
                    for block in (msg.get("content") or []):
                        if block.get("type") == "text":
                            last_content.append(block["text"])

                elif etype == "result":
                    output_summary = event.get("result", "")
                    if event.get("is_error"):
                        await db.add_log(
                            task["id"],
                            f"Agent error: {output_summary[:500]}",
                            level="error",
                        )

                elif etype == "content_block_delta":
                    delta = event.get("delta", {})
                    if delta.get("type") == "text_delta":
                        last_content.append(delta.get("text", ""))

    async def read_stderr():
        async for raw_line in proc.stderr:
            line = raw_line.decode(errors="replace").strip()
            if line:
                await db.add_log(task["id"], f"stderr: {line[:500]}", level="warn")

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
            proc.send_signal(signal.SIGTERM)
            await asyncio.sleep(5)
            if proc.returncode is None:
                proc.kill()
        except ProcessLookupError:
            pass
        await proc.wait()

    if not output_summary and last_content:
        output_summary = "".join(last_content)[-2000:]

    await db.add_log(
        task["id"],
        f"Agent exited with code {proc.returncode}",
    )

    return proc.returncode, output_summary


async def create_pr(
    worktree_path: Path, task: dict, repo: dict, db: Database,
) -> str | None:
    """Check for commits, push, and create a PR. Returns PR URL or None."""
    # Check if agent made any commits beyond the base
    rc, out, _ = await run_cmd(
        "git", "log", f"origin/{repo['default_branch']}..HEAD",
        "--oneline", cwd=worktree_path,
    )
    if rc != 0 or not out.strip():
        # Also check for uncommitted changes
        rc2, out2, _ = await run_cmd("git", "status", "--porcelain", cwd=worktree_path)
        if not out2.strip():
            log.info("Task %d: no changes to push", task["id"])
            return None
        # Stage + commit uncommitted changes
        await run_cmd("git", "add", "-A", cwd=worktree_path)
        commit_msg = f"voltron: {task['prompt'][:72]}\n\nTask #{task['id']}"
        rc3, _, err = await run_cmd("git", "commit", "-m", commit_msg, cwd=worktree_path)
        if rc3 != 0:
            await db.add_log(task["id"], f"git commit failed: {err}", level="error")
            return None

    branch = task["branch_name"]
    await db.add_log(task["id"], f"Pushing branch {branch}...")

    rc, _, err = await run_cmd(
        "git", "push", "-u", "origin", branch,
        cwd=worktree_path, timeout=120,
    )
    if rc != 0:
        await db.add_log(task["id"], f"git push failed: {err}", level="error")
        raise RuntimeError(f"git push failed: {err}")

    pr_title = f"[voltron] {task['prompt'][:60]}"
    pr_body = (
        f"## Voltron Task #{task['id']}\n\n"
        f"**Prompt:** {task['prompt'][:500]}\n\n"
        f"**Model:** {task['model']}\n\n"
        f"---\n_Created by Voltron dispatcher_"
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
        await db.add_log(task["id"], f"PR creation failed: {err}", level="error")
        return None

    pr_url = out.strip()
    log.info("Created PR: %s", pr_url)
    return pr_url


async def cleanup_worktree(repo_path: Path, task_id: int) -> bool:
    """Remove a task's worktree."""
    worktree_path = repo_path / ".worktrees" / str(task_id)
    if not worktree_path.exists():
        return False
    rc, _, err = await run_cmd(
        "git", "worktree", "remove", "--force",
        str(worktree_path), cwd=repo_path,
    )
    return rc == 0


async def dispatch_task(task: dict, config: Config, db: Database):
    """Full lifecycle: fetch → worktree → agent → PR."""
    task_id = task["id"]
    try:
        repo = await db.get_repo(task["repo_id"])
        if not repo:
            raise ValueError(f"Repo {task['repo_id']} not found")

        # Serialize git operations per-repo (fetch + worktree creation)
        repo_lock = _get_repo_lock(repo["id"])
        async with repo_lock:
            await db.add_log(task_id, "Fetching repository...")
            repo_path = await clone_or_fetch(repo, config)

            branch = make_branch_name(task_id, task["prompt"])
            await db.update_task(task_id, branch_name=branch)
            await db.add_log(task_id, f"Creating worktree on branch {branch}")
            worktree_path = await setup_worktree(
                repo_path, task_id, branch, repo["default_branch"],
            )
            await db.update_task(task_id, worktree_path=str(worktree_path))

        # Run agent
        await db.add_log(task_id, "Running agent...")
        exit_code, summary = await run_agent(task, worktree_path, config, db)
        await db.update_task(
            task_id,
            exit_code=exit_code,
            output_summary=summary[:4000] if summary else None,
        )

        if exit_code != 0:
            now = datetime.now(timezone.utc).isoformat()
            await db.update_task(
                task_id,
                status="failed",
                error_message=f"Agent exited with code {exit_code}",
                completed_at=now,
            )
            await db.add_log(task_id, f"Agent failed (exit {exit_code})", level="error")
            return

        # Create PR
        await db.add_log(task_id, "Creating pull request...")
        # Re-read task to get branch_name
        task = await db.get_task(task_id)
        pr_url = await create_pr(worktree_path, task, repo, db)
        now = datetime.now(timezone.utc).isoformat()

        if pr_url:
            await db.update_task(
                task_id, status="pr_created", pr_url=pr_url, completed_at=now,
            )
            await db.add_log(task_id, f"PR created: {pr_url}")
        else:
            await db.update_task(
                task_id, status="completed", completed_at=now,
            )
            await db.add_log(task_id, "Completed (no changes)")

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

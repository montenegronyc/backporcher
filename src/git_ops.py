"""Git operations: cloning, fetching, worktrees, branch management, cleanup."""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
from pathlib import Path
from urllib.parse import urlparse

from .config import Config
from .constants import TIMEOUT_GIT_CLONE, TIMEOUT_GIT_FETCH, TRUNCATE_BRANCH_SLUG
from .db import Database

log = logging.getLogger("backporcher.git_ops")

# Per-repo locks to serialize git operations (fetch/worktree) for the same repo
_repo_locks: dict[int, asyncio.Lock] = {}


def _get_repo_lock(repo_id: int) -> asyncio.Lock:
    if repo_id not in _repo_locks:
        _repo_locks[repo_id] = asyncio.Lock()
    return _repo_locks[repo_id]


BRANCH_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9/_-]{0,100}$")
GITHUB_URL_RE = re.compile(r"^https://github\.com/[a-zA-Z0-9._-]+/[a-zA-Z0-9._-]+(\.git)?$")


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
    slug = re.sub(r"[^a-z0-9]+", "-", prompt.lower().strip())[:TRUNCATE_BRANCH_SLUG].strip("-")
    if slug:
        branch = f"backporcher/{task_id}-{slug}"
    else:
        branch = f"backporcher/{task_id}"
    if not BRANCH_RE.match(branch):
        branch = f"backporcher/{task_id}"
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
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
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
        rc, _, err = await run_cmd("git", "fetch", "--all", "--prune", "--force", cwd=local_path)
        if rc != 0:
            raise RuntimeError(f"git fetch failed: {err}")
    else:
        log.info("Cloning %s -> %s", repo["github_url"], local_path)
        local_path.mkdir(parents=True, exist_ok=True)
        rc, _, err = await run_cmd("git", "clone", repo["github_url"], str(local_path), timeout=TIMEOUT_GIT_CLONE)
        if rc != 0:
            raise RuntimeError(f"git clone failed: {err}")

    return local_path


async def setup_worktree(
    repo_path: Path,
    task_id: int,
    branch_name: str,
    default_branch: str,
) -> Path:
    """Create a git worktree for the task."""
    worktree_path = repo_path / ".worktrees" / str(task_id)

    # Clean up stale worktree if exists
    if worktree_path.exists():
        await run_cmd(
            "git",
            "worktree",
            "remove",
            "--force",
            str(worktree_path),
            cwd=repo_path,
        )
        # Force-remove directory if git worktree remove didn't clean it
        if worktree_path.exists():
            import shutil

            shutil.rmtree(str(worktree_path), ignore_errors=True)

    # Prune stale worktree refs so branch -D can succeed
    await run_cmd("git", "worktree", "prune", cwd=repo_path)

    # Delete stale local branch from a previous attempt (makes re-queue idempotent)
    rc, _, berr = await run_cmd("git", "branch", "-D", branch_name, cwd=repo_path)
    if rc == 0:
        log.info("Deleted stale local branch %s", branch_name)
    elif "not found" not in berr.lower():
        log.warning("Branch cleanup for %s: %s", branch_name, berr.strip())

    # Delete stale remote branch too (prevents push rejection on re-queue)
    rc, _, _ = await run_cmd(
        "git",
        "push",
        "origin",
        "--delete",
        branch_name,
        cwd=repo_path,
        timeout=TIMEOUT_GIT_FETCH,
    )
    if rc == 0:
        log.info("Deleted stale remote branch %s", branch_name)

    worktree_path.parent.mkdir(parents=True, exist_ok=True)

    rc, _, err = await run_cmd(
        "git",
        "worktree",
        "add",
        "-b",
        branch_name,
        str(worktree_path),
        f"origin/{default_branch}",
        cwd=repo_path,
    )
    if rc != 0:
        raise RuntimeError(f"git worktree add failed: {err}")

    # Configure git identity in the worktree
    await run_cmd("git", "config", "user.name", "Backporcher", cwd=worktree_path)
    await run_cmd("git", "config", "user.email", "backporcher@dispatch.local", cwd=worktree_path)

    # Ensure worktree files are group-writable so agent user can modify them.
    # core.sharedRepository=group only affects new git objects, not checked-out files.
    await run_cmd("chmod", "-R", "g+w", str(worktree_path))

    return worktree_path


async def cleanup_worktree(repo_path: Path, task_id: int) -> bool:
    """Remove a task's worktree."""
    worktree_path = repo_path / ".worktrees" / str(task_id)
    if not worktree_path.exists():
        return False
    rc, _, err = await run_cmd(
        "git",
        "worktree",
        "remove",
        "--force",
        str(worktree_path),
        cwd=repo_path,
    )
    return rc == 0


async def cleanup_task_artifacts(task: dict, db: Database):
    """Delete worktree and remote branch for a finished task.

    Safe to call multiple times (idempotent). Logs but doesn't raise on
    partial failures — cleanup is best-effort.
    """
    task_id = task["id"]
    repo = await db.get_repo(task["repo_id"])
    if not repo:
        return

    repo_path = Path(repo["local_path"])
    cleaned_any = False

    # 1. Remove worktree
    worktree = task.get("worktree_path")
    if worktree and Path(worktree).exists():
        rc, _, err = await run_cmd(
            "git",
            "worktree",
            "remove",
            "--force",
            worktree,
            cwd=repo_path,
        )
        if rc == 0:
            log.info("Task #%d: removed worktree %s", task_id, worktree)
            cleaned_any = True
        else:
            log.warning("Task #%d: worktree remove failed: %s", task_id, err.strip())
            # Force-remove directory if git command failed
            if Path(worktree).exists():
                shutil.rmtree(worktree, ignore_errors=True)
                cleaned_any = True

    # 2. Prune stale worktree refs
    if cleaned_any:
        await run_cmd("git", "worktree", "prune", cwd=repo_path)

    # 3. Delete remote branch
    branch = task.get("branch_name")
    if branch:
        rc, _, _ = await run_cmd(
            "git",
            "push",
            "origin",
            "--delete",
            branch,
            cwd=repo_path,
            timeout=TIMEOUT_GIT_FETCH,
        )
        if rc == 0:
            log.info("Task #%d: deleted remote branch %s", task_id, branch)

    # 4. Clear paths in DB so we don't try again
    if cleaned_any or branch:
        await db.update_task(
            task_id,
            worktree_path=None,
            branch_name=None,
        )


async def ensure_repo_permissions(repo_path: Path, config: Config):
    """Set core.sharedRepository=group if agent_user is configured."""
    if not config.agent_user:
        return
    rc, out, _ = await run_cmd("git", "config", "core.sharedRepository", cwd=repo_path)
    if out.strip() not in ("group", "1"):
        await run_cmd("git", "config", "core.sharedRepository", "group", cwd=repo_path)

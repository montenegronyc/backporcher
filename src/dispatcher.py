"""Dispatcher: worktree setup, agent execution, PR creation."""

import asyncio
import json
import logging
import os
import re
import shutil
import signal
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from .config import Config
from .db import Database
from .github import (
    comment_on_issue,
    extract_pr_number_from_url,
    get_pr_diff,
    list_open_prs,
    repo_full_name_from_url,
    update_issue_labels,
)

log = logging.getLogger("backporcher.dispatcher")


async def _mark_issue_failed(task: dict, db: Database, reason: str):
    """Update GitHub labels on the source issue when a task permanently fails.

    Moves from backporcher-in-progress → backporcher-failed and posts a comment.
    No-op if the task didn't originate from a GitHub issue.
    """
    issue_num = task.get("github_issue_number")
    if not issue_num:
        return
    repo = await db.get_repo(task["repo_id"])
    if not repo:
        return
    repo_full = repo_full_name_from_url(repo["github_url"])
    await update_issue_labels(
        repo_full,
        issue_num,
        add=["backporcher-failed"],
        remove=["backporcher-in-progress"],
    )
    await comment_on_issue(
        repo_full,
        issue_num,
        f"{reason}\n\nRe-add the `backporcher` label to retry.",
    )


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
    slug = re.sub(r"[^a-z0-9]+", "-", prompt.lower().strip())[:40].strip("-")
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
        rc, _, err = await run_cmd("git", "clone", repo["github_url"], str(local_path), timeout=300)
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
        timeout=30,
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
    # Prepend non-interactive override: agents running via claude -p have no
    # interactive user, so skip any "wait for approval" instructions from CLAUDE.md
    prompt = (
        "IMPORTANT: You are running non-interactively via an automated dispatcher. "
        "Implement directly — do NOT give an approach summary or wait for approval. "
        "Start coding immediately.\n\n" + task["prompt"]
    )
    model = task["model"]
    log_file = config.logs_dir / f"{task['id']}.jsonl"

    cmd = [
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

    # Sandbox: wrap with sudo -u + prlimit when agent_user is configured
    if config.agent_user:
        cmd = [
            "sudo",
            "-u",
            config.agent_user,
            "--",
            "prlimit",
            "--nproc=500",  # max 500 processes
            "--fsize=2147483648",  # 2 GB max file size
            "--",
            *cmd,
        ]
        agent_env = None  # Let sudo reset env to target user's defaults
    else:
        # Clean env: strip sensitive vars and CLAUDECODE (nested-session detection)
        _sensitive_vars = {
            "CLAUDECODE",
            "SSH_AUTH_SOCK",
            "SSH_AGENT_PID",
            "GIT_ASKPASS",
            "GIT_CREDENTIALS",
            "GITHUB_TOKEN",
            "AWS_SECRET_ACCESS_KEY",
            "AWS_SESSION_TOKEN",
            "ANTHROPIC_API_KEY",
            "OPENAI_API_KEY",
        }
        agent_env = {k: v for k, v in os.environ.items() if k not in _sensitive_vars}

    log.info("Starting agent for task %d (model=%s, user=%s)", task["id"], model, config.agent_user or "self")
    await db.add_log(task["id"], f"Starting agent with model={model}")

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=str(worktree_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
        limit=1024 * 1024,  # 1 MB readline limit (Claude streams large JSON events)
        **({"env": agent_env} if agent_env is not None else {}),
    )

    await db.update_task(task["id"], agent_pid=proc.pid)

    output_summary = None
    last_content: list[str] = []
    content_size = 0
    MAX_CONTENT_BYTES = 10 * 1024 * 1024  # 10 MB cap on in-memory output

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

                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue

                etype = event.get("type", "")

                if etype == "assistant" and "message" in event:
                    msg = event["message"]
                    for block in msg.get("content") or []:
                        if block.get("type") == "text":
                            text = block["text"]
                            if content_size < MAX_CONTENT_BYTES:
                                last_content.append(text)
                                content_size += len(text)

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
                        text = delta.get("text", "")
                        if content_size < MAX_CONTENT_BYTES:
                            last_content.append(text)
                            content_size += len(text)

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
        output_summary = "".join(last_content)[-2000:]

    await db.add_log(
        task["id"],
        f"Agent exited with code {proc.returncode}",
    )

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
            "prlimit",
            "--nproc=500",
            "--fsize=2147483648",
            "--",
            *cmd,
        ]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=str(worktree_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=300)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return False, "Verify command timed out after 300s"

    output = stdout.decode(errors="replace")
    if proc.returncode == 0:
        await db.add_log(task_id, "Verify passed")
        return True, output

    # Truncate to last 3000 chars (most relevant part)
    if len(output) > 3000:
        output = "...(truncated)...\n" + output[-3000:]
    await db.add_log(task_id, f"Verify failed (exit {proc.returncode})", level="warn")
    return False, output


async def create_pr(
    worktree_path: Path,
    task: dict,
    repo: dict,
    db: Database,
) -> str | None:
    """Check for commits, push, and create a PR. Returns PR URL or None."""
    # Check if agent made any commits beyond the base
    rc, out, _ = await run_cmd(
        "git",
        "log",
        f"origin/{repo['default_branch']}..HEAD",
        "--oneline",
        cwd=worktree_path,
    )
    if rc != 0 or not out.strip():
        # Also check for uncommitted changes
        rc2, out2, _ = await run_cmd("git", "status", "--porcelain", cwd=worktree_path)
        if not out2.strip():
            log.info("Task %d: no changes to push", task["id"])
            return None
        # Stage + commit uncommitted changes
        await run_cmd("git", "add", "-A", cwd=worktree_path)
        commit_msg = f"backporcher: {task['prompt'][:72]}\n\nTask #{task['id']}"
        rc3, _, err = await run_cmd("git", "commit", "-m", commit_msg, cwd=worktree_path)
        if rc3 != 0:
            await db.add_log(task["id"], f"git commit failed: {err}", level="error")
            return None

    branch = task["branch_name"]
    await db.add_log(task["id"], f"Pushing branch {branch}...")

    rc, _, err = await run_cmd(
        "git",
        "push",
        "-u",
        "origin",
        branch,
        cwd=worktree_path,
        timeout=120,
    )
    if rc != 0:
        await db.add_log(task["id"], f"git push failed: {err}", level="error")
        raise RuntimeError(f"git push failed: {err}")

    pr_title = f"[backporcher] {task['prompt'][:60]}"
    issue_num = task.get("github_issue_number")
    closes_line = f"\n\nCloses #{issue_num}" if issue_num else ""
    pr_body = (
        f"## Backporcher Task #{task['id']}\n\n"
        f"**Prompt:** {task['prompt'][:500]}\n\n"
        f"**Model:** {task['model']}\n\n"
        f"---\n_Created by Backporcher dispatcher_{closes_line}"
    )
    rc, out, err = await run_cmd(
        "gh",
        "pr",
        "create",
        "--title",
        pr_title,
        "--body",
        pr_body,
        "--head",
        branch,
        "--base",
        repo["default_branch"],
        cwd=worktree_path,
        timeout=60,
    )
    if rc != 0:
        await db.add_log(task["id"], f"PR creation failed: {err}", level="error")
        return None

    pr_url = out.strip()
    pr_number = extract_pr_number_from_url(pr_url)
    if pr_number:
        await db.update_task(task["id"], pr_number=pr_number)
    else:
        log.warning("Could not extract PR number from URL: %s", pr_url)
    log.info("Created PR: %s", pr_url)
    return pr_url


REVIEW_PROMPT_TEMPLATE = """\
You are a code review coordinator. Your job is to review a PR created by an automated agent.

## Original Task
{task_prompt}

## PR Diff
{pr_diff}

## Blast Radius Analysis
{blast_radius}

The above shows which functions, classes, and tests are affected by this change,
including indirect dependencies. Pay special attention to impacted code that was
NOT modified — these are potential regression points.

## Other Open Backporcher PRs (same repo)
{other_prs}

## Review Criteria
1. Does the diff actually address the task?
2. Are there obvious bugs, regressions, or security issues?
3. Does it conflict with any of the other open PRs listed above?
4. Is the scope appropriate (not too broad, not touching unrelated files)?
5. Are there indirectly impacted functions/tests (from the blast radius) that might break?

## Your Response
Analyze the PR, then end with exactly one of:
VERDICT: APPROVE
VERDICT: REJECT — {{one-line reason}}
"""


async def run_review(
    task: dict,
    config: Config,
    db: Database,
) -> tuple[str, str]:
    """Run coordinator review on a PR. Returns (verdict, summary).

    verdict: 'approve' | 'reject'
    summary: explanation text
    """
    repo = await db.get_repo(task["repo_id"])
    if not repo:
        raise ValueError(f"Repo {task['repo_id']} not found")

    pr_number = task.get("pr_number")
    if not pr_number:
        return "reject", "No PR number found on task"

    repo_full = repo_full_name_from_url(repo["github_url"])

    # Gather context in parallel (request full diff — graph handles smart truncation)
    diff_coro = get_pr_diff(repo_full, pr_number, max_chars=0)
    prs_coro = list_open_prs(repo_full)
    pr_diff, open_prs = await asyncio.gather(diff_coro, prs_coro)

    if not pr_diff:
        return "reject", "Could not retrieve PR diff"

    # Build blast radius context via code graph
    blast_radius_text = "(dependency graph not available)"
    repo_local_path = Path(repo["local_path"]) if repo.get("local_path") else None
    if repo_local_path and repo_local_path.exists():
        try:
            from .graph.context import build_review_context, ensure_graph

            store = await ensure_graph(repo_local_path)
            if store:
                try:
                    pr_diff, blast_radius_text = build_review_context(store, pr_diff, repo_local_path)
                finally:
                    store.close()
        except Exception:
            log.exception("Graph context failed for task %d, falling back to raw diff", task["id"])
            # Fallback: truncate diff the old way
            if len(pr_diff) > 15000:
                pr_diff = pr_diff[:15000] + "\n...(diff truncated at 15000 chars)..."

    # Format other open PRs (exclude this one)
    other_prs_lines = []
    for pr in open_prs:
        if pr["number"] == pr_number:
            continue
        files = ", ".join(pr["changed_files"][:10]) or "(unknown)"
        other_prs_lines.append(f"- PR #{pr['number']}: {pr['title']} [files: {files}]")
    other_prs_text = "\n".join(other_prs_lines) if other_prs_lines else "(none)"

    # Build the review prompt
    review_prompt = REVIEW_PROMPT_TEMPLATE.format(
        task_prompt=task["prompt"][:2000],
        pr_diff=pr_diff,
        blast_radius=blast_radius_text,
        other_prs=other_prs_text,
    )

    # Run claude -p for the review
    worktree_path = task.get("worktree_path")
    cwd = worktree_path if worktree_path and Path(worktree_path).exists() else repo["local_path"]

    cmd = [
        "claude",
        "-p",
        "--output-format",
        "text",
        "--model",
        config.coordinator_model,
        review_prompt,
    ]

    if config.agent_user:
        cmd = [
            "sudo",
            "-u",
            config.agent_user,
            "--",
            "prlimit",
            "--nproc=500",
            "--fsize=2147483648",
            "--",
            *cmd,
        ]
        agent_env = None
    else:
        _sensitive_vars = {
            "CLAUDECODE",
            "SSH_AUTH_SOCK",
            "SSH_AGENT_PID",
            "GIT_ASKPASS",
            "GIT_CREDENTIALS",
            "GITHUB_TOKEN",
            "AWS_SECRET_ACCESS_KEY",
            "AWS_SESSION_TOKEN",
            "ANTHROPIC_API_KEY",
            "OPENAI_API_KEY",
        }
        agent_env = {k: v for k, v in os.environ.items() if k not in _sensitive_vars}

    log.info("Running coordinator review for task %d (PR #%d)", task["id"], pr_number)

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
        **({"env": agent_env} if agent_env is not None else {}),
    )

    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(),
            timeout=300,
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return "reject", "Review timed out after 300s"

    output = stdout.decode(errors="replace")
    if proc.returncode != 0:
        err_text = stderr.decode(errors="replace")
        log.error("Review agent failed for task %d: %s", task["id"], err_text[:500])
        return "reject", f"Review agent exited with code {proc.returncode}: {err_text[:500]}"

    # Parse verdict — strip markdown bold/italic markers before matching
    verdict = "reject"
    for line in reversed(output.strip().splitlines()):
        cleaned = line.strip().strip("*_").strip().upper()
        if cleaned.startswith("VERDICT: APPROVE"):
            verdict = "approve"
            break
        elif cleaned.startswith("VERDICT: REJECT"):
            verdict = "reject"
            break

    summary = output[-4000:] if len(output) > 4000 else output
    return verdict, summary


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
            timeout=30,
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


async def retry_with_ci_context(
    task: dict,
    ci_logs: str,
    config: Config,
    db: Database,
):
    """Re-run the agent with CI failure context on the existing branch."""
    task_id = task["id"]
    repo = await db.get_repo(task["repo_id"])
    if not repo:
        raise ValueError(f"Repo {task['repo_id']} not found")

    worktree_path = Path(task["worktree_path"])
    if not worktree_path.exists():
        raise RuntimeError(f"Worktree missing for task {task_id}: {worktree_path}")

    # Pull latest on the branch
    repo_lock = _get_repo_lock(repo["id"])
    async with repo_lock:
        rc, _, err = await run_cmd("git", "pull", "--rebase", cwd=worktree_path)
        if rc != 0:
            log.warning("git pull failed for retry task %d: %s", task_id, err)

    # Build augmented prompt
    augmented_prompt = (
        f"{task['prompt']}\n\n"
        f"---\n"
        f"IMPORTANT: The previous attempt created a PR but CI checks failed. "
        f"Please fix the issues shown in the CI logs below and commit the fixes.\n\n"
        f"CI FAILURE LOGS:\n```\n{ci_logs}\n```"
    )

    # Temporarily patch the task's prompt for the agent run
    patched_task = dict(task)
    patched_task["prompt"] = augmented_prompt

    await db.add_log(task_id, f"Retry #{task['retry_count']}: running agent with CI context")
    exit_code, summary = await run_agent(patched_task, worktree_path, config, db)

    if exit_code != 0:
        now = datetime.now(timezone.utc).isoformat()
        await db.update_task(
            task_id,
            status="failed",
            error_message=f"Retry agent exited with code {exit_code}",
            completed_at=now,
        )
        await _mark_issue_failed(
            task,
            db,
            f"CI retry agent failed with exit code {exit_code}.",
        )
        await cleanup_task_artifacts(task, db)
        return

    # Push fixes (force-with-lease since we're updating the same branch)
    branch = task["branch_name"]
    rc, _, err = await run_cmd(
        "git",
        "push",
        "--force-with-lease",
        "origin",
        branch,
        cwd=worktree_path,
        timeout=120,
    )
    if rc != 0:
        await db.add_log(task_id, f"Force push failed on retry: {err}", level="error")
        raise RuntimeError(f"git push failed on retry: {err}")

    # Back to pr_created — CI monitor will check again
    await db.update_task(task_id, status="pr_created")
    await db.add_log(task_id, f"Retry #{task['retry_count']}: pushed fixes, awaiting CI")


TRIAGE_PROMPT_TEMPLATE = """\
You are a task complexity classifier for a code agent system. Given a GitHub issue, decide which AI model should work on it.

## Models Available
- **sonnet**: Fast, cheap. Good for: bug fixes, single-file changes, config tweaks, adding a flag/parameter, documentation, straightforward implementations with clear instructions.
- **opus**: Slower, expensive, but much more capable. Required for: multi-file refactors, architectural changes, new subsystems, state management rewrites, complex feature implementations requiring design decisions, anything involving "extract", "redesign", "rewrite", or decomposition of large files.

## Issue
**Title:** {title}
**Body:**
{body}

## Instructions
Analyze the issue scope and complexity. Consider:
1. How many files will likely need changes?
2. Does it require architectural decisions or just following instructions?
3. Is it a patch/fix or a structural change?
4. How much code will likely be written (< 100 lines = sonnet, > 300 lines = opus)?

Respond with exactly one line in this format:
MODEL: sonnet — {{reason}}
or
MODEL: opus — {{reason}}
"""


async def triage_issue(title: str, body: str, config: Config) -> tuple[str, str]:
    """Run haiku to classify issue complexity. Returns (model, reason)."""
    prompt = TRIAGE_PROMPT_TEMPLATE.format(
        title=title,
        body=(body or "(no body)")[:3000],
    )

    cmd = ["claude", "-p", "--output-format", "text", "--model", "haiku", prompt]

    if config.agent_user:
        cmd = [
            "sudo",
            "-u",
            config.agent_user,
            "--",
            "prlimit",
            "--nproc=500",
            "--fsize=2147483648",
            "--",
            *cmd,
        ]
        agent_env = None
    else:
        _sensitive_vars = {
            "CLAUDECODE",
            "SSH_AUTH_SOCK",
            "SSH_AGENT_PID",
            "GIT_ASKPASS",
            "GIT_CREDENTIALS",
            "GITHUB_TOKEN",
            "AWS_SECRET_ACCESS_KEY",
            "AWS_SESSION_TOKEN",
            "ANTHROPIC_API_KEY",
            "OPENAI_API_KEY",
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
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        log.warning("Triage timed out, defaulting to %s", config.default_model)
        return config.default_model, "triage timed out"

    output = stdout.decode(errors="replace").strip()
    if proc.returncode != 0:
        log.warning("Triage failed (exit %d), defaulting to %s", proc.returncode, config.default_model)
        return config.default_model, f"triage failed (exit {proc.returncode})"

    # Parse "MODEL: opus — reason" or "MODEL: sonnet — reason"
    for line in output.strip().splitlines():
        cleaned = line.strip().strip("*_").strip()
        upper = cleaned.upper()
        if upper.startswith("MODEL: OPUS"):
            reason = (
                cleaned.split("—", 1)[-1].strip()
                if "—" in cleaned
                else cleaned.split("-", 1)[-1].strip()
                if "- " in cleaned
                else "classified as complex"
            )
            return "opus", reason
        elif upper.startswith("MODEL: SONNET"):
            reason = (
                cleaned.split("—", 1)[-1].strip()
                if "—" in cleaned
                else cleaned.split("-", 1)[-1].strip()
                if "- " in cleaned
                else "classified as straightforward"
            )
            return "sonnet", reason

    log.warning("Could not parse triage output, defaulting to %s: %s", config.default_model, output[:200])
    return config.default_model, "unparseable triage output"


BATCH_ORCHESTRATE_PROMPT_TEMPLATE = """\
You are a task orchestrator for a parallel code agent system. Given a batch of GitHub issues \
for the same repository, analyze them together and produce a plan.

## Models Available
- **sonnet**: Fast, cheap. Bug fixes, single-file changes, config tweaks, docs.
- **opus**: Slower, expensive. Multi-file refactors, architectural changes, complex features.

## Issues (same repo: {repo_name})
{issues_block}

## Instructions
For each issue, determine:
1. **model**: "sonnet" or "opus"
2. **priority**: integer 1 to {n_issues}. 1 = run first. No duplicates.
3. **depends_on**: issue number this depends on, or null. Use when changes would conflict \
or build upon another issue. Chains are fine (A -> B -> C). No circular dependencies.

Rules:
- Only set depends_on for genuine ordering requirements (file conflicts, sequential changes)
- Independent issues can run in parallel (no dependency needed)
- Priority reflects logical ordering: foundational changes first

## Response Format
Respond with ONLY a JSON array, no markdown fences:
[
  {{"issue_number": 1, "model": "sonnet", "priority": 1, "depends_on": null, "reason": "..."}},
  {{"issue_number": 2, "model": "opus", "priority": 2, "depends_on": 1, "reason": "..."}}
]
"""


async def orchestrate_batch(
    issues: list[dict],
    repo_name: str,
    config: Config,
) -> list[dict] | None:
    """Batch-orchestrate multiple issues via haiku. Returns list of dicts with
    issue_number, model, priority, depends_on, reason. Returns None on failure."""
    issues_lines = []
    for iss in issues:
        body = (iss.get("body") or "(no body)")[:1000]
        issues_lines.append(f"### Issue #{iss['number']}: {iss['title']}\n{body}\n")

    prompt = BATCH_ORCHESTRATE_PROMPT_TEMPLATE.format(
        repo_name=repo_name,
        issues_block="\n".join(issues_lines),
        n_issues=len(issues),
    )

    cmd = ["claude", "-p", "--output-format", "text", "--model", "haiku", prompt]

    if config.agent_user:
        cmd = [
            "sudo",
            "-u",
            config.agent_user,
            "--",
            "prlimit",
            "--nproc=500",
            "--fsize=2147483648",
            "--",
            *cmd,
        ]
        agent_env = None
    else:
        _sensitive_vars = {
            "CLAUDECODE",
            "SSH_AUTH_SOCK",
            "SSH_AGENT_PID",
            "GIT_ASKPASS",
            "GIT_CREDENTIALS",
            "GITHUB_TOKEN",
            "AWS_SECRET_ACCESS_KEY",
            "AWS_SESSION_TOKEN",
            "ANTHROPIC_API_KEY",
            "OPENAI_API_KEY",
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
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=90)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        log.warning("Batch orchestration timed out")
        return None

    output = stdout.decode(errors="replace").strip()
    if proc.returncode != 0:
        log.warning("Batch orchestration failed (exit %d): %s", proc.returncode, stderr.decode(errors="replace")[:200])
        return None

    # Strip markdown fences if present
    cleaned = output
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        # Remove first and last fence lines
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        cleaned = "\n".join(lines)

    try:
        result = json.loads(cleaned)
    except json.JSONDecodeError:
        log.warning("Batch orchestration returned invalid JSON: %s", cleaned[:300])
        return None

    if not isinstance(result, list):
        log.warning("Batch orchestration returned non-list: %s", type(result))
        return None

    # Validate entries
    issue_numbers = {iss["number"] for iss in issues}
    valid_models = {"sonnet", "opus"}
    validated = []

    for entry in result:
        num = entry.get("issue_number")
        if num not in issue_numbers:
            continue
        model = entry.get("model", config.default_model)
        if model not in valid_models:
            model = config.default_model
        priority = entry.get("priority", 100)
        if not isinstance(priority, int):
            priority = 100
        depends_on = entry.get("depends_on")
        if depends_on is not None and depends_on not in issue_numbers:
            depends_on = None
        reason = entry.get("reason", "")
        validated.append(
            {
                "issue_number": num,
                "model": model,
                "priority": priority,
                "depends_on": depends_on,
                "reason": str(reason)[:200],
            }
        )

    # Fill in any issues the orchestrator omitted
    seen_numbers = {e["issue_number"] for e in validated}
    for iss in issues:
        if iss["number"] not in seen_numbers:
            validated.append(
                {
                    "issue_number": iss["number"],
                    "model": config.default_model,
                    "priority": 100,
                    "depends_on": None,
                    "reason": "omitted by orchestrator, using defaults",
                }
            )

    return validated


async def sync_agent_credentials(config: Config):
    """Copy admin's Claude credentials to agent user if they're newer."""
    if not config.agent_user:
        return
    admin_cred = Path.home() / ".claude" / ".credentials.json"
    agent_cred = Path(f"/home/{config.agent_user}/.claude/.credentials.json")
    if not admin_cred.exists():
        return
    # Use sudo stat to check agent cred mtime (file is 600 owned by agent user)
    need_sync = True
    rc, out, _ = await run_cmd("sudo", "stat", "-c", "%Y", str(agent_cred))
    if rc == 0:
        try:
            agent_mtime = float(out.strip())
            need_sync = admin_cred.stat().st_mtime > agent_mtime
        except (ValueError, OSError):
            pass

    if need_sync:
        log.info("Syncing Claude credentials to %s", config.agent_user)
        rc, _, err = await run_cmd(
            "sudo",
            "install",
            "-m",
            "600",
            "-o",
            config.agent_user,
            "-g",
            "backporcher",
            str(admin_cred),
            str(agent_cred),
        )
        if rc != 0:
            log.warning("Failed to sync credentials: %s", err.strip())


def _pick_retry_model(current_model: str, retry_count: int) -> str:
    """Escalate model on retry. Sonnet -> opus after first attempt."""
    if current_model == "sonnet" and retry_count >= 1:
        log.info("Model escalation: sonnet -> opus (retry %d)", retry_count)
        return "opus"
    return current_model


CONFLICT_CHECK_PROMPT_TEMPLATE = """\
You are a task conflict detector for a parallel code agent system. Given a new task and the \
tasks already running in the same repository, determine if they likely touch overlapping files.

## New Task
{new_task_prompt}

## Currently In-Flight Tasks
{inflight_summaries}

## Instructions
Analyze whether the new task would likely modify the same files as any in-flight task.
Consider: same components, same modules, same config files, same test files.
Be conservative — if there's a reasonable chance of overlap, flag it.

Respond with ONLY a JSON object (no markdown fences):
{{"conflict": true/false, "conflicting_task_id": <id>|null, "reason": "brief explanation"}}
"""


async def check_task_conflict(
    task_prompt: str,
    inflight_tasks: list[dict],
    config: Config,
) -> dict | None:
    """Check if a new task conflicts with in-flight tasks. Returns conflict info or None.

    Calls haiku with a focused prompt. Fail-open: returns None on any error.
    """
    if not inflight_tasks:
        return None

    summaries = []
    for t in inflight_tasks:
        summaries.append(f"- Task #{t['id']} ({t['status']}): {t['prompt'][:200]}")
    inflight_text = "\n".join(summaries)

    prompt = CONFLICT_CHECK_PROMPT_TEMPLATE.format(
        new_task_prompt=task_prompt[:2000],
        inflight_summaries=inflight_text,
    )

    cmd = ["claude", "-p", "--output-format", "text", "--model", "haiku", prompt]

    if config.agent_user:
        cmd = [
            "sudo",
            "-u",
            config.agent_user,
            "--",
            "prlimit",
            "--nproc=500",
            "--fsize=2147483648",
            "--",
            *cmd,
        ]
        agent_env = None
    else:
        _sensitive_vars = {
            "CLAUDECODE",
            "SSH_AUTH_SOCK",
            "SSH_AGENT_PID",
            "GIT_ASKPASS",
            "GIT_CREDENTIALS",
            "GITHUB_TOKEN",
            "AWS_SECRET_ACCESS_KEY",
            "AWS_SESSION_TOKEN",
            "ANTHROPIC_API_KEY",
            "OPENAI_API_KEY",
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
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        log.warning("Conflict check timed out, proceeding without blocking")
        return None

    output = stdout.decode(errors="replace").strip()
    if proc.returncode != 0:
        log.warning("Conflict check failed (exit %d), proceeding", proc.returncode)
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
        log.warning("Conflict check returned invalid JSON: %s", cleaned[:200])
        return None

    if not isinstance(result, dict):
        return None

    if result.get("conflict"):
        return result
    return None


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
            await ensure_repo_permissions(repo_path, config)

            branch = make_branch_name(task_id, task["prompt"])
            await db.update_task(task_id, branch_name=branch)
            await db.add_log(task_id, f"Creating worktree on branch {branch}")
            worktree_path = await setup_worktree(
                repo_path,
                task_id,
                branch,
                repo["default_branch"],
            )
            await db.update_task(task_id, worktree_path=str(worktree_path))

        # Ensure agent credentials are fresh before launching
        await sync_agent_credentials(config)

        # Record agent start timing and model info
        agent_start_now = datetime.now(timezone.utc).isoformat()
        await db.update_task(
            task_id,
            agent_started_at=agent_start_now,
            initial_model=task["model"],
            model_used=task["model"],
        )
        await db.record_metric(
            "agent_start",
            task_id=task_id,
            repo=task.get("repo_name"),
            model=task["model"],
        )

        # Run agent
        await db.add_log(task_id, "Running agent...")
        exit_code, summary = await run_agent(task, worktree_path, config, db)

        # Record agent finish timing
        agent_finish_now = datetime.now(timezone.utc).isoformat()
        await db.update_task(
            task_id,
            exit_code=exit_code,
            output_summary=summary[:4000] if summary else None,
            agent_finished_at=agent_finish_now,
            model_used=task["model"],
        )

        if exit_code != 0:
            retry_count = task.get("retry_count", 0)
            max_retries = config.max_task_retries

            if retry_count < max_retries:
                new_count = retry_count + 1
                new_model = _pick_retry_model(task["model"], new_count)
                await db.update_task(
                    task_id,
                    status="queued",
                    error_message=None,
                    started_at=None,
                    branch_name=None,
                    worktree_path=None,
                    retry_count=new_count,
                    model=new_model,
                )
                reason = f"exit {exit_code}"
                await db.add_log(
                    task_id,
                    f"Agent failed ({reason}), retry {new_count}/{max_retries} (model={new_model})",
                    level="warn",
                )
                log.info(
                    "Task %d: agent failed (%s), retry %d/%d (model=%s)",
                    task_id,
                    reason,
                    new_count,
                    max_retries,
                    new_model,
                )
                await db.record_metric(
                    "retry_agent",
                    task_id=task_id,
                    repo=task.get("repo_name"),
                    model=new_model,
                )
                return

            # Max retries exhausted — permanent failure
            now = datetime.now(timezone.utc).isoformat()
            await db.update_task(
                task_id,
                status="failed",
                error_message=f"Agent exited with code {exit_code} (retries exhausted)",
                completed_at=now,
            )
            await db.add_log(task_id, f"Agent failed (exit {exit_code}), retries exhausted", level="error")
            await _mark_issue_failed(
                task,
                db,
                f"Agent failed with exit code {exit_code} (retries exhausted).",
            )
            await cleanup_task_artifacts(task, db)
            cascaded = await db.handle_dependency_failure(task_id)
            if cascaded:
                log.info("Task #%d failure cascaded to tasks: %s", task_id, cascaded)
            return

        # Build verification loop
        verify_command = repo.get("verify_command")
        if verify_command:
            for attempt in range(1, config.max_verify_retries + 2):  # +2: initial + retries
                passed, verify_output = await run_verify(
                    worktree_path,
                    verify_command,
                    task_id,
                    db,
                    config,
                )
                if passed:
                    break

                if attempt > config.max_verify_retries:
                    now = datetime.now(timezone.utc).isoformat()
                    await db.update_task(
                        task_id,
                        status="failed",
                        error_message=f"Verify failed after {config.max_verify_retries} fix attempts",
                        completed_at=now,
                    )
                    await db.add_log(
                        task_id,
                        f"Verify failed after {config.max_verify_retries} retries, giving up",
                        level="error",
                    )
                    await _mark_issue_failed(
                        task,
                        db,
                        f"Build verification failed after {config.max_verify_retries} fix attempts.",
                    )
                    await cleanup_task_artifacts(task, db)
                    cascaded = await db.handle_dependency_failure(task_id)
                    if cascaded:
                        log.info("Task #%d failure cascaded to tasks: %s", task_id, cascaded)
                    return

                # Re-run agent with verify failure context
                await db.add_log(
                    task_id,
                    f"Verify fix attempt {attempt}/{config.max_verify_retries}",
                )
                fix_prompt = (
                    f"{task['prompt']}\n\n"
                    f"---\n"
                    f"IMPORTANT: Your previous changes failed the build/test verification.\n"
                    f"The verify command `{verify_command}` failed with this output:\n\n"
                    f"```\n{verify_output}\n```\n\n"
                    f"Fix the errors and make sure the build passes."
                )
                fix_task = dict(task)
                fix_task["prompt"] = fix_prompt
                exit_code, summary = await run_agent(fix_task, worktree_path, config, db)
                if exit_code != 0:
                    # Let the outer exception handler deal with retry/escalation
                    raise RuntimeError(f"Verify fix agent exited with code {exit_code}")

        # Create PR
        await db.add_log(task_id, "Creating pull request...")
        # Re-read task to get branch_name
        task = await db.get_task(task_id)
        pr_url = await create_pr(worktree_path, task, repo, db)
        now = datetime.now(timezone.utc).isoformat()

        if pr_url:
            await db.update_task(
                task_id,
                status="pr_created",
                pr_url=pr_url,
                completed_at=now,
            )
            await db.add_log(task_id, f"PR created: {pr_url}")

            # Comment on the GitHub issue if this task came from one
            issue_num = task.get("github_issue_number")
            if issue_num:
                repo_full = repo_full_name_from_url(repo["github_url"])
                await comment_on_issue(
                    repo_full,
                    issue_num,
                    f"PR created: {pr_url}\n\nAwaiting CI checks.",
                )
        else:
            await db.update_task(
                task_id,
                status="completed",
                completed_at=now,
            )
            await db.add_log(task_id, "Completed (no changes)")
            await cleanup_task_artifacts(task, db)

    except Exception as e:
        log.exception("Task %d failed", task_id)
        err_str = str(e)[:2000]
        now = datetime.now(timezone.utc).isoformat()

        retry_count = task.get("retry_count", 0)
        if retry_count < config.max_task_retries:
            new_count = retry_count + 1
            new_model = _pick_retry_model(task.get("model", "sonnet"), new_count)
            await db.update_task(
                task_id,
                status="queued",
                error_message=None,
                started_at=None,
                branch_name=None,
                worktree_path=None,
                retry_count=new_count,
                model=new_model,
            )
            await db.add_log(
                task_id,
                f"Error, retry {new_count}/{config.max_task_retries} (model={new_model}): {err_str[:200]}",
                level="warn",
            )
            log.info(
                "Task %d: error, retry %d/%d (model=%s)",
                task_id,
                new_count,
                config.max_task_retries,
                new_model,
            )
            await db.record_metric(
                "retry_agent",
                task_id=task_id,
                repo=task.get("repo_name"),
                model=new_model,
            )
        else:
            await db.update_task(
                task_id,
                status="failed",
                error_message=err_str,
                completed_at=now,
            )
            await db.add_log(task_id, f"Fatal error (retries exhausted): {e}", level="error")
            await _mark_issue_failed(
                task,
                db,
                f"Task failed with error: {err_str[:300]}",
            )
            await cleanup_task_artifacts(task, db)
            cascaded = await db.handle_dependency_failure(task_id)
            if cascaded:
                log.info("Task #%d failure cascaded to tasks: %s", task_id, cascaded)

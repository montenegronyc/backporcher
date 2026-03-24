"""Review: coordinator PR review and PR creation."""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from .config import Config
from .constants import (
    MAX_PR_DIFF_CHARS,
    SENSITIVE_ENV_VARS,
    TIMEOUT_COMMIT_PUSH,
    TIMEOUT_PR_CREATE,
    TIMEOUT_REVIEW_AGENT,
    TRUNCATE_COMMIT_MSG,
    TRUNCATE_LOG_LINE,
    TRUNCATE_PR_TITLE,
    TRUNCATE_PROMPT_FOR_REVIEW,
    TRUNCATE_REVIEW_OUTPUT,
    TRUNCATE_SUMMARY,
    prlimit_args,
)
from .db import Database
from .git_ops import run_cmd
from .github import (
    extract_pr_number_from_url,
    get_pr_diff,
    list_open_prs,
    repo_full_name_from_url,
)
from .prompts import REVIEW_PROMPT_TEMPLATE

log = logging.getLogger("backporcher.review")


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
            if len(pr_diff) > MAX_PR_DIFF_CHARS:
                pr_diff = pr_diff[:MAX_PR_DIFF_CHARS] + f"\n...(diff truncated at {MAX_PR_DIFF_CHARS} chars)..."

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
        task_prompt=task["prompt"][:TRUNCATE_PROMPT_FOR_REVIEW],
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
            timeout=TIMEOUT_REVIEW_AGENT,
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return "reject", f"Review timed out after {TIMEOUT_REVIEW_AGENT}s"

    output = stdout.decode(errors="replace")
    if proc.returncode != 0:
        err_text = stderr.decode(errors="replace")
        log.error("Review agent failed for task %d: %s", task["id"], err_text[:TRUNCATE_LOG_LINE])
        return "reject", f"Review agent exited with code {proc.returncode}: {err_text[:TRUNCATE_LOG_LINE]}"

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

    summary = output[-TRUNCATE_REVIEW_OUTPUT:] if len(output) > TRUNCATE_REVIEW_OUTPUT else output
    return verdict, summary


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
        commit_msg = f"backporcher: {task['prompt'][:TRUNCATE_COMMIT_MSG]}\n\nTask #{task['id']}"
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
        timeout=TIMEOUT_COMMIT_PUSH,
    )
    if rc != 0:
        await db.add_log(task["id"], f"git push failed: {err}", level="error")
        raise RuntimeError(f"git push failed: {err}")

    pr_title = f"[backporcher] {task['prompt'][:TRUNCATE_PR_TITLE]}"
    issue_num = task.get("github_issue_number")
    closes_line = f"\n\nCloses #{issue_num}" if issue_num else ""
    pr_body = (
        f"## Backporcher Task #{task['id']}\n\n"
        f"**Prompt:** {task['prompt'][:TRUNCATE_SUMMARY]}\n\n"
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
        timeout=TIMEOUT_PR_CREATE,
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

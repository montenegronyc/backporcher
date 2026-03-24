"""GitHub shared helpers — gh CLI runner, dataclasses, URL utilities."""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass

log = logging.getLogger("backporcher.github")


@dataclass
class GitHubIssue:
    number: int
    title: str
    body: str
    url: str
    author: str
    labels: list[str]


@dataclass
class CIStatus:
    state: str  # pending | success | failure | no_checks
    failed_checks: list[str]
    total: int
    completed: int


async def _run_gh(
    *args: str,
    timeout: int = 30,
) -> tuple[int, str, str]:
    """Run a gh CLI command. Returns (returncode, stdout, stderr)."""
    proc = await asyncio.create_subprocess_exec(
        "gh",
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return -1, "", f"gh command timed out after {timeout}s"

    return (
        proc.returncode,
        stdout.decode(errors="replace"),
        stderr.decode(errors="replace"),
    )


def repo_full_name_from_url(github_url: str) -> str:
    """Extract 'owner/repo' from a GitHub URL."""
    path = github_url.strip().rstrip("/").removesuffix(".git")
    match = re.search(r"github\.com/([^/]+/[^/]+)", path)
    if not match:
        raise ValueError(f"Cannot extract owner/repo from: {github_url}")
    return match.group(1)


def extract_pr_number_from_url(pr_url: str) -> int | None:
    """Extract PR number from a GitHub PR URL."""
    match = re.search(r"/pull/(\d+)", pr_url)
    return int(match.group(1)) if match else None

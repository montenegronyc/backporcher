"""GitHub PR operations — CI status, diffs, comments, merge, conflict checks."""

from __future__ import annotations

import json
import logging

from .github_base import CIStatus, _run_gh

log = logging.getLogger("backporcher.github")


async def get_pr_ci_status(
    repo_full_name: str,
    pr_number: int,
) -> CIStatus:
    """Check CI status of a PR using statusCheckRollup."""
    rc, out, err = await _run_gh(
        "pr",
        "view",
        "--repo",
        repo_full_name,
        str(pr_number),
        "--json",
        "statusCheckRollup",
    )
    if rc != 0:
        log.error("Failed to get PR #%d status: %s", pr_number, err.strip())
        return CIStatus(state="pending", failed_checks=[], total=0, completed=0)

    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return CIStatus(state="pending", failed_checks=[], total=0, completed=0)

    checks = data.get("statusCheckRollup", []) or []
    if not checks:
        return CIStatus(state="no_checks", failed_checks=[], total=0, completed=0)

    total = len(checks)
    completed = 0
    failed = []

    for check in checks:
        conclusion = (check.get("conclusion") or "").upper()
        status = (check.get("status") or "").upper()
        name = check.get("name") or check.get("context") or "unknown"

        if conclusion in ("SUCCESS", "NEUTRAL", "SKIPPED"):
            completed += 1
        elif conclusion in ("FAILURE", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED"):
            completed += 1
            failed.append(name)
        elif status == "COMPLETED":
            completed += 1
            if conclusion and conclusion not in ("SUCCESS", "NEUTRAL", "SKIPPED"):
                failed.append(name)
        elif check.get("__typename") == "StatusContext":
            # StatusContext (e.g. CodeRabbit) uses a top-level `state` field, not `conclusion`
            state_val = (check.get("state") or "").upper()
            if state_val in ("SUCCESS", "NEUTRAL"):
                completed += 1
            elif state_val in ("FAILURE", "ERROR"):
                completed += 1
                failed.append(name)
            # PENDING stays as-is (still pending)

    if completed < total:
        state = "pending"
    elif failed:
        state = "failure"
    else:
        state = "success"

    return CIStatus(state=state, failed_checks=failed, total=total, completed=completed)


async def get_ci_failure_logs(
    repo_full_name: str,
    branch: str,
) -> str:
    """Get logs from the most recent failed CI run on a branch."""
    # Step 1: find the failed run
    rc, out, err = await _run_gh(
        "run",
        "list",
        "--repo",
        repo_full_name,
        "--branch",
        branch,
        "--status",
        "failure",
        "--json",
        "databaseId",
        "--limit",
        "1",
    )
    if rc != 0 or not out.strip():
        return "No failed CI runs found."

    try:
        runs = json.loads(out)
    except json.JSONDecodeError:
        return "Failed to parse CI run list."

    if not runs:
        return "No failed CI runs found."

    run_id = runs[0]["databaseId"]

    # Step 2: get the failed logs
    rc, out, err = await _run_gh(
        "run",
        "view",
        "--repo",
        repo_full_name,
        str(run_id),
        "--log-failed",
        timeout=60,
    )
    if rc != 0:
        return f"Failed to fetch CI logs: {err[:500]}"

    # Truncate to 4000 chars
    if len(out) > 4000:
        out = out[-4000:]  # Keep the tail (most relevant)
        out = "...(truncated)...\n" + out

    return out


async def get_pr_diff(repo_full_name: str, pr_number: int, max_chars: int = 15000) -> str:
    """Get unified diff for a PR. Truncate to max_chars (0 = no truncation)."""
    rc, out, err = await _run_gh(
        "pr",
        "diff",
        "--repo",
        repo_full_name,
        str(pr_number),
        timeout=60,
    )
    if rc != 0:
        log.error("Failed to get PR #%d diff: %s", pr_number, err.strip())
        return ""

    if max_chars and len(out) > max_chars:
        out = out[:max_chars] + f"\n...(diff truncated at {max_chars} chars)..."
    return out


async def comment_on_pr(
    repo_full_name: str,
    pr_number: int,
    body: str,
) -> bool:
    """Post a comment on a PR."""
    rc, _, err = await _run_gh(
        "pr",
        "comment",
        "--repo",
        repo_full_name,
        str(pr_number),
        "--body",
        body,
    )
    if rc != 0:
        log.error("Failed to comment on PR #%d: %s", pr_number, err.strip())
        return False
    return True


async def close_pr(
    repo_full_name: str,
    pr_number: int,
    comment: str | None = None,
) -> bool:
    """Close a PR with optional comment."""
    if comment:
        await _run_gh(
            "pr",
            "comment",
            "--repo",
            repo_full_name,
            str(pr_number),
            "--body",
            comment,
        )

    rc, _, err = await _run_gh(
        "pr",
        "close",
        "--repo",
        repo_full_name,
        str(pr_number),
    )
    if rc != 0:
        log.error("Failed to close PR #%d: %s", pr_number, err.strip())
        return False
    return True


async def is_pr_conflicting(repo_full_name: str, pr_number: int) -> bool:
    """Check if a PR has merge conflicts."""
    rc, out, _ = await _run_gh(
        "pr",
        "view",
        "--repo",
        repo_full_name,
        str(pr_number),
        "--json",
        "mergeable",
    )
    if rc != 0:
        return False
    try:
        data = json.loads(out)
        return data.get("mergeable") == "CONFLICTING"
    except (json.JSONDecodeError, KeyError):
        return False


async def merge_pr(
    repo_full_name: str,
    pr_number: int,
    method: str = "squash",
) -> bool:
    """Merge a PR directly (coordinator auto-merge)."""
    rc, _, err = await _run_gh(
        "pr",
        "merge",
        "--repo",
        repo_full_name,
        str(pr_number),
        f"--{method}",
    )
    if rc != 0:
        log.error("Failed to merge PR #%d: %s", pr_number, err.strip())
        return False

    log.info("Merged PR #%d on %s (%s)", pr_number, repo_full_name, method)
    return True


async def list_open_prs(
    repo_full_name: str,
    label: str = "backporcher-in-progress",
) -> list[dict]:
    """List open backporcher PRs for conflict awareness."""
    rc, out, err = await _run_gh(
        "pr",
        "list",
        "--repo",
        repo_full_name,
        "--label",
        label,
        "--state",
        "open",
        "--json",
        "number,title,headRefName,files",
        "--limit",
        "50",
    )
    if rc != 0:
        log.error("Failed to list open PRs for %s: %s", repo_full_name, err.strip())
        return []

    try:
        prs = json.loads(out)
    except json.JSONDecodeError:
        log.error("Invalid JSON from gh pr list: %s", out[:200])
        return []

    results = []
    for pr in prs:
        changed_files = [f.get("path", "") for f in (pr.get("files") or [])]
        results.append(
            {
                "number": pr["number"],
                "title": pr.get("title", ""),
                "branch": pr.get("headRefName", ""),
                "changed_files": changed_files,
            }
        )
    return results

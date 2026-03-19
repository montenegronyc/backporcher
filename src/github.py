"""GitHub CLI interactions — all `gh` calls run as administrator, never in sandbox."""

import asyncio
import json
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


REQUIRED_LABELS = {
    "backporcher-in-progress": ("FBCA04", "Backporcher agent working"),
    "backporcher-done": ("0075CA", "Backporcher completed"),
    "backporcher-failed": ("D93F0B", "Backporcher agent failed"),
}

# Track which repos we've already ensured labels for (per-process)
_labels_ensured: set[str] = set()


async def ensure_labels(repo_full_name: str) -> None:
    """Create required Backporcher labels if they don't exist on the repo."""
    if repo_full_name in _labels_ensured:
        return

    rc, out, _ = await _run_gh(
        "label",
        "list",
        "--repo",
        repo_full_name,
        "--json",
        "name",
        "--limit",
        "100",
    )
    if rc != 0:
        log.warning("Failed to list labels for %s, skipping ensure", repo_full_name)
        return

    try:
        existing = {lb["name"] for lb in json.loads(out)}
    except (json.JSONDecodeError, KeyError):
        return

    for label, (color, desc) in REQUIRED_LABELS.items():
        if label not in existing:
            rc, _, err = await _run_gh(
                "label",
                "create",
                label,
                "--repo",
                repo_full_name,
                "--color",
                color,
                "--description",
                desc,
            )
            if rc == 0:
                log.info("Created label '%s' on %s", label, repo_full_name)
            else:
                log.warning("Failed to create label '%s' on %s: %s", label, repo_full_name, err.strip())

    _labels_ensured.add(repo_full_name)


async def find_new_issues(
    repo_full_name: str,
    allowed_users: set[str],
) -> list[GitHubIssue]:
    """Find open issues labeled 'backporcher' that aren't claimed yet."""
    rc, out, err = await _run_gh(
        "issue",
        "list",
        "--repo",
        repo_full_name,
        "--label",
        "backporcher",
        "--state",
        "open",
        "--json",
        "number,title,body,url,labels,author",
        "--limit",
        "20",
    )
    if rc != 0:
        log.error("Failed to list issues for %s: %s", repo_full_name, err.strip())
        return []

    try:
        issues_data = json.loads(out)
    except json.JSONDecodeError:
        log.error("Invalid JSON from gh issue list: %s", out[:200])
        return []

    results = []
    for item in issues_data:
        author = item.get("author", {}).get("login", "")
        label_names = [lb.get("name", "") for lb in item.get("labels", [])]

        # Skip if already claimed
        if "backporcher-in-progress" in label_names:
            continue

        # Author allowlist check
        if author not in allowed_users:
            log.debug("Skipping issue #%d by %s (not in allowlist)", item["number"], author)
            continue

        results.append(
            GitHubIssue(
                number=item["number"],
                title=item.get("title", ""),
                body=item.get("body", ""),
                url=item.get("url", ""),
                author=author,
                labels=label_names,
            )
        )

    return results


async def claim_issue(repo_full_name: str, number: int) -> bool:
    """Add 'backporcher-in-progress' label, remove 'backporcher' label, assign self."""
    ok = await update_issue_labels(
        repo_full_name,
        number,
        add=["backporcher-in-progress"],
        remove=["backporcher"],
    )
    if not ok:
        return False

    # Assign to self (the gh-authenticated user)
    rc, _, err = await _run_gh(
        "issue",
        "edit",
        "--repo",
        repo_full_name,
        str(number),
        "--add-assignee",
        "@me",
    )
    if rc != 0:
        log.warning("Failed to assign issue #%d: %s", number, err.strip())
        # Non-fatal — label is the important part

    return True


async def comment_on_issue(
    repo_full_name: str,
    number: int,
    body: str,
) -> bool:
    """Post a comment on an issue."""
    rc, _, err = await _run_gh(
        "issue",
        "comment",
        "--repo",
        repo_full_name,
        str(number),
        "--body",
        body,
    )
    if rc != 0:
        log.error("Failed to comment on issue #%d: %s", number, err.strip())
        return False
    return True


async def close_issue(
    repo_full_name: str,
    number: int,
    reason: str = "completed",
) -> bool:
    """Close an issue."""
    rc, _, err = await _run_gh(
        "issue",
        "close",
        "--repo",
        repo_full_name,
        str(number),
        "--reason",
        reason,
    )
    if rc != 0:
        log.error("Failed to close issue #%d: %s", number, err.strip())
        return False
    return True


async def update_issue_labels(
    repo_full_name: str,
    number: int,
    add: list[str] | None = None,
    remove: list[str] | None = None,
) -> bool:
    """Add/remove labels on an issue."""
    args = ["issue", "edit", "--repo", repo_full_name, str(number)]
    for label in add or []:
        args.extend(["--add-label", label])
    for label in remove or []:
        args.extend(["--remove-label", label])

    rc, _, err = await _run_gh(*args)
    if rc != 0:
        log.error("Failed to update labels on issue #%d: %s", number, err.strip())
        return False
    return True


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
        # else: still pending

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

"""GitHub CLI interactions — issue operations, labels, and re-exports."""

from __future__ import annotations

import json
import logging

# Re-export shared helpers for backward compatibility
from .github_base import (  # noqa: F401
    CIStatus,
    GitHubIssue,
    _run_gh,
    extract_pr_number_from_url,
    repo_full_name_from_url,
)

# Re-export PR operations for backward compatibility
from .github_pr import (  # noqa: F401
    close_pr,
    comment_on_pr,
    get_ci_failure_logs,
    get_pr_ci_status,
    get_pr_diff,
    is_pr_conflicting,
    list_open_prs,
    merge_pr,
)

log = logging.getLogger("backporcher.github")

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

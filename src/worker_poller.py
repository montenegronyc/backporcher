"""Issue poller loop body: poll GitHub for new issues, triage, batch-orchestrate."""

from __future__ import annotations

import logging

from .config import Config
from .db import Database
from .dispatcher import (
    check_task_conflict,
    orchestrate_batch,
    triage_issue,
)
from .github import (
    claim_issue,
    ensure_labels,
    find_new_issues,
    repo_full_name_from_url,
)

log = logging.getLogger("backporcher.worker")


async def poll_issues(db: Database, config: Config, allowed_users: set[str]) -> None:
    """One iteration of issue polling across all repos."""
    repos = await db.list_repos()
    for repo in repos:
        repo_full = repo_full_name_from_url(repo["github_url"])
        await ensure_labels(repo_full)
        issues = await find_new_issues(repo_full, allowed_users)

        # Filter to genuinely new issues (dedup)
        new_issues = []
        for issue in issues:
            existing = await db.get_task_by_issue(repo["id"], issue.number)
            if not existing:
                new_issues.append(issue)

        if not new_issues:
            continue

        # Separate opus-labeled issues (manual override, no orchestration)
        opus_issues = [i for i in new_issues if "opus" in i.labels]
        normal_issues = [i for i in new_issues if "opus" not in i.labels]

        # Process opus-labeled issues directly
        for issue in opus_issues:
            await create_task_for_issue(
                db,
                config,
                repo,
                repo_full,
                issue,
                "opus",
                "opus label (manual override)",
            )

        # Normal issues: single = triage, 2+ = batch orchestrate
        if len(normal_issues) == 1:
            issue = normal_issues[0]
            agent, model, triage_reason = await triage_issue(
                issue.title,
                issue.body,
                config,
            )
            await create_task_for_issue(
                db,
                config,
                repo,
                repo_full,
                issue,
                model,
                triage_reason,
                agent=agent,
            )
        elif len(normal_issues) >= 2:
            await batch_create_tasks(
                db,
                config,
                repo,
                repo_full,
                normal_issues,
            )


async def create_task_for_issue(
    db: Database,
    config: Config,
    repo: dict,
    repo_full: str,
    issue,
    model: str,
    reason: str,
    priority: int = 100,
    depends_on_task_id: int | None = None,
    agent: str = "claude",
) -> int:
    """Create a single task from an issue and claim it on GitHub."""
    prompt = issue.title
    if issue.body and issue.body.strip():
        prompt = f"{issue.title}\n\n{issue.body}"

    task_id = await db.create_task_from_issue(
        repo["id"],
        prompt,
        model,
        issue.number,
        issue.url,
        priority=priority,
        depends_on_task_id=depends_on_task_id,
        agent=agent,
    )
    await db.add_log(
        task_id,
        f"Created from issue #{issue.number}: {issue.title[:80]}",
    )
    dep_info = f", depends_on=task#{depends_on_task_id}" if depends_on_task_id else ""
    await db.add_log(
        task_id,
        f"Triage: model={model}, priority={priority}{dep_info} — {reason[:200]}",
    )
    log.info(
        "Issue #%d -> Task #%d (pri=%d): %s",
        issue.number,
        task_id,
        priority,
        issue.title[:60],
    )
    await claim_issue(repo_full, issue.number)

    # Dispatch gate: in review-all mode, hold tasks for approval before dispatch
    if config.approval_mode == "review-all":
        await db.set_hold(task_id, "dispatch_approval")
        await db.add_log(task_id, "Held for dispatch approval (review-all mode)")
        log.info("Task #%d: held for dispatch approval", task_id)

    return task_id


async def batch_create_tasks(
    db: Database,
    config: Config,
    repo: dict,
    repo_full: str,
    issues: list,
) -> None:
    """Batch-orchestrate multiple issues and create tasks with dependencies."""
    issue_dicts = [{"number": i.number, "title": i.title, "body": i.body} for i in issues]
    log.info(
        "Batch orchestrating %d issues for %s",
        len(issues),
        repo["name"],
    )

    plan = await orchestrate_batch(issue_dicts, repo["name"], config)

    if plan is None:
        # Fallback: triage each individually
        log.warning("Batch orchestration failed, falling back to individual triage")
        for issue in issues:
            agent, model, reason = await triage_issue(
                issue.title,
                issue.body,
                config,
            )
            await create_task_for_issue(
                db,
                config,
                repo,
                repo_full,
                issue,
                model,
                reason,
                agent=agent,
            )
        return

    # Build issue_number -> issue object lookup
    issue_by_number = {i.number: i for i in issues}

    # Create tasks in priority order. Since dependencies always point to
    # lower-priority (already-created) tasks, we can resolve depends_on_task_id
    # inline — no second pass needed.
    issue_to_task_id: dict[int, int] = {}
    for entry in sorted(plan, key=lambda e: e["priority"]):
        issue = issue_by_number.get(entry["issue_number"])
        if not issue:
            continue

        # Resolve dependency to task_id (already created since lower priority)
        dep_task_id = None
        dep_issue = entry.get("depends_on")
        if dep_issue is not None:
            dep_task_id = issue_to_task_id.get(dep_issue)
            if dep_issue and not dep_task_id:
                log.warning(
                    "Issue #%d depends on #%d but no task found (created yet?), ignoring dep",
                    entry["issue_number"],
                    dep_issue,
                )

        task_id = await create_task_for_issue(
            db,
            config,
            repo,
            repo_full,
            issue,
            model=entry["model"],
            reason=entry["reason"],
            priority=entry["priority"],
            depends_on_task_id=dep_task_id,
            agent=entry.get("agent", config.default_agent),
        )
        issue_to_task_id[entry["issue_number"]] = task_id

        if dep_task_id:
            log.info(
                "Task #%d depends on task #%d (issue #%d -> #%d)",
                task_id,
                dep_task_id,
                entry["issue_number"],
                dep_issue,
            )


async def try_claim_and_dispatch(db: Database, config: Config) -> dict | None:
    """Try to claim a queued task. Returns the task dict if claimed, None otherwise.

    Handles dependency verification and conflict checks. Returns None (with
    the task re-queued) if the task can't be dispatched yet.
    """
    task = await db.claim_next_queued()
    if not task:
        return None

    # Guard against aiosqlite commit visibility race
    dep_id = task.get("depends_on_task_id")
    if dep_id:
        dep = await db.get_task(dep_id)
        if not dep or dep["status"] != "completed":
            dep_status = dep["status"] if dep else "missing"
            log.warning(
                "Task #%d claimed but dep #%d is '%s', re-queuing",
                task["id"],
                dep_id,
                dep_status,
            )
            await db.update_task(
                task["id"],
                status="queued",
                started_at=None,
            )
            return None

    # Pre-dispatch conflict check (non-full-auto modes)
    if config.approval_mode != "full-auto":
        inflight = await db.list_inflight_tasks_for_repo(task["repo_id"])
        inflight = [t for t in inflight if t["id"] != task["id"]]
        if inflight:
            conflict = await check_task_conflict(
                task["prompt"],
                inflight,
                config,
            )
            if conflict:
                conflict_tid = conflict.get("conflicting_task_id")
                reason = conflict.get("reason", "file overlap detected")
                dep_target = None
                if conflict_tid:
                    for inf in inflight:
                        if inf["id"] == conflict_tid:
                            dep_target = conflict_tid
                            break
                if not dep_target:
                    dep_target = inflight[-1]["id"]

                log.info(
                    "Task #%d conflicts with #%d (%s), serializing",
                    task["id"],
                    dep_target,
                    reason,
                )
                await db.update_task(
                    task["id"],
                    status="queued",
                    started_at=None,
                    depends_on_task_id=dep_target,
                )
                await db.add_log(
                    task["id"],
                    f"Conflict detected with task #{dep_target}: {reason[:200]}. Serialized.",
                )
                return None

    log.info(
        "Claimed task #%d: %s",
        task["id"],
        task["prompt"][:80],
    )
    return task

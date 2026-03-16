"""Background worker daemon: 4 concurrent loops — issue poller, task executor, coordinator reviewer, CI monitor."""

import asyncio
import logging
import os
import signal
from datetime import datetime, timezone

from .config import Config, load_config
from .db import Database
from .dispatcher import _mark_issue_failed, _pick_retry_model, check_task_conflict, cleanup_task_artifacts, dispatch_task, orchestrate_batch, retry_with_ci_context, run_review, sync_agent_credentials, triage_issue
from .github import (
    close_issue, close_pr, comment_on_issue, comment_on_pr,
    ensure_labels, extract_pr_number_from_url, find_new_issues,
    claim_issue, get_ci_failure_logs, get_pr_ci_status,
    is_pr_conflicting, merge_pr,
    repo_full_name_from_url, update_issue_labels,
)

log = logging.getLogger("backporcher.worker")

# Task executor poll interval (separate from issue poller)
EXECUTOR_POLL_SECONDS = 5
# Coordinator review loop interval
COORDINATOR_POLL_SECONDS = 15
# Artifact cleanup loop interval
CLEANUP_POLL_SECONDS = 300  # 5 minutes
# Minimum age before cleaning up terminal task artifacts
CLEANUP_MIN_AGE_MINUTES = 10


class WorkerDaemon:
    def __init__(self, config: Config, db: Database):
        self.config = config
        self.db = db
        self.semaphore = asyncio.Semaphore(config.max_workers)
        self._running = False
        self._tasks: set[asyncio.Task] = set()

    async def run(self):
        """Launch concurrent loops (4 core + optional dashboard)."""
        self._running = True
        log.info(
            "Worker daemon started (max_concurrency=%d, issue_poll=%ds, ci_poll=%ds, coordinator_model=%s, approval_mode=%s)",
            self.config.max_workers,
            self.config.poll_interval_seconds,
            self.config.ci_check_interval_seconds,
            self.config.coordinator_model,
            self.config.approval_mode,
        )

        # Log tasks with holds so the user knows what's waiting
        held = await self.db.list_held_tasks()
        if held:
            log.info("Tasks awaiting approval on startup: %s",
                     ", ".join(f"#{t['id']}({t['hold']})" for t in held))

        loops = [
            self._issue_poller_loop(),
            self._task_executor_loop(),
            self._coordinator_review_loop(),
            self._ci_monitor_loop(),
            self._cleanup_loop(),
        ]

        if self.config.dashboard_password:
            from .dashboard import start_dashboard
            loops.append(start_dashboard(self.db, self.config))
            log.info("Dashboard enabled on port %d", self.config.dashboard_port)
        else:
            log.info("Dashboard disabled (no BACKPORCHER_DASHBOARD_PASSWORD set)")

        await asyncio.gather(*loops)

    # --- Loop 1: Issue Poller ---

    async def _issue_poller_loop(self):
        """Poll GitHub for new issues labeled 'backporcher'. Batches per repo for orchestration."""
        allowed_users = set(self.config.allowed_github_users)

        while self._running:
            try:
                repos = await self.db.list_repos()
                for repo in repos:
                    repo_full = repo_full_name_from_url(repo["github_url"])
                    await ensure_labels(repo_full)
                    issues = await find_new_issues(repo_full, allowed_users)

                    # Filter to genuinely new issues (dedup)
                    new_issues = []
                    for issue in issues:
                        existing = await self.db.get_task_by_issue(repo["id"], issue.number)
                        if not existing:
                            new_issues.append(issue)

                    if not new_issues:
                        continue

                    # Separate opus-labeled issues (manual override, no orchestration)
                    opus_issues = [i for i in new_issues if "opus" in i.labels]
                    normal_issues = [i for i in new_issues if "opus" not in i.labels]

                    # Process opus-labeled issues directly
                    for issue in opus_issues:
                        await self._create_task_for_issue(
                            repo, repo_full, issue, "opus", "opus label (manual override)",
                        )

                    # Normal issues: single = triage, 2+ = batch orchestrate
                    if len(normal_issues) == 1:
                        issue = normal_issues[0]
                        model, triage_reason = await triage_issue(
                            issue.title, issue.body, self.config,
                        )
                        await self._create_task_for_issue(
                            repo, repo_full, issue, model, triage_reason,
                        )
                    elif len(normal_issues) >= 2:
                        await self._batch_create_tasks(
                            repo, repo_full, normal_issues,
                        )

            except Exception:
                log.exception("Error in issue poller loop")

            await asyncio.sleep(self.config.poll_interval_seconds)

    async def _create_task_for_issue(
        self, repo: dict, repo_full: str, issue, model: str, reason: str,
        priority: int = 100, depends_on_task_id: int | None = None,
    ):
        """Create a single task from an issue and claim it on GitHub."""
        prompt = issue.title
        if issue.body and issue.body.strip():
            prompt = f"{issue.title}\n\n{issue.body}"

        task_id = await self.db.create_task_from_issue(
            repo["id"], prompt, model,
            issue.number, issue.url,
            priority=priority,
            depends_on_task_id=depends_on_task_id,
        )
        await self.db.add_log(
            task_id,
            f"Created from issue #{issue.number}: {issue.title[:80]}",
        )
        dep_info = f", depends_on=task#{depends_on_task_id}" if depends_on_task_id else ""
        await self.db.add_log(
            task_id,
            f"Triage: model={model}, priority={priority}{dep_info} — {reason[:200]}",
        )
        log.info(
            "Issue #%d -> Task #%d (pri=%d): %s",
            issue.number, task_id, priority, issue.title[:60],
        )
        await claim_issue(repo_full, issue.number)

        # Dispatch gate: in review-all mode, hold tasks for approval before dispatch
        if self.config.approval_mode == "review-all":
            await self.db.set_hold(task_id, "dispatch_approval")
            await self.db.add_log(task_id, "Held for dispatch approval (review-all mode)")
            log.info("Task #%d: held for dispatch approval", task_id)

        return task_id

    async def _batch_create_tasks(
        self, repo: dict, repo_full: str, issues: list,
    ):
        """Batch-orchestrate multiple issues and create tasks with dependencies."""
        issue_dicts = [
            {"number": i.number, "title": i.title, "body": i.body}
            for i in issues
        ]
        log.info(
            "Batch orchestrating %d issues for %s",
            len(issues), repo["name"],
        )

        plan = await orchestrate_batch(issue_dicts, repo["name"], self.config)

        if plan is None:
            # Fallback: triage each individually
            log.warning("Batch orchestration failed, falling back to individual triage")
            for issue in issues:
                model, reason = await triage_issue(
                    issue.title, issue.body, self.config,
                )
                await self._create_task_for_issue(
                    repo, repo_full, issue, model, reason,
                )
            return

        # Build issue_number -> issue object lookup
        issue_by_number = {i.number: i for i in issues}

        # Create tasks in priority order. Since dependencies always point to
        # lower-priority (already-created) tasks, we can resolve depends_on_task_id
        # inline — no second pass needed, eliminating the race where the executor
        # claims a task before its dependency is set.
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
                        entry["issue_number"], dep_issue,
                    )

            task_id = await self._create_task_for_issue(
                repo, repo_full, issue,
                model=entry["model"],
                reason=entry["reason"],
                priority=entry["priority"],
                depends_on_task_id=dep_task_id,
            )
            issue_to_task_id[entry["issue_number"]] = task_id

            if dep_task_id:
                log.info(
                    "Task #%d depends on task #%d (issue #%d -> #%d)",
                    task_id, dep_task_id, entry["issue_number"], dep_issue,
                )

    # --- Loop 2: Task Executor ---

    async def _task_executor_loop(self):
        """Poll for queued tasks and dispatch them."""
        while self._running:
            try:
                # Check global queue pause
                if await self.db.is_queue_paused():
                    await asyncio.sleep(EXECUTOR_POLL_SECONDS)
                    continue

                if self.semaphore._value > 0:
                    task = await self.db.claim_next_queued()
                    if task:
                        # Guard against aiosqlite commit visibility race:
                        # back-to-back claims may not see the previous commit,
                        # so verify the dependency is actually met.
                        dep_id = task.get("depends_on_task_id")
                        if dep_id:
                            dep = await self.db.get_task(dep_id)
                            if not dep or dep["status"] != "completed":
                                dep_status = dep["status"] if dep else "missing"
                                log.warning(
                                    "Task #%d claimed but dep #%d is '%s', re-queuing",
                                    task["id"], dep_id, dep_status,
                                )
                                await self.db.update_task(
                                    task["id"], status="queued", started_at=None,
                                )
                                await asyncio.sleep(0.1)
                                continue

                        # Pre-dispatch conflict check (non-full-auto modes)
                        if self.config.approval_mode != "full-auto":
                            inflight = await self.db.list_inflight_tasks_for_repo(task["repo_id"])
                            if inflight:
                                conflict = await check_task_conflict(
                                    task["prompt"], inflight, self.config,
                                )
                                if conflict:
                                    conflict_tid = conflict.get("conflicting_task_id")
                                    reason = conflict.get("reason", "file overlap detected")
                                    # Find the best task to serialize after
                                    dep_target = None
                                    if conflict_tid:
                                        # Verify it's actually in-flight
                                        for inf in inflight:
                                            if inf["id"] == conflict_tid:
                                                dep_target = conflict_tid
                                                break
                                    if not dep_target:
                                        # Default to the most recent in-flight task
                                        dep_target = inflight[-1]["id"]

                                    log.info(
                                        "Task #%d conflicts with #%d (%s), serializing",
                                        task["id"], dep_target, reason,
                                    )
                                    await self.db.update_task(
                                        task["id"],
                                        status="queued",
                                        started_at=None,
                                        depends_on_task_id=dep_target,
                                    )
                                    await self.db.add_log(
                                        task["id"],
                                        f"Conflict detected with task #{dep_target}: {reason[:200]}. Serialized.",
                                    )
                                    await asyncio.sleep(0.1)
                                    continue

                        log.info(
                            "Claimed task #%d: %s",
                            task["id"], task["prompt"][:80],
                        )
                        atask = asyncio.create_task(
                            self._run_with_semaphore(task)
                        )
                        self._tasks.add(atask)
                        atask.add_done_callback(self._tasks.discard)
                        continue  # Check for more work immediately
            except Exception:
                log.exception("Error in task executor loop")

            await asyncio.sleep(EXECUTOR_POLL_SECONDS)

    async def _run_with_semaphore(self, task: dict):
        async with self.semaphore:
            try:
                await dispatch_task(task, self.config, self.db)
            except Exception:
                log.exception("Unhandled error dispatching task %d", task["id"])
            finally:
                # Record terminal metrics after dispatch completes
                try:
                    fresh = await self.db.get_task(task["id"])
                    if fresh:
                        await self._record_terminal_metric(fresh)
                except Exception:
                    log.warning("Failed to record terminal metric for task %d", task["id"], exc_info=True)

    async def _record_terminal_metric(self, task: dict):
        """Record metrics for task completion or failure."""
        status = task["status"]
        task_id = task["id"]
        repo = task.get("repo_name")
        model = task.get("model_used") or task.get("model")

        if status == "completed":
            # Compute agent runtime
            duration = self._compute_duration(task.get("agent_started_at"), task.get("agent_finished_at"))
            await self.db.record_metric("task_completed", task_id=task_id, repo=repo, model=model, value=duration)
        elif status == "failed":
            await self.db.record_metric("task_failed", task_id=task_id, repo=repo, model=model)

    @staticmethod
    def _compute_duration(start: str | None, end: str | None) -> float | None:
        if not start or not end:
            return None
        try:
            s = datetime.fromisoformat(start.replace("Z", "+00:00"))
            e = datetime.fromisoformat(end.replace("Z", "+00:00"))
            return (e - s).total_seconds()
        except Exception:
            return None

    # --- Loop 3: Coordinator Review ---

    async def _coordinator_review_loop(self):
        """Review PRs before they reach CI monitoring."""
        while self._running:
            try:
                pending = await self.db.list_pending_review()
                for task in pending:
                    if not self._running:
                        break

                    task_id = task["id"]
                    pr_number = task.get("pr_number")

                    # Backfill pr_number from pr_url if missing
                    if not pr_number and task.get("pr_url"):
                        pr_number = extract_pr_number_from_url(task["pr_url"])
                        if pr_number:
                            await self.db.update_task(task_id, pr_number=pr_number)
                            task["pr_number"] = pr_number  # Update dict for run_review
                            log.info("Task #%d: backfilled pr_number=%d from URL", task_id, pr_number)

                    if not pr_number:
                        # Re-fetch from DB in case it was set between list_pending_review and now
                        fresh = await self.db.get_task(task_id)
                        if fresh and fresh.get("pr_number"):
                            pr_number = fresh["pr_number"]
                            task["pr_number"] = pr_number
                        else:
                            log.warning("Task #%d: no pr_number, skipping review this cycle", task_id)
                            continue

                    repo_full = repo_full_name_from_url(task["github_url"])

                    await self.db.update_task(task_id, status="reviewing")
                    await self.db.add_log(task_id, "Coordinator review started")
                    log.info("Task #%d: starting coordinator review (PR #%d)", task_id, pr_number)

                    try:
                        verdict, summary = await run_review(task, self.config, self.db)
                    except Exception as e:
                        log.exception("Review failed for task %d", task_id)
                        # On review error, approve by default so CI can still gate
                        verdict, summary = "approve", f"Review error (auto-approved): {e}"

                    await self.db.update_task(task_id, review_summary=summary[:4000])

                    if verdict == "approve":
                        await self.db.update_task(task_id, status="reviewed")
                        await self.db.add_log(task_id, "Coordinator approved PR")
                        log.info("Task #%d: coordinator APPROVED", task_id)

                        # Post review summary as PR comment
                        short_summary = summary[:1500] if len(summary) > 1500 else summary
                        await comment_on_pr(
                            repo_full, pr_number,
                            f"**Coordinator Review: APPROVED**\n\n{short_summary}",
                        )
                    else:
                        # Reject: check retry budget before permanent failure
                        log.warning("Task #%d: coordinator REJECTED", task_id)
                        await self.db.add_log(task_id, f"Coordinator rejected PR: {summary[:200]}", level="warn")

                        retry_count = task.get("retry_count", 0)

                        if retry_count < self.config.max_task_retries:
                            # Retry with feedback + model escalation
                            new_count = retry_count + 1
                            new_model = _pick_retry_model(task.get("model", "sonnet"), new_count)

                            # Close the rejected PR
                            reject_comment = (
                                f"**Coordinator Review: REJECTED**\n\n{summary[:1500]}\n\n"
                                f"Retrying with {new_model} model (attempt {new_count}/{self.config.max_task_retries})."
                            )
                            await close_pr(repo_full, pr_number, comment=reject_comment)

                            # Inject rejection context into prompt
                            rejection_context = (
                                f"\n\n---\n"
                                f"IMPORTANT: A previous attempt at this task was rejected during code review.\n"
                                f"Reviewer feedback:\n\n{summary[:2000]}\n\n"
                                f"Address ALL the reviewer's concerns in your implementation."
                            )
                            original_prompt = task["prompt"]
                            # Strip any previous rejection context (avoid stacking)
                            if "\n\n---\nIMPORTANT: A previous attempt" in original_prompt:
                                original_prompt = original_prompt.split("\n\n---\nIMPORTANT: A previous attempt")[0]
                            new_prompt = original_prompt + rejection_context

                            await self.db.update_task(
                                task_id,
                                status="queued",
                                started_at=None,
                                branch_name=None,
                                worktree_path=None,
                                pr_url=None,
                                pr_number=None,
                                review_summary=None,
                                retry_count=new_count,
                                model=new_model,
                                prompt=new_prompt,
                            )
                            await self.db.add_log(
                                task_id,
                                f"Coordinator rejected, retry {new_count}/{self.config.max_task_retries} (model={new_model})",
                                level="warn",
                            )
                            log.info(
                                "Task #%d: coordinator rejected, retry %d/%d (model=%s)",
                                task_id, new_count, self.config.max_task_retries, new_model,
                            )

                            issue_num = task.get("github_issue_number")
                            if issue_num:
                                await comment_on_issue(
                                    repo_full, issue_num,
                                    f"PR rejected by coordinator. Retrying with {new_model} model "
                                    f"({new_count}/{self.config.max_task_retries})...\n\n"
                                    f"Feedback: {summary[:300]}",
                                )
                        else:
                            # Max retries exhausted — permanent failure
                            reject_comment = (
                                f"**Coordinator Review: REJECTED**\n\n{summary[:1500]}\n\n"
                                f"PR closed by Backporcher coordinator (retries exhausted)."
                            )
                            await close_pr(repo_full, pr_number, comment=reject_comment)
                            await self.db.update_task(
                                task_id, status="failed",
                                error_message=f"Coordinator rejected (retries exhausted): {summary[:500]}",
                            )

                            cascaded = await self.db.handle_dependency_failure(task_id)
                            if cascaded:
                                log.info("Task #%d rejection cascaded to tasks: %s", task_id, cascaded)

                            issue_num = task.get("github_issue_number")
                            if issue_num:
                                await update_issue_labels(
                                    repo_full, issue_num,
                                    add=["backporcher-failed"],
                                    remove=["backporcher-in-progress"],
                                )
                                await comment_on_issue(
                                    repo_full, issue_num,
                                    f"PR was rejected by coordinator review (retries exhausted):\n\n{summary[:500]}\n\n"
                                    f"Re-add the `backporcher` label to retry.",
                                )
                            await cleanup_task_artifacts(task, self.db)

                            # Webhook: failed
                            from . import notifications as _notif
                            _title = task.get("prompt", "")[:80]
                            await _notif.notify_failed(task_id, _title, f"coordinator rejected PR (retries exhausted)")

            except Exception:
                log.exception("Error in coordinator review loop")

            await asyncio.sleep(COORDINATOR_POLL_SECONDS)

    # --- Loop 4: CI Monitor ---

    async def _ci_monitor_loop(self):
        """Monitor PRs for CI results and handle retries."""
        while self._running:
            try:
                # Check reviewed tasks for CI status (coordinator-approved PRs)
                pr_tasks = await self.db.list_pr_tasks()
                for task in pr_tasks:
                    pr_number = task.get("pr_number")
                    if not pr_number:
                        continue

                    repo_full = repo_full_name_from_url(task["github_url"])
                    ci = await get_pr_ci_status(repo_full, pr_number)

                    if ci.state == "pending":
                        continue  # Check next cycle

                    if ci.state in ("success", "no_checks"):
                        await self._handle_ci_passed(task, repo_full)

                    elif ci.state == "failure":
                        await self._handle_ci_failure(task, repo_full, ci)

                # Sweep ci_passed tasks stuck due to merge failure
                stuck_tasks = await self.db.list_tasks_by_status("ci_passed")
                for task in stuck_tasks:
                    pr_number = task.get("pr_number")
                    if not pr_number:
                        continue
                    repo_full = repo_full_name_from_url(task["github_url"])
                    conflicting = await is_pr_conflicting(repo_full, pr_number)
                    if conflicting:
                        task_id = task["id"]
                        log.warning("Task #%d: PR #%d still conflicting, re-queuing", task_id, pr_number)
                        await self.db.add_log(
                            task_id,
                            f"PR #{pr_number} has merge conflicts — closing and re-queuing",
                            level="warn",
                        )
                        await close_pr(
                            repo_full, pr_number,
                            comment="Merge conflict detected. Closing PR and re-running agent from latest main.",
                        )
                        await self.db.update_task(
                            task_id,
                            status="queued",
                            branch_name=None,
                            worktree_path=None,
                            pr_url=None,
                            pr_number=None,
                            review_summary=None,
                            started_at=None,
                            completed_at=None,
                        )

                # Sweep approved tasks: ci_passed with hold cleared → proceed to merge
                approved_tasks = await self.db.list_tasks_by_status("ci_passed")
                for task in approved_tasks:
                    if task.get("hold"):
                        continue  # Still held, skip
                    pr_number = task.get("pr_number")
                    if not pr_number:
                        continue
                    repo_full = repo_full_name_from_url(task["github_url"])
                    await self._merge_approved_task(task, repo_full)

                # Process retrying tasks
                retry_tasks = await self.db.list_retrying_tasks()
                for task in retry_tasks:
                    await self._process_retry(task)

            except Exception:
                log.exception("Error in CI monitor loop")

            await asyncio.sleep(self.config.ci_check_interval_seconds)

    async def _handle_ci_passed(self, task: dict, repo_full: str):
        """CI passed — auto-merge PR (or hold for approval), mark completed, update labels."""
        task_id = task["id"]
        pr_number = task.get("pr_number")

        await self.db.update_task(task_id, status="ci_passed")
        await self.db.add_log(task_id, "CI checks passed")
        log.info("Task #%d: CI passed", task_id)

        # Merge gate: in non-full-auto modes, hold for approval
        if self.config.approval_mode != "full-auto":
            await self.db.set_hold(task_id, "merge_approval")
            await self.db.add_log(task_id, "Held for merge approval — run `backporcher approve %d` or use dashboard" % task_id)
            log.info("Task #%d: held for merge approval", task_id)
            # Post PR comment
            if pr_number:
                from .github import comment_on_pr as _comment_on_pr
                await _comment_on_pr(
                    repo_full, pr_number,
                    f"CI passed. Awaiting merge approval.\n\n"
                    f"Run `backporcher approve {task_id}` or use the dashboard to merge.",
                )
            # Webhook notification
            from . import notifications
            title = task.get("prompt", "")[:80]
            await notifications.notify_hold(task_id, title, "merge_approval")
            return

        # Auto-merge the PR
        if pr_number:
            merged = await merge_pr(repo_full, pr_number)
            if merged:
                now = datetime.now(timezone.utc).isoformat()
                await self.db.update_task(task_id, status="completed", completed_at=now)
                await self.db.add_log(task_id, f"PR #{pr_number} merged (squash)")
                log.info("Task #%d: PR #%d merged", task_id, pr_number)

                # Record merge metric
                try:
                    merge_duration = self._compute_duration(task.get("created_at"), now)
                    model = task.get("model_used") or task.get("model")
                    await self.db.record_metric(
                        "merge", task_id=task_id, repo=task.get("repo_name"),
                        model=model, value=merge_duration,
                    )
                except Exception:
                    log.warning("Failed to record merge metric for task %d", task_id, exc_info=True)

                issue_num = task.get("github_issue_number")
                if issue_num:
                    await update_issue_labels(
                        repo_full, issue_num,
                        add=["backporcher-done"],
                        remove=["backporcher-in-progress"],
                    )
                    await comment_on_issue(
                        repo_full, issue_num,
                        "CI passed. PR has been merged. Closing issue.",
                    )
                    await close_issue(repo_full, issue_num)
                await cleanup_task_artifacts(task, self.db)

                # Webhook: completed
                from . import notifications
                title = task.get("prompt", "")[:80]
                dur = self._compute_duration(task.get("created_at"), now)
                dur_str = f"{int(dur // 60)}m" if dur else "?"
                model = task.get("model_used") or task.get("model") or "?"
                await notifications.notify_completed(task_id, title, dur_str, model)
                return

            # Merge failed — check if it's a conflict
            conflicting = await is_pr_conflicting(repo_full, pr_number)
            if conflicting:
                await self.db.add_log(
                    task_id,
                    f"PR #{pr_number} has merge conflicts — closing and re-queuing for fresh attempt",
                    level="warn",
                )
                log.warning("Task #%d: PR #%d has merge conflicts, re-queuing", task_id, pr_number)
                await close_pr(
                    repo_full, pr_number,
                    comment="Merge conflict detected. Closing PR and re-running agent from latest main.",
                )
                await self.db.update_task(
                    task_id,
                    status="queued",
                    branch_name=None,
                    worktree_path=None,
                    pr_url=None,
                    pr_number=None,
                    review_summary=None,
                    started_at=None,
                    completed_at=None,
                )
            else:
                await self.db.update_task(
                    task_id, status="failed",
                    error_message=f"Merge failed for PR #{pr_number} (no conflict detected)",
                )
                await self.db.add_log(task_id, f"Failed to merge PR #{pr_number} (no conflict)", level="error")
                log.warning("Task #%d: merge failed for PR #%d (no conflict), marking failed", task_id, pr_number)

                issue_num = task.get("github_issue_number")
                if issue_num:
                    await update_issue_labels(
                        repo_full, issue_num,
                        add=["backporcher-failed"],
                        remove=["backporcher-in-progress"],
                    )
                    await comment_on_issue(
                        repo_full, issue_num,
                        f"Merge failed for PR #{pr_number} (reason unknown, not a conflict).\n\n"
                        f"Re-add the `backporcher` label to retry.",
                    )
                await cleanup_task_artifacts(task, self.db)

    async def _merge_approved_task(self, task: dict, repo_full: str):
        """Merge a task that has been approved (ci_passed, hold cleared)."""
        task_id = task["id"]
        pr_number = task.get("pr_number")

        log.info("Task #%d: merge approved, proceeding", task_id)
        await self.db.add_log(task_id, "Merge approved, proceeding to merge")

        merged = await merge_pr(repo_full, pr_number)
        if merged:
            now = datetime.now(timezone.utc).isoformat()
            await self.db.update_task(task_id, status="completed", completed_at=now)
            await self.db.add_log(task_id, f"PR #{pr_number} merged (squash)")
            log.info("Task #%d: PR #%d merged", task_id, pr_number)

            # Record merge metric
            try:
                merge_duration = self._compute_duration(task.get("created_at"), now)
                model = task.get("model_used") or task.get("model")
                await self.db.record_metric(
                    "merge", task_id=task_id, repo=task.get("repo_name"),
                    model=model, value=merge_duration,
                )
            except Exception:
                log.warning("Failed to record merge metric for task %d", task_id, exc_info=True)

            issue_num = task.get("github_issue_number")
            if issue_num:
                await update_issue_labels(
                    repo_full, issue_num,
                    add=["backporcher-done"],
                    remove=["backporcher-in-progress"],
                )
                await comment_on_issue(
                    repo_full, issue_num,
                    "CI passed. PR has been merged. Closing issue.",
                )
                await close_issue(repo_full, issue_num)
            await cleanup_task_artifacts(task, self.db)

            # Webhook: completed
            from . import notifications
            title = task.get("prompt", "")[:80]
            dur = self._compute_duration(task.get("created_at"), now)
            dur_str = f"{int(dur // 60)}m" if dur else "?"
            model = task.get("model_used") or task.get("model") or "?"
            await notifications.notify_completed(task_id, title, dur_str, model)
            return

        # Merge failed — check if it's a conflict
        conflicting = await is_pr_conflicting(repo_full, pr_number)
        if conflicting:
            await self.db.add_log(
                task_id,
                f"PR #{pr_number} has merge conflicts — closing and re-queuing",
                level="warn",
            )
            log.warning("Task #%d: PR #%d has merge conflicts, re-queuing", task_id, pr_number)
            await close_pr(
                repo_full, pr_number,
                comment="Merge conflict detected. Closing PR and re-running agent from latest main.",
            )
            await self.db.update_task(
                task_id,
                status="queued",
                hold=None,
                branch_name=None,
                worktree_path=None,
                pr_url=None,
                pr_number=None,
                review_summary=None,
                started_at=None,
                completed_at=None,
            )
        else:
            await self.db.update_task(
                task_id, status="failed",
                error_message=f"Merge failed for PR #{pr_number} (no conflict detected)",
            )
            await self.db.add_log(task_id, f"Failed to merge PR #{pr_number} (no conflict)", level="error")

            issue_num = task.get("github_issue_number")
            if issue_num:
                await update_issue_labels(
                    repo_full, issue_num,
                    add=["backporcher-failed"],
                    remove=["backporcher-in-progress"],
                )
                await comment_on_issue(
                    repo_full, issue_num,
                    f"Merge failed for PR #{pr_number} (reason unknown, not a conflict).\n\n"
                    f"Re-add the `backporcher` label to retry.",
                )
            await cleanup_task_artifacts(task, self.db)

    async def _handle_ci_failure(self, task: dict, repo_full: str, ci):
        """CI failed — retry or mark failed."""
        task_id = task["id"]
        retry_count = task.get("retry_count", 0)

        if retry_count < self.config.max_ci_retries:
            new_count = retry_count + 1
            await self.db.update_task(
                task_id, status="retrying", retry_count=new_count,
            )
            await self.db.add_log(
                task_id,
                f"CI failed ({', '.join(ci.failed_checks[:3])}). "
                f"Retry {new_count}/{self.config.max_ci_retries}",
                level="warn",
            )
            log.info("Task #%d: CI failed, retry %d/%d", task_id, new_count, self.config.max_ci_retries)
            await self.db.record_metric(
                "retry_ci", task_id=task_id, repo=task.get("repo_name"),
                model=task.get("model"),
            )

            issue_num = task.get("github_issue_number")
            if issue_num:
                await comment_on_issue(
                    repo_full, issue_num,
                    f"CI failed: {', '.join(ci.failed_checks[:3])}\n\n"
                    f"Auto-retrying ({new_count}/{self.config.max_ci_retries})...",
                )
        else:
            await self.db.update_task(task_id, status="failed")
            await self.db.add_log(
                task_id,
                f"CI failed after {self.config.max_ci_retries} retries: {', '.join(ci.failed_checks[:3])}",
                level="error",
            )
            log.warning("Task #%d: CI failed, max retries exhausted", task_id)

            issue_num = task.get("github_issue_number")
            if issue_num:
                await update_issue_labels(
                    repo_full, issue_num,
                    add=["backporcher-failed"],
                    remove=["backporcher-in-progress"],
                )
                await comment_on_issue(
                    repo_full, issue_num,
                    f"CI failed after {self.config.max_ci_retries} retries. "
                    f"Failed checks: {', '.join(ci.failed_checks[:5])}\n\n"
                    f"Marking as failed. Re-add the `backporcher` label to retry.",
                )
            await cleanup_task_artifacts(task, self.db)

            # Webhook: failed
            from . import notifications
            title = task.get("prompt", "")[:80]
            await notifications.notify_failed(task_id, title, f"CI failed after {self.config.max_ci_retries} retries")

    async def _process_retry(self, task: dict):
        """Fetch CI logs and re-run agent with context."""
        task_id = task["id"]
        branch = task.get("branch_name")
        if not branch:
            await self.db.update_task(task_id, status="failed", error_message="No branch for retry")
            await _mark_issue_failed(task, self.db, "CI retry failed: no branch available.")
            await cleanup_task_artifacts(task, self.db)
            return

        repo_full = repo_full_name_from_url(task["github_url"])
        log.info("Task #%d: fetching CI logs for retry", task_id)

        ci_logs = await get_ci_failure_logs(repo_full, branch)

        try:
            await retry_with_ci_context(task, ci_logs, self.config, self.db)
        except Exception as e:
            log.exception("Retry failed for task %d", task_id)
            await self.db.update_task(
                task_id, status="failed",
                error_message=f"Retry error: {str(e)[:500]}",
            )
            await self.db.add_log(task_id, f"Retry error: {e}", level="error")
            await _mark_issue_failed(
                task, self.db,
                f"CI retry failed with error: {str(e)[:300]}",
            )
            await cleanup_task_artifacts(task, self.db)

    # --- Loop 5: Periodic Cleanup ---

    async def _cleanup_loop(self):
        """Periodically clean up worktrees and remote branches for terminal tasks."""
        # Wait a bit before first run to let startup settle
        await asyncio.sleep(60)

        while self._running:
            try:
                tasks = await self.db.list_cleanable_tasks(
                    min_age_minutes=CLEANUP_MIN_AGE_MINUTES,
                )
                if tasks:
                    log.info("Cleanup loop: found %d tasks to clean", len(tasks))
                for task in tasks:
                    if not self._running:
                        break
                    try:
                        await cleanup_task_artifacts(task, self.db)
                    except Exception:
                        log.exception(
                            "Cleanup failed for task #%d", task["id"],
                        )
            except Exception:
                log.exception("Error in cleanup loop")

            await asyncio.sleep(CLEANUP_POLL_SECONDS)

    async def shutdown(self):
        """Graceful shutdown: stop polling, wait for active tasks."""
        log.info("Shutting down worker daemon...")
        self._running = False

        if self._tasks:
            log.info("Waiting for %d active tasks to finish...", len(self._tasks))
            _, pending = await asyncio.wait(self._tasks, timeout=30)
            for t in pending:
                log.warning("Force-cancelling task")
                t.cancel()

        await self.db.close()
        log.info("Worker daemon stopped")


async def _run_worker():
    """Entry point for the worker daemon."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    config = load_config()
    config.repos_dir.mkdir(parents=True, exist_ok=True)
    config.logs_dir.mkdir(parents=True, exist_ok=True)

    db = Database(config.db_path)
    await db.connect()
    log.info("Database connected: %s", config.db_path)

    # Recover stale tasks from previous crash/restart
    async with db._write_lock:
        # Reviewing → pr_created (re-review)
        async with db.db.execute(
            "UPDATE tasks SET status = 'pr_created', review_summary = NULL "
            "WHERE status = 'reviewing' RETURNING id"
        ) as cur:
            recovered_reviewing = [r[0] for r in await cur.fetchall()]
        # Working → queued (re-dispatch)
        async with db.db.execute(
            "UPDATE tasks SET status = 'queued', started_at = NULL, "
            "error_message = NULL, agent_pid = NULL, branch_name = NULL, "
            "worktree_path = NULL "
            "WHERE status = 'working' RETURNING id"
        ) as cur:
            recovered_working = [r[0] for r in await cur.fetchall()]
        await db.db.commit()
    if recovered_reviewing:
        log.info("Recovered %d stale reviewing tasks: %s", len(recovered_reviewing), recovered_reviewing)
    if recovered_working:
        log.info("Recovered %d stale working tasks: %s", len(recovered_working), recovered_working)

    # Preflight checks
    log.info("Running preflight checks...")
    preflight_ok = True

    # Check agent user can access repos
    if config.agent_user:
        proc = await asyncio.create_subprocess_exec(
            "sudo", "-u", config.agent_user, "test", "-r", str(config.repos_dir),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()
        if proc.returncode != 0:
            log.error("PREFLIGHT FAIL: %s cannot read %s", config.agent_user, config.repos_dir)
            preflight_ok = False
        else:
            log.info("Preflight OK: agent user can access repos")

        # Sync credentials if needed
        await sync_agent_credentials(config)

    if not preflight_ok:
        log.error("Preflight checks failed — starting anyway but tasks may fail")

    # Initialize webhook notifications
    from . import notifications
    notifications.init(config)
    if config.webhook_url:
        log.info("Webhooks enabled: %s (events: %s)", config.webhook_url, ",".join(config.webhook_events))

    daemon = WorkerDaemon(config, db)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, lambda: asyncio.create_task(daemon.shutdown()))

    await daemon.run()


def run_worker():
    """Synchronous entry point for CLI / systemd."""
    asyncio.run(_run_worker())

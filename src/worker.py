"""Background worker daemon: 4 concurrent loops — issue poller, task executor, coordinator reviewer, CI monitor."""

import asyncio
import logging
import os
import signal

from .config import Config, load_config
from .db import Database
from .dispatcher import dispatch_task, retry_with_ci_context, run_review
from .github import (
    close_pr, comment_on_issue, comment_on_pr, find_new_issues, claim_issue,
    get_ci_failure_logs, get_pr_ci_status, merge_pr, repo_full_name_from_url,
    update_issue_labels,
)

log = logging.getLogger("voltron.worker")

# Task executor poll interval (separate from issue poller)
EXECUTOR_POLL_SECONDS = 5
# Coordinator review loop interval
COORDINATOR_POLL_SECONDS = 15


class WorkerDaemon:
    def __init__(self, config: Config, db: Database):
        self.config = config
        self.db = db
        self.semaphore = asyncio.Semaphore(config.max_workers)
        self._running = False
        self._tasks: set[asyncio.Task] = set()

    async def run(self):
        """Launch 4 concurrent loops."""
        self._running = True
        log.info(
            "Worker daemon started (max_concurrency=%d, issue_poll=%ds, ci_poll=%ds, coordinator_model=%s)",
            self.config.max_workers,
            self.config.poll_interval_seconds,
            self.config.ci_check_interval_seconds,
            self.config.coordinator_model,
        )

        await asyncio.gather(
            self._issue_poller_loop(),
            self._task_executor_loop(),
            self._coordinator_review_loop(),
            self._ci_monitor_loop(),
        )

    # --- Loop 1: Issue Poller ---

    async def _issue_poller_loop(self):
        """Poll GitHub for new issues labeled 'voltron'."""
        allowed_users = set(self.config.allowed_github_users)

        while self._running:
            try:
                repos = await self.db.list_repos()
                for repo in repos:
                    repo_full = repo_full_name_from_url(repo["github_url"])
                    issues = await find_new_issues(repo_full, allowed_users)

                    for issue in issues:
                        # Dedup: check if we already have a task for this issue
                        existing = await self.db.get_task_by_issue(repo["id"], issue.number)
                        if existing:
                            continue

                        # Determine model from labels
                        model = self.config.default_model
                        if "opus" in issue.labels:
                            model = "opus"

                        # Build prompt from issue title + body
                        prompt = issue.title
                        if issue.body and issue.body.strip():
                            prompt = f"{issue.title}\n\n{issue.body}"

                        task_id = await self.db.create_task_from_issue(
                            repo["id"], prompt, model,
                            issue.number, issue.url,
                        )
                        await self.db.add_log(
                            task_id,
                            f"Created from issue #{issue.number}: {issue.title[:80]}",
                        )

                        # Claim on GitHub (add in-progress label, remove voltron label)
                        await claim_issue(repo_full, issue.number)

                        log.info(
                            "Issue #%d -> Task #%d: %s",
                            issue.number, task_id, issue.title[:60],
                        )

            except Exception:
                log.exception("Error in issue poller loop")

            await asyncio.sleep(self.config.poll_interval_seconds)

    # --- Loop 2: Task Executor ---

    async def _task_executor_loop(self):
        """Poll for queued tasks and dispatch them."""
        while self._running:
            try:
                if self.semaphore._value > 0:
                    task = await self.db.claim_next_queued()
                    if task:
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
                    if not pr_number:
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
                        # Reject: close PR, fail task
                        log.warning("Task #%d: coordinator REJECTED", task_id)
                        await self.db.add_log(task_id, f"Coordinator rejected PR: {summary[:200]}", level="warn")

                        reject_comment = (
                            f"**Coordinator Review: REJECTED**\n\n{summary[:1500]}\n\n"
                            f"PR closed by Voltron coordinator."
                        )
                        await close_pr(repo_full, pr_number, comment=reject_comment)
                        await self.db.update_task(task_id, status="failed", error_message=f"Coordinator rejected: {summary[:500]}")

                        # Update issue labels
                        issue_num = task.get("github_issue_number")
                        if issue_num:
                            await update_issue_labels(
                                repo_full, issue_num,
                                add=["voltron-failed"],
                                remove=["voltron-in-progress"],
                            )
                            await comment_on_issue(
                                repo_full, issue_num,
                                f"PR was rejected by coordinator review:\n\n{summary[:500]}\n\n"
                                f"Re-add the `voltron` label to retry.",
                            )

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

                # Process retrying tasks
                retry_tasks = await self.db.list_retrying_tasks()
                for task in retry_tasks:
                    await self._process_retry(task)

            except Exception:
                log.exception("Error in CI monitor loop")

            await asyncio.sleep(self.config.ci_check_interval_seconds)

    async def _handle_ci_passed(self, task: dict, repo_full: str):
        """CI passed — auto-merge PR, mark completed, update labels."""
        task_id = task["id"]
        pr_number = task.get("pr_number")

        await self.db.update_task(task_id, status="ci_passed")
        await self.db.add_log(task_id, "CI checks passed")
        log.info("Task #%d: CI passed", task_id)

        # Auto-merge the PR
        if pr_number:
            merged = await merge_pr(repo_full, pr_number)
            if merged:
                await self.db.update_task(task_id, status="completed")
                await self.db.add_log(task_id, f"PR #{pr_number} merged (squash)")
                log.info("Task #%d: PR #%d merged", task_id, pr_number)
            else:
                await self.db.add_log(task_id, f"Failed to merge PR #{pr_number}", level="warn")
                log.warning("Task #%d: merge failed for PR #%d", task_id, pr_number)

        issue_num = task.get("github_issue_number")
        if issue_num:
            await update_issue_labels(
                repo_full, issue_num,
                add=["voltron-done"],
                remove=["voltron-in-progress"],
            )
            await comment_on_issue(
                repo_full, issue_num,
                "CI passed. PR has been merged.",
            )

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
                    add=["voltron-failed"],
                    remove=["voltron-in-progress"],
                )
                await comment_on_issue(
                    repo_full, issue_num,
                    f"CI failed after {self.config.max_ci_retries} retries. "
                    f"Failed checks: {', '.join(ci.failed_checks[:5])}\n\n"
                    f"Marking as failed. Re-add the `voltron` label to retry.",
                )

    async def _process_retry(self, task: dict):
        """Fetch CI logs and re-run agent with context."""
        task_id = task["id"]
        branch = task.get("branch_name")
        if not branch:
            await self.db.update_task(task_id, status="failed", error_message="No branch for retry")
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

    # Recover stale 'reviewing' tasks from previous crash/restart
    async with db._write_lock:
        async with db.db.execute(
            "UPDATE tasks SET status = 'pr_created', review_summary = NULL "
            "WHERE status = 'reviewing' RETURNING id"
        ) as cur:
            recovered = [dict(r) for r in await cur.fetchall()]
            await db.db.commit()
    if recovered:
        ids = [r["id"] for r in recovered]
        log.info("Recovered %d stale reviewing tasks: %s", len(ids), ids)

    daemon = WorkerDaemon(config, db)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, lambda: asyncio.create_task(daemon.shutdown()))

    await daemon.run()


def run_worker():
    """Synchronous entry point for CLI / systemd."""
    asyncio.run(_run_worker())

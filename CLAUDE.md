# CLAUDE.md — Backporcher

Backporcher is a parallel Claude Code agent dispatcher. GitHub Issues are the task queue: label an issue with `backporcher`, and the daemon picks it up, runs a sandboxed `claude -p` agent in a git worktree, creates a PR, reviews it via a coordinator agent, monitors CI, auto-merges on success, and closes the issue.

## Architecture

```
GitHub Issue (label: backporcher)
  → Issue Poller (30s)
    → Batch Orchestrator (haiku, for 2+ issues per repo)
      → SQLite queue (with priority + dependency chain)
        → Conflict check (haiku, ~$0.001) — serializes overlapping tasks
          → Task Executor (semaphore: 2 concurrent, respects dependencies)
            → Credential sync (auto if stale)
              → claude -p in sandboxed worktree
                → Build verification (optional, per-repo)
                  → git push + gh pr create
                    → Coordinator Review (claude -p reviews diff)
                      → CI Monitor (retries up to 3x on failure)
                        → Merge gate (hold for approval or auto-merge)
                          → Close issue (label: backporcher-done)
```

## Six Concurrent Loops

The worker daemon (`src/worker.py`) runs up to 6 async loops via `asyncio.gather()`:

1. **Issue Poller** (every 30s) — scans GitHub for issues labeled `backporcher`, deduplicates (including failed tasks), batch-orchestrates 2+ issues per repo (assigns priorities, dependencies, models via haiku), creates tasks, claims issues with `backporcher-in-progress` label
2. **Task Executor** (every 5s) — claims queued tasks (bounded by semaphore), syncs credentials, runs `claude -p` in isolated worktrees, optionally runs build verification, creates PRs. Auto-retries transient failures (auth, permissions, stale branches)
3. **Coordinator Reviewer** (every 15s) — reviews each PR diff via `claude -p`, checks for conflicts with other open PRs, approves or rejects. Backfills missing `pr_number` from `pr_url`
4. **CI Monitor** (every 60s) — checks CI status on approved PRs, auto-merges passing PRs (squash), auto-retries failures with CI log context, closes GitHub issues on success
5. **Artifact Cleanup** (every 5 min) — removes worktrees and remote branches for terminal tasks older than 10 minutes
6. **Dashboard** (optional) — aiohttp web server with HTTP Basic Auth, real-time SSE updates every 5s, dark-themed HTML UI. Only starts when `BACKPORCHER_DASHBOARD_PASSWORD` is set

## Task Status Flow

```
queued → working → pr_created → reviewing → reviewed → ci_passed → completed (merged)
                                                                 → hold=merge_approval (review-merge mode)
                                                                   → backporcher approve → completed
                                                     → retrying → pr_created (retry loop, up to 3x)
                                                     → failed (max retries exhausted)
                              → reviewing → failed (coordinator rejected PR)
       → hold=dispatch_approval (review-all mode) → backporcher approve → working
       → failed (agent error / exit != 0)
       → completed (agent ran but no changes to push)
       → queued (auto-retry on transient failure, up to 2x)
any    → cancelled (manual via CLI)
```

## Orchestrator Mode

Controls how much human oversight the pipeline requires. Set via `BACKPORCHER_APPROVAL_MODE`:

| Mode | Dispatch | Merge | Default |
|------|----------|-------|---------|
| `full-auto` | automatic | automatic | |
| `review-merge` | automatic | approval required | yes |
| `review-all` | approval required | approval required | |

**Hold system**: Tasks have a `hold` column. When set, the task is skipped by the relevant loop. Hold values: `merge_approval`, `dispatch_approval`, `user_hold`, `conflict_hold`. CLI commands: `backporcher approve <id>`, `backporcher hold <id>`, `backporcher release <id>`.

**Conflict detection**: Before dispatching, Haiku checks if the new task overlaps in file footprint with in-flight tasks in the same repo. If conflict detected, the new task gets `depends_on_task_id` set to the conflicting task (serializes them via the existing dependency mechanism).

**Global pause**: `backporcher pause` / `backporcher resume` — freezes the dispatch queue. In-flight tasks finish normally.

## Key Files

| File | Purpose |
|------|---------|
| `src/cli.py` | CLI entry point: `fleet`, `status`, `cancel`, `cleanup`, `approve`, `hold`, `release`, `pause`, `resume`, `repo`, `worker` |
| `src/worker.py` | Background daemon — 6 async loops, graceful shutdown, startup recovery, preflight checks |
| `src/dashboard.py` | aiohttp web dashboard: HTTP Basic Auth, SSE real-time updates, JSON API, dark-themed HTML |
| `src/dispatcher.py` | Worktree setup, credential sync, agent execution, build verification, PR creation, coordinator review runner, CI retry, transient failure auto-retry |
| `src/db.py` | SQLite with WAL mode, schema migrations (v1→v6), async (`Database`) + sync (`SyncDatabase`) wrappers, write lock for concurrency |
| `src/config.py` | `Config` dataclass populated from environment variables |
| `src/github.py` | All `gh` CLI wrappers — issues, labels, PRs, CI status, diffs, comments, merge, close. Runs as `administrator`, never sandboxed |
| `backporcher.service` | systemd unit file with security hardening directives |
| `scripts/setup-sandbox.sh` | One-time idempotent setup for `backporcher-agent` sandbox user |
| `HANDOFF.md` | Session handoff document with current status |

## Database

SQLite with WAL mode at `data/backporcher.db`. Schema version 6.

**Tables:** `repos` (with `verify_command`), `tasks` (with `review_summary`, `pr_number`, `retry_count`, `priority`, `depends_on_task_id`, `hold`), `task_logs`, `system_state`, `schema_version`

**Concurrency:** All writes go through `asyncio.Lock` (`_write_lock`) to prevent SQLite write conflicts. `busy_timeout=5000ms` for reader contention. The sync wrapper (`SyncDatabase`) is used by CLI commands only.

**Migrations:** Handled in `_migrate_sync()` — runs on every connect. Creates fresh v6 schema for new databases, or migrates existing v1→v2→v3→v4→v5→v6 via table recreation + ALTER TABLE.

**Dedup:** `get_task_by_issue()` checks all non-cancelled tasks for the same issue, preventing duplicate task creation even for failed tasks.

## Configuration

All config via environment variables (see `src/config.py`):

| Variable | Default | Purpose |
|----------|---------|---------|
| `BACKPORCHER_BASE_DIR` | `~/backporcher` | Project root |
| `BACKPORCHER_MAX_CONCURRENCY` | `2` | Max parallel agents (semaphore) |
| `BACKPORCHER_DEFAULT_MODEL` | `sonnet` | Model for work agents |
| `BACKPORCHER_COORDINATOR_MODEL` | `sonnet` | Model for PR review agent |
| `BACKPORCHER_APPROVAL_MODE` | `review-merge` | `full-auto` / `review-merge` / `review-all` |
| `BACKPORCHER_TASK_TIMEOUT` | `3600` | Agent hard-kill timeout (seconds) |
| `BACKPORCHER_POLL_INTERVAL` | `30` | Issue poller interval (seconds) |
| `BACKPORCHER_CI_CHECK_INTERVAL` | `60` | CI monitor interval (seconds) |
| `BACKPORCHER_MAX_CI_RETRIES` | `3` | Max CI failure retries per task |
| `BACKPORCHER_MAX_VERIFY_RETRIES` | `2` | Max build verify fix attempts per task |
| `BACKPORCHER_AGENT_USER` | (none) | Sandbox user for agents (e.g. `backporcher-agent`) |
| `BACKPORCHER_GITHUB_OWNER` | (required) | GitHub org/owner |
| `BACKPORCHER_ALLOWED_USERS` | (required) | Comma-separated issue author allowlist |
| `BACKPORCHER_DASHBOARD_PORT` | `8080` | Dashboard web server port |
| `BACKPORCHER_DASHBOARD_PASSWORD` | (none) | Dashboard password — dashboard disabled if unset |

## Security Model

### Agent Sandboxing
Agents run as `backporcher-agent` via `sudo -u backporcher-agent`. This is a restricted system user with:
- **CAN:** Read/write worktree files, git commit/push, run build/test tools, call Anthropic API
- **CANNOT:** Read admin's home (`~/.ssh`, `~/.claude`, gh tokens), access OpenClaw secrets, sudo, modify system files
- Process limits enforced via `prlimit` (500 processes, 2GB file size)
- Claude credentials are copied (not symlinked) to the agent user's home
- Agent output buffer capped at 10MB to prevent memory exhaustion
- Sensitive env vars (`ANTHROPIC_API_KEY`, `GITHUB_TOKEN`, etc.) stripped from agent subprocess

### Privilege Separation
- `gh` CLI (GitHub API) runs only in the worker process as the admin user — agents never call `gh` directly
- The worker process is the only one that modifies issue labels, posts comments, merges/closes PRs
- Agent output is treated as untrusted — summaries are truncated before storage

### systemd Hardening
The service runs with: `ProtectSystem=full`, `PrivateTmp=yes`, `PrivateDevices=yes`, `PrivateIPC=yes`, `ProtectKernelTunables=yes`, `ProtectKernelLogs=yes`, `ProtectControlGroups=yes`, `ProtectHostname=yes`, `ProtectClock=yes`, `RestrictNamespaces=yes`. Log files created with `0o640` permissions.

### Author Allowlist
Only issues created by users in `BACKPORCHER_ALLOWED_USERS` are picked up. Prevents arbitrary code execution from unknown issue authors.

## Self-Healing Features

1. **Idempotent branch cleanup** — Before creating a worktree, deletes any stale branch from a previous attempt. Re-queuing a failed task always works.
2. **Startup recovery** — On daemon start, `working` tasks are reset to `queued` and `reviewing` tasks to `pr_created`. No manual intervention needed after crashes.
3. **Credential auto-sync** — Before each dispatch, compares admin vs agent credential file mtimes. If admin's are newer, auto-copies to agent user.
4. **Transient failure auto-retry** — Auth errors, EACCES, stale branches, and missing directories auto-retry up to 2x instead of marking permanently failed.
5. **PR number backfill** — If `pr_number` is NULL but `pr_url` exists, the coordinator extracts it automatically. Never blocks on missing data.
6. **Startup preflight** — Verifies agent user can access repos directory and syncs credentials before entering poll loops.
7. **Dependency failure cascade** — When a task fails, all queued tasks that depend on it (and their dependents) are automatically marked as failed.
8. **Terminal state label sync** — All failure paths (agent, verify, CI, coordinator, exceptions) update GitHub issue labels to `backporcher-failed`. No more stale `backporcher-in-progress` labels on finished issues.
9. **Automatic artifact cleanup** — Worktrees and remote branches are deleted on every terminal state (completed, failed, cancelled). A periodic cleanup loop (every 5 min) catches any stragglers older than 10 minutes.
10. **Merge failure recovery** — When PR merge fails without a conflict, the task is marked `failed` instead of silently stalling in `ci_passed`.

## Batch Orchestration

When the issue poller finds 2+ new issues for the same repo, it batch-orchestrates them via a single haiku call instead of triaging each individually. The orchestrator:

1. Assigns **model** (sonnet/opus) per issue based on complexity
2. Assigns **priority** (1-N, lower = runs first)
3. Identifies **dependencies** between issues (e.g., sequential file changes)

Tasks are created in a two-pass process: first all tasks are inserted, then `depends_on_task_id` is set using the issue→task_id mapping. The executor skips blocked tasks (those whose dependency hasn't completed). If a task fails, failure cascades recursively to all queued dependents.

Single new issues still use the existing `triage_issue()` haiku call. Opus-labeled issues bypass orchestration entirely.

**Fallback:** If batch orchestration times out (90s) or returns invalid JSON, falls back to individual triage per issue.

## Build Verification

Repos can have a `verify_command` (e.g., `cd deliverme-rs && cargo check --workspace 2>&1`) that runs after the agent completes but before PR creation. If verification fails, the agent is re-run with the error output as context, up to `BACKPORCHER_MAX_VERIFY_RETRIES` times.

```bash
backporcher repo verify <name> <command>   # Set verify command
backporcher repo verify <name>             # Clear verify command
```

## GitHub Label Protocol

| Label | Meaning | Set by |
|-------|---------|--------|
| `backporcher` | Ready for agent pickup | User |
| `backporcher-in-progress` | Agent claimed and working | Daemon |
| `backporcher-done` | CI passed, PR merged, issue closed | Daemon |
| `backporcher-failed` | Max retries exhausted or coordinator rejected | Daemon |
| `opus` | Use opus model instead of default | User |

## CLI Commands

```bash
backporcher fleet              # Dashboard: running/queued/reviewing/CI status
backporcher status             # All tasks overview
backporcher status <id>        # Single task detail with logs and review summary
backporcher approve <id>       # Approve a held task (merge or dispatch)
backporcher hold <id>          # Set user hold on any non-terminal task
backporcher release <id>       # Release a user hold
backporcher pause              # Pause the dispatch queue (in-flight tasks finish)
backporcher resume             # Resume the dispatch queue
backporcher cancel <id>        # Cancel task + kill agent + restore GitHub labels
backporcher cleanup            # Remove worktrees for finished tasks
backporcher cleanup <id>       # Remove specific task's worktree
backporcher repo add <url>     # Register a GitHub repo
backporcher repo list          # List registered repos (shows verify command)
backporcher repo verify <name> <cmd>  # Set build verification command
backporcher worker             # Run daemon foreground (for systemd)
```

### Fleet Status Badges

| Badge | Status |
|-------|--------|
| `WAIT` | queued |
| ` RUN` | working (agent running) |
| `  PR` | pr_created (awaiting coordinator review) |
| ` REV` | reviewing (coordinator reviewing now) |
| `RVWD` | reviewed (approved, awaiting CI) |
| `  OK` | ci_passed |
| `APRV` | ci_passed + hold=merge_approval (awaiting merge approval) |
| `GATE` | queued + hold=dispatch_approval (awaiting dispatch approval) |
| `HOLD` | any + hold=user_hold (manually held) |
| ` RTY` | retrying (CI failed, agent re-running with CI logs) |
| `DONE` | completed |
| `FAIL` | failed |
| ` CXL` | cancelled |

## Coordinator Review

The coordinator is a lightweight `claude -p` call (300s timeout, not the full 3600s agent timeout) that receives:
- The original task prompt
- The full PR diff (truncated to 15000 chars)
- A list of all other open Backporcher PRs in the same repo (for conflict detection)

It evaluates: task relevance, obvious bugs/regressions, security issues, conflicts with other in-flight PRs, and scope appropriateness.

Output must end with `VERDICT: APPROVE` or `VERDICT: REJECT — {reason}`. The parser strips markdown bold/italic before matching.

On approve: status → `reviewed`, summary posted as PR comment, CI monitor picks up.
On reject: PR closed with explanation, issue labeled `backporcher-failed`, status → `failed`.
On review error: auto-approved (fail-open) so CI can still gate.

## Auto-Merge & Issue Lifecycle

When CI passes on an approved PR:
1. PR is merged via `gh pr merge --squash`
2. Issue gets `backporcher-done` label, `backporcher-in-progress` removed
3. Comment posted: "CI passed. PR has been merged. Closing issue."
4. Issue is closed with reason `completed`

## Operations

```bash
# Start/stop/restart
sudo systemctl start backporcher
sudo systemctl stop backporcher
sudo systemctl restart backporcher

# Watch logs
journalctl -u backporcher -f

# Check schema
sqlite3 data/backporcher.db ".schema tasks"

# Task status breakdown
sqlite3 data/backporcher.db "SELECT status, COUNT(*) FROM tasks GROUP BY status"

# Create test issue
gh issue create --repo owner/repo \
  --title "Test task" --body "Do something" --label backporcher

# Credential rotation for sandbox user
sudo bash scripts/setup-sandbox.sh

# Deploy new code (while service is running — safe until restart)
pip install -e . && sudo systemctl restart backporcher
```

## Known Gotchas

- **CLAUDECODE env var:** Must be unset in agent subprocesses to avoid Claude Code nested-session detection. The dispatcher strips it from the environment.
- **Per-repo git locks:** Git operations (fetch, worktree create) are serialized per-repo via `asyncio.Lock` to prevent concurrent fetch/checkout conflicts.
- **SQLite write lock:** All DB writes go through a single `asyncio.Lock` because SQLite doesn't handle concurrent writers well even in WAL mode.
- **Git identity in worktrees:** Each worktree gets `user.name`/`user.email` set explicitly — the agent user's global gitconfig may not be inherited.
- **Markdown in verdicts:** The coordinator model sometimes wraps `VERDICT: APPROVE` in `**bold**` markers. The parser strips `*` and `_` before matching.
- **Credential expiry:** Claude credentials expire periodically. The auto-sync compares mtimes, but if admin credentials also expire, re-authenticate via `claude` CLI as administrator.
- **Directory permissions:** The `backporcher-agent` user accesses repos via group membership (`backporcher` group). If permissions break, re-run `scripts/setup-sandbox.sh`.
- **Task reset procedure:** Never reset tasks via SQL while the daemon is running — the executor claims them immediately. Always: stop daemon, kill agent processes, reset DB, start daemon. See `docs/solutions/daemon-task-reset-race.md`.
- **Model selection:** Sonnet struggles with multi-file refactoring (may commit only auto-generated files). Use opus for architectural changes, state extraction, or any task touching 3+ files. See `docs/solutions/opus-for-complex-refactoring.md`.
- **System deps for verify:** Tauri projects need GTK/Cairo dev packages for `cargo check --workspace` (`libcairo2-dev`, `libgtk-3-dev`, `libwebkit2gtk-4.1-dev`, etc.). Missing deps cause silent verify failures.

## Solutions Directory

`docs/solutions/` is the institutional knowledge base. Every solved problem becomes searchable documentation. Before starting work on a problem, search solutions first:

```bash
grep -r "relevant keyword" docs/solutions/
```

After solving a non-trivial problem, capture it using the template at `docs/solutions/TEMPLATE.md`. Include: problem, root cause, solution code, prevention strategy.

## Development

```bash
# Install in dev mode
pip install -e .

# Run worker foreground (ctrl+c to stop)
backporcher worker

# Python 3.11+ required, single dependency (aiosqlite)
```

The codebase is intentionally minimal — no web framework, no ORM, no task queue library. Just asyncio + sqlite + subprocess + gh CLI.

# CLAUDE.md — Voltron

Voltron is a parallel Claude Code agent dispatcher. GitHub Issues are the task queue: label an issue with `voltron`, and the daemon picks it up, runs a sandboxed `claude -p` agent in a git worktree, creates a PR, reviews it via a coordinator agent, monitors CI, auto-merges on success, and closes the issue.

## Architecture

```
GitHub Issue (label: voltron)
  → Issue Poller (30s)
    → SQLite queue
      → Task Executor (semaphore: 2 concurrent)
        → Credential sync (auto if stale)
          → claude -p in sandboxed worktree
            → Build verification (optional, per-repo)
              → git push + gh pr create
                → Coordinator Review (claude -p reviews diff)
                  → CI Monitor (retries up to 3x on failure)
                    → Auto-merge PR (squash)
                      → Close issue (label: voltron-done)
```

## Four Concurrent Loops

The worker daemon (`src/worker.py`) runs 4 async loops via `asyncio.gather()`:

1. **Issue Poller** (every 30s) — scans GitHub for issues labeled `voltron`, deduplicates (including failed tasks), creates tasks, claims issues with `voltron-in-progress` label
2. **Task Executor** (every 5s) — claims queued tasks (bounded by semaphore), syncs credentials, runs `claude -p` in isolated worktrees, optionally runs build verification, creates PRs. Auto-retries transient failures (auth, permissions, stale branches)
3. **Coordinator Reviewer** (every 15s) — reviews each PR diff via `claude -p`, checks for conflicts with other open PRs, approves or rejects. Backfills missing `pr_number` from `pr_url`
4. **CI Monitor** (every 60s) — checks CI status on approved PRs, auto-merges passing PRs (squash), auto-retries failures with CI log context, closes GitHub issues on success

## Task Status Flow

```
queued → working → pr_created → reviewing → reviewed → ci_passed → completed (merged)
                                                     → retrying → pr_created (retry loop, up to 3x)
                                                     → failed (max retries exhausted)
                              → reviewing → failed (coordinator rejected PR)
       → failed (agent error / exit != 0)
       → completed (agent ran but no changes to push)
       → queued (auto-retry on transient failure, up to 2x)
any    → cancelled (manual via CLI)
```

## Key Files

| File | Purpose |
|------|---------|
| `src/cli.py` | CLI entry point: `fleet`, `status`, `cancel`, `cleanup`, `repo`, `worker` |
| `src/worker.py` | Background daemon — 4 async loops, graceful shutdown, startup recovery, preflight checks |
| `src/dispatcher.py` | Worktree setup, credential sync, agent execution, build verification, PR creation, coordinator review runner, CI retry, transient failure auto-retry |
| `src/db.py` | SQLite with WAL mode, schema migrations (v1→v4), async (`Database`) + sync (`SyncDatabase`) wrappers, write lock for concurrency |
| `src/config.py` | `Config` dataclass populated from environment variables |
| `src/github.py` | All `gh` CLI wrappers — issues, labels, PRs, CI status, diffs, comments, merge, close. Runs as `administrator`, never sandboxed |
| `voltron.service` | systemd unit file with security hardening directives |
| `scripts/setup-sandbox.sh` | One-time idempotent setup for `voltron-agent` sandbox user |
| `HANDOFF.md` | Session handoff document with current status |

## Database

SQLite with WAL mode at `data/voltron.db`. Schema version 4.

**Tables:** `repos` (with `verify_command`), `tasks` (with `review_summary`, `pr_number`, `retry_count`), `task_logs`, `schema_version`

**Concurrency:** All writes go through `asyncio.Lock` (`_write_lock`) to prevent SQLite write conflicts. `busy_timeout=5000ms` for reader contention. The sync wrapper (`SyncDatabase`) is used by CLI commands only.

**Migrations:** Handled in `_migrate_sync()` — runs on every connect. Creates fresh v4 schema for new databases, or migrates existing v1→v2→v3→v4 via table recreation + ALTER TABLE.

**Dedup:** `get_task_by_issue()` checks all non-cancelled tasks for the same issue, preventing duplicate task creation even for failed tasks.

## Configuration

All config via environment variables (see `src/config.py`):

| Variable | Default | Purpose |
|----------|---------|---------|
| `VOLTRON_BASE_DIR` | `/home/administrator/voltron` | Project root |
| `VOLTRON_MAX_CONCURRENCY` | `2` | Max parallel agents (semaphore) |
| `VOLTRON_DEFAULT_MODEL` | `sonnet` | Model for work agents |
| `VOLTRON_COORDINATOR_MODEL` | `sonnet` | Model for PR review agent |
| `VOLTRON_TASK_TIMEOUT` | `3600` | Agent hard-kill timeout (seconds) |
| `VOLTRON_POLL_INTERVAL` | `30` | Issue poller interval (seconds) |
| `VOLTRON_CI_CHECK_INTERVAL` | `60` | CI monitor interval (seconds) |
| `VOLTRON_MAX_CI_RETRIES` | `3` | Max CI failure retries per task |
| `VOLTRON_MAX_VERIFY_RETRIES` | `2` | Max build verify fix attempts per task |
| `VOLTRON_AGENT_USER` | (none) | Sandbox user for agents (e.g. `voltron-agent`) |
| `VOLTRON_GITHUB_OWNER` | `montenegronyc` | GitHub org/owner |
| `VOLTRON_ALLOWED_USERS` | `montenegronyc` | Comma-separated issue author allowlist |

## Security Model

### Agent Sandboxing
Agents run as `voltron-agent` via `sudo -u voltron-agent`. This is a restricted system user with:
- **CAN:** Read/write worktree files, git commit/push, run build/test tools, call Anthropic API
- **CANNOT:** Read admin's home (`~/.ssh`, `~/.claude`, gh tokens), access OpenClaw secrets, sudo, modify system files
- Process limits enforced via `prlimit` (100 processes, 2GB file size)
- Claude credentials are copied (not symlinked) to the agent user's home
- Agent output buffer capped at 10MB to prevent memory exhaustion
- Sensitive env vars (`ANTHROPIC_API_KEY`, `GITHUB_TOKEN`, etc.) stripped from agent subprocess

### Privilege Separation
- `gh` CLI (GitHub API) runs only in the worker process as `administrator` — agents never call `gh` directly
- The worker process is the only one that modifies issue labels, posts comments, merges/closes PRs
- Agent output is treated as untrusted — summaries are truncated before storage

### systemd Hardening
The service runs with: `ProtectSystem=full`, `PrivateTmp=yes`, `PrivateDevices=yes`, `PrivateIPC=yes`, `ProtectKernelTunables=yes`, `ProtectKernelLogs=yes`, `ProtectControlGroups=yes`, `ProtectHostname=yes`, `ProtectClock=yes`, `RestrictNamespaces=yes`. Log files created with `0o640` permissions.

### Author Allowlist
Only issues created by users in `VOLTRON_ALLOWED_USERS` are picked up. Prevents arbitrary code execution from unknown issue authors.

## Self-Healing Features

1. **Idempotent branch cleanup** — Before creating a worktree, deletes any stale branch from a previous attempt. Re-queuing a failed task always works.
2. **Startup recovery** — On daemon start, `working` tasks are reset to `queued` and `reviewing` tasks to `pr_created`. No manual intervention needed after crashes.
3. **Credential auto-sync** — Before each dispatch, compares admin vs agent credential file mtimes. If admin's are newer, auto-copies to agent user.
4. **Transient failure auto-retry** — Auth errors, EACCES, stale branches, and missing directories auto-retry up to 2x instead of marking permanently failed.
5. **PR number backfill** — If `pr_number` is NULL but `pr_url` exists, the coordinator extracts it automatically. Never blocks on missing data.
6. **Startup preflight** — Verifies agent user can access repos directory and syncs credentials before entering poll loops.

## Build Verification

Repos can have a `verify_command` (e.g., `cd deliverme-rs && cargo check --workspace 2>&1`) that runs after the agent completes but before PR creation. If verification fails, the agent is re-run with the error output as context, up to `VOLTRON_MAX_VERIFY_RETRIES` times.

```bash
voltron repo verify <name> <command>   # Set verify command
voltron repo verify <name>             # Clear verify command
```

## GitHub Label Protocol

| Label | Meaning | Set by |
|-------|---------|--------|
| `voltron` | Ready for agent pickup | User |
| `voltron-in-progress` | Agent claimed and working | Daemon |
| `voltron-done` | CI passed, PR merged, issue closed | Daemon |
| `voltron-failed` | Max retries exhausted or coordinator rejected | Daemon |
| `opus` | Use opus model instead of default | User |

## CLI Commands

```bash
voltron fleet              # Dashboard: running/queued/reviewing/CI status
voltron status             # All tasks overview
voltron status <id>        # Single task detail with logs and review summary
voltron cancel <id>        # Cancel task + kill agent + restore GitHub labels
voltron cleanup            # Remove worktrees for finished tasks
voltron cleanup <id>       # Remove specific task's worktree
voltron repo add <url>     # Register a GitHub repo
voltron repo list          # List registered repos (shows verify command)
voltron repo verify <name> <cmd>  # Set build verification command
voltron worker             # Run daemon foreground (for systemd)
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
| ` RTY` | retrying (CI failed, agent re-running with CI logs) |
| `DONE` | completed |
| `FAIL` | failed |
| ` CXL` | cancelled |

## Coordinator Review

The coordinator is a lightweight `claude -p` call (300s timeout, not the full 3600s agent timeout) that receives:
- The original task prompt
- The full PR diff (truncated to 15000 chars)
- A list of all other open Voltron PRs in the same repo (for conflict detection)

It evaluates: task relevance, obvious bugs/regressions, security issues, conflicts with other in-flight PRs, and scope appropriateness.

Output must end with `VERDICT: APPROVE` or `VERDICT: REJECT — {reason}`. The parser strips markdown bold/italic before matching.

On approve: status → `reviewed`, summary posted as PR comment, CI monitor picks up.
On reject: PR closed with explanation, issue labeled `voltron-failed`, status → `failed`.
On review error: auto-approved (fail-open) so CI can still gate.

## Auto-Merge & Issue Lifecycle

When CI passes on an approved PR:
1. PR is merged via `gh pr merge --squash`
2. Issue gets `voltron-done` label, `voltron-in-progress` removed
3. Comment posted: "CI passed. PR has been merged. Closing issue."
4. Issue is closed with reason `completed`

## Operations

```bash
# Start/stop/restart
sudo systemctl start voltron
sudo systemctl stop voltron
sudo systemctl restart voltron

# Watch logs
journalctl -u voltron -f

# Check schema
sqlite3 data/voltron.db ".schema tasks"

# Task status breakdown
sqlite3 data/voltron.db "SELECT status, COUNT(*) FROM tasks GROUP BY status"

# Create test issue
gh issue create --repo montenegronyc/deliverme \
  --title "Test task" --body "Do something" --label voltron

# Credential rotation for sandbox user
sudo bash scripts/setup-sandbox.sh

# Deploy new code (while service is running — safe until restart)
pip install -e . && sudo systemctl restart voltron
```

## Known Gotchas

- **CLAUDECODE env var:** Must be unset in agent subprocesses to avoid Claude Code nested-session detection. The dispatcher strips it from the environment.
- **Per-repo git locks:** Git operations (fetch, worktree create) are serialized per-repo via `asyncio.Lock` to prevent concurrent fetch/checkout conflicts.
- **SQLite write lock:** All DB writes go through a single `asyncio.Lock` because SQLite doesn't handle concurrent writers well even in WAL mode.
- **Git identity in worktrees:** Each worktree gets `user.name`/`user.email` set explicitly — the agent user's global gitconfig may not be inherited.
- **Markdown in verdicts:** The coordinator model sometimes wraps `VERDICT: APPROVE` in `**bold**` markers. The parser strips `*` and `_` before matching.
- **Credential expiry:** Claude credentials expire periodically. The auto-sync compares mtimes, but if admin credentials also expire, re-authenticate via `claude` CLI as administrator.
- **Directory permissions:** The `voltron-agent` user accesses repos via group membership (`voltron` group). If permissions break, re-run `scripts/setup-sandbox.sh`.

## Development

```bash
# Install in dev mode
pip install -e .

# Run worker foreground (ctrl+c to stop)
voltron worker

# Python 3.11+ required, single dependency (aiosqlite)
```

The codebase is intentionally minimal — no web framework, no ORM, no task queue library. Just asyncio + sqlite + subprocess + gh CLI.

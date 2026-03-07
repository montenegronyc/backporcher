# CLAUDE.md — Voltron

Voltron is a parallel Claude Code agent dispatcher. GitHub Issues are the task queue: label an issue with `voltron`, and the daemon picks it up, runs a sandboxed `claude -p` agent in a git worktree, creates a PR, reviews it via a coordinator agent, monitors CI, and auto-retries on failure.

## Architecture

```
GitHub Issue (label: voltron)
  → Issue Poller (30s)
    → SQLite queue
      → Task Executor (semaphore: 2 concurrent)
        → claude -p in sandboxed worktree
          → git push + gh pr create
            → Coordinator Review (claude -p reviews diff)
              → CI Monitor (retries up to 3x on failure)
                → Done (labels issue voltron-done)
```

## Four Concurrent Loops

The worker daemon (`src/worker.py`) runs 4 async loops via `asyncio.gather()`:

1. **Issue Poller** (every 30s) — scans GitHub for issues labeled `voltron`, deduplicates, creates tasks, claims issues with `voltron-in-progress` label
2. **Task Executor** (every 5s) — claims queued tasks (bounded by semaphore), runs `claude -p` in isolated worktrees, creates PRs
3. **Coordinator Reviewer** (every 15s) — reviews each PR diff via `claude -p`, checks for conflicts with other open PRs, approves or rejects
4. **CI Monitor** (every 60s) — checks CI status on approved PRs, auto-retries failures with CI log context, marks done on success

## Task Status Flow

```
queued → working → pr_created → reviewing → reviewed → ci_passed (success)
                                                     → retrying → pr_created (retry loop, up to 3x)
                                                     → failed (max retries exhausted)
                              → reviewing → failed (coordinator rejected PR)
       → failed (agent error / exit != 0)
       → completed (agent ran but no changes to push)
any    → cancelled (manual via CLI)
```

## Key Files

| File | Purpose |
|------|---------|
| `src/cli.py` | CLI entry point: `fleet`, `status`, `cancel`, `cleanup`, `repo`, `worker` |
| `src/worker.py` | Background daemon — 4 async loops, graceful shutdown, startup recovery |
| `src/dispatcher.py` | Worktree setup, agent execution, PR creation, coordinator review runner, CI retry |
| `src/db.py` | SQLite with WAL mode, schema migrations (v1→v2→v3), async (`Database`) + sync (`SyncDatabase`) wrappers, write lock for concurrency |
| `src/config.py` | `Config` dataclass populated from environment variables |
| `src/github.py` | All `gh` CLI wrappers — issues, labels, PRs, CI status, diffs, comments. Runs as `administrator`, never sandboxed |
| `voltron.service` | systemd unit file |
| `scripts/setup-sandbox.sh` | One-time idempotent setup for `voltron-agent` sandbox user |
| `HANDOFF.md` | Session handoff document with current status |

## Database

SQLite with WAL mode at `data/voltron.db`. Schema version 3.

**Tables:** `repos`, `tasks`, `task_logs`, `schema_version`

**Concurrency:** All writes go through `asyncio.Lock` (`_write_lock`) to prevent SQLite write conflicts. `busy_timeout=5000ms` for reader contention. The sync wrapper (`SyncDatabase`) is used by CLI commands only.

**Migrations:** Handled in `_migrate_sync()` — runs on every connect. Creates fresh v3 schema for new databases, or migrates existing v1→v2→v3 via table recreation (copy data, drop old, rename).

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

### Privilege Separation
- `gh` CLI (GitHub API) runs only in the worker process as `administrator` — agents never call `gh` directly
- The worker process is the only one that modifies issue labels, posts comments, or closes PRs
- Agent output is treated as untrusted — summaries are truncated before storage

### Author Allowlist
Only issues created by users in `VOLTRON_ALLOWED_USERS` are picked up. Prevents arbitrary code execution from unknown issue authors.

## GitHub Label Protocol

| Label | Meaning | Set by |
|-------|---------|--------|
| `voltron` | Ready for agent pickup | User |
| `voltron-in-progress` | Agent claimed and working | Daemon |
| `voltron-done` | CI passed, PR ready for human review | Daemon |
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
voltron repo list          # List registered repos
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

## Startup Recovery

On daemon start, any tasks stuck in `reviewing` (from a previous crash/restart) are reset to `pr_created` for re-review. Tasks stuck in `working` must be manually reset via the database.

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
gh issue create --repo montenegronyc/voltron \
  --title "Test task" --body "Do something" --label voltron

# Credential rotation for sandbox user
sudo bash scripts/setup-sandbox.sh
```

## Known Gotchas

- **CLAUDECODE env var:** Must be unset in agent subprocesses to avoid Claude Code nested-session detection. The dispatcher strips it from the environment.
- **Per-repo git locks:** Git operations (fetch, worktree create) are serialized per-repo via `asyncio.Lock` to prevent concurrent fetch/checkout conflicts.
- **SQLite write lock:** All DB writes go through a single `asyncio.Lock` because SQLite doesn't handle concurrent writers well even in WAL mode.
- **Git identity in worktrees:** Each worktree gets `user.name`/`user.email` set explicitly — the agent user's global gitconfig may not be inherited.
- **Markdown in verdicts:** The coordinator model sometimes wraps `VERDICT: APPROVE` in `**bold**` markers. The parser strips `*` and `_` before matching.
- **PR number backfill:** Early tasks (pre-v2 schema) may have `pr_url` but null `pr_number`. The coordinator skips these unless backfilled.

## Development

```bash
# Install in dev mode
pip install -e .

# Run worker foreground (ctrl+c to stop)
voltron worker

# Python 3.11+ required, single dependency (aiosqlite)
```

The codebase is intentionally minimal — no web framework, no ORM, no task queue library. Just asyncio + sqlite + subprocess + gh CLI.

# Backporcher — GitHub Issues as Task Queue

## Architecture

```
GitHub Issue (label: backporcher)
  → Issue Poller → Batch Orchestrator (haiku, 2+ issues) → SQLite queue (priority + deps)
    → Task Executor (respects deps) → credential sync → claude -p → build verify → git push + gh pr create
      → Coordinator Review (claude -p reviews diff, checks conflicts)
        → CI Monitor → auto-merge PR → close issue
```

Backporcher is a parallel Claude Code agent dispatcher. Create a GitHub issue, add the `backporcher` label, and the daemon picks it up — runs a sandboxed AI agent, verifies the build, creates a PR, reviews it, monitors CI, auto-merges on success, and closes the issue.

## Four Concurrent Loops

The worker daemon runs 4 async loops via `asyncio.gather()`:

1. **Issue Poller** (every 30s) — scans GitHub for issues labeled `backporcher`, deduplicates, batch-orchestrates 2+ issues per repo (haiku assigns priorities, dependencies, models), creates tasks with dependency chains, claims issues
2. **Task Executor** (every 5s) — claims queued tasks, syncs credentials, runs `claude -p` in sandboxed worktrees, runs build verification, creates PRs. Auto-retries transient failures
3. **Coordinator Reviewer** (every 15s) — reviews PR diffs via `claude -p`, approves or rejects with explanation
4. **CI Monitor** (every 60s) — checks PR CI status on approved PRs, auto-merges passing PRs, auto-retries failures (up to 3x), closes issues on success

## Label Protocol

| Label | Meaning | Set by |
|-------|---------|--------|
| `backporcher` | Ready for pickup | User |
| `backporcher-in-progress` | Agent working on it | Daemon |
| `backporcher-done` | CI passed, PR merged, issue closed | Daemon |
| `backporcher-failed` | Max retries exhausted or rejected | Daemon |
| `opus` | Force opus model (skips triage) | User |

## Task Status Flow

```
queued → working → pr_created → reviewing → reviewed → ci_passed → completed (merged)
                                                     → retrying → pr_created (retry loop)
                                                     → failed (max retries)
                              → reviewing → failed (coordinator rejected)
       → failed (agent error)
       → completed (no changes)
       → queued (auto-retry on transient failure)
any    → cancelled (manual)
```

## Key Files

| File | Purpose |
|------|---------|
| `src/github.py` | All `gh` CLI interactions (issues, labels, CI status, comments, merge, close) |
| `src/db.py` | SQLite with schema migration (v1→v5), async + sync wrappers |
| `src/config.py` | Env-var based config |
| `src/dispatcher.py` | Worktree setup, credential sync, agent execution, build verify, PR creation, review runner |
| `src/worker.py` | 4-loop daemon (issue poller, task executor, coordinator reviewer, CI monitor) |
| `src/cli.py` | CLI: fleet, status, cancel, cleanup, repo (add/list/verify), worker |
| `backporcher.service` | systemd unit with security hardening |
| `scripts/setup-sandbox.sh` | One-time sandbox user setup |

## Self-Healing

- **Startup recovery** — `working` → `queued`, `reviewing` → `pr_created` on restart
- **Credential auto-sync** — copies admin credentials to agent user when stale
- **Idempotent branches** — deletes stale branches before worktree creation
- **Transient auto-retry** — auth errors, EACCES, stale branches retry up to 2x
- **PR number backfill** — extracts from URL if database field is NULL
- **Preflight checks** — verifies agent access and credentials on startup
- **Dependency failure cascade** — failed tasks cascade failure to all queued dependents

## Security

- **Agent sandbox** — `claude -p` runs as `backporcher-agent` via `sudo -u`, prlimit enforced
- **`gh` CLI in worker only** — all GitHub API calls run as `administrator`
- **Author allowlist** — `BACKPORCHER_ALLOWED_USERS` filters issue authors
- **Env var filtering** — sensitive vars stripped from agent subprocess
- **Output cap** — 10MB buffer limit on agent output
- **systemd hardening** — PrivateTmp, PrivateDevices, ProtectSystem, etc.

## Configuration (Environment Variables)

| Variable | Default | Purpose |
|----------|---------|---------|
| `BACKPORCHER_POLL_INTERVAL` | 30 | Issue polling interval (seconds) |
| `BACKPORCHER_CI_CHECK_INTERVAL` | 60 | CI check interval (seconds) |
| `BACKPORCHER_MAX_CI_RETRIES` | 3 | Max CI failure retries |
| `BACKPORCHER_MAX_VERIFY_RETRIES` | 2 | Max build verify fix attempts |
| `BACKPORCHER_MAX_CONCURRENCY` | 2 | Parallel agent limit |
| `BACKPORCHER_GITHUB_OWNER` | (required) | GitHub owner |
| `BACKPORCHER_ALLOWED_USERS` | (required) | Comma-separated allowed issue authors |
| `BACKPORCHER_AGENT_USER` | — | Sandbox user for agent |
| `BACKPORCHER_COORDINATOR_MODEL` | sonnet | Model for coordinator PR reviews |

## CLI Commands

```bash
backporcher fleet              # Dashboard: running/queued/CI status
backporcher status             # All tasks overview
backporcher status <id>        # Single task detail with logs
backporcher cancel <id>        # Cancel + restore GitHub labels
backporcher cleanup            # Remove worktrees for finished tasks
backporcher repo add <url>     # Register a repo
backporcher repo list          # List repos (shows verify command)
backporcher repo verify <name> <cmd>  # Set build verification command
backporcher worker             # Run daemon (foreground, for systemd)
```

## Operations

```bash
# Restart worker
sudo systemctl restart backporcher

# Watch daemon
journalctl -u backporcher -f

# Task status breakdown
sqlite3 data/backporcher.db "SELECT status, COUNT(*) FROM tasks GROUP BY status"

# Create test issue
gh issue create --repo owner/repo \
  --title "Test task" --body "Do something" --label backporcher

# Deploy new code
pip install -e . && sudo systemctl restart backporcher

# Re-sync sandbox credentials
sudo bash scripts/setup-sandbox.sh
```

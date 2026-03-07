# Voltron — GitHub Issues as Task Queue

## Architecture

```
GitHub Issue (label: voltron)
  → Issue Poller → SQLite queue
    → Task Executor → credential sync → claude -p → build verify → git push + gh pr create
      → Coordinator Review (claude -p reviews diff, checks conflicts)
        → CI Monitor → auto-merge PR → close issue
```

Voltron is a parallel Claude Code agent dispatcher. Create a GitHub issue, add the `voltron` label, and the daemon picks it up — runs a sandboxed AI agent, verifies the build, creates a PR, reviews it, monitors CI, auto-merges on success, and closes the issue.

## Four Concurrent Loops

The worker daemon runs 4 async loops via `asyncio.gather()`:

1. **Issue Poller** (every 30s) — scans GitHub for issues labeled `voltron`, deduplicates, runs haiku triage to classify complexity (sonnet vs opus), creates tasks, claims issues
2. **Task Executor** (every 5s) — claims queued tasks, syncs credentials, runs `claude -p` in sandboxed worktrees, runs build verification, creates PRs. Auto-retries transient failures
3. **Coordinator Reviewer** (every 15s) — reviews PR diffs via `claude -p`, approves or rejects with explanation
4. **CI Monitor** (every 60s) — checks PR CI status on approved PRs, auto-merges passing PRs, auto-retries failures (up to 3x), closes issues on success

## Label Protocol

| Label | Meaning | Set by |
|-------|---------|--------|
| `voltron` | Ready for pickup | User |
| `voltron-in-progress` | Agent working on it | Daemon |
| `voltron-done` | CI passed, PR merged, issue closed | Daemon |
| `voltron-failed` | Max retries exhausted or rejected | Daemon |
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
| `src/db.py` | SQLite with schema migration (v1→v4), async + sync wrappers |
| `src/config.py` | Env-var based config |
| `src/dispatcher.py` | Worktree setup, credential sync, agent execution, build verify, PR creation, review runner |
| `src/worker.py` | 4-loop daemon (issue poller, task executor, coordinator reviewer, CI monitor) |
| `src/cli.py` | CLI: fleet, status, cancel, cleanup, repo (add/list/verify), worker |
| `voltron.service` | systemd unit with security hardening |
| `scripts/setup-sandbox.sh` | One-time sandbox user setup |

## Self-Healing

- **Startup recovery** — `working` → `queued`, `reviewing` → `pr_created` on restart
- **Credential auto-sync** — copies admin credentials to agent user when stale
- **Idempotent branches** — deletes stale branches before worktree creation
- **Transient auto-retry** — auth errors, EACCES, stale branches retry up to 2x
- **PR number backfill** — extracts from URL if database field is NULL
- **Preflight checks** — verifies agent access and credentials on startup

## Security

- **Agent sandbox** — `claude -p` runs as `voltron-agent` via `sudo -u`, prlimit enforced
- **`gh` CLI in worker only** — all GitHub API calls run as `administrator`
- **Author allowlist** — `VOLTRON_ALLOWED_USERS` filters issue authors
- **Env var filtering** — sensitive vars stripped from agent subprocess
- **Output cap** — 10MB buffer limit on agent output
- **systemd hardening** — PrivateTmp, PrivateDevices, ProtectSystem, etc.

## Configuration (Environment Variables)

| Variable | Default | Purpose |
|----------|---------|---------|
| `VOLTRON_POLL_INTERVAL` | 30 | Issue polling interval (seconds) |
| `VOLTRON_CI_CHECK_INTERVAL` | 60 | CI check interval (seconds) |
| `VOLTRON_MAX_CI_RETRIES` | 3 | Max CI failure retries |
| `VOLTRON_MAX_VERIFY_RETRIES` | 2 | Max build verify fix attempts |
| `VOLTRON_MAX_CONCURRENCY` | 2 | Parallel agent limit |
| `VOLTRON_GITHUB_OWNER` | montenegronyc | GitHub owner |
| `VOLTRON_ALLOWED_USERS` | montenegronyc | Comma-separated allowed issue authors |
| `VOLTRON_AGENT_USER` | — | Sandbox user for agent |
| `VOLTRON_COORDINATOR_MODEL` | sonnet | Model for coordinator PR reviews |

## CLI Commands

```bash
voltron fleet              # Dashboard: running/queued/CI status
voltron status             # All tasks overview
voltron status <id>        # Single task detail with logs
voltron cancel <id>        # Cancel + restore GitHub labels
voltron cleanup            # Remove worktrees for finished tasks
voltron repo add <url>     # Register a repo
voltron repo list          # List repos (shows verify command)
voltron repo verify <name> <cmd>  # Set build verification command
voltron worker             # Run daemon (foreground, for systemd)
```

## Operations

```bash
# Restart worker
sudo systemctl restart voltron

# Watch daemon
journalctl -u voltron -f

# Task status breakdown
sqlite3 data/voltron.db "SELECT status, COUNT(*) FROM tasks GROUP BY status"

# Create test issue
gh issue create --repo montenegronyc/deliverme \
  --title "Test task" --body "Do something" --label voltron

# Deploy new code
pip install -e . && sudo systemctl restart voltron

# Re-sync sandbox credentials
sudo bash scripts/setup-sandbox.sh
```

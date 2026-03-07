# Voltron — GitHub Issues as Task Queue

## Architecture

```
GitHub Issue (label: voltron) → Issue Poller → SQLite queue → Task Executor → claude -p → git push + gh pr create → CI Monitor → auto-retry on failure
```

Voltron is a parallel Claude Code agent dispatcher. Create a GitHub issue, add the `voltron` label, and the daemon picks it up — runs an AI agent, creates a PR, monitors CI, and auto-retries on failure.

## Four Concurrent Loops

The worker daemon runs 4 async loops via `asyncio.gather()`:

1. **Issue Poller** (every 30s) — scans GitHub for issues labeled `voltron`, creates tasks, claims issues
2. **Task Executor** (every 5s) — claims queued tasks, runs `claude -p` in sandboxed worktrees, creates PRs
3. **Coordinator Reviewer** (every 15s) — reviews PR diffs via `claude -p`, approves or rejects with explanation
4. **CI Monitor** (every 60s) — checks PR CI status on approved PRs, auto-retries failures (up to 3x), marks done

## Label Protocol

| Label | Meaning | Set by |
|-------|---------|--------|
| `voltron` | Ready for pickup | User |
| `voltron-in-progress` | Agent working on it | Daemon |
| `voltron-done` | CI passed, ready for review | Daemon |
| `voltron-failed` | Max retries exhausted | Daemon |
| `opus` | Use opus model (optional) | User |

## Task Status Flow

```
queued → working → pr_created → reviewing → reviewed → ci_passed (success)
                                                     → retrying → pr_created (retry loop)
                                                     → failed (max retries)
                              → reviewing → failed (coordinator rejected)
         working → failed (agent error)
         working → completed (no changes)
any → cancelled (manual)
```

## Key Files

| File | Purpose |
|------|---------|
| `src/github.py` | All `gh` CLI interactions (issues, labels, CI status, comments) |
| `src/db.py` | SQLite with schema migration (v1→v2→v3), async + sync wrappers |
| `src/config.py` | Env-var based config |
| `src/dispatcher.py` | Worktree setup, agent execution, PR creation, CI retry |
| `src/worker.py` | 3-loop daemon (issue poller, task executor, CI monitor) |
| `src/cli.py` | CLI: fleet (with REV/RVWD badges), status, cancel, cleanup, repo, worker |
| `voltron.service` | systemd unit |

## Security

- **Agent sandbox** — `claude -p` runs as `voltron-agent` via `sudo -u`, can't read admin secrets
- **`gh` CLI in worker only** — all GitHub API calls run as `administrator`
- **Author allowlist** — `VOLTRON_ALLOWED_USERS` filters issue authors (default: montenegronyc)
- **Bounded retries** — max 3 CI retries prevents infinite loops
- **Audit trail** — every state change visible on GitHub (labels, comments, assignees)

## Configuration (Environment Variables)

| Variable | Default | Purpose |
|----------|---------|---------|
| `VOLTRON_POLL_INTERVAL` | 30 | Issue polling interval (seconds) |
| `VOLTRON_CI_CHECK_INTERVAL` | 60 | CI check interval (seconds) |
| `VOLTRON_MAX_CI_RETRIES` | 3 | Max CI failure retries |
| `VOLTRON_MAX_CONCURRENCY` | 2 | Parallel agent limit |
| `VOLTRON_GITHUB_OWNER` | montenegronyc | GitHub owner |
| `VOLTRON_ALLOWED_USERS` | montenegronyc | Comma-separated allowed issue authors |
| `VOLTRON_AGENT_USER` | — | Sandbox user for agent |
| `VOLTRON_COORDINATOR_MODEL` | sonnet | Model for coordinator PR reviews |

## Agent Sandboxing

Agents run as a dedicated `voltron-agent` system user via `sudo -u voltron-agent`. This provides OS-level isolation:

**What agents CAN do:** edit files in worktree, git commit/push, run build/test tools, access Anthropic API

**What agents CANNOT do:** read admin's home (~/.ssh, ~/.claude, gh tokens), access OpenClaw secrets, sudo, modify system files

Setup: `sudo bash scripts/setup-sandbox.sh` (one-time, idempotent)

## CLI Commands

```bash
voltron fleet              # Dashboard: running/queued/CI status
voltron status             # All tasks overview
voltron status <id>        # Single task detail with logs
voltron cancel <id>        # Cancel + restore GitHub labels
voltron cleanup            # Remove worktrees for finished tasks
voltron repo add <url>     # Register a repo
voltron repo list          # List repos
voltron worker             # Run daemon (foreground, for systemd)
```

## Verification

```bash
# Restart worker
sudo systemctl restart voltron

# Verify migration
sqlite3 /home/administrator/voltron/data/voltron.db ".schema tasks"

# Create test issue
gh issue create --repo montenegronyc/voltron \
  --title "Test: add comment to pyproject.toml" \
  --body "Add a comment saying '# sandbox test'" \
  --label voltron

# Watch daemon
journalctl -u voltron -f

# Fleet status
voltron fleet
```

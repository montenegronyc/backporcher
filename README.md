# Backporcher

![Backporcher Demo](resources/backporcher_demo.gif)

A fully autonomous software engineering pipeline. Label a GitHub issue with `backporcher`, and in ~20 minutes you get a merged PR with tests passing and the issue closed — no human in the loop.

Built in early 2026. 100% auto-merge rate on its first production run (15 PRs, zero manual interventions). This mirrors the agent orchestration architectures emerging from Anthropic's Claude Code and Augment's multi-agent systems — but as a standalone, open-source daemon you can run on your own infra.

## What makes it different

Most "AI coding" tools are glorified autocomplete. Backporcher is an **end-to-end pipeline**: it triages, plans dependencies, dispatches sandboxed agents, reviews their work with a coordinator agent, retries CI failures with error context, and merges — all autonomously.

The key insight: treat agents like junior developers. Give them isolated worktrees, review their PRs, and let CI be the final gate. No magic — just good engineering around `claude -p`.

## The Pipeline

```
GitHub Issue (label: backporcher)
  → Haiku triages complexity (sonnet vs opus)
    → Batch orchestrator assigns priorities + dependency chains
      → Sandboxed claude -p in git worktree
        → Build verification (optional, per-repo)
          → PR created
            → Coordinator reviews diff for bugs, conflicts, scope
              → CI monitor (auto-retries up to 3x with error context)
                → Orchestrator mode: hold for approval -or- auto-merge
                  → Issue closed
```

For 2+ issues in the same repo, a single Haiku call batch-orchestrates all of them — assigning models, priorities, and identifying which issues must be serialized (e.g., both touching the same component).

## Orchestrator Mode

Backporcher defaults to **review-merge** mode: everything is automatic except the final merge to main, which requires `backporcher approve <id>` or a click on the web dashboard. This gives you full visibility and a kill switch without slowing down the pipeline.

Three modes via `BACKPORCHER_APPROVAL_MODE`:
- **`full-auto`** — hands-off, merge on CI pass (the original behavior)
- **`review-merge`** — pause before merge, approve via CLI or dashboard (default)
- **`review-all`** — pause before dispatch AND before merge

Pre-dispatch conflict detection (powered by Haiku, ~$0.001/call) automatically serializes tasks that would touch overlapping files. Global pause/resume lets you freeze the queue without stopping in-flight work.

## Quick Start

```bash
# Install
pip install -e .

# Register a repo
backporcher repo add https://github.com/owner/repo

# Optional: set build verification
backporcher repo verify myrepo "npm test"

# Set up sandbox user (one-time, requires root)
sudo bash scripts/setup-sandbox.sh

# Generate local service files from templates
./scripts/configure.sh

# Install systemd unit
sudo cp backporcher.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now backporcher

# Create an issue to test
gh issue create --repo owner/repo \
  --title "Add a health check endpoint" \
  --body "Add GET /health returning 200 OK" \
  --label backporcher

# Watch it work
backporcher fleet
journalctl -u backporcher -f
```

## CLI

```bash
backporcher fleet              # Live dashboard — what's running, queued, reviewing
backporcher status <id>        # Task detail with logs
backporcher approve <id>       # Approve a held task (merge or dispatch)
backporcher hold <id>          # Manually hold any task
backporcher release <id>       # Release a user hold
backporcher pause              # Freeze the dispatch queue
backporcher resume             # Unfreeze
backporcher cancel <id>        # Kill agent, cancel task, restore labels
backporcher cleanup            # Remove worktrees for finished tasks
backporcher repo add <url>     # Register a GitHub repo
backporcher repo verify <n> <cmd>  # Set build verification command
backporcher worker             # Run daemon foreground
```

## Web Dashboard

Real-time dark-themed dashboard with SSE updates every 5 seconds. Enable by setting `BACKPORCHER_DASHBOARD_PASSWORD`. Shows repo breakdown, active agents with elapsed time, pipeline status, and approve/pause buttons.

## Architecture

Six concurrent async loops in a single process:

| Loop | Interval | Job |
|------|----------|-----|
| Issue Poller | 30s | Scans GitHub for `backporcher`-labeled issues, batch-orchestrates |
| Task Executor | 5s | Claims queued tasks, runs conflict check, dispatches agents |
| Coordinator | 15s | Reviews PR diffs for bugs, conflicts, scope |
| CI Monitor | 60s | Watches CI, auto-retries with error context, merges or holds |
| Cleanup | 5min | Removes worktrees and remote branches for terminal tasks |
| Dashboard | always | aiohttp web server with SSE, approve buttons, pause/resume |

No web framework, no ORM, no task queue library. Just asyncio + SQLite + subprocess + `gh` CLI. Fewer dependencies means a smaller attack surface and an easier audit.

## Configuration

All via environment variables (set in your `.service` file or shell):

| Variable | Default | Purpose |
|----------|---------|---------|
| `BACKPORCHER_BASE_DIR` | `~/backporcher` | Project root |
| `BACKPORCHER_MAX_CONCURRENCY` | `2` | Parallel agents |
| `BACKPORCHER_APPROVAL_MODE` | `review-merge` | `full-auto` / `review-merge` / `review-all` |
| `BACKPORCHER_AGENT_USER` | (none) | Sandbox user (e.g. `backporcher-agent`) |
| `BACKPORCHER_GITHUB_OWNER` | (required) | GitHub org or username that owns the repos |
| `BACKPORCHER_ALLOWED_USERS` | (required) | Comma-separated issue author allowlist |
| `BACKPORCHER_DEFAULT_MODEL` | `sonnet` | Default agent model |
| `BACKPORCHER_COORDINATOR_MODEL` | `sonnet` | PR review model |
| `BACKPORCHER_MAX_CI_RETRIES` | `3` | CI failure retries per task |
| `BACKPORCHER_MAX_TASK_RETRIES` | `3` | Agent failure retries (escalates sonnet→opus) |
| `BACKPORCHER_DASHBOARD_PORT` | `8080` | Dashboard port |
| `BACKPORCHER_DASHBOARD_PASSWORD` | (none) | Dashboard password (required to enable) |

## Security Model

Most open-source agent tools run with full user privileges. Backporcher doesn't. The entire design is built around the assumption that **AI-generated code is untrusted** — and the system treats it that way at every layer.

### Privilege Separation

The worker daemon runs as your user and handles all GitHub API operations (comments, merges, label changes, issue closes). Agents run as a separate `backporcher-agent` user via `sudo -u` — a restricted system account that:

- **Can:** Read/write worktree files, git commit/push, run build/test tools
- **Cannot:** Read your `~/.ssh`, `~/.claude`, GitHub tokens, or any env secrets. Cannot sudo. Cannot modify system files. Cannot access other repos

This means a compromised or misbehaving agent can only damage the worktree it was assigned. It can't escalate to GitHub admin operations, read credentials, or affect other tasks.

### Defense in Depth

| Layer | What it does |
|-------|-------------|
| **Agent sandbox** | `sudo -u backporcher-agent` with `prlimit` (500 processes, 2GB file limit) |
| **Env scrubbing** | `ANTHROPIC_API_KEY`, `GITHUB_TOKEN`, etc. stripped from agent subprocesses |
| **Author allowlist** | Only issues from specified GitHub users trigger agents — prevents arbitrary code execution from unknown authors |
| **Coordinator review** | A separate agent reviews every PR diff for bugs, regressions, and scope creep before CI runs |
| **systemd hardening** | `PrivateTmp`, `PrivateDevices`, `ProtectSystem=full`, `RestrictNamespaces`, and more |
| **Credential copying** | Agent gets credential *copies*, not symlinks — no path traversal back to your home |

### Design Trade-offs

The coordinator review is **fail-open**: if the review agent errors out, the PR is auto-approved and CI becomes the sole gate. This is pragmatic for a solo operator (a stuck review shouldn't block the pipeline), but in a team context you'd want to flip this to fail-closed. A one-line change in `worker.py`.

## Self-Healing

- Stale tasks recovered on restart (working → queued, reviewing → re-review)
- Credentials auto-synced when admin's are newer than agent's
- Transient failures (auth, permissions, stale branches) auto-retry
- Merge conflicts detected and re-queued from fresh main
- Task failure cascades recursively through dependency chains
- Worktrees and remote branches cleaned up automatically

## Smart Retry

When an agent fails, Backporcher doesn't just retry blindly:
- **Agent failure**: re-queues with model escalation (sonnet → opus after first failure)
- **Build verification failure**: re-runs agent with error output as context
- **CI failure**: fetches CI logs, re-runs agent with failure context
- **Coordinator rejection**: closes PR, re-queues with reviewer feedback injected into prompt

## Scaling Limits

Backporcher uses SQLite with WAL mode and a single async write lock. This is production-grade for single-writer workloads and works well at 2-5 concurrent agents. At 10+ concurrent agents, the write lock would become a bottleneck — you'd want to move to PostgreSQL or shard the task queue. For most users running on a single machine, this isn't a practical concern. If you need multi-machine scaling, Backporcher's architecture (poller → queue → executor) maps cleanly onto a proper job queue like Redis + RQ.

SQLite + WAL mode is not a toy database. It's what [Litestream](https://litestream.io/) was built to back up, and it handles the write patterns here (a few writes per minute) with no contention. If you want backup guarantees, point Litestream at `data/backporcher.db`.

## GitHub Labels

| Label | Meaning | Set by |
|-------|---------|--------|
| `backporcher` | Ready for pickup | User |
| `backporcher-in-progress` | Agent working | Daemon |
| `backporcher-done` | Merged and closed | Daemon |
| `backporcher-failed` | Exhausted retries | Daemon |
| `opus` | Force opus model | User |

## Requirements

- Python 3.11+
- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) (`claude`)
- [GitHub CLI](https://cli.github.com/) (`gh`)
- SQLite (bundled with Python)
- A Claude Max subscription or API key

## License

MIT

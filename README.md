# Backporcher

![Backporcher Demo](backporcher_demo_v02.gif)

A fully autonomous software engineering pipeline. Label a GitHub issue with `backporcher`, and in ~20 minutes you get a merged PR with tests passing and the issue closed. A real-time web dashboard lets you manage the fleet: approve or hold tasks before merge, pause/resume the dispatch queue, re-run failed agents, and monitor every stage of the pipeline from triage to merge.

Built in early 2026. 100% auto-merge rate on its first production run (15 PRs, zero manual interventions). This mirrors the agent orchestration architectures emerging from Anthropic's Claude Code and Augment's multi-agent systems, but as a standalone, open-source daemon you can run on your own infra.

## What makes it different

Most "AI coding" tools are glorified autocomplete. Backporcher is an **end-to-end pipeline**: it triages, plans dependencies, dispatches sandboxed agents, reviews their work with a coordinator agent, retries CI failures with error context, and merges, all autonomously.

The key insight: treat agents like junior developers. Give them isolated worktrees, review their PRs, and let CI be the final gate. But unlike junior developers, give them a **code-aware navigation map** so they don't waste time exploring the codebase, and feed them **learnings from every past success and failure** in that repo so they get better over time. No magic, just good engineering around `claude -p`.

## The Pipeline

```
GitHub Issue (label: backporcher)
  → Haiku triages complexity (sonnet vs opus)
    → Batch orchestrator assigns priorities + dependency chains
      → Sonnet queries code graph → navigation map of relevant files/symbols
        → Sandboxed claude -p in git worktree (with stack info + learnings + navigation map)
          → Build verification (optional, per-repo)
            → PR created
              → Code graph builds blast radius (Tree-sitter + dependency BFS)
                → Coordinator reviews diff + impacted code for bugs, conflicts, scope
                → CI monitor (auto-retries up to 3x with error context)
                  → Orchestrator mode: hold for approval -or- auto-merge
                    → Issue closed
```

For 2+ issues in the same repo, a single Haiku call batch-orchestrates all of them, assigning models, priorities, and identifying which issues must be serialized (e.g., both touching the same component).

## Orchestrator Mode

Backporcher defaults to **review-merge** mode: everything is automatic except the final merge to main, which requires `backporcher approve <id>` or a click on the web dashboard. This gives you full visibility and a kill switch without slowing down the pipeline.

Three modes via `BACKPORCHER_APPROVAL_MODE`:
- **`full-auto`**: hands-off, merge on CI pass (the original behavior)
- **`review-merge`**: pause before merge, approve via CLI or dashboard (default)
- **`review-all`**: pause before dispatch AND before merge

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

# Install systemd units
sudo cp backporcher.service backporcher-dashboard.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now backporcher
# Optional: enable dashboard (requires BACKPORCHER_DASHBOARD_PASSWORD in service file)
sudo systemctl enable --now backporcher-dashboard

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
backporcher fleet              # Live dashboard: what's running, queued, reviewing
backporcher status <id>        # Task detail with logs
backporcher approve <id>       # Approve a held task (merge or dispatch)
backporcher hold <id>          # Manually hold any task
backporcher release <id>       # Release a user hold
backporcher pause              # Freeze the dispatch queue
backporcher resume             # Unfreeze
backporcher cancel <id>        # Kill agent, cancel task, restore labels
backporcher cleanup            # Remove worktrees for finished tasks
backporcher stats              # Pipeline performance stats
backporcher repo add <url>     # Register a GitHub repo
backporcher repo list          # List registered repos
backporcher repo verify <n> <cmd>  # Set build verification command
backporcher repo learnings <name> # Show recorded learnings for a repo
backporcher worker             # Run daemon foreground
```

## Web Dashboard

Real-time orchestration dashboard with SSE updates every 5 seconds. Enable by setting `BACKPORCHER_DASHBOARD_PASSWORD`.

- **Fleet overview**: every task's status, repo, model, elapsed time, and current pipeline stage
- **Agent visualizer**: animated orbs showing which agents (coordinator, orchestrator, workers) are active
- **Task control**: approve, hold, reject, re-queue, or escalate individual tasks inline
- **Dispatch on demand**: run a single task immediately without waiting for the poller
- **Edit in flight**: rewrite a task's prompt, switch its model, or change priority before dispatch
- **Pipeline metrics**: merged count, success rate, average time-to-merge, retry rate
- **Global pause/resume**: freeze the dispatch queue while in-flight work finishes
- **Task detail panel**: full timeline with logs, review summary, PR link, and error context

## Architecture

Six concurrent async loops in a single process:

| Loop | Interval | Job |
|------|----------|-----|
| Issue Poller | 30s | Scans GitHub for `backporcher`-labeled issues, batch-orchestrates |
| Task Executor | 5s | Claims queued tasks, runs conflict check, generates navigation context (sonnet + code graph), dispatches agents with structured prompt |
| Coordinator | 15s | Builds code graph, analyzes blast radius, reviews PR diffs for bugs, conflicts, scope |
| CI Monitor | 60s | Watches CI, auto-retries with error context, merges or holds |
| Cleanup | 5min | Removes worktrees and remote branches for terminal tasks |
| Dashboard | always | aiohttp web server with SSE, approve buttons, pause/resume |

No ORM, no task queue library. Just asyncio + aiohttp + SQLite + Tree-sitter + subprocess + `gh` CLI. Fewer dependencies means a smaller attack surface and an easier audit.

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
| `BACKPORCHER_NAVIGATION_MODEL` | `sonnet` | Navigation context model (graph → file map for agents) |
| `BACKPORCHER_NAVIGATION_ENABLED` | `true` | Enable/disable navigation context generation |
| `BACKPORCHER_MAX_CI_RETRIES` | `3` | CI failure retries per task |
| `BACKPORCHER_MAX_TASK_RETRIES` | `3` | Agent failure retries (escalates sonnet→opus) |
| `BACKPORCHER_TASK_TIMEOUT` | `3600` | Agent hard-kill timeout (seconds) |
| `BACKPORCHER_POLL_INTERVAL` | `30` | Issue poller interval (seconds) |
| `BACKPORCHER_CI_CHECK_INTERVAL` | `60` | CI monitor interval (seconds) |
| `BACKPORCHER_MAX_VERIFY_RETRIES` | `2` | Build verification fix attempts per task |
| `BACKPORCHER_DASHBOARD_PORT` | `8080` | Dashboard port |
| `BACKPORCHER_DASHBOARD_HOST` | `127.0.0.1` | Dashboard bind address |
| `BACKPORCHER_DASHBOARD_PASSWORD` | (none) | Dashboard password (required to enable) |
| `BACKPORCHER_WEBHOOK_URL` | (none) | Webhook URL for notifications (Slack/Discord) |
| `BACKPORCHER_WEBHOOK_EVENTS` | `hold,failed` | Comma-separated: `hold`, `failed`, `completed`, `paused` |

## Security Model

Most open-source agent tools run with full user privileges. Backporcher doesn't. The entire design is built around the assumption that **AI-generated code is untrusted**, and the system treats it that way at every layer.

### Privilege Separation

The worker daemon runs as your user and handles all GitHub API operations (comments, merges, label changes, issue closes). Agents run as a separate `backporcher-agent` user via `sudo -u`, a restricted system account that:

- **Can:** Read/write worktree files, git commit/push, run build/test tools
- **Cannot:** Read your `~/.ssh`, `~/.claude`, GitHub tokens, or any env secrets. Cannot sudo. Cannot modify system files. Cannot access other repos

This means a compromised or misbehaving agent can only damage the worktree it was assigned. It can't escalate to GitHub admin operations, read credentials, or affect other tasks.

### Defense in Depth

| Layer | What it does |
|-------|-------------|
| **Agent sandbox** | `sudo -u backporcher-agent` with `prlimit` (500 processes, 2GB file limit) |
| **Env scrubbing** | `ANTHROPIC_API_KEY`, `GITHUB_TOKEN`, etc. stripped from agent subprocesses |
| **Author allowlist** | Only issues from specified GitHub users trigger agents. Prevents arbitrary code execution from unknown authors |
| **Coordinator review** | A separate agent reviews every PR diff with dependency-aware blast radius analysis for bugs, regressions, and scope creep before CI runs |
| **Graph data sanitization** | All code-derived names flowing into coordinator prompts are sanitized: `VERDICT` stripped to prevent prompt injection, names truncated to 120 chars, control characters removed, graph data wrapped in untrusted-data delimiters |
| **systemd hardening** | `PrivateTmp`, `PrivateDevices`, `ProtectSystem=full`, `RestrictNamespaces`, and more |
| **Credential copying** | Agent gets credential *copies*, not symlinks, so no path traversal back to your home |

### Design Trade-offs

The coordinator review is **fail-open**: if the review agent errors out, the PR is auto-approved and CI becomes the sole gate. This is pragmatic for a solo operator (a stuck review shouldn't block the pipeline), but in a team context you'd want to flip this to fail-closed. A one-line change in `worker.py`.

## Agent Intelligence

Agents don't run blind. Backporcher builds a per-repo code dependency graph (Tree-sitter AST parsing across 17 languages, stored in SQLite, traversed via NetworkX BFS) and uses it at **two** points in the pipeline — once to help the agent navigate, and once to help the coordinator review.

### Navigation Context (before the agent starts)

Before dispatching the work agent, a sonnet call queries the code graph with keywords extracted from the task prompt, walks 1-hop dependencies, and produces a focused navigation map: the 5-15 most relevant files, their key symbols, and a rationale for each. This gets injected into the agent's prompt so it starts with the right files open instead of spending tokens grepping around.

The agent prompt is structured in layers, each adding context:
1. **Project Context** — auto-detected tech stack (e.g., "Next.js 15 + TypeScript + Prisma + Jest")
2. **Learnings** — outcomes from previous tasks in this repo (successes and failures)
3. **Navigation Context** — graph-informed file map with symbols and rationale
4. **Task** — the actual issue to implement
5. **Execution Guidelines** — non-interactive agent rules

### Blast Radius Analysis (after the PR is created)

Before the coordinator reviews a PR, the graph runs a 2-hop BFS from changed files to identify indirectly impacted code:
- **Directly changed** symbols (functions, classes) with file locations
- **Indirectly impacted** code (callers, dependents, tests) that wasn't in the diff but could regress
- **Key dependency edges** (CALLS, INHERITS, IMPORTS_FROM) connecting changed and impacted code
- **Impacted files** not in the diff that have dependencies on changed code

### Stack Detection

On first contact with a repo, Backporcher auto-detects the tech stack by inspecting project files (`package.json`, `pyproject.toml`, `Cargo.toml`, etc.). The result (e.g., "Python + FastAPI + Alembic + pytest + Docker + GitHub Actions") is stored per-repo and included in every agent prompt, so the agent knows what tools and conventions to use without discovering them.

### Graph Storage

The graph persists per-repo at `{repo}/.code-review-graph/graph.db` (auto-gitignored). First build parses all source files (pre-built during preflight at daemon startup); subsequent dispatches/reviews use incremental updates that only re-parse changed files and their dependents. Falls back gracefully at every level — if the graph fails, agents still run.

## Self-Healing & Learning

Backporcher treats failure as data. When something goes wrong, the system both recovers immediately and remembers what happened so future tasks avoid the same pitfalls.

### Immediate Recovery

- **Crash recovery**: on restart, stale `working` tasks reset to `queued`, `reviewing` tasks reset to `pr_created` — no manual intervention
- **Credential auto-sync**: before each dispatch, compares admin vs agent credential mtimes; auto-copies if admin's are newer
- **Dependency cascades**: when a task fails, all queued tasks that depend on it (and their dependents) are automatically marked failed
- **Artifact cleanup**: worktrees and remote branches are deleted on every terminal state; a periodic sweep catches stragglers

### Smart Retry

Retries aren't blind — each failure mode gets targeted recovery:
- **Agent failure**: re-queues with model escalation (sonnet → opus after first failure)
- **Build verification failure**: re-runs agent with the error output as context
- **CI failure**: fetches CI logs, re-runs agent with failure context
- **Coordinator rejection**: closes PR, re-queues with reviewer feedback injected into prompt

### Learning Loop

Every terminal outcome — success or failure — gets recorded as a per-repo learning:
- **Success**: what task prompt led to a clean merge
- **Agent failure**: what prompt caused the agent to fail after retries
- **Verify failure**: which build commands broke and why
- **CI failure**: which checks failed after retries were exhausted
- **Coordinator rejection**: what the reviewer flagged as wrong

The last 10 learnings are injected into every new agent prompt for that repo. Agents learn from their predecessors — they know which patterns work, which build commands are finicky, and which areas of the codebase are fragile. The system gets better at each repo the more you use it, without configuration.

## Scaling Limits

Backporcher uses SQLite with WAL mode and a single async write lock. This is production-grade for single-writer workloads and works well at 2-5 concurrent agents. At 10+ concurrent agents, the write lock would become a bottleneck. You'd want to move to PostgreSQL or shard the task queue. For most users running on a single machine, this isn't a practical concern. If you need multi-machine scaling, Backporcher's architecture (poller → queue → executor) maps cleanly onto a proper job queue like Redis + RQ.

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
- Tree-sitter + language pack (installed automatically via `pip install -e .`)

## License

MIT

---
title: Resetting tasks while daemon is running causes immediate re-claim
date: 2026-03-07
tags: [operations, daemon, sqlite, race-condition]
severity: medium
---

# Problem

When resetting tasks from `failed` to `queued` via direct SQL while the daemon is running, the executor loop (polling every 5s) immediately claims the task — often before the operator can restart the daemon with updated code. The task runs with the old code and fails again.

Worse: if you `systemctl restart` while the task is being dispatched, the shutdown kills the daemon but the agent subprocess (running as backporcher-agent) keeps running until it finishes or times out.

# Root Cause

The daemon's executor loop polls every 5 seconds. Any queued task with met dependencies gets claimed immediately. There's no "pause" mechanism.

Agent subprocesses (`sudo -u backporcher-agent prlimit ... claude -p`) are not in the daemon's process group, so `systemctl stop` doesn't kill them.

# Solution

Operational procedure — always follow this order:

```bash
# 1. Stop daemon FIRST
sudo systemctl stop backporcher

# 2. Kill any lingering agent processes
sudo pkill -9 -u backporcher-agent

# 3. Reset tasks in DB
sqlite3 data/backporcher.db "UPDATE tasks SET status='queued', ... WHERE id IN (...)"

# 4. Install updated code if needed
pip install -e .

# 5. Start daemon
sudo systemctl start backporcher
```

Never reset tasks while the daemon is running.

# Prevention

- Could add a CLI command: `backporcher pause` / `backporcher resume` to disable the executor loop
- Could add a `backporcher retry <id>` command that stops, resets, and restarts atomically
- Document the stop-reset-start procedure in CLAUDE.md operations section

# Related

- Multiple failed attempts during this session from resetting while daemon was running
- The startup recovery code (`working` -> `queued`) is fine — it's the live reset that's dangerous

---
title: Docker container nofile ulimit causes uv_thread_create assertion failure
date: 2026-03-21
tags: [docker, nodejs, claude-code, ulimit]
severity: critical
---

# Problem

All backporcher jobs fail immediately with exit code -6. The agent process crashes
before doing any useful work. Dashboard shows "Agent exited with code -6 (retries
exhausted)" on every job.

Container logs show:

```
backporcher.dispatch INFO Task N: agent failed (exit -6), retry 1/3
```

Running the agent manually inside the container reproduces a Node.js assertion crash:

```
node: ../src/uv/uv-unix.c:NNN: uv__thread_create: Assertion `(0) == (uv_thread_create(...))` failed.
Aborted (core dumped)
```

# Root Cause

Docker's default `nofile` soft limit is **1024 open file descriptors**. Node.js (which
backs the Claude Code agent) calls `uv_thread_create` to spawn worker threads, and
`libuv` internally creates file descriptors for inter-thread communication. When the
soft limit is too low, `uv_thread_create` fails with `EMFILE` and triggers an assertion.

Verified via:

```bash
docker exec backporcher cat /proc/self/limits | grep "open files"
# Max open files   1024   524288   files
#                  ^^^^
#                  soft limit is the problem
```

The hard limit (524288) is fine — only the soft limit needs raising.

# Solution

Add `ulimits` to the backporcher service in your `docker-compose.yml`:

```yaml
services:
  backporcher:
    # ... other config ...
    ulimits:
      nofile:
        soft: 65536
        hard: 524288
```

Then restart the container:

```bash
docker compose stop backporcher && docker compose up -d backporcher
```

Verify the fix is active:

```bash
docker exec backporcher cat /proc/self/limits | grep "open files"
# Max open files   65536   524288   files
```

See `docker-compose.example.yml` in the repo root for a complete working example.

# Prevention

- Always include `ulimits.nofile` in any Docker Compose config for backporcher.
- The `privileged: true` flag (required for cgroup delegation) does **not** automatically
  raise the nofile limit — it must be set explicitly.
- Symptom is easy to misread: exit code -6 looks like a signal, but it is actually
  `SIGABRT` from the Node.js assertion, not a problem with the task itself.

# Related

- Docker docs: [ulimits in Compose](https://docs.docker.com/compose/compose-file/05-services/#ulimits)
- Node.js libuv thread pool: each worker needs file descriptors for eventfd/pipes
- `docker-compose.example.yml` in this repo shows the complete corrected config

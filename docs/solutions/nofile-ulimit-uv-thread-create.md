# Solution: agents fail immediately with `uv_thread_create` assertion

**Symptom:** All agent runs fail within 2-3 seconds. Logs show exit code -6 (SIGABRT):

```
Agent exited with code -6
Assertion failed: (0) == (uv_thread_create(&t->thread, worker, w)), function worker_thread_entry
```

**Root cause:** The `backporcher-agent` process runs Claude Code, which is a Node.js application that creates worker threads. Node.js requires more than the default `1024` open file descriptor limit to create worker threads. When the `nofile` soft limit is 1024, `uv_thread_create` fails at startup with an assertion.

This affects:
- All systemd deployments where `LimitNOFILE` is not set (inherits system default of 1024)
- Docker deployments where `ulimits.nofile` is not raised

**Fix (systemd):**

Add `LimitNOFILE=65536` to the `[Service]` section of `backporcher.service`:

```ini
[Service]
...
LimitNOFILE=65536
```

This is now included in `backporcher.service.example`.

**Fix (Docker):**

Add `ulimits` to the backporcher service in `docker-compose.yml`:

```yaml
backporcher:
  ...
  ulimits:
    nofile:
      soft: 65536
      hard: 524288
```

**Verify the limit is applied:**

```bash
# systemd
systemctl show backporcher | grep LimitNOFILE

# Docker
docker exec backporcher sh -c "cat /proc/self/limits | grep 'open files'"
# Expected: soft=65536, hard=524288
```

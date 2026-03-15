---
title: Stale remote branches block worktree re-creation
date: 2026-03-07
tags: [git, worktree, cleanup, requeue]
severity: medium
---

# Problem

When a task is re-queued (e.g., after merge conflict), `setup_worktree()` deletes the local branch and worktree directory. But the remote branch still exists from the previous `git push`. On the next run, `git push` fails with "non-fast-forward" because the remote branch has different history.

Also, `git worktree remove --force` sometimes leaves the directory behind, causing `git worktree add` to fail.

# Root Cause

The original cleanup only handled:
- Local branch deletion (`git branch -D`)
- Worktree removal (`git worktree remove --force`)

Missing:
- Remote branch deletion (`git push origin --delete`)
- Directory cleanup when git worktree remove doesn't fully clean up

# Solution

Two additions in `setup_worktree()` (`src/dispatcher.py`):

```python
# After git worktree remove --force:
if worktree_path.exists():
    import shutil
    shutil.rmtree(str(worktree_path), ignore_errors=True)

# After deleting local branch:
rc, _, _ = await run_cmd(
    "git", "push", "origin", "--delete", branch_name,
    cwd=repo_path, timeout=30,
)
if rc == 0:
    log.info("Deleted stale remote branch %s", branch_name)
```

# Prevention

- Worktree setup is now fully idempotent — re-queuing a task always works regardless of previous state
- The remote delete is non-fatal (branch may not exist remotely on first run)
- Periodically clean up stale remote branches: `git branch -r | grep backporcher/ | sed 's|origin/||' | xargs -I{} git push origin --delete {}`

# Related

- Commit: `2974b76`
- 92 stale branches accumulated before this fix was deployed

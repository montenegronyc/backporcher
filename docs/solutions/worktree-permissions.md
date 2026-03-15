---
title: Worktree files not group-writable for agent user
date: 2026-03-08
tags: [permissions, worktree, sandbox]
severity: critical
symptoms:
  - Agent output says "files don't have group write permissions"
  - Tasks complete with no PR (no code changes made)
  - All agents in a batch fail identically
root_cause: >
  core.sharedRepository=group only affects new git objects (blobs, trees),
  not the permissions of files checked out by git worktree add. Checked-out
  files inherit the umask of the process that ran git, resulting in 0644
  (no group write).
fix: >
  Added chmod -R g+w after worktree creation in setup_worktree() so the
  backporcher-agent user can modify all files.
file: src/dispatcher.py
---

## Problem

After `git worktree add`, all source files have `0644` permissions. The
`backporcher-agent` user is in the `backporcher` group and can read them, but
cannot write. The agent runs, identifies the fix, but can't save changes.

## Why core.sharedRepository=group isn't enough

`core.sharedRepository=group` tells git to create new objects (in `.git/objects/`)
with group permissions. It does NOT affect the working tree file permissions
created during checkout. The checkout respects the process umask (typically 0022),
resulting in `rw-r--r--` files.

## Fix

```python
# In setup_worktree(), after git worktree add + git config:
await run_cmd("chmod", "-R", "g+w", str(worktree_path))
```

## Detection

If agents complete with no PR and output mentions "permission denied" or
"not writable", this is the cause. Check with:
```bash
ls -la ~/backporcher/repos/<repo>/.worktrees/<task_id>/
```

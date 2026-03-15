# Handoff — Rename voltron to backporcher (complete)
Generated: 2026-03-15

## Goal
Rename the project from "voltron" (Hasbro trademark) to "backporcher" before making the repo public. Mechanical find-and-replace across ~26 files, plus GitHub repo rename, label migration, and local infrastructure updates.

## Current status
**Done.** All 5 phases complete, committed, and pushed.

- Phase 1 (source files): 7 files renamed
- Phase 2 (config/infra): 2 files renamed, `voltron.service` renamed to `backporcher.service`
- Phase 3 (tests): 4 files renamed
- Phase 4 (documentation): CLAUDE.md, README.md, HANDOFF.md, 3 solution docs renamed
- Phase 5 (GitHub + local): repo renamed, labels renamed on 5 repos, remote URL updated, directory moved to `~/backporcher`, database renamed, venv recreated, pip reinstalled
- Verification: `grep -ri voltron` = zero hits, 66/66 tests pass, `backporcher fleet` works, GitHub repo accessible

## Key decisions made
- **Longest-match-first replacement order** to avoid partial replacements (e.g., `voltron-in-progress` before `voltron`, `VOLTRON_` before `Voltron`)
- **15 replacement patterns** applied in strict order per file
- **Venv recreated** (not patched) because shebangs pointed to old `/home/administrator/voltron/.venv/bin/python3` path
- **~/CLAUDE.md updated** too (references `voltron` in repo structure and Python style sections)

## What worked
- Parallel background agents for Phase 4 docs (4 agents, all completed successfully)
- `replace_all=true` in Edit tool for bulk replacements per file
- Post-phase grep verification caught a stray `VOLTRON` (all-caps) in dashboard.py HTML header that wasn't in the original pattern list

## What didn't work
- **pyproject.toml**: Edit tool requires reading the file in the current conversation context, not just in a sub-agent. Had to re-read and retry.
- **Venv shebang breakage**: After `mv ~/voltron ~/backporcher`, all venv scripts had shebangs pointing to the old path. Fixed by recreating the venv with `python3 -m venv .venv --clear`.

## Files modified
20 files changed (380 insertions, 380 deletions):

| File | Change |
|------|--------|
| `src/config.py` | `VOLTRON_*` env vars, `voltron.db` path |
| `src/github.py` | Labels dict, logger name |
| `src/dispatcher.py` | Labels, branch prefix, git identity, logger, group name |
| `src/worker.py` | Labels, CLI refs in messages, logger |
| `src/cli.py` | prog name, label refs, CLI output |
| `src/dashboard.py` | Logger, HTML title/header/footer |
| `pyproject.toml` | Package name, entry point |
| `backporcher.service` | Renamed from `voltron.service`, all env vars and paths |
| `scripts/setup-sandbox.sh` | User, group, git identity, directory paths |
| `tests/test_config.py` | Env var names |
| `tests/test_cli.py` | Helper function name |
| `tests/test_dispatcher.py` | Branch prefix assertions |
| `tests/test_github.py` | Label references |
| `CLAUDE.md` | All references (~45+) |
| `README.md` | All references (~30+) |
| `HANDOFF.md` | All references (~20+) |
| `docs/solutions/daemon-task-reset-race.md` | Agent user, db path, CLI commands |
| `docs/solutions/worktree-permissions.md` | Agent user, group, paths |
| `docs/solutions/stale-branch-cleanup.md` | Branch prefix in grep pattern |

## Next steps
1. **Deploy the service**: Copy `backporcher.service` to `/etc/systemd/system/`, update env vars, `daemon-reload`, restart
2. **Re-run sandbox setup**: `sudo bash scripts/setup-sandbox.sh` (creates `backporcher-agent` user and `backporcher` group)
3. **Verify the old `voltron-agent` user/group** can be removed (check no running processes)
4. **Update any external references** (CI configs, monitoring, documentation outside this repo)
5. **Make repo public** when ready

## Context the next session needs
- **Repo location**: `~/backporcher` (was `~/voltron`)
- **GitHub URL**: `github.com/montenegronyc/backporcher`
- **Git remote**: Already updated to new URL
- **Database**: `data/backporcher.db` (renamed from `voltron.db`, contents unchanged)
- **Venv**: Recreated fresh at `~/backporcher/.venv` — clean install of `backporcher-0.2.0`
- **Old service file**: The systemd service at `/etc/systemd/system/` may still reference `voltron` — needs manual update with the new `backporcher.service`
- **Old sandbox user**: `voltron-agent` user and `voltron` group still exist on the system — the new `backporcher-agent` user hasn't been created yet (need to run `setup-sandbox.sh`)
- **Labels**: Already renamed on all 5 registered repos (voltron/backporcher, deliverme, shipular-engine, shipular, shipular-api)

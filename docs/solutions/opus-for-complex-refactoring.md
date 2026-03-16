---
title: Use opus for multi-file TypeScript refactoring
date: 2026-03-07
tags: [model-selection, agent-quality, refactoring]
severity: medium
---

# Problem

Sonnet was assigned (via batch orchestrator) to extract Zustand stores from a 647-line App.tsx. The agent ran, committed only an auto-generated schema file, and didn't create any of the required TypeScript files. The coordinator correctly rejected the PR.

# Root Cause

Sonnet is fast but sometimes doesn't follow through on complex multi-file refactoring tasks. In this case it:
1. Ran cargo check (which generated a schema file)
2. Committed the schema file
3. Exited without creating usePresetStore.ts or modifying App.tsx

The task required reading a 647-line file, understanding state ownership, creating a new Zustand store with proper API integration, and updating multiple consumer components.

# Solution

Switched the model from `sonnet` to `opus` for tasks #174-177. Opus:
- Created proper Zustand stores with typed state and actions
- Correctly moved state from App.tsx to stores
- Updated all consumer components
- Passed both cargo check and npm build verification
- All 4 PRs approved by coordinator on first review

# Prevention

Model selection heuristic for the batch orchestrator:
- **Sonnet**: Bug fixes, single-file changes, config tweaks, docs, simple additions
- **Opus**: Multi-file refactoring, state management changes, architectural work, tasks that require reading and understanding >300 lines of context

Consider updating the orchestrator prompt to be more aggressive about assigning opus for refactoring tasks, or add a keyword trigger (e.g., "extract", "refactor", "migrate" in the issue title).

# Related

- PRs merged successfully with opus after sonnet failed on the same tasks
- The `opus` GitHub label already exists as a manual override

# Multi-Agent Backend Integration for Backporcher

**Date:** 2026-03-24
**Status:** Draft
**Goal:** Add Kimi Code and OpenAI Codex as alternative agent backends alongside Claude Code, with intelligent routing, fallback, and load balancing.

## Motivation

Backporcher currently uses Claude Code exclusively for all agent work. This design adds support for multiple coding agent backends to achieve:

1. **Cost optimisation** — route simpler tasks to cheaper agents (Kimi), reserve Claude for complex work
2. **Capability diversity** — different agents have different strengths per language/task type
3. **Redundancy/fallback** — if Claude is rate-limited or down, fall back to other agents

## CLI Interface Comparison

All three agents support non-interactive subprocess invocation with JSON streaming:

| | `claude -p "prompt"` | `kimi -p "prompt" --print` | `codex exec "prompt"` |
|---|---|---|---|
| Auto-approve | `--dangerously-skip-permissions` | `--yolo` (implied by `--print`) | `--full-auto` or `--yolo` |
| JSON output | `--output-format stream-json` | `--output-format stream-json` | `--json` |
| Working dir | cwd | `-w path` | `-C path` |
| Model select | `--model` | `-m` | `-m` |
| Auth env var | `ANTHROPIC_API_KEY` | `KIMI_API_KEY` | `CODEX_API_KEY` |

## Architecture

### 1. Agent Backend Abstraction

New package `src/backends/` with a common protocol and per-agent implementations.

```
src/backends/
  __init__.py       # AgentBackend protocol, AgentEvent dataclass, registry
  claude.py         # Claude Code backend (extracted from current agent.py)
  kimi.py           # Kimi Code backend
  codex.py          # OpenAI Codex backend
```

**`AgentBackend` protocol:**

```python
class AgentBackend(Protocol):
    name: str

    def build_command(self, prompt: str, model: str, worktree_path: Path, config: Config) -> list[str]:
        """Build the subprocess argv for this agent."""
        ...

    def build_env(self, config: Config) -> dict[str, str] | None:
        """Build sanitised environment for the subprocess. None = inherit (sudo resets)."""
        ...

    def parse_output_line(self, line: str) -> AgentEvent | None:
        """Parse a single line of stdout into a normalised event."""
        ...

    def get_capabilities(self) -> AgentCapabilities:
        """Report available models and features."""
        ...
```

**`AgentEvent` dataclass** (normalised across all backends):

```python
@dataclass
class AgentEvent:
    type: str          # "text", "result", "error", "tool_use", "progress"
    content: str       # text content or summary
    is_error: bool     # whether this is an error event
    raw: dict          # original parsed JSON for backend-specific inspection
```

**`AgentCapabilities` dataclass:**

```python
@dataclass
class AgentCapabilities:
    models: list[str]              # e.g. ["sonnet", "opus"] or ["kimi-latest"]
    supports_stream_json: bool     # whether --output-format stream-json works
    supports_working_dir: bool     # whether agent can be pointed at a directory
    default_model: str             # default model if none specified
```

### 2. Backend Implementations

**Claude backend** (`claude.py`) — extracted from current hardcoded behavior in `agent.py`:
- Command: `["claude", "-p", "--output-format", "stream-json", "--verbose", "--dangerously-skip-permissions", "--model", model, prompt]`
- Env: strips `SENSITIVE_ENV_VARS` + `CLAUDECODE` + SSH vars
- Parse: handles `type="assistant"`, `type="result"`, `type="content_block_delta"` events
- Models: `["sonnet", "opus", "haiku"]`

**Kimi backend** (`kimi.py`):
- Command: `["kimi", "-p", prompt, "--print", "--output-format", "stream-json", "-y", "-w", str(worktree_path), "-m", model]`
- Env: ensures `KIMI_API_KEY` is set, strips same sensitive vars
- Parse: Kimi uses Anthropic tool calling — stream-json events should follow the same schema as Claude. Parse identically to Claude backend, with fallback for any Kimi-specific event types.
- Models: `["kimi-latest"]` (or whatever models Kimi exposes)

**Codex backend** (`codex.py`):
- Command: `["codex", "exec", prompt, "--json", "--full-auto", "--skip-git-repo-check", "-C", str(worktree_path), "-m", model]`
- Env: ensures `CODEX_API_KEY` is set, strips sensitive vars
- Parse: maps Codex JSONL events (`item.completed`, `turn.completed`, `turn.failed`) to `AgentEvent`
- Models: `["gpt-5", "gpt-5.4", "gpt-5.3-codex"]`

### 3. Backend Registry

A simple dict mapping agent name to backend instance, auto-populated based on availability:

```python
def discover_backends(config: Config) -> dict[str, AgentBackend]:
    """Return available backends. An agent is available if its CLI is in PATH and API key is set."""
    backends = {}
    if shutil.which("claude"):
        backends["claude"] = ClaudeBackend()
    if shutil.which("kimi") and config.kimi_api_key:
        backends["kimi"] = KimiBackend()
    if shutil.which("codex") and config.codex_api_key:
        backends["codex"] = CodexBackend()
    return backends
```

### 4. Triage Enhancement

The triage system currently returns `(model, reason)`. Extended to return `(agent, model, reason)`.

**Updated triage prompt** adds an agents section:
```
## Agents Available
- **claude**: Most capable. Complex multi-file changes, architectural work. Expensive.
- **kimi**: Good general capability, cost-effective. Single/multi-file changes.
- **codex**: OpenAI-backed. Good for straightforward implementations.

Respond: AGENT: <agent> MODEL: <model> — <reason>
```

**Batch orchestration** JSON schema gains an `"agent"` field per issue.

**Fallback logic:** If triage picks an unavailable agent, fall back through the chain: `claude -> kimi -> codex` (configurable via `BACKPORCHER_FALLBACK_CHAIN`).

### 5. Config Changes

`src/config.py` gains new fields loaded from environment variables:

```python
# Agent backend configuration
kimi_api_key: str          # KIMI_API_KEY
codex_api_key: str         # CODEX_API_KEY
enabled_agents: list[str]  # BACKPORCHER_ENABLED_AGENTS (default: ["claude"])
default_agent: str         # BACKPORCHER_DEFAULT_AGENT (default: "claude")
fallback_chain: list[str]  # BACKPORCHER_FALLBACK_CHAIN (default: ["claude", "kimi", "codex"])
```

### 6. Database Schema Migration (v9)

```sql
ALTER TABLE tasks ADD COLUMN agent TEXT NOT NULL DEFAULT 'claude';
```

Tracks which backend executed each task. Used for:
- Per-agent learnings and success rates
- Dashboard display
- Stats and cost tracking

### 7. Agent Execution Changes

`src/agent.py` `run_agent()` changes:
1. Receives `backend: AgentBackend` parameter (looked up from registry by task's `agent` field)
2. Calls `backend.build_command()` instead of hardcoding Claude CLI args
3. Calls `backend.build_env()` instead of hardcoding env var stripping
4. Stream parsing loop calls `backend.parse_output_line()` instead of inline JSON parsing
5. Everything else (timeout, logging, pid tracking, log file writing) stays the same

**Fallback on failure:** If an agent exits non-zero and fallback is enabled, re-queue the task with the next agent in the fallback chain. Current sonnet→opus escalation remains orthogonal (model escalation within an agent).

### 8. Docker Changes

**Dockerfile** additions:
```dockerfile
# Kimi Code CLI
RUN uv tool install --python 3.13 kimi-cli

# OpenAI Codex CLI
RUN npm install -g @openai/codex
```

**docker-compose.yml** additions:
```yaml
environment:
  - KIMI_API_KEY=${KIMI_API_KEY}
  - CODEX_API_KEY=${CODEX_API_KEY}
  - BACKPORCHER_ENABLED_AGENTS=claude,kimi,codex
```

API keys stored in `.env` file (not committed), passed through as environment variables.

### 9. Dashboard Enhancements

- Task list shows agent name alongside model for each task
- Stats page shows per-agent success rates and average completion time
- Agent health indicators (available/unavailable) in the dashboard header

## File Change Summary

| File | Change |
|---|---|
| `src/backends/__init__.py` | NEW — Protocol, dataclasses, registry |
| `src/backends/claude.py` | NEW — Claude backend (extracted from agent.py) |
| `src/backends/kimi.py` | NEW — Kimi backend |
| `src/backends/codex.py` | NEW — Codex backend |
| `src/agent.py` | MODIFY — use backend protocol instead of hardcoded Claude CLI |
| `src/triage.py` | MODIFY — return agent alongside model, update prompts |
| `src/prompts.py` | MODIFY — add agent selection to triage/batch prompts |
| `src/config.py` | MODIFY — add agent-related config fields |
| `src/db.py` | MODIFY — add agent column, migration v9 |
| `src/db_schema.py` | MODIFY — schema v9 with agent column |
| `src/worker.py` | MODIFY — pass backend to run_agent, fallback logic |
| `src/dispatch.py` | MODIFY — store agent on task creation |
| `src/review.py` | MODIFY — use backend for review agent calls (or keep Claude-only for reviews) |
| `src/dashboard.py` | MODIFY — show agent info |
| `Dockerfile` | MODIFY — install kimi-cli and codex |
| `docker-compose.yml` | MODIFY — add env vars |
| `.env.example` | NEW — document required env vars |

## Decisions

1. **Reviews stay Claude-only initially** — the coordinator review is a critical quality gate. Keep it on Claude (the most capable) until we have data on other agents' review quality.
2. **Triage stays on Claude Haiku** — it's cheap, fast, and already works. Adding agent selection to its output is a prompt change, not an architecture change.
3. **Navigation context is agent-agnostic** — the Tree-sitter graph and navigation prompt work regardless of which agent executes the task.
4. **No shared agent state** — each backend is stateless. No session resumption across agents.

## Testing Strategy

1. Unit tests for each backend's `build_command()` and `parse_output_line()`
2. Integration test: run each agent on a trivial task (add a comment to a file) in a test repo
3. Triage test: verify the updated prompt correctly outputs agent selection
4. Fallback test: mock agent failure, verify re-queue to next agent
5. DB migration test: verify v8→v9 migration preserves existing data

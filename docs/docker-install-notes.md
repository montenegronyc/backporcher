# Docker Installation Notes for Multi-Agent Backends

Add to the backporcher Dockerfile (in devinfra/images/backporcher/):

## Kimi CLI
RUN uv tool install kimi-cli

## OpenAI Codex CLI
RUN curl -fsSL https://github.com/openai/codex/releases/latest/download/install.sh | sh

## Environment Variables
Pass via docker-compose.yml:
- KIMI_API_KEY=${KIMI_API_KEY}
- CODEX_API_KEY=${CODEX_API_KEY}
- BACKPORCHER_ENABLED_AGENTS=claude,kimi,codex

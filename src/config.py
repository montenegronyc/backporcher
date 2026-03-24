"""Configuration from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

BASE_DIR = Path(os.environ.get("BACKPORCHER_BASE_DIR", str(Path.home() / "backporcher")))


@dataclass(frozen=True)
class Config:
    base_dir: Path = field(default_factory=lambda: BASE_DIR)
    db_path: Path = field(default_factory=lambda: BASE_DIR / "data" / "backporcher.db")
    repos_dir: Path = field(default_factory=lambda: BASE_DIR / "repos")
    logs_dir: Path = field(default_factory=lambda: BASE_DIR / "logs")

    max_workers: int = 2  # Conservative for Max subscription rate limits
    default_model: str = "sonnet"
    allowed_models: tuple[str, ...] = ("sonnet", "opus", "haiku")

    task_timeout_seconds: int = 3600  # 1 hour hard kill
    poll_interval_seconds: int = 30  # Issue polling interval
    allowed_git_hosts: tuple[str, ...] = ("github.com",)

    agent_user: str | None = None  # Run agents as this user via sudo -u

    # GitHub Issues integration
    github_owner: str = ""
    max_ci_retries: int = 3
    ci_check_interval_seconds: int = 60
    allowed_github_users: tuple[str, ...] = ()

    # Coordinator review
    coordinator_model: str = "sonnet"

    # Navigation context (graph-informed agent guidance)
    navigation_model: str = "sonnet"
    navigation_enabled: bool = True

    # Build verification
    max_verify_retries: int = 2

    # Smart retry (shared budget across all failure modes)
    max_task_retries: int = 3

    # Approval mode: full-auto | review-merge | review-all
    approval_mode: str = "review-merge"

    # Dashboard
    dashboard_port: int = 8080
    dashboard_host: str = "127.0.0.1"
    dashboard_password: str | None = None
    dashboard_skip_auth: bool = False  # Skip auth when behind a reverse proxy (e.g. Caddy)

    # Webhooks
    webhook_url: str | None = None
    webhook_events: tuple[str, ...] = ("hold", "failed")

    # Multi-agent backends
    kimi_api_key: str = ""
    codex_api_key: str = ""
    gemini_api_key: str = ""
    opencode_model: str = ""  # e.g. "local/Qwen3.5-9B-Q4_K_M.gguf"
    enabled_agents: tuple[str, ...] = ("claude",)
    default_agent: str = "claude"
    fallback_chain: tuple[str, ...] = ("claude", "kimi", "codex", "gemini", "opencode")


def load_config() -> Config:
    """Load config from environment variables."""
    base_dir = Path(os.environ.get("BACKPORCHER_BASE_DIR", str(BASE_DIR)))

    allowed_users_str = os.environ.get("BACKPORCHER_ALLOWED_USERS", "")
    allowed_users = tuple(u.strip() for u in allowed_users_str.split(",") if u.strip())

    return Config(
        base_dir=base_dir,
        db_path=Path(os.environ.get("BACKPORCHER_DB_PATH", str(base_dir / "data" / "backporcher.db"))),
        repos_dir=Path(os.environ.get("BACKPORCHER_REPOS_DIR", str(base_dir / "repos"))),
        logs_dir=Path(os.environ.get("BACKPORCHER_LOG_DIR", str(base_dir / "logs"))),
        max_workers=int(os.environ.get("BACKPORCHER_MAX_CONCURRENCY", "2")),
        default_model=os.environ.get("BACKPORCHER_DEFAULT_MODEL", "sonnet"),
        task_timeout_seconds=int(os.environ.get("BACKPORCHER_TASK_TIMEOUT", "3600")),
        poll_interval_seconds=int(os.environ.get("BACKPORCHER_POLL_INTERVAL", "30")),
        agent_user=os.environ.get("BACKPORCHER_AGENT_USER") or None,
        github_owner=os.environ.get("BACKPORCHER_GITHUB_OWNER", ""),
        max_ci_retries=int(os.environ.get("BACKPORCHER_MAX_CI_RETRIES", "3")),
        ci_check_interval_seconds=int(os.environ.get("BACKPORCHER_CI_CHECK_INTERVAL", "60")),
        allowed_github_users=allowed_users,
        coordinator_model=os.environ.get("BACKPORCHER_COORDINATOR_MODEL", "sonnet"),
        navigation_model=os.environ.get("BACKPORCHER_NAVIGATION_MODEL", "sonnet"),
        navigation_enabled=os.environ.get("BACKPORCHER_NAVIGATION_ENABLED", "true").lower() in ("true", "1", "yes"),
        max_verify_retries=int(os.environ.get("BACKPORCHER_MAX_VERIFY_RETRIES", "2")),
        max_task_retries=int(os.environ.get("BACKPORCHER_MAX_TASK_RETRIES", "3")),
        approval_mode=os.environ.get("BACKPORCHER_APPROVAL_MODE", "review-merge"),
        dashboard_port=int(os.environ.get("BACKPORCHER_DASHBOARD_PORT", "8080")),
        dashboard_host=os.environ.get("BACKPORCHER_DASHBOARD_HOST", "127.0.0.1"),
        dashboard_password=os.environ.get("BACKPORCHER_DASHBOARD_PASSWORD") or None,
        dashboard_skip_auth=os.environ.get("BACKPORCHER_DASHBOARD_SKIP_AUTH", "").lower() in ("true", "1", "yes"),
        webhook_url=os.environ.get("BACKPORCHER_WEBHOOK_URL") or None,
        webhook_events=tuple(
            e.strip() for e in os.environ.get("BACKPORCHER_WEBHOOK_EVENTS", "hold,failed").split(",") if e.strip()
        ),
        kimi_api_key=os.environ.get("KIMI_API_KEY", ""),
        codex_api_key=os.environ.get("CODEX_API_KEY", ""),
        gemini_api_key=os.environ.get("GEMINI_API_KEY", ""),
        opencode_model=os.environ.get("BACKPORCHER_OPENCODE_MODEL", ""),
        enabled_agents=tuple(
            a.strip() for a in os.environ.get("BACKPORCHER_ENABLED_AGENTS", "claude").split(",") if a.strip()
        ),
        default_agent=os.environ.get("BACKPORCHER_DEFAULT_AGENT", "claude").strip(),
        fallback_chain=tuple(
            a.strip()
            for a in os.environ.get("BACKPORCHER_FALLBACK_CHAIN", "claude,kimi,codex,gemini,opencode").split(",")
            if a.strip()
        ),
    )

"""Configuration from environment variables with secure defaults."""

import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Config:
    # Auth — required, no defaults for credentials
    auth_user: str = ""
    auth_pass: str = ""

    # Server
    host: str = "127.0.0.1"  # Loopback only by default
    port: int = 8420

    # Paths
    base_dir: Path = field(default_factory=lambda: Path.cwd())
    db_path: Path = field(default_factory=lambda: Path.cwd() / "data" / "compound.db")
    repos_dir: Path = field(default_factory=lambda: Path.cwd() / "repos")
    logs_dir: Path = field(default_factory=lambda: Path.cwd() / "logs")

    # Worker pool
    max_workers: int = 3
    default_max_budget_usd: float = 5.0
    max_budget_limit_usd: float = 50.0  # Hard cap per task

    # Agent
    default_model: str = "sonnet"
    allowed_models: tuple[str, ...] = ("sonnet", "opus", "haiku")

    # Security
    session_secret: str = field(default_factory=lambda: secrets.token_hex(32))
    max_prompt_length: int = 50_000  # Characters
    max_log_lines: int = 10_000
    task_timeout_seconds: int = 3600  # 1 hour hard kill

    # Allowed GitHub hosts for repo URLs
    allowed_git_hosts: tuple[str, ...] = ("github.com",)


def load_config() -> Config:
    """Load config from environment variables."""
    base_dir = Path(os.environ.get("COMPOUND_BASE_DIR", Path.cwd()))

    auth_user = os.environ.get("COMPOUND_AUTH_USER", "")
    auth_pass = os.environ.get("COMPOUND_AUTH_PASS", "")

    if not auth_user or not auth_pass:
        raise RuntimeError(
            "COMPOUND_AUTH_USER and COMPOUND_AUTH_PASS must be set. "
            "Refusing to start without authentication."
        )

    return Config(
        auth_user=auth_user,
        auth_pass=auth_pass,
        host=os.environ.get("COMPOUND_HOST", "127.0.0.1"),
        port=int(os.environ.get("COMPOUND_PORT", "8420")),
        base_dir=base_dir,
        db_path=base_dir / "data" / "compound.db",
        repos_dir=base_dir / "repos",
        logs_dir=base_dir / "logs",
        max_workers=int(os.environ.get("COMPOUND_MAX_WORKERS", "3")),
        default_max_budget_usd=float(
            os.environ.get("COMPOUND_DEFAULT_BUDGET", "5.0")
        ),
        max_budget_limit_usd=float(
            os.environ.get("COMPOUND_MAX_BUDGET_LIMIT", "50.0")
        ),
        default_model=os.environ.get("COMPOUND_DEFAULT_MODEL", "sonnet"),
        task_timeout_seconds=int(
            os.environ.get("COMPOUND_TASK_TIMEOUT", "3600")
        ),
    )

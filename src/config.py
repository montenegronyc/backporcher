"""Configuration from environment variables."""

import os
from dataclasses import dataclass, field
from pathlib import Path

BASE_DIR = Path("/home/administrator/voltron")


@dataclass(frozen=True)
class Config:
    base_dir: Path = BASE_DIR
    db_path: Path = field(default_factory=lambda: BASE_DIR / "data" / "voltron.db")
    repos_dir: Path = field(default_factory=lambda: BASE_DIR / "repos")
    logs_dir: Path = field(default_factory=lambda: BASE_DIR / "logs")

    max_workers: int = 2  # Conservative for Max subscription rate limits
    default_model: str = "sonnet"
    allowed_models: tuple[str, ...] = ("sonnet", "opus", "haiku")

    task_timeout_seconds: int = 3600  # 1 hour hard kill
    poll_interval_seconds: int = 5
    allowed_git_hosts: tuple[str, ...] = ("github.com",)


def load_config() -> Config:
    """Load config from environment variables."""
    base_dir = Path(os.environ.get("VOLTRON_BASE_DIR", str(BASE_DIR)))

    return Config(
        base_dir=base_dir,
        db_path=Path(os.environ.get(
            "VOLTRON_DB_PATH", str(base_dir / "data" / "voltron.db")
        )),
        repos_dir=Path(os.environ.get(
            "VOLTRON_REPOS_DIR", str(base_dir / "repos")
        )),
        logs_dir=Path(os.environ.get(
            "VOLTRON_LOG_DIR", str(base_dir / "logs")
        )),
        max_workers=int(os.environ.get("VOLTRON_MAX_CONCURRENCY", "2")),
        default_model=os.environ.get("VOLTRON_DEFAULT_MODEL", "sonnet"),
        task_timeout_seconds=int(
            os.environ.get("VOLTRON_TASK_TIMEOUT", "3600")
        ),
    )

"""Repository intelligence: stack detection and learning loop."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from .constants import TRUNCATE_SUMMARY
from .db import Database

log = logging.getLogger("backporcher.repo_intel")


def detect_stack(repo_path: Path) -> str:
    """Detect tech stack from project files. Returns a summary string."""
    parts = []

    # Language detection
    pyproject = repo_path / "pyproject.toml"
    package_json = repo_path / "package.json"
    cargo_toml = repo_path / "Cargo.toml"
    go_mod = repo_path / "go.mod"
    gemfile = repo_path / "Gemfile"

    if pyproject.exists():
        parts.append("Python")
        try:
            content = pyproject.read_text(errors="replace")
            if "django" in content.lower():
                parts.append("Django")
            elif "fastapi" in content.lower():
                parts.append("FastAPI")
            elif "flask" in content.lower():
                parts.append("Flask")
            if "alembic" in content.lower():
                parts.append("Alembic")
            if "pytest" in content.lower():
                parts.append("pytest")
        except OSError:
            pass
    elif (repo_path / "requirements.txt").exists() or (repo_path / "setup.py").exists():
        parts.append("Python")

    if package_json.exists():
        try:
            pkg = json.loads(package_json.read_text(errors="replace"))
            deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
            if "next" in deps:
                version = deps["next"].lstrip("^~>=<")
                major = version.split(".")[0] if version[0].isdigit() else ""
                parts.append(f"Next.js {major}" if major else "Next.js")
            elif "react" in deps:
                parts.append("React")
            elif "vue" in deps:
                parts.append("Vue")
            elif "svelte" in deps or "@sveltejs/kit" in deps:
                parts.append("Svelte")
            else:
                parts.append("Node.js")
            if "typescript" in deps:
                parts.append("TypeScript")
            if "@prisma/client" in deps or "prisma" in deps:
                parts.append("Prisma")
            if "jest" in deps or "@jest/core" in deps:
                parts.append("Jest")
            elif "vitest" in deps:
                parts.append("Vitest")
        except (json.JSONDecodeError, KeyError, TypeError, IndexError, OSError):
            parts.append("Node.js")

    if cargo_toml.exists():
        parts.append("Rust")
        try:
            content = cargo_toml.read_text(errors="replace")
            if "tauri" in content.lower():
                parts.append("Tauri")
        except OSError:
            pass

    if go_mod.exists():
        parts.append("Go")

    if gemfile.exists():
        parts.append("Ruby")
        try:
            content = gemfile.read_text(errors="replace")
            if "rails" in content.lower():
                parts.append("Rails")
        except OSError:
            pass

    # Infra
    if (repo_path / "Dockerfile").exists() or (repo_path / "docker-compose.yml").exists():
        parts.append("Docker")
    if (repo_path / ".github" / "workflows").is_dir():
        parts.append("GitHub Actions")

    return " + ".join(parts) if parts else "Unknown"


async def detect_and_store_stack(repo: dict, db: Database):
    """Detect stack if not already stored."""
    if repo.get("stack_info"):
        return
    repo_path = Path(repo["local_path"])
    if not repo_path.exists():
        return
    stack = detect_stack(repo_path)
    if stack and stack != "Unknown":
        await db.update_repo(repo["id"], stack_info=stack)
        log.info("Detected stack for %s: %s", repo["name"], stack)


async def record_learning(
    db: Database,
    repo_id: int,
    task_id: int | None,
    learning_type: str,
    context: str,
):
    """Extract a learning from context and store it."""
    # Take first meaningful line (skip empty/whitespace)
    content = ""
    for line in context.strip().splitlines():
        line = line.strip()
        if line:
            content = line[:TRUNCATE_SUMMARY]
            break
    if not content:
        content = context.strip()[:TRUNCATE_SUMMARY]
    if not content:
        return
    try:
        await db.add_learning(repo_id, learning_type, content, task_id=task_id)
    except Exception:
        log.warning("Failed to record learning for repo %d", repo_id, exc_info=True)


async def get_learnings_text(db: Database, repo_id: int) -> str | None:
    """Format recent learnings for prompt injection."""
    learnings = await db.get_learnings(repo_id, limit=10)
    if not learnings:
        return None
    lines = []
    for entry in learnings:
        icon = {
            "success": "+",
            "agent_failure": "!",
            "verify_failure": "!",
            "ci_failure": "!",
            "coordinator_rejection": "!",
        }.get(entry["learning_type"], "-")
        lines.append(f"  [{icon}] {entry['content']}")
    return "## Learnings from Previous Tasks\n" + "\n".join(lines) + "\n\n"

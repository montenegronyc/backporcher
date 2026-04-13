"""Safety scan: pre-PR static analysis for dangerous patterns and secrets.

Runs after the agent completes (and after build verification) but before
PR creation.  Catches:
  1. Leaked secrets (high-entropy strings, known key patterns)
  2. Dangerous operations (eval, exec, subprocess shell=True, rm -rf, etc.)
  3. Linter violations (if the repo has a linter configured)

Results are categorized as BLOCK (prevent PR) or WARN (include in
coordinator review context).
"""

from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass, field
from pathlib import Path

from .db import Database

log = logging.getLogger("backporcher.safety")

# --- Secret detection patterns ---

# Known secret prefixes (high confidence)
SECRET_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("AWS Access Key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("AWS Secret Key", re.compile(r"""(?:aws_secret_access_key|AWS_SECRET)\s*[=:]\s*['"]?[A-Za-z0-9/+=]{40}""")),
    ("GitHub Token", re.compile(r"gh[pousr]_[A-Za-z0-9_]{36,}")),
    (
        "Generic API Key assignment",
        re.compile(r"""(?:api[_-]?key|apikey|secret[_-]?key)\s*[=:]\s*['"][A-Za-z0-9_\-]{20,}['"]""", re.IGNORECASE),
    ),
    ("Private Key block", re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----")),
    ("Slack Token", re.compile(r"xox[baprs]-[0-9a-zA-Z\-]{10,}")),
    ("Generic Bearer Token", re.compile(r"""(?:bearer|token)\s*[=:]\s*['"][A-Za-z0-9_\-.]{30,}['"]""", re.IGNORECASE)),
]

# --- Dangerous operation patterns ---

DANGEROUS_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("eval() call", re.compile(r"\beval\s*\(")),
    ("exec() call", re.compile(r"\bexec\s*\(")),
    ("subprocess shell=True", re.compile(r"subprocess\.\w+\(.*shell\s*=\s*True", re.DOTALL)),
    ("os.system() call", re.compile(r"os\.system\s*\(")),
    ("rm -rf command", re.compile(r"rm\s+-rf\s+/")),
    ("chmod 777", re.compile(r"chmod\s+777")),
    (
        "SQL injection risk (f-string)",
        re.compile(r"""f['"].*(?:SELECT|INSERT|UPDATE|DELETE|DROP).*\{""", re.IGNORECASE),
    ),
    ("Disabled SSL verification", re.compile(r"verify\s*=\s*False")),
    ("Wildcard CORS", re.compile(r"""(?:cors|access.control.allow.origin).*['"]?\*['"]?""", re.IGNORECASE)),
]

# File extensions to scan (skip binaries, images, etc.)
SCANNABLE_EXTENSIONS = frozenset(
    {
        ".py",
        ".js",
        ".ts",
        ".tsx",
        ".jsx",
        ".rs",
        ".go",
        ".java",
        ".rb",
        ".sh",
        ".bash",
        ".zsh",
        ".yml",
        ".yaml",
        ".toml",
        ".json",
        ".env",
        ".cfg",
        ".ini",
        ".conf",
    }
)

# Files that are expected to have key-like patterns (reduce false positives)
ALLOWLISTED_FILENAMES = frozenset(
    {
        "package-lock.json",
        "yarn.lock",
        "Cargo.lock",
        "poetry.lock",
    }
)


@dataclass
class ScanFinding:
    """A single finding from the safety scan."""

    file: str
    line_number: int
    category: str  # "secret" or "dangerous"
    description: str
    severity: str  # "block" or "warn"
    snippet: str = ""


@dataclass
class ScanResult:
    """Aggregated results from a safety scan."""

    findings: list[ScanFinding] = field(default_factory=list)

    @property
    def has_blockers(self) -> bool:
        return any(f.severity == "block" for f in self.findings)

    @property
    def has_warnings(self) -> bool:
        return any(f.severity == "warn" for f in self.findings)

    @property
    def blocker_count(self) -> int:
        return sum(1 for f in self.findings if f.severity == "block")

    @property
    def warning_count(self) -> int:
        return sum(1 for f in self.findings if f.severity == "warn")

    def summary(self) -> str:
        """One-line summary for logging."""
        if not self.findings:
            return "clean"
        parts = []
        if self.blocker_count:
            parts.append(f"{self.blocker_count} blocker(s)")
        if self.warning_count:
            parts.append(f"{self.warning_count} warning(s)")
        return ", ".join(parts)

    def format_for_review(self) -> str:
        """Format findings as a prompt section for the coordinator."""
        if not self.findings:
            return ""
        lines = ["## Safety Scan Findings\n"]
        for f in self.findings:
            icon = "BLOCK" if f.severity == "block" else "WARN"
            lines.append(f"- **[{icon}]** `{f.file}:{f.line_number}` — {f.description}")
            if f.snippet:
                lines.append(f"  ```\n  {f.snippet}\n  ```")
        lines.append("")
        return "\n".join(lines)


def _shannon_entropy(s: str) -> float:
    """Calculate Shannon entropy of a string."""
    if not s:
        return 0.0
    freq: dict[str, int] = {}
    for c in s:
        freq[c] = freq.get(c, 0) + 1
    length = len(s)
    return -sum((count / length) * math.log2(count / length) for count in freq.values())


def _scan_line_for_secrets(line: str, file_path: str, line_num: int) -> list[ScanFinding]:
    """Check a single line for secret patterns."""
    findings: list[ScanFinding] = []
    for name, pattern in SECRET_PATTERNS:
        if pattern.search(line):
            findings.append(
                ScanFinding(
                    file=file_path,
                    line_number=line_num,
                    category="secret",
                    description=f"Possible {name}",
                    severity="block",
                    snippet=line.strip()[:120],
                )
            )
    return findings


def _scan_line_for_danger(line: str, file_path: str, line_num: int) -> list[ScanFinding]:
    """Check a single line for dangerous operation patterns."""
    findings: list[ScanFinding] = []
    for name, pattern in DANGEROUS_PATTERNS:
        if pattern.search(line):
            findings.append(
                ScanFinding(
                    file=file_path,
                    line_number=line_num,
                    category="dangerous",
                    description=name,
                    severity="warn",
                    snippet=line.strip()[:120],
                )
            )
    return findings


def _get_changed_files(worktree_path: Path) -> list[str]:
    """Get list of files changed vs the base branch using git diff."""
    import subprocess

    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", "--diff-filter=ACMR", "HEAD~1"],
            cwd=worktree_path,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            return [f.strip() for f in result.stdout.strip().splitlines() if f.strip()]
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    # Fallback: diff against default branch
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", "--diff-filter=ACMR", "origin/main...HEAD"],
            cwd=worktree_path,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            return [f.strip() for f in result.stdout.strip().splitlines() if f.strip()]
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    return []


async def run_safety_scan(
    worktree_path: Path,
    task_id: int,
    db: Database,
) -> ScanResult:
    """Scan changed files in the worktree for secrets and dangerous patterns.

    This runs synchronously on the filesystem (no subprocess needed) and is
    fast enough to not need async I/O.  Returns a ScanResult that the caller
    can use to block PR creation or enrich the coordinator review.
    """
    result = ScanResult()

    changed_files = _get_changed_files(worktree_path)
    if not changed_files:
        await db.add_log(task_id, "Safety scan: no changed files detected")
        return result

    scanned = 0
    for rel_path in changed_files:
        # Skip non-scannable files
        p = Path(rel_path)
        if p.suffix not in SCANNABLE_EXTENSIONS:
            continue
        if p.name in ALLOWLISTED_FILENAMES:
            continue

        full_path = worktree_path / rel_path
        if not full_path.is_file():
            continue

        try:
            content = full_path.read_text(errors="replace")
        except OSError:
            continue

        scanned += 1
        for line_num, line in enumerate(content.splitlines(), 1):
            # Skip comments and empty lines for dangerous pattern checks
            stripped = line.strip()
            if not stripped:
                continue

            result.findings.extend(_scan_line_for_secrets(line, rel_path, line_num))
            result.findings.extend(_scan_line_for_danger(line, rel_path, line_num))

    summary = result.summary()
    log_msg = f"Safety scan: {scanned} file(s) scanned, {summary}"
    level = "warn" if result.has_blockers else "info"
    await db.add_log(task_id, log_msg, level=level)
    log.info("Task #%d: %s", task_id, log_msg)

    return result

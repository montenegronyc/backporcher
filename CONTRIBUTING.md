# Contributing to Voltron

Thank you for your interest in contributing to Voltron! This document provides guidelines for contributing to the project.

## Table of Contents

- [Code of Conduct](#code-of-conduct)
- [Getting Started](#getting-started)
- [Development Setup](#development-setup)
- [Making Changes](#making-changes)
- [Commit Guidelines](#commit-guidelines)
- [Pull Request Process](#pull-request-process)
- [Testing](#testing)
- [Code Style](#code-style)
- [Reporting Issues](#reporting-issues)

## Code of Conduct

- Be respectful and inclusive
- Focus on constructive feedback
- Help others learn and grow
- Maintain a professional environment

## Getting Started

1. **Fork the repository** on GitHub
2. **Clone your fork** locally:
   ```bash
   git clone https://github.com/YOUR-USERNAME/voltron.git
   cd voltron
   ```
3. **Add upstream remote**:
   ```bash
   git remote add upstream https://github.com/ORIGINAL-OWNER/voltron.git
   ```

## Development Setup

### Prerequisites

- Python 3.11 or higher
- Git
- Claude Code CLI (for testing agent dispatching)

### Installation

1. Create a virtual environment:
   ```bash
   python -m venv venv
   source venv/bin/activate  # On Windows: venv\Scripts\activate
   ```

2. Install in development mode:
   ```bash
   pip install -e .
   ```

3. Verify installation:
   ```bash
   voltron --help
   ```

### Configuration

Voltron uses `~/.config/voltron/config.json` for configuration. Default values are provided, but you can customize:

- `repos_dir`: Where repositories are cloned
- `worktrees_dir`: Where git worktrees are created
- `db_path`: SQLite database location
- `max_concurrent_agents`: Parallel agent limit

## Making Changes

### Branching Strategy

- Create feature branches from `main`:
  ```bash
  git checkout -b feature/your-feature-name
  ```
- Use descriptive branch names:
  - `feature/` for new features
  - `fix/` for bug fixes
  - `docs/` for documentation
  - `refactor/` for code refactoring

### Development Workflow

1. **Keep your fork updated**:
   ```bash
   git fetch upstream
   git rebase upstream/main
   ```

2. **Make your changes** in small, logical commits

3. **Test your changes** thoroughly

4. **Update documentation** if needed

## Commit Guidelines

### Commit Message Format

Write clear, concise commit messages:

```
Short summary (50 chars or less)

More detailed explanation if needed. Wrap at 72 characters.
Explain what changed and why, not how.

- Bullet points are okay
- Reference issues: Fixes #123
```

### Good Commit Examples

```
Add retry mechanism for failed agent dispatches

Agents now automatically retry up to 3 times on transient
failures before marking the task as failed.

Fixes #45
```

```
Fix worker daemon not respecting max_concurrent_agents

The worker was spawning unlimited agents due to incorrect
semaphore initialization. Now properly limits concurrency
to the configured value.
```

### Commit Best Practices

- Make atomic commits (one logical change per commit)
- Write in imperative mood: "Add feature" not "Added feature"
- Separate subject from body with a blank line
- Keep the subject line under 50 characters
- Wrap the body at 72 characters
- Reference issues and pull requests where relevant

## Pull Request Process

### Before Submitting

1. **Rebase on latest main**:
   ```bash
   git fetch upstream
   git rebase upstream/main
   ```

2. **Run tests** (if available):
   ```bash
   python -m pytest
   ```

3. **Check code style**:
   ```bash
   # Format with black
   black src/

   # Check with flake8
   flake8 src/
   ```

### Submitting the PR

1. **Push to your fork**:
   ```bash
   git push origin feature/your-feature-name
   ```

2. **Create Pull Request** on GitHub with:
   - Clear title describing the change
   - Description of what changed and why
   - Reference to related issues
   - Screenshots/examples if applicable

### PR Template

```markdown
## Summary
Brief description of changes

## Changes
- Change 1
- Change 2
- Change 3

## Testing
How you tested these changes

## Related Issues
Fixes #123, Related to #456
```

### Review Process

- Maintainers will review your PR
- Address feedback promptly
- Keep the PR focused and reasonably sized
- Be open to suggestions and discussion

## Testing

### Manual Testing

Test your changes with real workflows:

```bash
# Add a test repository
voltron repo add https://github.com/user/repo

# Dispatch a test task
voltron dispatch owner/repo "test task description"

# Check status
voltron status

# Test worker daemon
voltron worker start
voltron worker status
voltron worker stop
```

### Future: Automated Tests

We welcome contributions to add automated testing:
- Unit tests for individual components
- Integration tests for the full workflow
- Test fixtures and utilities

## Code Style

### Python Style Guidelines

- Follow PEP 8
- Use type hints where appropriate
- Write docstrings for functions and classes
- Keep functions focused and reasonably sized

### Formatting

- Use **black** for code formatting (line length: 88)
- Use **isort** for import sorting
- Use **flake8** for linting

### Example

```python
"""Module docstring explaining purpose."""

from pathlib import Path
from typing import Optional

def process_task(task_id: int, repo_name: str) -> Optional[dict]:
    """Process a dispatched task.

    Args:
        task_id: Unique task identifier
        repo_name: Name of the repository

    Returns:
        Task result dictionary or None if failed
    """
    # Implementation
    pass
```

## Reporting Issues

### Bug Reports

Include:
- Clear description of the bug
- Steps to reproduce
- Expected vs actual behavior
- Environment details (OS, Python version)
- Relevant logs or error messages

### Feature Requests

Include:
- Use case and motivation
- Proposed solution or API
- Alternatives considered
- Willingness to implement

### Issue Template

```markdown
## Description
Clear description of issue/feature

## Steps to Reproduce (for bugs)
1. Step 1
2. Step 2
3. Expected: X, Got: Y

## Environment
- OS: Ubuntu 24.04
- Python: 3.11.5
- Voltron version: 0.1.0

## Additional Context
Any other relevant information
```

## Questions?

- Open an issue for questions
- Check existing issues and documentation
- Be specific and provide context

## License

By contributing, you agree that your contributions will be licensed under the same license as the project.

---

Thank you for contributing to Voltron!

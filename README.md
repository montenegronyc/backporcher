# Voltron

Voltron is a parallel Claude Code agent dispatcher that orchestrates multiple AI-powered coding tasks across GitHub repositories using isolated git worktrees. It provides a CLI for dispatching tasks and managing repositories, along with a background worker daemon that spawns Claude Code sessions in separate worktrees to safely execute tasks in parallel without interference, tracking each job's state through a SQLite database for reliable task management and monitoring.

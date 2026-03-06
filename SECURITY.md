# Security Policy

## Supported Versions

| Version | Supported          |
| ------- | ------------------ |
| 0.1.x   | :white_check_mark: |

## Reporting a Vulnerability

We take the security of Voltron seriously. If you believe you have found a security vulnerability, please report it to us responsibly.

### How to Report

**Please DO NOT report security vulnerabilities through public GitHub issues.**

Instead, please report security vulnerabilities by:

1. **Email**: Send details to the project maintainer's email address (check git commit history or GitHub profile)
2. **GitHub Security Advisories**: Use the "Security" tab on the GitHub repository to privately report a vulnerability

### What to Include

Please include the following information in your report:

- **Description** of the vulnerability
- **Steps to reproduce** the issue
- **Potential impact** of the vulnerability
- **Suggested fix** (if you have one)
- **Your contact information** for follow-up questions

### What to Expect

- **Acknowledgment**: We will acknowledge receipt of your report within 48 hours
- **Updates**: We will provide regular updates on our progress (at least every 7 days)
- **Timeline**: We aim to provide an initial assessment within 5 business days
- **Resolution**: We will work to address confirmed vulnerabilities promptly, prioritizing based on severity
- **Credit**: If you wish, we will credit you in the security advisory and release notes

### Disclosure Policy

- **Coordinated Disclosure**: Please allow us reasonable time to address the vulnerability before public disclosure
- **Public Disclosure**: We will coordinate with you on timing for public disclosure once a fix is available
- **Security Advisories**: Confirmed vulnerabilities will be published as GitHub Security Advisories

## Security Considerations

### Architecture

Voltron is a parallel agent dispatcher that:
- Manages SQLite database with task state and git worktree mappings
- Spawns Claude Code agent subprocesses via `claude-code` CLI
- Executes git operations in worktree directories
- Runs as a systemd service with worker processes

### Key Security Areas

When reviewing or contributing code, please pay special attention to:

1. **Command Injection**: All git commands and subprocess invocations must properly escape arguments
2. **Path Traversal**: Worktree paths and file operations must be validated to prevent directory traversal
3. **SQLite Injection**: Use parameterized queries for all database operations
4. **Concurrency**: Database locking and git operation serialization to prevent race conditions
5. **Process Isolation**: Agent subprocesses should have appropriate resource limits and isolation
6. **Credential Handling**: Ensure git credentials and API keys are never logged or leaked

### Known Limitations

- Voltron assumes git repositories are trusted (does not sandbox git operations)
- Worker processes inherit the user's git configuration and credentials
- SQLite database access is not authenticated (file-system permissions only)

## Security Updates

Security updates will be released as:
- Patch versions (e.g., 0.1.1) for minor security fixes
- Minor versions (e.g., 0.2.0) for security fixes with breaking changes
- Security advisories published on GitHub

## Best Practices for Users

When deploying Voltron:

1. **File Permissions**: Ensure the SQLite database has appropriate permissions (0600 recommended)
2. **User Isolation**: Run the voltron service as a dedicated user with minimal privileges
3. **Repository Trust**: Only use voltron with repositories you trust
4. **Audit Logs**: Monitor the systemd journal for voltron service activity
5. **Network Isolation**: If possible, run on a system with restricted network access
6. **API Keys**: Store Claude API keys securely (environment variables, systemd credentials, etc.)

## Contact

For non-security issues, please use GitHub Issues.
For security concerns, please follow the reporting process above.

"""Tests for github.py — URL parsing, data structures, and helpers."""

import pytest

from src.github import (
    CIStatus,
    GitHubIssue,
    extract_pr_number_from_url,
    repo_full_name_from_url,
)
from src.github_pr import _is_ignored_check


class TestRepoFullNameFromUrl:
    def test_https_url(self):
        assert repo_full_name_from_url("https://github.com/owner/repo") == "owner/repo"

    def test_https_url_with_git(self):
        assert repo_full_name_from_url("https://github.com/owner/repo.git") == "owner/repo"

    def test_trailing_slash(self):
        assert repo_full_name_from_url("https://github.com/owner/repo/") == "owner/repo"

    def test_with_whitespace(self):
        assert repo_full_name_from_url("  https://github.com/owner/repo  ") == "owner/repo"

    def test_invalid_url_raises(self):
        with pytest.raises(ValueError):
            repo_full_name_from_url("not-a-url")

    def test_no_repo_raises(self):
        with pytest.raises(ValueError):
            repo_full_name_from_url("https://example.com/nope")


class TestExtractPrNumber:
    def test_standard_pr_url(self):
        assert extract_pr_number_from_url("https://github.com/owner/repo/pull/42") == 42

    def test_pr_url_with_trailing(self):
        assert extract_pr_number_from_url("https://github.com/owner/repo/pull/7/files") == 7

    def test_no_pr_in_url(self):
        assert extract_pr_number_from_url("https://github.com/owner/repo") is None

    def test_issue_url_not_matched(self):
        assert extract_pr_number_from_url("https://github.com/owner/repo/issues/5") is None


class TestCIStatus:
    def test_success(self):
        ci = CIStatus(state="success", failed_checks=[], total=3, completed=3)
        assert ci.state == "success"
        assert len(ci.failed_checks) == 0

    def test_failure(self):
        ci = CIStatus(state="failure", failed_checks=["lint", "test"], total=3, completed=3)
        assert ci.state == "failure"
        assert "lint" in ci.failed_checks

    def test_no_checks(self):
        ci = CIStatus(state="no_checks", failed_checks=[], total=0, completed=0)
        assert ci.state == "no_checks"


class TestIgnoredChecks:
    def test_auto_merge_ignored(self):
        assert _is_ignored_check("Auto-merge") is True
        assert _is_ignored_check("auto-merge") is True

    def test_coderabbit_ignored(self):
        assert _is_ignored_check("CodeRabbit") is True
        assert _is_ignored_check("coderabbit/summary") is True

    def test_codecov_ignored(self):
        assert _is_ignored_check("codecov/patch") is True

    def test_sonarcloud_ignored(self):
        assert _is_ignored_check("SonarCloud Code Analysis") is True

    def test_dependabot_ignored(self):
        assert _is_ignored_check("dependabot") is True

    def test_real_ci_not_ignored(self):
        assert _is_ignored_check("Rust (macOS ARM64 Metal)") is False
        assert _is_ignored_check("Rust (Windows x64 CUDA)") is False
        assert _is_ignored_check("ci") is False
        assert _is_ignored_check("build") is False
        assert _is_ignored_check("test") is False


class TestGitHubIssue:
    def test_construction(self):
        issue = GitHubIssue(
            number=1,
            title="Fix bug",
            body="Details here",
            url="https://github.com/o/r/issues/1",
            author="testuser",
            labels=["backporcher"],
        )
        assert issue.number == 1
        assert issue.author == "testuser"
        assert "backporcher" in issue.labels

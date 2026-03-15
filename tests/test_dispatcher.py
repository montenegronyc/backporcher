"""Tests for dispatcher.py — URL validation, branch names, PR body."""

import pytest
from unittest.mock import patch

from src.config import Config
from src.dispatcher import (
    validate_github_url, repo_name_from_url, make_branch_name,
)


class TestValidateGithubUrl:
    @pytest.fixture
    def config(self):
        return Config()

    def test_valid_url(self, config):
        url = validate_github_url("https://github.com/owner/repo", config)
        assert url == "https://github.com/owner/repo"

    def test_valid_url_with_git(self, config):
        url = validate_github_url("https://github.com/owner/repo.git", config)
        assert url == "https://github.com/owner/repo.git"

    def test_strips_trailing_slash(self, config):
        url = validate_github_url("https://github.com/owner/repo/", config)
        assert url == "https://github.com/owner/repo"

    def test_rejects_http(self, config):
        with pytest.raises(ValueError, match="HTTPS"):
            validate_github_url("http://github.com/owner/repo", config)

    def test_rejects_non_github(self, config):
        with pytest.raises(ValueError, match="not in allowed"):
            validate_github_url("https://gitlab.com/owner/repo", config)


class TestRepoNameFromUrl:
    def test_simple(self):
        assert repo_name_from_url("https://github.com/owner/repo") == "repo"

    def test_with_git(self):
        assert repo_name_from_url("https://github.com/owner/repo.git") == "repo"

    def test_invalid_raises(self):
        with pytest.raises(ValueError):
            repo_name_from_url("https://github.com/repo")


class TestMakeBranchName:
    def test_basic(self):
        branch = make_branch_name(1, "Add a health check")
        assert branch.startswith("backporcher/1-")
        assert "health" in branch

    def test_sanitizes_special_chars(self):
        branch = make_branch_name(2, "Fix @#$ weird! stuff?")
        assert "@" not in branch
        assert "!" not in branch

    def test_truncates_long_prompt(self):
        branch = make_branch_name(3, "a" * 200)
        assert len(branch) <= 110  # backporcher/3- prefix + 40 chars max

    def test_fallback_for_invalid(self):
        branch = make_branch_name(4, "!@#$%^")
        assert branch == "backporcher/4"

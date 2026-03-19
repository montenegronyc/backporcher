"""Tests for config.py — env var loading, defaults."""

import os
from unittest.mock import patch

from src.config import load_config


class TestConfigDefaults:
    def test_default_values(self):
        with patch.dict(os.environ, {}, clear=True):
            config = load_config()
        assert config.poll_interval_seconds == 30
        assert config.max_ci_retries == 3
        assert config.ci_check_interval_seconds == 60
        assert config.github_owner == ""
        assert config.allowed_github_users == ()
        assert config.max_workers == 2
        assert config.default_model == "sonnet"
        assert config.agent_user is None


class TestConfigEnvVars:
    def test_poll_interval(self):
        with patch.dict(os.environ, {"BACKPORCHER_POLL_INTERVAL": "10"}):
            config = load_config()
        assert config.poll_interval_seconds == 10

    def test_ci_check_interval(self):
        with patch.dict(os.environ, {"BACKPORCHER_CI_CHECK_INTERVAL": "120"}):
            config = load_config()
        assert config.ci_check_interval_seconds == 120

    def test_max_ci_retries(self):
        with patch.dict(os.environ, {"BACKPORCHER_MAX_CI_RETRIES": "5"}):
            config = load_config()
        assert config.max_ci_retries == 5

    def test_github_owner(self):
        with patch.dict(os.environ, {"BACKPORCHER_GITHUB_OWNER": "otherowner"}):
            config = load_config()
        assert config.github_owner == "otherowner"

    def test_allowed_users_single(self):
        with patch.dict(os.environ, {"BACKPORCHER_ALLOWED_USERS": "alice"}):
            config = load_config()
        assert config.allowed_github_users == ("alice",)

    def test_allowed_users_multiple(self):
        with patch.dict(os.environ, {"BACKPORCHER_ALLOWED_USERS": "alice,bob,charlie"}):
            config = load_config()
        assert config.allowed_github_users == ("alice", "bob", "charlie")

    def test_allowed_users_with_spaces(self):
        with patch.dict(os.environ, {"BACKPORCHER_ALLOWED_USERS": " alice , bob "}):
            config = load_config()
        assert config.allowed_github_users == ("alice", "bob")

    def test_agent_user(self):
        with patch.dict(os.environ, {"BACKPORCHER_AGENT_USER": "backporcher-agent"}):
            config = load_config()
        assert config.agent_user == "backporcher-agent"

    def test_agent_user_empty_is_none(self):
        with patch.dict(os.environ, {"BACKPORCHER_AGENT_USER": ""}):
            config = load_config()
        assert config.agent_user is None

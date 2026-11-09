"""Credential selection: exactly one of the API key and the Claude Code OAuth token."""

import pytest

from dep_reviewer.config import Config, ConfigError

BASE = {"INPUT_GITHUB_TOKEN": "ghs", "GITHUB_REPOSITORY": "o/r"}


def test_api_key_uses_api_engine():
    cfg = Config.from_env({**BASE, "INPUT_ANTHROPIC_API_KEY": "sk-ant-api"})
    assert not cfg.uses_claude_code


def test_oauth_token_uses_claude_code_engine():
    cfg = Config.from_env({**BASE, "INPUT_CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat",
                           "DEP_REVIEWER_CLAUDE_BIN": "/opt/claude"})
    assert cfg.uses_claude_code and cfg.claude_bin == "/opt/claude"


def test_both_credentials_rejected():
    with pytest.raises(ConfigError, match="not both"):
        Config.from_env({**BASE, "INPUT_ANTHROPIC_API_KEY": "k", "INPUT_CLAUDE_CODE_OAUTH_TOKEN": "t"})


def test_no_credential_rejected():
    with pytest.raises(ConfigError, match="both empty"):
        Config.from_env(BASE)


def test_empty_bin_falls_back_to_path_lookup():
    cfg = Config.from_env({**BASE, "INPUT_CLAUDE_CODE_OAUTH_TOKEN": "t", "DEP_REVIEWER_CLAUDE_BIN": ""})
    assert cfg.claude_bin == "claude"


def test_web_fetch_domains_default_and_override():
    from dep_reviewer.config import DEFAULT_WEB_FETCH_DOMAINS
    cfg = Config.from_env({**BASE, "INPUT_CLAUDE_CODE_OAUTH_TOKEN": "t"})
    assert cfg.web_fetch_domains == DEFAULT_WEB_FETCH_DOMAINS
    cfg = Config.from_env({**BASE, "INPUT_CLAUDE_CODE_OAUTH_TOKEN": "t",
                           "INPUT_WEB_FETCH_DOMAINS": "GitHub.com,\n docs.example.org"})
    assert cfg.web_fetch_domains == ("github.com", "docs.example.org")


@pytest.mark.parametrize("bad", ["*", "https://github.com", "github.com/path", "localhost", "a b.com"])
def test_web_fetch_domains_rejects_non_hosts(bad):
    with pytest.raises(ConfigError, match="web-fetch-domains"):
        Config.from_env({**BASE, "INPUT_CLAUDE_CODE_OAUTH_TOKEN": "t", "INPUT_WEB_FETCH_DOMAINS": bad})

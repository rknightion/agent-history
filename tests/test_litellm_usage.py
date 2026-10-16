"""LiteLLM usage source: configuration and the spend-log mapping, without a database."""

import pytest

from agent_history import litellm_usage
from agent_history.config import ConfigError, LiteLLM, parse_config


def test_without_a_dsn_nothing_runs(monkeypatch):
    monkeypatch.delenv("AGENT_HISTORY_LITELLM_DSN", raising=False)
    assert litellm_usage.filler(LiteLLM(namespaces=("claude-proxied",))) is None
    assert litellm_usage.filler(parse_config({}).litellm) is None
    assert litellm_usage.filler(parse_config({"litellm": {"namespaces": ["claude-proxied"]}}).litellm) is None


def test_the_dsn_comes_from_the_environment_first(monkeypatch, tmp_path):
    file = tmp_path / "reader.dsn"
    file.write_text("postgresql://example.invalid/from-file\n")
    section = {"dsn_file": str(file), "namespaces": ["claude-proxied"]}
    monkeypatch.delenv("AGENT_HISTORY_LITELLM_DSN", raising=False)
    assert parse_config({"litellm": section}).litellm.dsn.endswith("/from-file")
    monkeypatch.setenv("AGENT_HISTORY_LITELLM_DSN", "postgresql://example.invalid/from-env")
    configured = parse_config({"litellm": section}).litellm
    assert configured.enabled and configured.dsn.endswith("/from-env")
    assert litellm_usage.filler(configured) is not None


@pytest.mark.parametrize(
    "section",
    [{"namespaces": ["codex-proxied"]}, {"namespaces": "claude-proxied"}, {"lookback_days": 0}, {"unknown": 1}],
)
def test_bad_sections_are_refused(section):
    with pytest.raises(ConfigError):
        parse_config({"litellm": section})


def test_spend_usage_mapping():
    usage = {"prompt_tokens_details": {"cached_tokens": 1000}, "completion_tokens_details": {"reasoning_tokens": 3}}
    assert litellm_usage.usage_from_spend(1300, 77, usage) == (300, 1000, 0, 77, 3)
    anthropic = {"cache_read_input_tokens": 100, "cache_creation_input_tokens": 50}
    assert litellm_usage.usage_from_spend(200, 1, anthropic) == (50, 100, 50, 1, None)
    assert litellm_usage.usage_from_spend(10, 1, None) == (10, 0, 0, 1, None)
    # Missing or inconsistent counts supply nothing rather than a guess.
    assert litellm_usage.usage_from_spend(None, 1, usage) is None
    assert litellm_usage.usage_from_spend(10, True, usage) is None
    assert litellm_usage.usage_from_spend(10, 1, usage) is None

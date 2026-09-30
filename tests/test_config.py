"""Configuration parsing (config.py) and the source-map inventory (load.inventory_map)."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from agent_history.config import ConfigError, load_config, parse_config

FIXTURES = Path(__file__).parent / "fixtures"


def test_defaults_are_the_three_local_homes(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    config = parse_config({})
    assert sorted(config.sources) == ["claude-local", "codex-local", "pi-local"]
    assert config.sources["pi-local"] == (tmp_path / ".pi" / "agent").resolve()
    assert config.namespaces() == ["claude-local", "codex-local", "pi-local"]
    assert not config.embedding.enabled and config.dsn is None


def test_missing_file_means_defaults(tmp_path):
    assert load_config(tmp_path / "absent.toml").default_context == "default"


def test_contexts_identities_and_secrets_from_files(tmp_path, monkeypatch):
    (tmp_path / "dsn").write_text("postgresql://ah_writer@localhost/agent_history\n")
    (tmp_path / "key").write_text("not-a-real-key\n")
    config_file = tmp_path / "config.toml"
    config_file.write_text(f"""
dsn_file = "{tmp_path / "dsn"}"
default_context = "home"

[sources]
claude-home = "{tmp_path}"
pi-lab = "{tmp_path}"

[contexts]
home = ["claude-home"]
lab = ["pi-lab"]

[identities]
owner_emails = ["Owner@Example.com"]
git_owners = ["github.com/example-org/"]

[embedding]
enabled = true
api_key_env = "NO_SUCH_VARIABLE"
api_key_file = "{tmp_path / "key"}"
""")
    monkeypatch.delenv("NO_SUCH_VARIABLE", raising=False)
    config = load_config(config_file)
    assert config.dsn == "postgresql://ah_writer@localhost/agent_history"
    assert config.namespaces() == ["claude-home"] and config.namespaces("lab") == ["pi-lab"]
    assert config.identities.is_owner(" owner@example.com") and not config.identities.is_owner("someone@example.com")
    assert config.identities.git_owners == {"github.com/example-org"}
    assert config.embedding.enabled and config.embedding.token() == "not-a-real-key"


@pytest.mark.parametrize(
    "data, message",
    [
        ({"sources": {"claude": "/tmp"}}, "must look like"),
        ({"sources": {"gemini-x": "/tmp"}}, "must look like"),
        ({"contexts": {"a": ["nope"]}}, "list of namespaces"),
        ({"default_context": "missing"}, "is not a context"),
        ({"embedding": {"dimensions": 1536}}, "must be 1024"),
        ({"surprise": 1}, "unknown top-level keys"),
    ],
)
def test_invalid_config_is_refused(data, message):
    with pytest.raises(ConfigError, match=message):
        parse_config(data)


@pytest.mark.parametrize("boundary", ["config", "cli"])
def test_invalid_namespace_diagnostics_do_not_echo_configured_value(tmp_path, capsys, boundary):
    from agent_history.cli import main

    marker = "private-looking-namespace-marker"
    namespace = f"invalid/{marker}"
    config_file = tmp_path / "config.toml"
    config_file.write_text(f'[sources]\n"{namespace}" = "/tmp"\n')

    if boundary == "config":
        with pytest.raises(ConfigError) as error:
            load_config(config_file)
        diagnostic = str(error.value)
    else:
        assert main(["--config", str(config_file), "stats"]) == 2
        captured = capsys.readouterr()
        assert captured.out == ""
        diagnostic = captured.out + captured.err
    assert marker not in diagnostic
    assert "sources" in diagnostic and "namespace" in diagnostic
    assert "must look like claude-<name>, codex-<name> or pi-<name>" in diagnostic


def test_unknown_context_is_refused():
    with pytest.raises(ConfigError, match="unknown context"):
        parse_config({}).namespaces("elsewhere")


def test_inventory_map_reads_only_parser_subtrees_and_skips_symlinks(tmp_path):
    pytest.importorskip("psycopg")
    from agent_history import load

    claude = tmp_path / "claude-home"
    shutil.copytree(FIXTURES / "claude" / "projects", claude / "projects")
    (claude / "todos").mkdir()
    (claude / "todos" / "stray.jsonl").write_text("{}\n")  # outside projects/: never a transcript
    link = claude / "projects" / "-home-tester-repo" / "linked.jsonl"
    link.symlink_to(claude / "todos" / "stray.jsonl")
    codex = tmp_path / "codex-home"
    (codex / "sessions" / "2026" / "09" / "01").mkdir(parents=True)
    shutil.copy(FIXTURES / "codex" / "main_v155.jsonl", codex / "sessions" / "2026" / "09" / "01" / "rollout-a.jsonl")
    (codex / "history.jsonl").write_text("{}\n")  # Codex prompt history: not a session
    entries = load.inventory_map({"claude-home": claude, "codex-home": codex, "pi-none": tmp_path / "absent"})
    assert all(not e.rel_path.endswith(("stray.jsonl", "linked.jsonl", "history.jsonl")) for e in entries.values())
    assert {e.namespace for e in entries.values()} == {"claude-home", "codex-home"}
    assert "codex-home/sessions/2026/09/01/rollout-a.jsonl" in entries
    assert {e.role for e in entries.values() if e.agent == "claude"} >= {"main", "subagent"}
    assert all(e.hot is not None and e.profile == "home" for e in entries.values())

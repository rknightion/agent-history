"""Synthetic checkout warning across the real efficiency and hourly summary boundaries."""

import json
import subprocess

import pytest

from test_checkout_observer import _explicit

from agent_history.config import Config
from agent_history.metrics import checkout as checkout_observer
from agent_history import collect_git


def test_unconfigured_root_warning(tmp_path, monkeypatch, capsys):
    source = tmp_path / "source"
    sessions = source / "sessions" / "example"
    sessions.mkdir(parents=True)
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    subprocess.run(["git", "init", str(checkout)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(checkout), "remote", "add", "origin", "https://github.com/example/widget.git"],
        check=True,
    )
    transcript = sessions / "root.jsonl"
    transcript.write_text(
        json.dumps({"type": "session", "id": "example-root", "cwd": str(checkout)})
        + "\n"
        + json.dumps(
            {
                "type": "message",
                "message": {
                    "role": "user",
                    "content": [{"type": "text", "text": "You are the root. Write codex/closeout-widget-loop1.md"}],
                },
            }
        )
        + "\n"
    )
    config = Config()
    config.sources = {"pi-example": source}
    _explicit(config, tmp_path)
    config.identities.git_owners = ["github.com/example"]
    config.exporter.state_dir = tmp_path / "state"
    config.collector.lock_file = tmp_path / "collect.lock"
    checkout_observer.collect(config)
    snapshot = json.loads((config.exporter.state_dir / "loop-checkout-summary.json").read_text())
    warnings = {f.name: f for f in checkout_observer.families(snapshot)}
    assert warnings["agent_history_loop_checkout_unconfigured"].samples[0].value == 1
    assert warnings["agent_history_loop_checkout_observation_complete"].samples[0].value == 1

    monkeypatch.setattr("agent_history.config.load_config", lambda _: config)
    monkeypatch.setattr(collect_git, "machine_name", lambda: ("example-machine", "example-machine"))
    monkeypatch.setattr(collect_git.Collector, "repositories", lambda _: None)
    monkeypatch.setattr(collect_git.Collector, "homes", lambda *args: None)
    assert collect_git._main(["--dry-run"]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["warnings"] == [
        {"step": "loop_checkout", "warning": "unconfigured_allowlisted_checkout", "count": 1}
    ]
    assert summary["loop_checkout_observation"] == "complete"

    # Canonical configured aliases suppress the warning without enrolling another path.
    alias = tmp_path / "alias"
    alias.symlink_to(checkout, target_is_directory=True)
    config.git_repos = [alias]
    assert checkout_observer.collect(config)["warnings"] == []
    assert config.git_repos == [alias]

    config.git_repos = []
    config.identities.git_owners = ["github.com/other"]
    assert checkout_observer.collect(config)["warnings"] == []


def test_exact_repository_observation_and_admission_scope_freshness(tmp_path, monkeypatch):
    source = tmp_path / "source"
    sessions = source / "sessions" / "example"
    sessions.mkdir(parents=True)
    repos = []
    for name in ("widget", "sibling"):
        repo = tmp_path / name
        repo.mkdir()
        subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
        subprocess.run(
            ["git", "-C", str(repo), "remote", "add", "origin", f"https://github.com/example/{name}.git"],
            check=True,
        )
        (sessions / f"{name}.jsonl").write_text(
            json.dumps({"type": "session", "id": name, "cwd": str(repo)})
            + "\n"
            + json.dumps(
                {
                    "type": "message",
                    "message": {"role": "user", "content": "You are the root. Write codex/closeout-widget-loop1.md"},
                }
            )
            + "\n"
        )
        repos.append(repo)
    config = Config()
    config.sources = {"pi-example": source}
    _explicit(config, tmp_path)
    config.exporter.state_dir = tmp_path / "state"
    config.identities.git_repos = frozenset({"github.com/example/widget"})
    # Observation must use its own identities, not the hourly collector's module configuration.
    monkeypatch.setattr(collect_git, "ALLOWED_REPOS", {"github.com/other/project"})
    monkeypatch.setattr(collect_git, "ALLOWED_OWNERS", {("github.com", "other")})
    result = checkout_observer.collect(config)
    assert result["loop_checkout_observation"] == "complete"
    assert result["warnings"] == [{"step": "loop_checkout", "warning": "unconfigured_allowlisted_checkout", "count": 1}]
    assert collect_git.ALLOWED_REPOS == {"github.com/other/project"}
    assert collect_git.ALLOWED_OWNERS == {("github.com", "other")}
    assert config.git_repos == []
    # An admission change invalidates a diagnostic snapshot without changing source roots.
    config.identities.git_repos = frozenset({"github.com/example/sibling"})
    assert checkout_observer.summary_fields(config)["loop_checkout_observation"] == "unavailable"
    config.identities.git_repos = frozenset({"github.com/example/widget"})
    config.git_repos = [repos[0]]
    assert checkout_observer.collect(config)["warnings"] == []  # sibling owner was not widened


def test_observer_storage_failure_does_not_stop_other_collectors(tmp_path, monkeypatch, capsys):
    config = Config()
    home = tmp_path / "home"
    home.mkdir()
    config.sources = {"pi-example": home}
    _explicit(config, tmp_path)
    config.exporter.state_dir = tmp_path / "state"
    config.collector.lock_file = tmp_path / "collect.lock"
    monkeypatch.setattr("agent_history.config.load_config", lambda _: config)
    monkeypatch.setattr(collect_git, "machine_name", lambda: ("example-machine", "example-machine"))
    calls = []
    monkeypatch.setattr(collect_git.Collector, "repositories", lambda _: calls.append("repositories"))
    monkeypatch.setattr(collect_git.Collector, "homes", lambda *args: calls.append("homes"))

    def fail(*args):
        raise PermissionError("synthetic state unavailable")

    monkeypatch.setattr("agent_history.metrics.efficiency._save", fail)
    assert collect_git._main(["--dry-run"]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert calls == ["repositories", "homes"]
    assert summary["loop_checkout_observation"] == "unavailable"
    assert summary["errors"] == [{"step": "loop_checkout", "error": "PermissionError"}]


@pytest.mark.parametrize("existing", [True, False])
def test_implicit_default_sources_are_not_observed(tmp_path, monkeypatch, capsys, existing):
    path = tmp_path / "config.toml"
    if existing:
        path.write_text(f'[collector]\nlock_file = "{tmp_path / "lock"}"\n')
    monkeypatch.setattr(collect_git, "machine_name", lambda: ("example-machine", "example-machine"))
    monkeypatch.setattr(collect_git.Collector, "repositories", lambda _: None)
    monkeypatch.setattr(collect_git.Collector, "homes", lambda *args: None)

    def refuse_inventory(*args):
        raise AssertionError("implicit homes must not be scanned")

    monkeypatch.setattr("agent_history.metrics.efficiency.EfficiencyCollector.inventory", refuse_inventory)
    assert collect_git._main(["--dry-run", "--config", str(path)]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["loop_checkout_observation"] == "unavailable"
    assert summary["errors"] == []


def test_observation_unavailable_is_not_zero(tmp_path, monkeypatch, capsys):
    config = Config()
    config.exporter.state_dir = tmp_path / "state"
    config.collector.lock_file = tmp_path / "collect.lock"
    monkeypatch.setattr("agent_history.config.load_config", lambda _: config)
    monkeypatch.setattr(collect_git, "machine_name", lambda: ("example-machine", "example-machine"))
    monkeypatch.setattr(collect_git.Collector, "repositories", lambda _: None)
    monkeypatch.setattr(collect_git.Collector, "homes", lambda *args: None)
    assert collect_git._main(["--dry-run"]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["loop_checkout_observation"] == "unavailable"
    assert summary["warnings"] == []

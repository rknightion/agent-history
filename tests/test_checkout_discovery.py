"""Privacy regressions through real collector discovery, using disposable homes only."""

import json
import os
from pathlib import Path

import pytest

from agent_history.config import Config, load_config
from agent_history.metrics import archive, checkout
from test_checkout_observer import _explicit, _git, _source


@pytest.mark.parametrize("provenance", ["missing", "no-file", "no-table", "empty-table"])
def test_only_actual_file_tables_allow_discovery(tmp_path, monkeypatch, provenance):
    # HOME is supplied before Python starts by the test runner, before config imports.
    path = tmp_path / "config.toml"
    if provenance in ("no-table", "empty-table"):
        path.write_text("[sources]\n" if provenance == "empty-table" else "[collector]\n")
    config = load_config(path)
    if provenance == "no-file":
        config = Config(sources={"pi-example": tmp_path / "unproven"})
        config.collector.homes = {str(tmp_path / "also-unproven"): "pi-other"}
    config.exporter.state_dir = tmp_path / "state"

    def refuse(*args, **kwargs):
        raise AssertionError("unconfigured discovery attempted")

    monkeypatch.setattr("agent_history.metrics.efficiency.EfficiencyCollector.inventory", refuse)
    assert checkout.collect(config) == {"loop_checkout_observation": "unavailable", "warnings": []}
    assert not config.exporter.state_dir.exists()


def test_unreadable_discovery_preserves_unknown_cached_evidence(tmp_path, monkeypatch):
    repo = _git(tmp_path / "checkout")
    home = tmp_path / "home"
    sessions = home / "sessions" / "example"
    sessions.mkdir(parents=True)
    _source(
        sessions,
        "root.jsonl",
        [
            {"type": "session", "cwd": str(repo)},
            {
                "type": "message",
                "message": {"role": "user", "content": "You are the root. Write codex/closeout-widget-loop1.md"},
            },
        ],
    )
    config = _explicit(Config(sources={"pi-example": home}), tmp_path)
    config.identities.git_owners = ["github.com/example"]
    config.exporter.state_dir = tmp_path / "state"
    assert checkout.collect(config)["warnings"][0]["count"] == 1
    state_path = config.exporter.state_dir / "loop-checkout-state.json"
    before = json.loads(state_path.read_text())["checkout_files"]
    scandir = os.scandir

    def denied(path):
        if Path(path) == sessions:
            raise PermissionError("synthetic unreadable subtree")
        return scandir(path)

    monkeypatch.setattr(archive.os, "scandir", denied)
    result = checkout.collect(config)
    assert result["loop_checkout_observation"] == "partial"
    assert json.loads(state_path.read_text())["checkout_files"] == before
    snapshot = json.loads((config.exporter.state_dir / "loop-checkout-summary.json").read_text())
    assert "filesystem" in snapshot["inventory_errors"]
    # A confirmed complete removal, unlike an unreadable subtree, may withdraw evidence.
    monkeypatch.setattr(archive.os, "scandir", scandir)
    (sessions / "root.jsonl").unlink()
    assert checkout.collect(config) == {"loop_checkout_observation": "complete", "warnings": []}


@pytest.mark.parametrize("bound", ["time", "entries", "directories", "memory"])
def test_large_and_slow_inventory_stops_during_discovery(tmp_path, monkeypatch, bound):
    home = tmp_path / "home"
    home.mkdir()
    for index in range(80):
        (home / f"branch{index}").mkdir()
    config = _explicit(Config(sources={"pi-example": home}), tmp_path)
    config.exporter.state_dir = tmp_path / "state"
    scandir = os.scandir
    visited = []
    opened = []
    clock = [0.0]

    class Entries:
        def __init__(self, path):
            opened.append(path)
            self.entries = scandir(path)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.entries.close()

        def __iter__(self):
            return self

        def __next__(self):
            value = next(self.entries)
            visited.append(value.name)
            clock[0] += 0.1
            return value

    monkeypatch.setattr(archive.os, "scandir", Entries)
    monkeypatch.setattr(checkout.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(checkout, "BUDGET_SECONDS", 0.25 if bound == "time" else 100)
    if bound == "entries":
        monkeypatch.setattr(checkout, "MAX_INVENTORY_ENTRIES", 16, raising=False)
    elif bound == "directories":
        monkeypatch.setattr(checkout, "MAX_INVENTORY_DIRS", 4, raising=False)
    elif bound == "memory":
        monkeypatch.setattr(checkout, "MAX_INVENTORY_MEMORY", 512, raising=False)
    result = checkout.collect(config)
    if bound == "time":
        assert len(visited) <= 3, "discovery eagerly consumed the directory before checking its deadline"
    elif bound == "entries":
        assert len(visited) <= 16
    elif bound == "directories":
        assert len(opened) <= 4
    else:
        assert len(visited) <= 8
    assert result["loop_checkout_observation"] == "partial"

"""Bounded inventory, eligibility and snapshot freshness for the metadata observer."""

import json
import subprocess
import time

from agent_history.config import Config
from agent_history.metrics import checkout


def _explicit(config, tmp_path):
    """Actual source-table provenance for synthetic programmatic configurations."""
    config.path = tmp_path / "explicit.toml"
    config.path.write_text(
        "[sources]\n"
        + "".join(f"{json.dumps(key)} = {json.dumps(str(path))}\n" for key, path in config.sources.items())
    )
    return config


def _git(path, origin="https://github.com/example/widget.git"):
    path.mkdir()
    subprocess.run(["git", "init", str(path)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(path), "remote", "add", "origin", origin], check=True)
    return path


def _source(tmp_path, name, records):
    path = tmp_path / name
    path.write_text("".join(json.dumps(record) + "\n" for record in records))
    stat = path.stat()
    return path, stat.st_size, stat.st_mtime_ns


def test_harnesses_children_non_git_and_inventory_boundary(tmp_path):
    repo = _git(tmp_path / "checkout")
    non_git = tmp_path / "plain"
    non_git.mkdir()
    config = Config()
    config.identities.git_owners = ["github.com/example"]
    launch = "You are the root. Write codex/closeout-widget-loop1.md"
    sources = {
        "codex-example/sessions/root.jsonl": _source(
            tmp_path,
            "codex.jsonl",
            [
                {"type": "session_meta", "payload": {"cwd": str(repo)}},
                {"type": "event_msg", "payload": {"type": "user_message", "message": launch}},
            ],
        ),
        "claude-example/projects/root.jsonl": _source(
            tmp_path,
            "claude.jsonl",
            [
                {"type": "user", "cwd": str(repo), "message": {"content": launch}},
            ],
        ),
        "pi-example/sessions/child.jsonl": _source(
            tmp_path,
            "child.jsonl",
            [
                {"type": "session", "parentSession": "/example/root.jsonl", "cwd": str(repo)},
                {"type": "message", "message": {"role": "user", "content": launch}},
            ],
        ),
        "claude-example/projects/plain.jsonl": _source(
            tmp_path,
            "plain.jsonl",
            [
                {"type": "user", "cwd": str(non_git), "message": {"content": launch}},
            ],
        ),
    }
    # Two harness roots in one checkout produce one warning, never duplicate enrollment.
    state = {}
    summary = checkout.observe(config, sources, state)
    assert summary["count"] == 1
    assert summary["complete"]
    assert config.git_repos == []
    # Removing the roots from the supplied inventory must withdraw their evidence.
    summary = checkout.observe(
        config, {key: value for key, value in sources.items() if "child" in key or "plain" in key}, state
    )
    assert summary["count"] == 0
    assert summary["complete"]
    # No observer discovery of the omitted root files, even though they remain on disk.
    assert (tmp_path / "codex.jsonl").exists()


def test_budget_resumes_and_partial_stays_distinct(tmp_path, monkeypatch):
    repo = _git(tmp_path / "checkout")
    config = Config()
    config.identities.git_owners = ["github.com/example"]
    records = [
        {"type": "session", "cwd": str(repo)},
        {
            "type": "message",
            "message": {"role": "user", "content": "You are the root. Write codex/closeout-widget-loop1.md"},
        },
    ]
    source = _source(tmp_path, "root.jsonl", records)
    sources = {"pi-example/sessions/root.jsonl": source}
    state = {}
    monkeypatch.setattr(checkout, "MAX_BYTES", 1)
    summary = checkout.observe(config, sources, state)
    assert not summary["complete"]
    assert state["checkout_files"][next(iter(sources))]["offset"] == 0
    monkeypatch.setattr(checkout, "MAX_BYTES", 4096)
    summary = checkout.observe(config, sources, state)
    assert summary["complete"]
    assert summary["count"] == 1

    # Malformed complete input is consumed but never converted into complete zero on a later pass.
    malformed = tmp_path / "bad.jsonl"
    malformed.write_text("not json\n")
    stat = malformed.stat()
    sources = {"pi-example/sessions/bad.jsonl": (malformed, stat.st_size, stat.st_mtime_ns)}
    assert not checkout.observe(config, sources, state)["complete"]
    assert not checkout.observe(config, sources, state)["complete"]


def test_snapshot_staleness_and_config_change(tmp_path):
    config = Config()
    config.exporter.state_dir = tmp_path
    path = tmp_path / "loop-checkout-summary.json"
    summary = {
        "at": time.time() - checkout.MAX_AGE - 1,
        "config": checkout.fingerprint(config),
        "count": 3,
        "complete": False,
    }
    path.write_text(json.dumps(summary))
    assert checkout.summary_fields(config)["loop_checkout_observation"] == "unavailable"
    summary["at"] = time.time()
    path.write_text(json.dumps(summary))
    assert checkout.summary_fields(config)["loop_checkout_observation"] == "partial"
    config.identities.git_repos = frozenset({"github.com/example/widget"})
    assert checkout.summary_fields(config)["loop_checkout_observation"] == "unavailable"
    config.identities.git_repos = frozenset()
    config.git_repos = [tmp_path / "new"]
    assert checkout.summary_fields(config)["loop_checkout_observation"] == "unavailable"

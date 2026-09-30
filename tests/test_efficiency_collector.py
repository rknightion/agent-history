"""The public seam counts complete lines exactly once, including after process restart."""

import json
from datetime import datetime, timedelta, timezone

import pytest
from pathlib import Path

from agent_history.config import parse_config
from agent_history.metrics.efficiency import EfficiencyCollector


def value(families, name, **labels):
    return sum(s.value for f in families if f.name == name for s in f.samples if dict(s.labels) == labels)


def test_incremental_restart_and_partial_line(tmp_path: Path):
    source = tmp_path / "pi" / "sessions" / "synthetic"
    source.mkdir(parents=True)
    transcript = source / "test.jsonl"

    def line(record):
        return json.dumps(record, separators=(",", ":")) + "\n"

    header = line({"type": "session", "id": "synthetic", "timestamp": "2026-01-01T00:00:00Z"})
    prompt = line(
        {"type": "message", "timestamp": "2026-01-01T00:00:01Z", "message": {"role": "user", "content": "hello"}}
    )
    call = line(
        {
            "type": "message",
            "timestamp": "2026-01-01T00:00:02Z",
            "message": {
                "role": "assistant",
                "model": "test-model",
                "usage": {"input": 10, "cacheRead": 2, "output": 3},
                "content": [],
            },
        }
    )
    transcript.write_text(header + prompt + call[:-1])
    config = parse_config(
        {"sources": {"pi-local": str(tmp_path / "pi")}, "efficiency": {"baseline_ts": 0, "first_parse_days": 1000}}
    )
    state = tmp_path / "state"
    first = EfficiencyCollector(config, state).collect()
    labels = {"agent": "pi", "namespace": "pi-local", "role": "solo", "trigger": "user", "loop": "none"}
    assert value(first, "agent_efficiency_llm_calls_total", **labels) == 0
    with transcript.open("a") as output:
        output.write("\n")
    second = EfficiencyCollector(config, state).collect()
    assert value(second, "agent_efficiency_llm_calls_total", **labels) == 1
    third = EfficiencyCollector(config, state).collect()
    assert value(third, "agent_efficiency_llm_calls_total", **labels) == 1
    with transcript.open("a") as output:
        output.write(prompt + call)
    fourth = EfficiencyCollector(config, state).collect()
    assert value(fourth, "agent_efficiency_llm_calls_total", **labels) == 2


@pytest.mark.parametrize("failure", ["stat", "open"])
def test_disappearing_transcript_does_not_block_other_sources(tmp_path: Path, monkeypatch, failure):
    source = tmp_path / "pi" / "sessions" / "synthetic"
    source.mkdir(parents=True)
    content = (
        json.dumps({"type": "session", "id": "synthetic", "timestamp": "2026-09-29T20:00:00Z"})
        + "\n"
        + json.dumps(
            {"type": "message", "timestamp": "2026-09-29T20:00:01Z", "message": {"role": "user", "content": "hello"}}
        )
        + "\n"
        + json.dumps(
            {
                "type": "message",
                "timestamp": "2026-09-29T20:00:02Z",
                "message": {"role": "assistant", "model": "test", "usage": {"input": 2, "output": 1}, "content": []},
            }
        )
        + "\n"
    )
    bad, good = source / "bad.jsonl", source / "good.jsonl"
    bad.write_text(content)
    good.write_text(content)
    if failure == "stat":
        original = Path.stat
        calls = 0

        def rotating_stat(self, *args, **kwargs):
            nonlocal calls
            if self == bad:
                calls += 1
                if calls >= 2:
                    raise FileNotFoundError("synthetic rotation")
            return original(self, *args, **kwargs)

        monkeypatch.setattr(Path, "stat", rotating_stat)
    else:
        original = Path.open

        def rotating_open(self, *args, **kwargs):
            if self == bad:
                raise FileNotFoundError("synthetic rotation")
            return original(self, *args, **kwargs)

        monkeypatch.setattr(Path, "open", rotating_open)
    config = parse_config(
        {"sources": {"pi-local": str(tmp_path / "pi")}, "efficiency": {"baseline_ts": 0, "first_parse_days": 1000}}
    )
    state = tmp_path / "state"
    families = EfficiencyCollector(config, state).collect()
    assert sum(s.value for f in families if f.name == "agent_efficiency_llm_calls_total" for s in f.samples) == 1
    assert (state / "efficiency-state.json").exists()


def test_new_file_counts_only_events_inside_first_parse_window(tmp_path: Path, monkeypatch):
    fixed = datetime(2026, 9, 29, 20, tzinfo=timezone.utc)
    monkeypatch.setattr("agent_history.metrics.efficiency.time.time", lambda: fixed.timestamp())
    source = tmp_path / "pi" / "sessions" / "synthetic"
    source.mkdir(parents=True)
    transcript = source / "window.jsonl"

    def record(role, timestamp, response):
        return (
            json.dumps(
                {
                    "type": "message",
                    "timestamp": timestamp.isoformat(),
                    "message": {
                        "role": role,
                        "model": "test-model",
                        "responseId": response,
                        "usage": {"input": 10, "output": 3},
                        "content": "hello",
                    },
                }
            )
            + "\n"
        )

    old = fixed - timedelta(days=5)
    recent = fixed - timedelta(hours=1)
    transcript.write_text(
        json.dumps({"type": "session", "id": "synthetic", "timestamp": old.isoformat()})
        + "\n"
        + record("user", old, "old-prompt")
        + record("assistant", old, "old-call")
        + record("user", recent, "recent-prompt")
        + record("assistant", recent, "recent-call")
    )
    config = parse_config(
        {"sources": {"pi-local": str(tmp_path / "pi")}, "efficiency": {"baseline_ts": 0, "first_parse_days": 1}}
    )
    families = EfficiencyCollector(config, tmp_path / "state").collect()
    assert sum(s.value for f in families if f.name == "agent_efficiency_llm_calls_total" for s in f.samples) == 1

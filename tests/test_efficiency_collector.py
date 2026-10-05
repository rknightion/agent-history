"""The public seam counts complete lines exactly once, including after process restart."""

import json
from datetime import datetime, timedelta, timezone

import pytest
from pathlib import Path

from agent_history.config import parse_config
from agent_history.metrics.efficiency import EfficiencyCollector


def value(families, name, **labels):
    return sum(s.value for f in families if f.name == name for s in f.samples if dict(s.labels) == labels)


@pytest.mark.parametrize("retained_count", [40, 200])
def test_mapped_loop_labels_are_not_capacity_limited(tmp_path: Path, monkeypatch, retained_count):
    from agent_history.efficiency import parser as rules

    now = datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp()
    monkeypatch.setattr("agent_history.metrics.efficiency.time.time", lambda: now)
    source = tmp_path / "pi" / "sessions" / "synthetic"
    source.mkdir(parents=True)
    records = [
        {"type": "session", "id": "synthetic", "timestamp": "2026-01-01T00:00:00Z"},
        {
            "type": "message",
            "timestamp": "2026-01-01T00:00:00Z",
            "message": {"role": "user", "content": "hello"},
        },
        {
            "type": "message",
            "timestamp": "2026-01-01T00:00:00Z",
            "message": {
                "role": "assistant",
                "model": "test-model",
                "usage": {"input": 10, "output": 3},
                "content": [],
            },
        },
    ]
    (source / "test.jsonl").write_text("".join(json.dumps(record) + "\n" for record in records))
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    state_path = state_dir / "efficiency-state.json"
    state = rules.read_efficiency_state(state_path, 0)
    state["loops"] = {f"repo/loop{i}": now - 60 for i in range(retained_count)}
    expired = "repo/loop-expired"
    state["loops"][expired] = now - rules.EFFICIENCY_LOOP_RETAIN_SECONDS - 1
    metric = "agent_efficiency_llm_calls_total"
    state["totals"][metric] = {f"pi\tpi-local\tsolo\tuser\t{expired}": 7}
    label = f"repo/loop{retained_count}"
    state["loop_map"] = {
        "fetched": now,
        "roots": {},
        "members": {rules.loop_session_key("pi", "synthetic", ""): label},
    }
    state_path.write_text(json.dumps(state))
    config = parse_config(
        {"sources": {"pi-local": str(tmp_path / "pi")}, "efficiency": {"baseline_ts": 0, "first_parse_days": 1000}}
    )
    collector = EfficiencyCollector(config, state_dir)
    families = collector.collect()
    labels = {"agent": "pi", "namespace": "pi-local", "role": "root", "trigger": "user"}
    assert value(families, metric, **labels, loop=label) == 1
    assert value(families, metric, **labels, loop="other") == 0
    assert value(families, metric, **labels, loop=expired) == 0
    assert value(families, "agent_efficiency_loop_labels") == retained_count + 1
    saved = json.loads(state_path.read_text())
    assert label in saved["loops"]
    assert expired not in saved["loops"]
    assert expired in collector.retired_loops[metric]
    assert expired in saved["retired_loops"][metric]
    assert value(EfficiencyCollector(config, state_dir).collect(), metric, **labels, loop=label) == 1


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


@pytest.mark.parametrize(
    "deadline_hit,exit_code,result",
    [
        (False, 0, "event"),
        (False, 1, "event"),
        (True, None, "timed_out"),
        (None, 0, "timed_out"),
        (False, None, "timed_out"),
    ],
)
def test_pi_process_watch_wait_counters_reach_otlp(tmp_path: Path, monkeypatch, deadline_hit, exit_code, result):
    from agent_history.metrics.otlp import _index

    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    monkeypatch.setattr("agent_history.metrics.efficiency.time.time", lambda: now.timestamp() + 100)
    source = tmp_path / "pi" / "sessions" / "synthetic"
    source.mkdir(parents=True)
    transcript = source / "watch.jsonl"
    records = [{"type": "session", "id": "synthetic", "timestamp": now.isoformat()}]
    config = parse_config(
        {"sources": {"pi-local": str(tmp_path / "pi")}, "efficiency": {"baseline_ts": 0, "first_parse_days": 1000}}
    )
    state = tmp_path / "state"
    labels = {"agent": "pi", "namespace": "pi-local", "role": "solo", "loop": "none"}

    for count in (1, 2):
        records.extend(
            [
                {
                    "type": "message",
                    "timestamp": (now + timedelta(seconds=count * 10)).isoformat(),
                    "message": {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "toolCall",
                                "id": f"watch-{count}",
                                "name": "watch_process",
                                "arguments": {
                                    "command": "gh run watch 123 --exit-status --interval 60",
                                    "deadline_s": 30,
                                    "tail_lines": 20,
                                },
                            }
                        ],
                    },
                },
                {
                    "type": "message",
                    "timestamp": (now + timedelta(seconds=count * 10 + 3)).isoformat(),
                    "message": {
                        "role": "toolResult",
                        "toolCallId": f"watch-{count}",
                        "content": [{"type": "text", "text": "synthetic watcher output"}],
                        "details": {"exit_code": exit_code, "deadline_hit": deadline_hit, "tail": ""},
                    },
                },
            ]
        )
        transcript.write_text("".join(json.dumps(record) + "\n" for record in records))
        families = EfficiencyCollector(config, state).collect()
        assert value(families, "agent_efficiency_wait_requests_total", **labels, tool="watch_process") == count
        assert (
            value(families, "agent_efficiency_wait_timeout_ms_total", **labels, tool="watch_process") == count * 30000
        )
        assert value(families, "agent_efficiency_poll_calls_total", **labels, target="ci", result=result) == count
        assert value(families, "agent_efficiency_poll_seconds_total", **labels, target="ci", result=result) == count * 3
        index = _index(families)
        assert not index.rejected
        for metric, expected in (
            ("agent_efficiency_wait_requests_total", count),
            ("agent_efficiency_wait_timeout_ms_total", count * 30000),
        ):
            assert index.observations[metric] == (
                (tuple(sorted({**labels, "tool": "watch_process"}.items())), expected),
            )
        restarted = EfficiencyCollector(config, state).collect()
        assert value(restarted, "agent_efficiency_wait_requests_total", **labels, tool="watch_process") == count


@pytest.mark.parametrize("model", [{"unexpected": "model"}, ["unexpected"], 17])
@pytest.mark.parametrize("record_kind", ["assistant", "model_change"])
def test_malformed_pi_model_does_not_poison_cache_resume(tmp_path: Path, monkeypatch, model, record_kind):
    fixed = datetime(2026, 9, 29, 20, tzinfo=timezone.utc)
    monkeypatch.setattr("agent_history.metrics.efficiency.time.time", lambda: fixed.timestamp())
    source = tmp_path / "pi" / "sessions" / "synthetic"
    source.mkdir(parents=True)
    transcript = source / "model.jsonl"

    def line(record):
        return json.dumps(record, separators=(",", ":")) + "\n"

    def call(model_value, seconds=2):
        return line(
            {
                "type": "message",
                "timestamp": (fixed + timedelta(seconds=seconds)).isoformat(),
                "message": {
                    "role": "assistant",
                    "model": model_value,
                    "usage": {"input": 10, "output": 3},
                    "content": [],
                },
            }
        )

    prompt = line({"type": "message", "timestamp": fixed.isoformat(), "message": {"role": "user", "content": "hello"}})
    malformed = (
        call(model)
        if record_kind == "assistant"
        else line({"type": "model_change", "timestamp": fixed.isoformat(), "modelId": model}) + call(None)
    )
    transcript.write_text(
        line({"type": "session", "id": "synthetic", "timestamp": fixed.isoformat()}) + prompt + malformed
    )
    config = parse_config(
        {"sources": {"pi-local": str(tmp_path / "pi")}, "efficiency": {"baseline_ts": 0, "first_parse_days": 1000}}
    )
    state = tmp_path / "state"
    first = EfficiencyCollector(config, state).collect()
    restarted = EfficiencyCollector(config, state)
    second = restarted.collect()
    with transcript.open("a") as output:
        output.write(call("unsupported-model", seconds=3))
    third = restarted.collect()

    def calls(families):
        return sum(s.value for f in families if f.name == "agent_efficiency_llm_calls_total" for s in f.samples)

    counts = [calls(families) for families in (first, second, third)]
    assert counts == [1, 1, 2], f"cold/restart/append-valid counts: {counts}"
    for families in (first, second, third):
        assert (
            value(
                families,
                "agent_efficiency_model_calls_total",
                agent="pi",
                namespace="pi-local",
                role="solo",
                model="unknown",
                loop="none",
            )
            == 1
        )
        assert not any(f.name == "agent_efficiency_context_fill_ratio" and f.samples for f in families)
    saved = json.loads((state / "efficiency-state.json").read_text())
    assert all(isinstance(item, str) for item in saved["models"])
    assert all(entry["model"] is None or isinstance(entry["model"], str) for entry in saved["files"].values())
    fourth = EfficiencyCollector(config, state).collect()
    assert calls(fourth) == 2


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
    injected = False
    if failure == "stat":
        original = Path.stat
        is_file, is_symlink = Path.is_file, Path.is_symlink
        # Discovery is not the rotation boundary. Isolate its predicates so this
        # fault fires at the collector's explicit metadata read on every Python.
        monkeypatch.setattr(Path, "is_file", lambda self: True if self == bad else is_file(self))
        monkeypatch.setattr(Path, "is_symlink", lambda self: False if self == bad else is_symlink(self))

        def rotating_stat(self, *args, **kwargs):
            nonlocal injected
            if self == bad:
                injected = True
                raise FileNotFoundError("synthetic rotation")
            return original(self, *args, **kwargs)

        monkeypatch.setattr(Path, "stat", rotating_stat)
    else:
        original = Path.open

        def rotating_open(self, *args, **kwargs):
            nonlocal injected
            if self == bad:
                injected = True
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
    assert injected, "rotation fault must reach the explicit metadata/open boundary"


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


def test_transcript_sources_match_legacy_plus_opt_in_workflow_agents(tmp_path: Path):
    """Workflow agent transcripts are opt-out; workflow journals and Codex archives never count."""
    claude = tmp_path / "claude"
    session = claude / "projects" / "example-project" / "session-a"
    files = [
        claude / "projects" / "example-project" / "session-a.jsonl",
        session / "subagents" / "agent-direct.jsonl",
        session / "subagents" / "workflows" / "wf-1" / "agent-workflow.jsonl",
        session / "subagents" / "workflows" / "wf-1" / "journal.jsonl",
        tmp_path / "codex" / "sessions" / "2026" / "rollout-a.jsonl",
        tmp_path / "codex" / "archived_sessions" / "rollout-a.jsonl",
    ]
    for file in files:
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text('{"type":"user"}\n')
    sources = {"claude-local": str(claude), "codex-local": str(tmp_path / "codex")}

    def tracked(**efficiency):
        config = parse_config({"sources": sources, "efficiency": {"baseline_ts": 0, **efficiency}})
        families = EfficiencyCollector(config, tmp_path / f"state-{len(efficiency)}").collect()
        return value(families, "agent_efficiency_tracked_files")

    # Legacy's set plus the workflow agent transcript: never the journal or the moved Codex copy.
    assert tracked() == 4
    assert tracked(workflow_transcripts=False) == 3
    with pytest.raises(Exception):
        parse_config({"efficiency": {"workflow_transcripts": "yes"}})

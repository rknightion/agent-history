"""V3 protocol and quiet-state evidence through the real transcript collector."""

import json
import time

import pytest

from agent_history.efficiency.parser import efficiency_protocol
from agent_history.metrics.collection import State, render
from efficiency_transcripts import (
    Harness,
    claude,
    claude_tool,
    codex,
    codex_meta,
    iso,
    record,
    spawn_fn,
    user_item,
    value,
)
from test_efficiency_contract import PI_ROOT, PI_ROOT_BASE, pi_call, pi_message, pi_record, pi_result


def test_protocol_recognition_never_checks_local_launch_files(monkeypatch):
    def forbidden(_path):
        raise AssertionError("Protocol recognition must not inspect collector files")

    monkeypatch.setattr("agent_history.loop_launch.os.path.isfile", forbidden)
    assert efficiency_protocol("launch-You are the root.txt") == "none"


@pytest.fixture
def eff(tmp_path, monkeypatch):
    return Harness(tmp_path, monkeypatch, time.time() - 2000)


def root(eff, agent, text, legacy=False, *, prompt=None):
    start = eff.baseline + 10
    if prompt is None:
        prompt = (
            "You are the root. Contract: loop-v2.1. Write codex/report-example-loop7.md when done."
            if legacy
            else "You are the root. Write codex/closeout-example-loop7.md when done."
        )
    if agent == "codex":
        path = eff.codex_file(
            "rollout-root.jsonl",
            [
                codex_meta(start, "root"),
                user_item(start + 1, prompt),
                spawn_fn(start + 2, "spawn"),
                record(start + 3),
            ],
        )
        reply = lambda ts, body: codex(
            ts,
            "response_item",
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": body}],
            },
        )
        activity = lambda ts: record(ts)
    elif agent == "claude":
        path = eff.claude_file(
            "root.jsonl",
            [
                claude(start + 1, "user", {"content": prompt}),
                claude_tool(start + 2, "a1", "spawn", "Agent", {"prompt": "work"}),
            ],
        )
        reply = lambda ts, body: claude(
            ts,
            "assistant",
            {
                "id": str(ts),
                "model": "test-model",
                "usage": {"input_tokens": 1, "output_tokens": 1},
                "content": [{"type": "text", "text": body}],
            },
        )
        activity = lambda ts: claude_tool(ts, "a2", "tool", "Read", {"file_path": "/synthetic/file"})
    else:
        directory = eff.hot / "pi-local" / "sessions" / "-synthetic-"
        directory.mkdir(parents=True)
        path = directory / f"{PI_ROOT_BASE}.jsonl"
        path.write_text(
            "".join(
                [
                    pi_record(start, "session", id=PI_ROOT, cwd="/synthetic/cwd", version=3),
                    pi_message(start + 1, "u", "user", content=[{"type": "text", "text": prompt}]),
                    pi_call(
                        start + 2, "a1", "r1", "spawn", "subagent", {"tasks": [{"agent": "worker", "task": "work"}]}
                    ),
                    pi_result(
                        start + 3,
                        "result",
                        "spawn",
                        "subagent",
                        details={"results": [{"agent": "worker", "index": 0, "exitCode": 0}]},
                    ),
                ]
            )
        )
        reply = lambda ts, body: pi_message(
            ts,
            "a2",
            "assistant",
            model="test-model",
            usage={"input": 1, "output": 1},
            content=[{"type": "text", "text": body}],
        )
        activity = lambda ts: pi_call(ts, "a3", "r3", "tool", "bash", {"command": "true"})
    with path.open("a") as handle:
        handle.write(reply(start + 1300, text))
    return path, activity


@pytest.mark.parametrize("agent", ["codex", "claude", "pi"])
@pytest.mark.parametrize(
    "state,expected",
    [
        ("working", 1),
        ("wait", 0),
        ("paused", 0),
        ("expired", 1),
        ("invalid", 1),
        ("quoted", 1),
        ("not-last", 1),
    ],
)
def test_v3_stall_quiet_and_resume(eff, agent, state, expected):
    now = eff.baseline + 1400
    waiting = f"WAITING: gate until {iso(now + 60)}"
    text = {
        "working": "Still working",
        "wait": "Summary\n" + waiting + "\n\n",
        "paused": "PAUSED: owner decision",
        "expired": f"WAITING: gate until {iso(now - 1)}",
        "invalid": "WAITING: gate until tomorrow",
        "quoted": "> " + waiting,
        "not-last": waiting + "\nContinuing work",
    }[state]
    path, activity = root(eff, agent, text)
    samples = eff.collect(now)
    saved = eff.saved()
    assert {entry["proto"] for entry in saved["files"].values()} == {"v3"}
    assert value(samples, "agent_efficiency_roots_without_lanes", agent=agent, namespace=f"{agent}-local") == expected
    assert value(
        samples, "agent_efficiency_root_no_lane_seconds", agent=agent, namespace=f"{agent}-local"
    ) == pytest.approx(1388 * expected, abs=0.01)
    # Quiet state survives an unchanged collection and is cleared by fresh activity.
    assert (
        value(eff.collect(now + 1), "agent_efficiency_roots_without_lanes", agent=agent, namespace=f"{agent}-local")
        == expected
    )
    with path.open("a") as handle:
        handle.write(activity(now + 2))
    assert (
        value(eff.collect(now + 3), "agent_efficiency_roots_without_lanes", agent=agent, namespace=f"{agent}-local")
        == 1
    )


@pytest.mark.parametrize("agent", ["codex", "claude", "pi"])
def test_minute_only_wait_renders_quiet_until_expiry(eff, agent):
    now = eff.baseline + 1400
    deadline = int((now + 600) // 60) * 60
    root(eff, agent, f"WAITING: review until {iso(deadline)[:16]}Z")
    text, _ = render(list(eff.families(now)), State(eff.root / "render-state"))
    gauges = [
        float(line.rsplit(" ", 1)[1])
        for line in text.splitlines()
        if line.startswith(("agent_efficiency_roots_without_lanes{", "agent_efficiency_root_no_lane_seconds{"))
        and f'agent="{agent}"' in line
    ]
    assert gauges == [0, 0]
    assert (
        value(eff.collect(deadline), "agent_efficiency_roots_without_lanes", agent=agent, namespace=f"{agent}-local")
        == 1
    )


@pytest.mark.parametrize("agent", ["codex", "claude", "pi"])
@pytest.mark.parametrize(
    "report_names,contract,expected",
    [
        (["example-loop7"], "Contract: loop-v2.1. ", "v2.1"),
        (["first-loop7", "second-loop7"], "", "none"),
    ],
)
def test_report_precedence_and_ambiguity_control_protocol(eff, agent, report_names, contract, expected):
    prompt = "You are the root. " + contract + "Write "
    prompt += " or ".join(f"codex/report-{name}.md" for name in report_names)
    prompt += ". Background notes: codex/closeout-notes-loop1.md."
    root(eff, agent, "Working", prompt=prompt)
    samples = eff.collect(eff.baseline + 1400)
    assert {entry["proto"] for entry in eff.saved()["files"].values()} == {expected}
    assert value(samples, "agent_efficiency_roots_without_lanes", agent=agent, namespace=f"{agent}-local") == (
        1 if expected == "v2.1" else 0
    )


@pytest.mark.parametrize("legacy", [False, True])
def test_wait_expires_without_new_transcript_activity(eff, legacy):
    now = eff.baseline + 1400
    root(eff, "codex", f"WAITING: gate until {iso(now + 60)}", legacy=legacy)
    assert value(eff.collect(now), "agent_efficiency_roots_without_lanes", agent="codex", namespace="codex-local") == 0
    assert (
        value(eff.collect(now + 60), "agent_efficiency_roots_without_lanes", agent="codex", namespace="codex-local")
        == 1
    )


@pytest.mark.parametrize("agent", ["codex", "claude", "pi"])
def test_old_cached_prompt_is_recovered_without_recounting(eff, agent):
    root(eff, agent, "PAUSED: owner decision")
    now = eff.baseline + 1400
    eff.collect(now)
    saved = eff.saved()
    totals = saved["totals"]
    for entry in saved["files"].values():
        entry["proto"] = "none"
        entry.pop("protocol_shape_version", None)
        entry.pop("quiet", None)
    eff.state.write_text(json.dumps(saved))
    samples = eff.collect(now + 1)
    assert value(samples, "agent_efficiency_roots_without_lanes", agent=agent, namespace=f"{agent}-local") == 0
    recovered = eff.saved()
    assert {entry["proto"] for entry in recovered["files"].values()} == {"v3"}
    assert recovered["totals"] == totals

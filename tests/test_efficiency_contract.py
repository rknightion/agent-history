"""Codex, Claude and pi efficiency contract through the public collector, over synthetic transcripts.

Each test writes invented rollouts or sessions, runs `EfficiencyCollector.collect()` at a pinned
clock and asserts the emitted series. The expectations encode the efficiency contract the
collector was ported from: time states, call triggers, poll results, delivery outcomes, spawn
routes, interventions, the loop protocol, stalled loop roots, lanes and rate limits.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

import pytest

from agent_history.efficiency.parser import (
    EFFICIENCY_BUDGET_CHECK_LINES,
    EFFICIENCY_LOOP_RETAIN_SECONDS,
    LOOP_MAP_MAX_AGE_SECONDS,
    EfficiencyParser,
    EfficiencyRun,
    efficiency_file_state,
    efficiency_session_keys,
    loop_label_value,
    read_efficiency_state,
)
from agent_history.metrics.archive import ArchiveCollector
from efficiency_transcripts import (
    CLAUDE,
    CODEX,
    UNLOOPED,
    Harness,
    calls,
    claude,
    claude_result,
    claude_tool,
    codex,
    codex_meta,
    cr_line,
    exec_fn,
    exec_js,
    exited,
    fn_out,
    iso,
    js_out,
    record,
    running,
    select,
    spawn_fn,
    stdin_fn,
    total,
    usage,
    user_item,
    value,
    wait_agent,
    waited,
)


@pytest.fixture
def eff(tmp_path: Path, monkeypatch) -> Harness:
    harness = Harness(tmp_path, monkeypatch, time.time() - 1000)
    harness.now = harness.baseline + 1000
    return harness


def key(**labels: str) -> frozenset:
    return frozenset(labels.items())


def wait_results(samples: dict) -> dict:
    return {
        "calls": {
            dict(labels)["trigger"]: v
            for (name, labels), v in samples.items()
            if name == "agent_efficiency_llm_calls_total" and dict(labels)["trigger"] in ("wait", "event")
        },
        "polls": {
            (dict(labels)["target"], dict(labels)["result"]): v
            for (name, labels), v in samples.items()
            if name == "agent_efficiency_poll_calls_total"
        },
        "requests": {
            dict(labels)["tool"]: v
            for (name, labels), v in samples.items()
            if name == "agent_efficiency_wait_requests_total"
        },
        "ms": {
            dict(labels)["tool"]: v
            for (name, labels), v in samples.items()
            if name == "agent_efficiency_wait_timeout_ms_total"
        },
    }


# -- time states, triggers and waits ----------------------------------------------------------


def test_codex_root_spawn_poll_and_wait_attribution(eff: Harness):
    b = eff.baseline + 500
    eff.codex_file(
        "rollout-root.jsonl",
        [
            codex_meta(b, "root-thread"),
            codex(b, "turn_context", {"model": "gpt-test"}),
            codex(b + 1, "event_msg", {"type": "task_started"}),
            codex(b + 1, "event_msg", {"type": "user_message", "message": "go"}),
            codex(b + 1.5, "response_item", {"type": "message", "role": "assistant", "content": []}),
            record(b + 2, "root-thread", 1000, 800, 50),
            codex(
                b + 3,
                "response_item",
                {"type": "function_call", "name": "spawn_agent", "arguments": "{}", "call_id": "c1"},
            ),
            codex(b + 4, "response_item", {"type": "function_call_output", "call_id": "c1", "output": "ok"}),
            record(b + 6, "root-thread", 2000, 1500, 20),
            exec_js(
                b + 7,
                "c2",
                'const r = await tools.exec_command({cmd: "gh run watch 123 --exit-status", yield_time_ms: 1000}); text(JSON.stringify(r));',
            ),
            codex(
                b + 17,
                "response_item",
                {
                    "type": "custom_tool_call_output",
                    "call_id": "c2",
                    "output": [{"type": "input_text", "text": '{"session_id":7,"output":"running"}'}],
                },
            ),
            record(b + 18, "root-thread", 3000, 2900, 5),
            exec_js(
                b + 19,
                "c3",
                'const r = await tools.write_stdin({session_id:7,chars:"",yield_time_ms:30000,max_output_tokens:1000}); text(r);',
            ),
            codex(
                b + 49,
                "response_item",
                {
                    "type": "custom_tool_call_output",
                    "call_id": "c3",
                    "output": [{"type": "input_text", "text": "Wall time 30.0 seconds\nProcess exited with code 0"}],
                },
            ),
            record(b + 50, "root-thread", 4000, 3900, 5),
            wait_agent(b + 51, "c4", 60000),
            fn_out(b + 111, "c4", '{"message":"none","timed_out":true}'),
            record(b + 112, "root-thread", 5000, 4900, 5),
            codex(b + 113, "event_msg", {"type": "task_complete"}),
            codex(b + 213, "event_msg", {"type": "task_started"}),
        ],
    )
    eff.codex_file(
        "rollout-worker.jsonl",
        [
            codex_meta(eff.baseline + 90, "worker-thread", parent="root-thread"),
            codex(eff.baseline + 90, "turn_context", {"model": "gpt-test"}),
            record(eff.baseline + 100, "worker-thread", 100, 0, 1),
        ],
    )
    samples = eff.collect(eff.now)
    v = lambda name, **labels: value(samples, name, agent="codex", namespace=CODEX, **labels)
    assert v("agent_efficiency_time_seconds_total", role="solo", state="model") == 3
    assert v("agent_efficiency_time_seconds_total", role="root", state="model") == 9
    assert v("agent_efficiency_time_seconds_total", role="root", state="tool_wait") == 100
    assert v("agent_efficiency_time_seconds_total", role="root", state="tool_orchestrate") == 1
    assert v("agent_efficiency_time_seconds_total", role="root", state="idle") == 100
    assert v("agent_efficiency_model_seconds_total", role="solo", model="gpt-test") == 3
    assert v("agent_efficiency_llm_calls_total", role="solo", trigger="user") == 1
    assert v("agent_efficiency_llm_calls_total", role="root", trigger="orchestrate") == 1
    assert v("agent_efficiency_llm_calls_total", role="root", trigger="wait") == 2
    assert v("agent_efficiency_llm_calls_total", role="root", trigger="event") == 1
    assert v("agent_efficiency_llm_calls_total", role="worker", trigger="user") == 1
    assert v("agent_efficiency_input_tokens_total", role="solo", trigger="user", cache="hit") == 800
    assert v("agent_efficiency_input_tokens_total", role="solo", trigger="user", cache="miss") == 200
    assert v("agent_efficiency_input_tokens_total", role="root", trigger="wait", cache="hit") == 7800
    assert v("agent_efficiency_input_tokens_total", role="root", trigger="wait", cache="miss") == 200
    assert v("agent_efficiency_input_tokens_total", role="root", trigger="event", cache="hit") == 3900
    assert v("agent_efficiency_input_tokens_total", role="root", trigger="event", cache="miss") == 100
    assert v("agent_efficiency_output_tokens_total", role="root") == 35
    assert v("agent_efficiency_model_calls_total", role="root", model="gpt-test") == 4
    assert v("agent_efficiency_tool_calls_total", role="root", **{"class": "orchestrate"}) == 1
    assert v("agent_efficiency_tool_calls_total", role="root", **{"class": "wait"}) == 3
    assert v("agent_efficiency_poll_calls_total", role="root", target="ci", result="timed_out") == 1
    assert v("agent_efficiency_poll_seconds_total", role="root", target="ci", result="timed_out") == 10
    assert v("agent_efficiency_poll_calls_total", role="root", target="ci", result="event") == 1
    assert v("agent_efficiency_poll_seconds_total", role="root", target="ci", result="event") == 30
    assert v("agent_efficiency_poll_calls_total", role="root", target="agent", result="timed_out") == 1
    assert v("agent_efficiency_poll_seconds_total", role="root", target="agent", result="timed_out") == 60
    assert v("agent_efficiency_wait_requests_total", role="root", tool="write_stdin") == 1
    assert v("agent_efficiency_wait_timeout_ms_total", role="root", tool="write_stdin") == 30000
    assert v("agent_efficiency_wait_requests_total", role="root", tool="wait_agent") == 1
    assert v("agent_efficiency_wait_timeout_ms_total", role="root", tool="wait_agent") == 60000
    assert v("agent_efficiency_spawns_total") == 1
    assert v("agent_efficiency_active_threads", role="root") == 1
    assert v("agent_efficiency_active_threads", role="worker") == 0
    assert v("agent_efficiency_active_roots_with_idle_workers") == 1
    assert v("agent_efficiency_context_tokens", role="root", quantile="0.5") == 3000
    assert v("agent_efficiency_context_tokens", role="root", quantile="0.9") == 5000
    assert value(samples, "agent_efficiency_tracked_files") == 2
    assert value(samples, "agent_efficiency_baseline_timestamp_seconds") == pytest.approx(eff.baseline, abs=0.01)


def test_claude_subagent_is_worker_and_bash_sleep_is_wait(eff: Harness):
    b = eff.baseline + 500
    use = {"input_tokens": 10, "cache_read_input_tokens": 900, "cache_creation_input_tokens": 90, "output_tokens": 30}

    def assistant(ts: float, mid: str, content: list) -> str:
        return claude(
            ts, "assistant", {"id": mid, "model": "claude-test", "role": "assistant", "usage": use, "content": content}
        )

    def result(ts: float, tool_id: str, **extra: object) -> str:
        return claude_result(ts, tool_id, "x", **extra)

    eff.claude_file(
        "sess-1.jsonl",
        [
            claude(b, "user", {"role": "user", "content": "prompt"}),
            assistant(
                b + 2,
                "m1",
                [
                    {
                        "type": "tool_use",
                        "id": "tu1",
                        "name": "Agent",
                        "input": {"prompt": "p", "run_in_background": True},
                    }
                ],
            ),
            result(b + 3, "tu1", toolUseResult={"agentId": "a1", "isAsync": True}),
            assistant(b + 5, "m2", [{"type": "text", "text": "t"}]),
            assistant(
                b + 5, "m2", [{"type": "tool_use", "id": "tu2", "name": "Bash", "input": {"command": "sleep 30"}}]
            ),
            result(b + 35, "tu2"),
            assistant(
                b + 36,
                "m3",
                [{"type": "tool_use", "id": "tu3", "name": "TaskOutput", "input": {"task_id": "a1", "block": True}}],
            ),
            result(b + 96, "tu3"),
            assistant(b + 97, "m4", [{"type": "text", "text": "done"}]),
            claude(b + 98, "system", subtype="compact_boundary"),
            claude(b + 98, "user", {"role": "user", "content": "summary"}, isCompactSummary=True),
        ],
    )
    subagents = eff.claude_dir / "sess-1" / "subagents"
    subagents.mkdir(parents=True)
    (subagents / "agent-a1.jsonl").write_text(
        "".join(
            [
                claude(b + 10, "user", {"role": "user", "content": "task"}),
                assistant(
                    b + 12, "m9", [{"type": "tool_use", "id": "tu9", "name": "Bash", "input": {"command": "ls -la"}}]
                ),
                result(b + 13, "tu9"),
            ]
        ),
        encoding="utf-8",
    )
    samples = eff.collect(eff.now)
    v = lambda name, **labels: value(samples, name, agent="claude", namespace=CLAUDE, **labels)
    assert v("agent_efficiency_llm_calls_total", role="solo", trigger="user") == 1
    assert v("agent_efficiency_llm_calls_total", role="root", trigger="orchestrate") == 1
    assert v("agent_efficiency_llm_calls_total", role="root", trigger="wait") == 2
    assert v("agent_efficiency_llm_calls_total", role="worker", trigger="user") == 1
    assert v("agent_efficiency_input_tokens_total", role="solo", trigger="user", cache="hit") == 900
    assert v("agent_efficiency_input_tokens_total", role="solo", trigger="user", cache="miss") == 100
    assert v("agent_efficiency_tool_calls_total", role="root", **{"class": "orchestrate"}) == 1
    assert v("agent_efficiency_tool_calls_total", role="root", **{"class": "wait"}) == 2
    assert v("agent_efficiency_tool_calls_total", role="worker", **{"class": "work"}) == 1
    assert v("agent_efficiency_poll_calls_total", role="root", target="sleep", result="timed_out") == 1
    assert v("agent_efficiency_poll_seconds_total", role="root", target="sleep", result="timed_out") == 30
    assert v("agent_efficiency_poll_calls_total", role="root", target="agent", result="timed_out") == 1
    assert v("agent_efficiency_poll_seconds_total", role="root", target="agent", result="timed_out") == 60
    assert v("agent_efficiency_time_seconds_total", role="root", state="tool_wait") == 90
    assert v("agent_efficiency_spawns_total") == 1
    assert v("agent_efficiency_compactions_total", role="root") == 1
    model_calls = v("agent_efficiency_model_calls_total", role="root", model="claude-test")
    assert model_calls + v("agent_efficiency_model_calls_total", role="solo", model="claude-test") == 4


def test_wait_agent_result_follows_timed_out_flag(eff: Harness):
    b = eff.baseline + 500
    rec = lambda ts: record(ts, None, 100, 90, 1)
    eff.codex_file(
        "rollout-wait-agent.jsonl",
        [
            codex_meta(b, "w"),
            *(wait_agent(b + 1, "a1"), waited(b + 2, "a1", True), rec(b + 3)),
            *(wait_agent(b + 4, "a2"), waited(b + 6, "a2", False), rec(b + 7)),
        ],
    )
    got = wait_results(eff.collect(eff.now))
    assert got["calls"] == {"wait": 1, "event": 1}
    assert got["polls"] == {("agent", "timed_out"): 1, ("agent", "event"): 1}
    assert got["requests"] == {"wait_agent": 2}
    assert got["ms"] == {"wait_agent": 2000}


def test_cell_wait_and_empty_write_stdin_result_follows_process_exit(eff: Harness):
    b = eff.baseline + 500
    rec = lambda ts: record(ts, None, 100, 90, 1)
    text = lambda body: [{"type": "input_text", "text": body}]
    cell_out = lambda ts, cid, body: codex(
        ts, "response_item", {"type": "custom_tool_call_output", "call_id": cid, "output": text(body)}
    )
    stdin = 'const r = await tools.write_stdin({session_id:9,chars:"",yield_time_ms:1000,max_output_tokens:100}); text(JSON.stringify(r));'
    cell_wait = lambda ts, cid: codex(
        ts,
        "response_item",
        {"type": "function_call", "name": "wait", "call_id": cid, "arguments": '{"cell_id":"3","yield_time_ms":5000}'},
    )
    eff.codex_file(
        "rollout-cell.jsonl",
        [
            codex_meta(b, "c"),
            exec_js(
                b + 1,
                "x1",
                'const r = await tools.exec_command({cmd: "just check", yield_time_ms: 1000}); text(JSON.stringify(r));',
            ),
            cell_out(b + 2, "x1", '{"session_id":9,"output":""}'),
            rec(b + 3),
            exec_js(b + 4, "x2", stdin),
            cell_out(b + 5, "x2", '{"session_id":9,"output":"still going"}'),
            rec(b + 6),
            exec_js(b + 7, "x3", stdin),
            cell_out(b + 9, "x3", '{"exit_code":0,"output":"ok"}'),
            rec(b + 10),
            cell_wait(b + 11, "x4"),
            fn_out(b + 12, "x4", '{"session_id":4,"output":""}'),
            rec(b + 13),
            cell_wait(b + 14, "x5"),
            fn_out(b + 15, "x5", "Process exited with code 1"),
            rec(b + 16),
            exec_js(b + 17, "x6", stdin),
            cell_out(b + 18, "x6", '{"session_id":9,"exit_code":0}'),
            rec(b + 19),
        ],
    )
    got = wait_results(eff.collect(eff.now))
    # x1 starts the gate process (still running); x2/x4 time out; x3/x5/x6 deliver an exit, x6 beside its session id.
    assert got["calls"] == {"wait": 2, "event": 3}
    assert got["polls"] == {
        ("gate", "timed_out"): 1,
        ("gate", "event"): 2,
        ("cell", "timed_out"): 1,
        ("cell", "event"): 1,
    }
    assert got["requests"] == {"write_stdin": 3, "cell_wait": 2}
    assert got["ms"] == {"write_stdin": 3000, "cell_wait": 10000}


def test_claude_monitor_task_output_and_notification_results(eff: Harness):
    b = eff.baseline + 500
    use = {"input_tokens": 1, "cache_read_input_tokens": 9, "output_tokens": 1}
    assistant = lambda ts, mid, tool=None: claude(
        ts, "assistant", {"id": mid, "model": "claude-test", "usage": use, "content": [tool] if tool else []}
    )
    result = lambda ts, tid, extra: claude_result(ts, tid, "x", toolUseResult=extra)
    task = lambda status: {
        "retrieval_status": "success" if status == "completed" else "timeout",
        "task": {"status": status, "task_id": "b1"},
    }
    output = lambda tid: {
        "type": "tool_use",
        "id": tid,
        "name": "TaskOutput",
        "input": {"task_id": "b1", "block": True},
    }
    eff.claude_file(
        "sess-wait.jsonl",
        [
            claude(b, "user", {"role": "user", "content": "go"}),
            assistant(
                b + 1,
                "n1",
                {
                    "type": "tool_use",
                    "id": "t1",
                    "name": "Monitor",
                    "input": {"command": "tail -f log", "timeout_ms": 600000},
                },
            ),
            result(b + 2, "t1", {"taskId": "b1", "persistent": False, "timeoutMs": 600000}),
            assistant(b + 3, "n2", output("t2")),
            result(b + 13, "t2", task("running")),
            assistant(b + 14, "n3", output("t3")),
            result(b + 20, "t3", task("completed")),
            assistant(b + 21, "n4"),
            claude(b + 30, "user", {"role": "user", "content": "<task-notification>\nfinished</task-notification>"}),
            assistant(b + 31, "n5"),
        ],
    )
    got = wait_results(eff.collect(eff.now))
    # n2 follows Monitor (event), n3 a running TaskOutput (wait), n4 a completed one (event), n5 a notification (event).
    assert got["calls"] == {"wait": 1, "event": 3}
    assert got["polls"] == {("other", "event"): 2, ("other", "timed_out"): 1}


def test_claude_time_between_turns_is_idle(eff: Harness):
    eff.baseline = eff.now - 2000
    b = eff.baseline + 500
    say = lambda ts, mid, stop: claude(
        ts,
        "assistant",
        {
            "id": mid,
            "model": "claude-test",
            "stop_reason": stop,
            "usage": {"input_tokens": 1, "output_tokens": 1},
            "content": [{"type": "text", "text": "t"}],
        },
    )
    # Thread A ends its turn with stop_reason end_turn; thread B only with a system turn_duration record.
    eff.claude_file(
        "sess-idle-a.jsonl",
        [
            claude(b, "user", {"role": "user", "content": "one"}),
            say(b + 5, "i1", "end_turn"),
            claude(b + 605, "user", {"role": "user", "content": "two"}),
            say(b + 609, "i2", "end_turn"),
        ],
    )
    eff.claude_file(
        "sess-idle-b.jsonl",
        [
            claude(b, "user", {"role": "user", "content": "one"}),
            claude_tool(b + 2, "j1", "k1", "Read", {"file_path": "/x"}),
            claude_result(b + 3, "k1", "x"),
            say(b + 5, "j2", None),
            claude(b + 6, "system", subtype="turn_duration", durationMs=6000),
            claude(b + 606, "user", {"role": "user", "content": "two"}),
            say(b + 610, "j3", None),
        ],
    )
    samples = eff.collect(eff.now)
    v = lambda state: value(
        samples, "agent_efficiency_time_seconds_total", agent="claude", namespace=CLAUDE, role="solo", state=state
    )
    assert v("idle") == pytest.approx(600 + 600, abs=0.01)
    assert v("model") == pytest.approx(5 + 4 + 2 + 2 + 1 + 4, abs=0.01)
    assert v("tool_work") == pytest.approx(1, abs=0.01)


def test_codex_token_count_not_counted_beside_usage_record_in_same_turn(eff: Harness):
    b = eff.baseline + 500
    count = lambda ts, total, tokens: codex(
        ts,
        "event_msg",
        {
            "type": "token_count",
            "info": {"total_token_usage": {"total_tokens": total}, "last_token_usage": usage(tokens, 0, 1)},
        },
    )
    start = lambda ts: [
        codex(ts, "event_msg", {"type": "task_started"}),
        codex(ts, "event_msg", {"type": "user_message", "message": "go"}),
    ]
    eff.codex_file(
        "rollout-dup.jsonl",
        [
            codex_meta(b, "dup"),
            *start(b + 1),
            count(b + 2, 1001, 1000),
            record(b + 2, None, 1000, 0, 1),
            codex(b + 3, "event_msg", {"type": "task_complete"}),
        ],
    )
    eff.codex_file(
        "rollout-counts-only.jsonl",
        [
            codex_meta(b, "counts"),
            *start(b + 1),
            count(b + 2, 501, 500),
            count(b + 3, 501, 500),
            count(b + 4, 1102, 600),
            codex(b + 5, "event_msg", {"type": "task_complete"}),
        ],
    )
    samples = eff.collect(eff.now)
    # rollout-dup: one call reacting to the user; rollout-counts-only: two distinct token_count totals.
    assert select(samples, "agent_efficiency_llm_calls_total", agent="codex", namespace=CODEX, role="solo") == {
        key(trigger="user"): 2,
        key(trigger="model"): 1,
    }
    tokens = select(
        samples, "agent_efficiency_input_tokens_total", agent="codex", namespace=CODEX, role="solo", cache="miss"
    )
    assert tokens == {key(trigger="user"): 1500, key(trigger="model"): 600}


def test_events_before_baseline_rebuild_state_but_are_not_counted(eff: Harness):
    now = eff.now
    eff.baseline = now
    path = eff.codex_file(
        "rollout-old.jsonl",
        [
            codex_meta(now - 500, "old-thread"),
            codex(now - 500, "turn_context", {"model": "gpt-test"}),
            record(now - 450),
            codex(
                now - 420,
                "response_item",
                {"type": "function_call", "name": "spawn_agent", "arguments": "{}", "call_id": "s1"},
            ),
            codex(now - 410, "response_item", {"type": "function_call_output", "call_id": "s1", "output": "ok"}),
            record(now - 400),
        ],
    )
    stale = eff.codex_file(
        "rollout-stale.jsonl", [codex_meta(now - 5 * 86400, "stale"), record(now - 5 * 86400, None, 1, 0, 1)]
    )
    import os

    os.utime(stale, (now - 4 * 86400, now - 4 * 86400))
    samples = eff.collect(now)
    assert [k for k in samples if k[0].endswith("_total") and samples[k]] == [], "pre-baseline events were counted"
    assert value(samples, "agent_efficiency_tracked_files") == 1
    with path.open("a", encoding="utf-8") as handle:
        handle.write(record(now - 100))
        handle.write(record(now + 5))
    samples = eff.collect(now + 10)
    v = lambda name, **labels: value(samples, name, agent="codex", namespace=CODEX, **labels)
    # The thread became a root before the baseline, so its one counted call is a root call.
    assert v("agent_efficiency_llm_calls_total", role="root", trigger="model") == 1
    assert v("agent_efficiency_llm_calls_total", role="solo", trigger="model") == 0
    # Transcript timestamps carry millisecond precision; the baseline does not.
    assert v("agent_efficiency_time_seconds_total", role="root", state="model") == pytest.approx(5, abs=0.01)


def test_context_fill_is_codex_window_only_and_context_tokens_cover_claude(eff: Harness):
    """Codex records its window; Claude fill comes only from the public model table, never the transcript."""
    b = eff.now - 300
    eff.codex_file(
        "rollout-fill.jsonl",
        [
            codex_meta(b, "fill"),
            codex(b + 1, "event_msg", {"type": "task_started", "model_context_window": 100000}),
            *(record(b + 2, None, 10000, 0, 1), record(b + 3, None, 50000, 0, 1), record(b + 4, None, 90000, 0, 1)),
        ],
    )
    use = lambda tokens: {"input_tokens": 10, "cache_read_input_tokens": tokens - 10, "output_tokens": 1}
    eff.claude_file(
        "sess-fill.jsonl",
        [
            claude(b, "user", {"role": "user", "content": "go"}),
            claude(b + 1, "assistant", {"id": "f1", "model": "claude-test[1m]", "usage": use(100000), "content": []}),
            claude(b + 2, "assistant", {"id": "f2", "model": "claude-test", "usage": use(100000), "content": []}),
        ],
    )
    samples = eff.collect(eff.now)
    v = lambda q: value(
        samples, "agent_efficiency_context_fill_ratio", agent="codex", namespace=CODEX, role="solo", quantile=q
    )
    assert v("0.5") == pytest.approx(0.5)
    assert v("0.9") == pytest.approx(0.9)
    # An unknown Claude model has no public window (tests/test_context_windows.py covers the known ones).
    assert select(samples, "agent_efficiency_context_fill_ratio", agent="claude") == {}
    assert (
        "agent_efficiency_context_tokens",
        key(agent="claude", namespace=CLAUDE, role="solo", quantile="0.5", loop="none"),
    ) in samples


# -- spawns ------------------------------------------------------------------------------------


def test_spawns_by_route_and_first_spawn_histogram(eff: Harness):
    b = eff.baseline + 500
    ok = lambda ts, cid: fn_out(ts, cid, '{"task_name":"/root/x"}')
    eff.codex_file(
        "rollout-route.jsonl",
        [
            codex_meta(b, "route-root"),
            *(spawn_fn(b + 20, "s1", model="gpt-6-sol", reasoning_effort="high", fork_turns="none"), ok(b + 21, "s1")),
            *(spawn_fn(b + 22, "s2", agent_type="poller"), ok(b + 23, "s2")),
            *(spawn_fn(b + 24, "s3", model="gpt-6-luna", reasoning_effort="max", fork_turns="all"), ok(b + 25, "s3")),
            *(spawn_fn(b + 26, "s4", model="gpt-6-sol", reasoning_effort="minimal", fork_turns="3"), ok(b + 27, "s4")),
        ],
    )
    eff.claude_file(
        "sess-route.jsonl",
        [
            claude(b, "user", {"role": "user", "content": "go"}),
            claude_tool(b + 400, "r1", "a1", "Agent", {"prompt": "p", "subagent_type": "agent-workflows:reviewer"}),
            claude_result(b + 401, "a1", "ok"),
            claude_tool(b + 402, "r2", "a2", "Agent", {"prompt": "p"}),
            claude_result(b + 403, "a2", "ok"),
            claude_tool(b + 404, "r3", "a3", "Task", {"prompt": "p", "subagent_type": "claude"}),
            claude_result(b + 405, "a3", "ok"),
            claude_tool(
                b + 406,
                "r4",
                "a4",
                "Agent",
                {"prompt": "p", "subagent_type": "agent-workflows:reviewer", "model": "sonnet"},
            ),
            claude_result(b + 407, "a4", "ok"),
            claude_tool(b + 408, "r5", "a5", "Agent", {"prompt": "p", "model": "haiku"}),
            claude_result(b + 409, "a5", "ok"),
        ],
    )
    samples = eff.collect(eff.now)
    assert select(samples, "agent_efficiency_spawns_by_route_total", agent="codex", namespace=CODEX, role="root") == {
        key(spawn_model="gpt-6-sol", effort="high", agent_type="default", fork="none"): 1,
        key(spawn_model="inherit", effort="inherit", agent_type="poller", fork="none"): 1,
        key(spawn_model="gpt-6-luna", effort="max", agent_type="default", fork="all"): 1,
        key(spawn_model="gpt-6-sol", effort="other", agent_type="default", fork="other"): 1,
    }
    assert select(samples, "agent_efficiency_spawns_by_route_total", agent="claude", namespace=CLAUDE, role="root") == {
        key(spawn_model="opus", effort="inherit", agent_type="reviewer", fork="none"): 1,
        key(spawn_model="inherit", effort="inherit", agent_type="default", fork="none"): 1,
        key(spawn_model="inherit", effort="inherit", agent_type="other", fork="none"): 1,
        key(spawn_model="sonnet", effort="inherit", agent_type="reviewer", fork="none"): 1,
        key(spawn_model="haiku", effort="inherit", agent_type="default", fork="none"): 1,
    }
    hist = lambda agent, namespace: {
        dict(labels)["le"]: v
        for labels, v in select(
            samples, "agent_efficiency_first_spawn_seconds_bucket", agent=agent, namespace=namespace
        ).items()
    }
    assert hist("codex", CODEX) == {"60": 1, "300": 1, "900": 1, "1800": 1, "3600": 1, "+Inf": 1}
    assert hist("claude", CLAUDE) == {"60": 0, "300": 0, "900": 1, "1800": 1, "3600": 1, "+Inf": 1}
    assert value(samples, "agent_efficiency_first_spawn_seconds_sum", agent="codex", namespace=CODEX) == 20
    assert value(samples, "agent_efficiency_first_spawn_seconds_count", agent="claude", namespace=CLAUDE) == 1


def test_spawn_errors_one_per_kind_and_failed_spawns_register_nothing(eff: Harness):
    b = eff.baseline + 500
    outputs = {
        "thread_limit": "collab spawn failed: agent thread limit reached",
        "unknown_model": "Unknown model `gpt-x` for spawn_agent. Available models: gpt-a, gpt-b",
        "path_exists": "agent path `/root/w1` already exists",
        "fork_type": "Full-history forked agents inherit the parent agent type; omit agent_type, or spawn without a full-history fork.",
        "parse": "failed to parse function arguments: missing field `task_name` at line 1 column 2",
        "other": "collab spawn failed: something unexpected",
    }
    lines = [codex_meta(b, "errs")]
    for index, text in enumerate(outputs.values()):
        lines += [
            spawn_fn(b + 2 * index + 1, f"e{index}", model="gpt-novel-failed", agent_type="novel-failed-type"),
            fn_out(b + 2 * index + 2, f"e{index}", text),
        ]
    lines += [spawn_fn(b + 20, "ok", model="gpt-6-sol"), fn_out(b + 21, "ok", '{"task_name":"/root/fine"}')]
    eff.codex_file("rollout-errs.jsonl", lines)
    eff.claude_file(
        "sess-errs.jsonl",
        [
            claude(b, "user", {"role": "user", "content": "go"}),
            claude_tool(b + 1, "q1", "g1", "Agent", {"prompt": "p", "model": "claude-novel-failed"}),
            claude_result(b + 2, "g1", "Agent type not found", is_error=True),
        ],
    )
    samples = eff.collect(eff.now)
    assert select(samples, "agent_efficiency_spawn_errors_total", agent="codex", namespace=CODEX) == {
        key(kind=k): 1 for k in outputs
    }
    assert value(samples, "agent_efficiency_spawns_total", agent="codex", namespace=CODEX) == 7
    # Only the successful spawn has a route; failed spawns never take a registry slot.
    assert select(samples, "agent_efficiency_spawns_by_route_total", agent="codex", namespace=CODEX, role="root") == {
        key(spawn_model="gpt-6-sol", effort="inherit", agent_type="default", fork="none"): 1
    }
    assert select(samples, "agent_efficiency_spawns_by_route_total", agent="claude") == {}
    state = eff.saved()
    assert "gpt-novel-failed" not in state["models"]
    assert "claude-novel-failed" not in state["models"]
    assert "novel-failed-type" not in state["agent_types"]


def test_code_mode_spawns(eff: Harness):
    b = eff.baseline + 500
    eff.codex_file(
        "rollout-cell-spawn.jsonl",
        [
            codex_meta(b, "cells"),
            *(exec_js(b + 10, "k0", "text(typeof tools.spawn_agent)"), js_out(b + 11, "k0", "function")),
            exec_js(
                b + 20,
                "k1",
                'const r = await tools.spawn_agent({task_name:"a", model:"gpt-6-sol", reasoning_effort:"high", '
                'fork_turns:"none", message:`do {this}`}); text(JSON.stringify(r));',
            ),
            js_out(b + 21, "k1", {"task_name": "/root/a"}),
            exec_js(
                b + 22,
                "k2",
                'const r = await tools.collaboration__spawn_agent({task_name:"b", model:"gpt-novel-cell", agent_type:"novel-cell-type"}); text(r);',
            ),
            js_out(b + 23, "k2", "Error: collab spawn failed: agent thread limit reached", status="Script failed"),
            exec_js(
                b + 24,
                "k3",
                "for (const [task_name, model] of rows) { await tools.spawn_agent({task_name, model, message}); }",
            ),
            js_out(b + 25, "k3", {"task_name": "/root/c"}),
            exec_js(
                b + 26, "k4", 'await tools.spawn_agent({task_name:"d", model:"gpt-6-luna", message:"m"}); text("ok")'
            ),
            js_out(b + 27, "k4", "Error: something else", status="Script failed"),
            # The tool is not exposed in code mode, so nothing was spawned.
            exec_js(b + 28, "k5", 'const r = await tools.spawn_agent({task_name:"x", model:"gpt-6-sol"}); text(r);'),
            js_out(
                b + 29,
                "k5",
                "Script error: TypeError: tools.spawn_agent is not a function\n    at exec_main.mjs:1:30",
                status="Script failed",
            ),
            *(spawn_fn(b + 30, "f1", model="gpt-6-luna"), fn_out(b + 31, "f1", '{"task_name":"/root/e"}')),
        ],
    )
    samples = eff.collect(eff.now)
    fixed = {"agent": "codex", "namespace": CODEX}
    assert value(samples, "agent_efficiency_spawns_total", **fixed) == 5
    assert select(samples, "agent_efficiency_spawns_by_route_total", role="root", **fixed) == {
        key(spawn_model="gpt-6-sol", effort="high", agent_type="default", fork="none"): 1,
        key(spawn_model="gpt-6-luna", effort="inherit", agent_type="default", fork="none"): 1,
    }
    assert select(samples, "agent_efficiency_spawn_errors_total", **fixed) == {key(kind="thread_limit"): 1}
    # A cell's spawns count when its result arrives: meta at b, first cell result at b + 21.
    assert value(samples, "agent_efficiency_first_spawn_seconds_sum", **fixed) == 21
    state = eff.saved()
    assert "gpt-novel-cell" not in state["models"]
    assert "novel-cell-type" not in state["agent_types"]


# -- delivery outcomes and failures --------------------------------------------------------------


def test_git_push_outcomes_ignore_mentions(eff: Harness):
    b = eff.baseline + 500
    eff.codex_file(
        "rollout-push.jsonl",
        [
            codex_meta(b, "push"),
            *(exec_fn(b + 1, "p1", "git push origin main"), fn_out(b + 2, "p1", exited(0, "ok"))),
            exec_js(
                b + 3,
                "p2",
                'const r = await tools.exec_command({cmd: "cd repo && git -C . push", yield_time_ms: 1000}); text(JSON.stringify(r));',
            ),
            js_out(
                b + 4,
                "p2",
                {
                    "chunk_id": "c",
                    "wall_time_seconds": 1,
                    "exit_code": 1,
                    "original_token_count": 3,
                    "output": "rejected",
                },
            ),
            *(
                exec_fn(b + 5, "p3", "cat > notes.md <<'EOF'\ngit push origin main\nEOF"),
                fn_out(b + 6, "p3", exited(0)),
            ),
            *(
                exec_fn(b + 7, "p4", 'echo "git push origin main" && git commit -m "then; git push"'),
                fn_out(b + 8, "p4", exited(0)),
            ),
            *(
                exec_fn(b + 9, "p5", "git push --force-with-lease"),
                fn_out(b + 10, "p5", running(41, "Enumerating objects")),
            ),
            *(stdin_fn(b + 40, "p6", 41), fn_out(b + 41, "p6", exited(0, "done"))),
        ],
    )
    eff.claude_file(
        "sess-push.jsonl",
        [
            claude(b, "user", {"role": "user", "content": "go"}),
            claude_tool(b + 1, "m1", "t1", "Bash", {"command": "git add -A; git push"}),
            claude_result(b + 2, "t1", "Exit code 1\n! [rejected]", is_error=True),
        ],
    )
    samples = eff.collect(eff.now)
    assert select(samples, "agent_efficiency_git_pushes_total", agent="codex", namespace=CODEX, role="solo") == {
        key(outcome="success"): 2,
        key(outcome="failure"): 1,
    }
    assert select(samples, "agent_efficiency_git_pushes_total", agent="claude", namespace=CLAUDE, role="solo") == {
        key(outcome="failure"): 1
    }


def test_ci_wait_outcomes(eff: Harness):
    b = eff.baseline + 500
    eff.codex_file(
        "rollout-ci.jsonl",
        [
            codex_meta(b, "ci"),
            *(exec_fn(b + 1, "c1", "gh run watch 123 --exit-status"), fn_out(b + 2, "c1", running(7))),
            *(stdin_fn(b + 30, "c2", 7), fn_out(b + 31, "c2", exited(0, "Run CI (123) completed with 'success'"))),
            *(exec_fn(b + 32, "c3", "gh pr checks 5 --watch"), fn_out(b + 33, "c3", exited(1, "X build  fail"))),
            *(
                exec_fn(b + 34, "c4", "timeout 600 gh run watch 9 --exit-status"),
                fn_out(b + 35, "c4", exited(1, "X Run CI (9) completed with 'cancelled'")),
            ),
            *(exec_fn(b + 36, "c5", "gh run watch 7"), fn_out(b + 37, "c5", exited(0))),
        ],
    )
    samples = eff.collect(eff.now)
    assert select(samples, "agent_efficiency_ci_waits_total", agent="codex", namespace=CODEX, role="solo") == {
        key(outcome=o): 1 for o in ("success", "failure", "cancelled")
    }


def test_gate_run_outcomes(eff: Harness):
    b = eff.baseline + 500
    eff.codex_file(
        "rollout-gate.jsonl",
        [
            codex_meta(b, "gate"),
            *(exec_fn(b + 1, "g1", "just check"), fn_out(b + 2, "g1", exited(0))),
            *(exec_fn(b + 3, "g2", "cd app && make -C src test"), fn_out(b + 4, "g2", exited(2, "FAIL"))),
            *(exec_fn(b + 5, "g3", "just fmt"), fn_out(b + 6, "g3", exited(0))),
        ],
    )
    eff.claude_file(
        "sess-gate.jsonl",
        [
            claude(b, "user", {"role": "user", "content": "go"}),
            claude_tool(b + 1, "m1", "t1", "Bash", {"command": "just test"}),
            claude_result(b + 2, "t1", "all green"),
        ],
    )
    samples = eff.collect(eff.now)
    assert select(samples, "agent_efficiency_gate_runs_total", agent="codex", namespace=CODEX, role="solo") == {
        key(outcome="success"): 1,
        key(outcome="failure"): 1,
    }
    assert select(samples, "agent_efficiency_gate_runs_total", agent="claude", namespace=CLAUDE, role="solo") == {
        key(outcome="success"): 1
    }


def test_coderabbit_findings_and_review_outcomes(eff: Harness):
    b = eff.baseline + 500
    first = "\n".join(
        [
            cr_line("status", phase="setup", status="setting_up"),
            cr_line("finding", severity="major", fileName="a.py", codegenInstructions="handle it"),
        ]
    )
    second = "\n".join(
        [
            cr_line("finding", severity="minor", fileName="b.py"),
            cr_line("finding", severity="minor", fileName="c.py"),
            cr_line("complete", status="review_completed", findings=3),
        ]
    )
    limited = cr_line("error", errorType="rate_limit", message="Rate limit exceeded", recoverable=False)
    eff.codex_file(
        "rollout-cr.jsonl",
        [
            codex_meta(b, "cr"),
            *(exec_fn(b + 1, "r1", "coderabbit review --agent --base main"), fn_out(b + 2, "r1", running(50, first))),
            *(stdin_fn(b + 60, "r2", 50), fn_out(b + 61, "r2", exited(0, second))),
            exec_js(
                b + 62,
                "r3",
                'const r = await tools.exec_command({cmd: "coderabbit review --agent", yield_time_ms: 30000}); text(JSON.stringify(r));',
            ),
            js_out(
                b + 63,
                "r3",
                {"chunk_id": "d", "wall_time_seconds": 1, "exit_code": 1, "original_token_count": 9, "output": limited},
            ),
            *(
                exec_fn(b + 64, "r4", "coderabbit review --agent"),
                fn_out(b + 65, "r4", exited(1, cr_line("error", errorType="connection", message="socket hang up"))),
            ),
            # A cell that prints only the output shows neither an exit nor a process: the review is not judged.
            exec_js(
                b + 66,
                "r5",
                'const r = await tools.exec_command({cmd: "coderabbit review --agent", yield_time_ms: 5000}); text(r.output);',
            ),
            js_out(b + 67, "r5", cr_line("status", phase="analyzing", status="reviewing")),
        ],
    )
    samples = eff.collect(eff.now)
    fixed = {"agent": "codex", "namespace": CODEX, "role": "solo"}
    assert select(samples, "agent_efficiency_coderabbit_findings_total", **fixed) == {
        key(severity="major"): 1,
        key(severity="minor"): 2,
    }
    assert select(samples, "agent_efficiency_coderabbit_reviews_total", **fixed) == {
        key(outcome=o): 1 for o in ("complete", "rate_limited", "failed")
    }


def test_tool_failures(eff: Harness):
    b = eff.baseline + 500
    eff.codex_file(
        "rollout-fail.jsonl",
        [
            codex_meta(b, "fail"),
            exec_js(b + 1, "f1", 'const r = await tools.exec_command({cmd: "ls -la /some/where/far/away"}); text(r);'),
            js_out(b + 2, "f1", "Error: boom", status="Script failed"),
            exec_js(b + 3, "f2", 'const r = await tools.exec_command({cmd: "ls -la /some/where/far/away"}); text(r);'),
            js_out(b + 4, "f2", {"chunk_id": "e", "exit_code": 0, "output": "exit_code: 1 is printed by the program"}),
            exec_fn(b + 5, "f3", "gh run list --limit 5 --json status,conclusion,databaseId --repo owner/name"),
            fn_out(b + 6, "f3", exited(4)),
        ],
    )
    eff.claude_file(
        "sess-fail.jsonl",
        [
            claude(b, "user", {"role": "user", "content": "go"}),
            claude_tool(b + 1, "m1", "t1", "Read", {"file_path": "/nope"}),
            claude_result(b + 2, "t1", "File does not exist.", is_error=True),
            claude_tool(b + 3, "m2", "t2", "Read", {"file_path": "/yes"}),
            claude_result(b + 4, "t2", "contents"),
        ],
    )
    samples = eff.collect(eff.now)
    assert select(samples, "agent_efficiency_tool_failures_total", agent="codex", namespace=CODEX, role="solo") == {
        key(**{"class": "work"}): 1,
        key(**{"class": "status"}): 1,
    }
    assert select(samples, "agent_efficiency_tool_failures_total", agent="claude", namespace=CLAUDE, role="solo") == {
        key(**{"class": "work"}): 1
    }


# -- humans and the loop protocol ------------------------------------------------------------------


def test_interventions(eff: Harness):
    b = eff.baseline + 500
    eff.codex_file(
        "rollout-human.jsonl",
        [
            codex_meta(b, "human"),
            codex(b + 1, "event_msg", {"type": "task_started", "model_context_window": 1000}),
            codex(b + 1, "event_msg", {"type": "user_message", "message": "first prompt"}),
            codex(b + 5, "event_msg", {"type": "user_message", "message": "steer while running"}),
            codex(b + 9, "event_msg", {"type": "turn_aborted", "reason": "interrupted"}),
            codex(b + 20, "event_msg", {"type": "task_started", "model_context_window": 1000}),
            codex(b + 30, "event_msg", {"type": "turn_aborted", "reason": "replaced"}),
        ],
    )
    eff.codex_file(
        "rollout-human-worker.jsonl",
        [
            codex_meta(b, "human-worker", parent="human"),
            codex(b + 1, "event_msg", {"type": "user_message", "message": "task"}),
            codex(b + 2, "event_msg", {"type": "user_message", "message": "parent follow-up"}),
        ],
    )
    eff.claude_file(
        "sess-human.jsonl",
        [
            claude(b, "user", {"role": "user", "content": "first"}, origin={"kind": "human"}),
            claude(
                b + 1,
                "assistant",
                {"id": "h1", "model": "claude-test", "usage": {"input_tokens": 1, "output_tokens": 1}, "content": []},
            ),
            claude(
                b + 2,
                "user",
                {"role": "user", "content": "<task-notification>\ndone</task-notification>"},
                origin={"kind": "task-notification"},
            ),
            claude(b + 3, "user", {"role": "user", "content": "caveat"}, isMeta=True),
            claude(b + 4, "user", {"role": "user", "content": "second"}, origin={"kind": "human"}),
            claude(b + 5, "user", {"role": "user", "content": "legacy third without origin"}),
            claude(
                b + 6, "user", {"role": "user", "content": [{"type": "text", "text": "[Request interrupted by user]"}]}
            ),
        ],
    )
    samples = eff.collect(eff.now)
    assert select(samples, "agent_efficiency_interventions_total", agent="codex", namespace=CODEX, role="solo") == {
        key(kind="user_message"): 1,
        key(kind="interrupt"): 1,
    }
    assert select(samples, "agent_efficiency_interventions_total", agent="codex", namespace=CODEX, role="worker") == {}
    assert select(samples, "agent_efficiency_interventions_total", agent="claude", namespace=CLAUDE, role="solo") == {
        key(kind="user_message"): 2,
        key(kind="interrupt"): 1,
    }


def test_codex_response_item_human_messages(eff: Harness):
    b = eff.baseline + 500
    eff.codex_file(
        "rollout-items.jsonl",
        [
            codex_meta(b, "items"),
            codex(b + 1, "event_msg", {"type": "task_started"}),
            user_item(
                b + 1,
                "# AGENTS.md instructions for /repo\n\n<INSTRUCTIONS>x</INSTRUCTIONS>",
                "<environment_context>\n  <cwd>/repo</cwd>\n</environment_context>",
                "first prompt",
            ),
            record(b + 2),  # trigger user (first prompt)
            codex(b + 3, "event_msg", {"type": "task_complete"}),
            codex(b + 10, "event_msg", {"type": "task_started"}),
            user_item(b + 10, "a follow-up typed by the human"),  # intervention 1
            codex(
                b + 10, "event_msg", {"type": "user_message", "message": "a follow-up typed by the human"}
            ),  # same message
            record(b + 11),  # trigger user
            user_item(b + 12, "<environment_context>\n  <cwd>/other</cwd>\n</environment_context>"),
            user_item(b + 12, "<recommended_plugins>\nx\n</recommended_plugins>"),
            user_item(b + 12, "<codex_internal_context>x</codex_internal_context>"),
            user_item(b + 12, "<skill>\n<name>x</name>\n</skill>"),
            user_item(b + 12, "<turn_aborted>\nThe user interrupted.\n</turn_aborted>"),
            user_item(b + 12, "<user_instructions>x</user_instructions>"),
            user_item(b + 12, "<unknown_injected_block>x</unknown_injected_block>"),
            user_item(b + 12, "Another language model started to solve this problem and produced a summary"),
            record(b + 13),  # trigger model: nothing human since
            codex(b + 14, "event_msg", {"type": "user_message", "message": "steer"}),  # intervention 2 (event first)
            user_item(b + 14, "steer"),  # same message
            record(b + 15),  # trigger user
            user_item(b + 16, "<image>", "[Image #1] what is this", image=True),  # intervention 3
            user_item(b + 16.5, "and a second message straight after"),  # intervention 4: a new item, not a duplicate
            record(b + 17),  # trigger user
            user_item(
                b + 18, "<send_user_message_question_reply>yes</send_user_message_question_reply>"
            ),  # intervention 5
            record(b + 19),  # trigger user
        ],
    )
    eff.codex_file(
        "rollout-items-worker.jsonl",
        [
            codex_meta(b, "items-worker", parent="items"),
            *(user_item(b + 1, "task from the parent"), record(b + 2)),
            *(
                user_item(b + 3, "follow-up from the parent"),
                record(b + 4),
            ),  # workers: no intervention, trigger unchanged
        ],
    )
    samples = eff.collect(eff.now)
    v = lambda role: select(samples, "agent_efficiency_interventions_total", agent="codex", namespace=CODEX, role=role)
    assert v("solo") == {key(kind="user_message"): 5}
    assert v("worker") == {}
    by_role = lambda role: select(
        samples, "agent_efficiency_llm_calls_total", agent="codex", namespace=CODEX, role=role
    )
    assert by_role("solo") == {key(trigger="user"): 5, key(trigger="model"): 1}
    assert by_role("worker") == {key(trigger="user"): 1, key(trigger="model"): 1}


def test_protocol_label_comes_from_the_first_human_prompt_only(eff: Harness):
    b = eff.baseline + 500
    eff.codex_file(
        "rollout-proto-21.jsonl",
        [
            codex_meta(b, "p21"),
            codex(b + 1, "event_msg", {"type": "task_started"}),
            user_item(
                b + 1,
                "# AGENTS.md instructions for /repo\n\nContract: loop-v2.0",
                "Run the campaign.\nContract: `loop-v2.1`",
            ),
            record(b + 2),  # poll=false (reacts to the prompt)
            *(spawn_fn(b + 3, "s1"), fn_out(b + 4, "s1", '{"task_name":"/root/a"}')),
            record(b + 5),  # poll=false (orchestrate)
            *(wait_agent(b + 6, "w1"), waited(b + 16, "w1", True), record(b + 17)),  # poll=true
            *(wait_agent(b + 18, "w2"), waited(b + 20, "w2", False), record(b + 21)),  # poll=false: an event wake
        ],
    )
    eff.codex_file(
        "rollout-proto-none.jsonl",
        [
            codex_meta(b, "pnone"),
            codex(b + 1, "event_msg", {"type": "task_started"}),
            user_item(b + 1, "Read the protocol doc and summarise it."),
            record(b + 2),
            *(
                exec_fn(b + 3, "d1", "cat fan-out-protocol.md"),
                fn_out(b + 4, "d1", exited(0, "Example header:\nContract: loop-v2.0\n")),
            ),
            record(b + 5),
            codex(b + 6, "event_msg", {"type": "task_complete"}),
            codex(b + 7, "event_msg", {"type": "task_started"}),
            user_item(b + 7, "Now start it. Contract: loop-v2.0"),  # a later prompt never sets the protocol
            record(b + 8),
        ],
    )
    eff.codex_file(
        "rollout-proto-worker.jsonl",
        [codex_meta(b, "pw", parent="p21"), user_item(b + 1, "Contract: loop-v2.1 lane"), record(b + 2)],
    )
    assistant = lambda ts, mid: claude(
        ts,
        "assistant",
        {"id": mid, "model": "claude-test", "usage": {"input_tokens": 1, "output_tokens": 1}, "content": []},
    )
    eff.claude_file(
        "sess-proto-20.jsonl",
        [
            claude(b, "user", {"role": "user", "content": "go\nContract: loop-v2"}, origin={"kind": "human"}),
            assistant(b + 3, "c1"),
        ],
    )
    eff.claude_file(
        "sess-proto-other.jsonl",
        [claude(b, "user", {"role": "user", "content": "Contract:  loop-v3.1"}), assistant(b + 3, "c2")],
    )
    samples = eff.collect(eff.now)
    by_protocol = lambda agent: select(samples, "agent_efficiency_root_llm_calls_by_protocol_total", agent=agent)
    proto = lambda namespace, protocol, poll: key(namespace=namespace, protocol=protocol, poll=poll)
    assert by_protocol("codex") == {
        proto(CODEX, "v2.1", "false"): 3,
        proto(CODEX, "v2.1", "true"): 1,
        proto(CODEX, "none", "false"): 3,
    }
    assert by_protocol("claude") == {proto(CLAUDE, "v2.0", "false"): 1, proto(CLAUDE, "other", "false"): 1}
    time_by = select(
        samples, "agent_efficiency_root_time_seconds_by_protocol_total", agent="codex", namespace=CODEX, protocol="v2.1"
    )
    assert time_by == {key(state="model"): 7, key(state="tool_orchestrate"): 1, key(state="tool_wait"): 12}
    claude_time = value(
        samples,
        "agent_efficiency_root_time_seconds_by_protocol_total",
        agent="claude",
        namespace=CLAUDE,
        protocol="v2.0",
        state="model",
    )
    assert claude_time == 3
    protocols = {Path(name).name: entry["proto"] for name, entry in eff.saved()["files"].items()}
    assert protocols == {
        "rollout-proto-21.jsonl": "v2.1",
        "rollout-proto-none.jsonl": "none",
        "rollout-proto-worker.jsonl": None,
        "sess-proto-20.jsonl": "v2.0",
        "sess-proto-other.jsonl": "other",
    }


# -- stalled loop roots, lanes and quota -------------------------------------------------------------


def test_stalled_loop_root_without_lanes(eff: Harness):
    now = eff.now
    eff.baseline = now - 7200
    root = eff.codex_file(
        "rollout-stall-root.jsonl",
        [
            codex_meta(now - 3600, "stall-root"),
            codex(now - 3590, "event_msg", {"type": "task_started"}),
            user_item(now - 3590, "Contract: loop-v2.1"),
            record(now - 3580, "stall-root"),
            *(spawn_fn(now - 3000, "s1"), fn_out(now - 2999, "s1", '{"task_name":"/root/lane"}')),
            record(now - 1600, "stall-root"),
            record(now - 60, "stall-root"),  # still working, in task
        ],
    )
    eff.codex_file(
        "rollout-stall-lane.jsonl",
        [
            codex_meta(now - 2995, "stall-lane", parent="stall-root"),
            codex(now - 2995, "event_msg", {"type": "task_started"}),
            record(now - 2000, "stall-lane"),
            codex(now - 1500, "event_msg", {"type": "task_complete"}),  # lane ends 25 min ago
        ],
    )
    # Not live: a spawning root whose last event is 30 minutes old; and a solo thread.
    eff.codex_file(
        "rollout-stall-idle.jsonl",
        [
            codex_meta(now - 5000, "idle-root"),
            codex(now - 5000, "event_msg", {"type": "task_started"}),
            user_item(now - 5000, "Contract: loop-v2.1"),
            *(spawn_fn(now - 4000, "s9"), fn_out(now - 3999, "s9", '{"task_name":"/root/x"}')),
            record(now - 1800, "idle-root"),
        ],
    )
    eff.codex_file("rollout-stall-solo.jsonl", [codex_meta(now - 600, "solo"), record(now - 30, "solo")])
    # The same history in a thread with no loop contract (an interactive session) is not a loop root.
    other = eff.hot / "codex-other" / "sessions" / "2026" / "09" / "25"
    other.mkdir(parents=True)
    (other / "rollout-plain-root.jsonl").write_text(
        "".join(
            [
                codex_meta(now - 3600, "plain-root"),
                codex(now - 3590, "event_msg", {"type": "task_started"}),
                user_item(now - 3590, "Please refactor this module."),
                record(now - 3580, "plain-root"),
                *(spawn_fn(now - 3000, "s1"), fn_out(now - 2999, "s1", '{"task_name":"/root/lane"}')),
                record(now - 1600, "plain-root"),
                record(now - 60, "plain-root"),
            ]
        ),
        encoding="utf-8",
    )
    (other / "rollout-plain-lane.jsonl").write_text(
        "".join(
            [
                codex_meta(now - 2995, "plain-lane", parent="plain-root"),
                codex(now - 2995, "event_msg", {"type": "task_started"}),
                record(now - 2000, "plain-lane"),
                codex(now - 1500, "event_msg", {"type": "task_complete"}),
            ]
        ),
        encoding="utf-8",
    )
    v = lambda samples, name: value(samples, name, agent="codex", namespace=CODEX)
    samples = eff.collect(now)
    assert v(samples, "agent_efficiency_roots_without_lanes") == 1
    assert v(samples, "agent_efficiency_root_no_lane_seconds") == pytest.approx(1500, abs=0.01)
    plain = lambda name: samples[(name, key(agent="codex", namespace="codex-other", loop="none"))]
    assert (plain("agent_efficiency_roots_without_lanes"), plain("agent_efficiency_root_no_lane_seconds")) == (0, 0)
    # A new spawn resets the clock even before the new lane's file appears.
    with root.open("a", encoding="utf-8") as handle:
        handle.write(spawn_fn(now + 10, "s2") + fn_out(now + 11, "s2", '{"task_name":"/root/lane2"}'))
    samples = eff.collect(now + 15)
    assert v(samples, "agent_efficiency_roots_without_lanes") == 1
    assert v(samples, "agent_efficiency_root_no_lane_seconds") == pytest.approx(5, abs=0.01)
    eff.codex_file(
        "rollout-stall-lane2.jsonl",
        [
            codex_meta(now + 12, "stall-lane2", parent="stall-root"),
            codex(now + 12, "event_msg", {"type": "task_started"}),
            record(now + 20, "stall-lane2"),
        ],
    )
    samples = eff.collect(now + 30)
    assert v(samples, "agent_efficiency_roots_without_lanes") == 0
    assert v(samples, "agent_efficiency_root_no_lane_seconds") == 0
    assert ("agent_efficiency_roots_without_lanes", key(agent="codex", namespace=CODEX, loop="none")) in samples


def test_stalled_loop_claude_subagents_are_children(eff: Harness):
    now = eff.now
    eff.baseline = now - 7200
    use = {"input_tokens": 1, "output_tokens": 1}
    eff.claude_file(
        "sess-stall.jsonl",
        [
            claude(now - 900, "user", {"role": "user", "content": "go\nContract: loop-v2.0"}),
            claude_tool(now - 600, "r1", "a1", "Agent", {"prompt": "p", "subagent_type": "general-purpose"}),
            claude_result(now - 599, "a1", "launched", toolUseResult={"agentId": "x1", "isAsync": True}),
            claude_tool(now - 60, "r2", "a2", "TaskOutput", {"task_id": "x1", "block": True}),  # waiting, in task
        ],
    )
    subagents = eff.claude_dir / "sess-stall" / "subagents"
    subagents.mkdir(parents=True)
    lane = subagents / "agent-x1.jsonl"
    lane.write_text(
        claude(now - 598, "user", {"role": "user", "content": "task"})
        + claude_tool(now - 100, "w1", "t1", "Bash", {"command": "ls -la /tmp/far/away/dir"}, stop_reason="tool_use"),
        encoding="utf-8",
    )
    samples = eff.collect(now)
    v = lambda name: value(samples, name, agent="claude", namespace=CLAUDE)
    assert v("agent_efficiency_roots_without_lanes") == 0
    assert v("agent_efficiency_root_no_lane_seconds") == 0
    assert ("agent_efficiency_root_no_lane_seconds", key(agent="claude", namespace=CLAUDE, loop="none")) in samples
    # The lane finishes its turn: the live root now has no lane in flight.
    with lane.open("a", encoding="utf-8") as handle:
        handle.write(claude_result(now - 90, "t1", "x"))
        handle.write(
            claude(
                now - 80,
                "assistant",
                {"id": "w2", "model": "claude-test", "usage": use, "stop_reason": "end_turn", "content": []},
            )
        )
    samples = eff.collect(now)
    assert v("agent_efficiency_roots_without_lanes") == 1
    assert v("agent_efficiency_root_no_lane_seconds") == pytest.approx(80, abs=0.01)


def test_lane_seconds_histogram(eff: Harness):
    now = eff.now
    eff.baseline = now - 20000
    b = eff.baseline + 100
    rec = lambda ts: record(ts, None, 10, 0, 1)
    eff.codex_file("rollout-lane-root.jsonl", [codex_meta(b, "lane-root"), rec(b + 10)])
    eff.codex_file("rollout-lane-a.jsonl", [codex_meta(b, "lane-a", parent="lane-root"), rec(b + 500), rec(b + 1000)])
    eff.codex_file(
        "rollout-lane-b.jsonl",
        [
            *(codex_meta(b + 2000, "lane-b", parent="lane-root"), rec(b + 2100)),  # segment 1: 100 s
            *(rec(b + 4000), rec(b + 4200)),  # quiet 1900 s, then segment 2: 200 s
        ],
    )
    eff.codex_file("rollout-lane-live.jsonl", [codex_meta(now - 600, "lane-live", parent="lane-root"), rec(now - 60)])
    samples = eff.collect(now)
    lane = lambda name: value(samples, name, agent="codex", namespace=CODEX)
    buckets = {
        dict(labels)["le"]: v
        for labels, v in select(samples, "agent_efficiency_lane_seconds_bucket", agent="codex", namespace=CODEX).items()
    }
    assert buckets == {"300": 2, "900": 2, "1800": 3, "3600": 3, "7200": 3, "14400": 3, "+Inf": 3}
    assert lane("agent_efficiency_lane_seconds_sum") == 1300
    assert lane("agent_efficiency_lane_seconds_count") == 3
    # A later run neither re-observes a closed lane nor the still-live one.
    samples = eff.collect(now + 1)
    assert lane("agent_efficiency_lane_seconds_count") == 3


def test_rate_limit_gauges_newest_event_wins(eff: Harness):
    b = eff.baseline + 500
    limits = lambda used, secondary=None, resets=1790000000: {
        "limit_id": "codex",
        "primary": {"used_percent": used, "window_minutes": 300, "resets_at": resets},
        "secondary": secondary,
    }
    count = lambda ts, rl: codex(ts, "event_msg", {"type": "token_count", "info": None, "rate_limits": rl})
    # The newest event sits in the smaller file, which is parsed first.
    newest = limits(55.0, {"used_percent": 10.0, "window_minutes": 10080, "resets_at": 1790500000}, 1790001000)
    eff.codex_file("rollout-rl-new.jsonl", [codex_meta(b, "rl-new"), count(b + 20, newest)])
    eff.codex_file(
        "rollout-rl-old.jsonl",
        [
            codex_meta(b, "rl-old-thread-with-a-longer-name"),
            count(b + 5, limits(30.0)),
            count(b + 10, limits(40.0, {"used_percent": 5.0, "window_minutes": 10080, "resets_at": 1790400000})),
            count(b + 12, limits(41.0)),
        ],
    )
    samples = eff.collect(eff.now)
    v = lambda name, window: value(samples, name, agent="codex", namespace=CODEX, window=window)
    assert v("agent_efficiency_rate_limit_used_percent", "primary") == 55
    assert v("agent_efficiency_rate_limit_resets_at_seconds", "primary") == 1790001000
    assert v("agent_efficiency_rate_limit_window_minutes", "primary") == 300
    assert v("agent_efficiency_rate_limit_used_percent", "secondary") == 10
    assert v("agent_efficiency_rate_limit_window_minutes", "secondary") == 10080
    assert v("agent_efficiency_rate_limit_resets_at_seconds", "secondary") == 1790500000


# -- incremental state ----------------------------------------------------------------------------


def test_incremental_runs_count_complete_lines_and_reset_on_truncate_or_rewrite(eff: Harness):
    history: list[dict] = []

    def run() -> dict:
        samples = eff.collect(eff.now)
        for previous in history:
            for series, number in previous.items():
                if series[0].endswith("_total"):
                    assert series in samples, f"counter series disappeared: {series}"
                    assert samples[series] >= number, f"counter decreased: {series}"
        history.append(samples)
        return samples

    b = eff.baseline
    head = codex_meta(b + 1, "inc-thread") + codex(b + 1, "turn_context", {"model": "gpt-test"})
    rec = lambda ts: record(ts, None, 100, 50, 1)
    path = eff.codex_dir / "rollout-inc.jsonl"
    path.write_text(head + rec(b + 10), encoding="utf-8")
    assert calls(run()) == 1

    partial = rec(b + 30)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(rec(b + 20) + partial[:-1])
    assert calls(run()) == 2
    entry = next(iter(eff.saved()["files"].values()))
    assert entry["offset"] == path.stat().st_size - len(partial) + 1

    with path.open("a", encoding="utf-8") as handle:
        handle.write("\n")
    assert calls(run()) == 3
    assert calls(run()) == 3

    # A rewritten file (new head, still longer than the old offset) restarts from its start but never
    # recounts what was already counted: b+30 is skipped and all four newer calls count, including
    # those that now sit before the old offset.
    rewritten = codex_meta(b + 1, "inc-thread-rewritten")
    path.write_text(rewritten + "".join(rec(b + t) for t in (30, 40, 50, 60, 70)), encoding="utf-8")
    assert path.stat().st_size > entry["offset"] + 1
    assert calls(run()) == 7

    # A truncated file (same first line) resets its offset without recounting, so a call appended afterwards counts.
    path.write_text(rewritten, encoding="utf-8")
    assert calls(run()) == 7
    with path.open("a", encoding="utf-8") as handle:
        handle.write(rec(b + 80))
    assert calls(run()) == 8
    assert eff.state.stat().st_mode & 0o777 == 0o600


def test_transiently_missing_file_is_not_recounted(eff: Harness):
    b = eff.baseline
    path = eff.codex_file("rollout-moved.jsonl", [codex_meta(b + 1, "moved"), record(b + 10, None, 10, 0, 1)])
    assert calls(eff.collect(eff.now)) == 1
    aside = eff.root / "aside.jsonl"
    path.rename(aside)
    assert calls(eff.collect(eff.now)) == 1
    aside.rename(path)
    assert calls(eff.collect(eff.now)) == 1


def test_malformed_record_is_skipped_without_pinning_the_file(eff: Harness, capsys):
    b = eff.baseline
    path = eff.codex_file(
        "rollout-bad.jsonl",
        [
            codex_meta(b + 1, "bad"),
            codex(b + 10, "token_usage_record", {"usage": {"input_tokens": "not-a-number"}}),
            record(b + 20, None, 10, 0, 1),
        ],
    )
    samples = eff.collect(eff.now)
    assert calls(samples) == 1
    assert "not-a-number" not in capsys.readouterr().err
    entry = next(iter(eff.saved()["files"].values()))
    assert entry["offset"] == path.stat().st_size
    # The skip is counted per namespace, never with record content; a clean namespace reads zero.
    malformed = select(samples, "agent_efficiency_malformed_records_total")
    assert malformed == {key(agent="codex", namespace=CODEX): 1, key(agent="claude", namespace=CLAUDE): 0}
    assert "not-a-number" not in json.dumps(eff.saved().get("malformed"))
    # A consumed record is not counted again, and a later one adds to the persisted total.
    assert value(eff.collect(eff.now), "agent_efficiency_malformed_records_total", agent="codex", namespace=CODEX) == 1
    with path.open("a", encoding="utf-8") as handle:
        handle.write(codex(b + 30, "token_usage_record", {"usage": {"input_tokens": "not-a-number"}}))
    samples = eff.collect(eff.now)
    assert value(samples, "agent_efficiency_malformed_records_total", agent="codex", namespace=CODEX) == 2
    assert calls(samples) == 1


class _Ticking:
    """A monotonic clock that advances one second every time the collector reads it."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        self.now += 1.0
        return self.now


def test_budget_exhaustion_defers_parsing_to_next_run(eff: Harness):
    b = eff.baseline
    path = eff.codex_file("rollout-budget.jsonl", [codex_meta(b + 1, "t"), record(b + 10, None, 10, 0, 1)])
    samples = eff.collect(eff.now, budget=0, monotonic=_Ticking())
    assert calls(samples) == 0
    entry = next(iter(eff.saved()["files"].values()))
    assert entry["offset"] == 0
    samples = eff.collect(eff.now)
    assert calls(samples) == 1
    assert next(iter(eff.saved()["files"].values()))["offset"] == path.stat().st_size


def test_budget_stops_mid_file_and_resumes_at_the_next_complete_line(eff: Harness):
    b = eff.baseline
    lines = [codex_meta(b + 1, "long")] + [
        record(b + 10 + n, None, 10, 0, 1) for n in range(2 * EFFICIENCY_BUDGET_CHECK_LINES)
    ]
    path = eff.codex_file("rollout-long.jsonl", lines)
    small = eff.codex_file("rollout-small.jsonl", [codex_meta(b + 1, "small"), record(b + 20, None, 10, 0, 1)])
    # Budget 1.5s on a clock ticking once per read: the deadline passes after one file starts. Smaller
    # remaining work goes first, so the short file is consumed and the long one waits.
    samples = eff.collect(eff.now, budget=1.5, monotonic=_Ticking())
    files = eff.saved()["files"]
    long_entry = next(v for k, v in files.items() if k.endswith("rollout-long.jsonl"))
    small_entry = next(v for k, v in files.items() if k.endswith("rollout-small.jsonl"))
    assert small_entry["offset"] == small.stat().st_size
    assert long_entry["offset"] == 0
    assert calls(samples) == 1
    # Now the long file starts and stops at its first in-file budget check, on a complete line.
    samples = eff.collect(eff.now, budget=1.5, monotonic=_Ticking())
    long_entry = next(v for k, v in eff.saved()["files"].items() if k.endswith("rollout-long.jsonl"))
    consumed = "".join(lines[:EFFICIENCY_BUDGET_CHECK_LINES]).encode()
    assert long_entry["offset"] == len(consumed)
    assert calls(samples) == 1 + EFFICIENCY_BUDGET_CHECK_LINES - 1
    samples = eff.collect(eff.now)
    assert (
        next(v for k, v in eff.saved()["files"].items() if k.endswith("rollout-long.jsonl"))["offset"]
        == path.stat().st_size
    )
    assert calls(samples) == 1 + 2 * EFFICIENCY_BUDGET_CHECK_LINES
    assert calls(eff.collect(eff.now)) == 1 + 2 * EFFICIENCY_BUDGET_CHECK_LINES


# -- privacy ----------------------------------------------------------------------------------------

SENTINEL = "SENTINEL-PRIVACY-7f3a91"


class _Bridge:
    """Captures what the OTLP bridge would publish from the same collection."""

    def __init__(self) -> None:
        self.published: list = []

    def publish(self, samples) -> None:
        self.published.append(samples)


def test_message_command_cwd_and_spawn_text_never_reach_output_state_or_stderr(eff: Harness, capsys):
    from agent_history.metrics.collection import Collection, State
    from agent_history.metrics.efficiency import EfficiencyCollector

    b = eff.baseline + 500
    s = SENTINEL
    eff.codex_file(
        "rollout-private.jsonl",
        [
            codex_meta(b, "private-thread", cwd=f"/work/{s}"),
            user_item(b + 0.5, f"{s} Contract: loop-v{s}"),
            codex(b, "turn_context", {"model": "gpt-test", "cwd": s}),
            codex(b + 1, "event_msg", {"type": "user_message", "message": s}),
            exec_js(b + 2, "p1", f'const r = await tools.exec_command({{cmd: "echo {s}; sleep 5"}}); text(r);'),
            codex(
                b + 7,
                "response_item",
                {
                    "type": "custom_tool_call_output",
                    "call_id": "p1",
                    "output": [{"type": "input_text", "text": f"{s} session_id: 3"}],
                },
            ),
            codex(b + 8, "event_msg", {"type": "task_complete", "error": {"message": s, "codex_error_info": "other"}}),
            # Spawn text, a push that yields and is polled, a CodeRabbit finding, a failed cell.
            codex(b + 9, "event_msg", {"type": "user_message", "message": s}),
            spawn_fn(b + 10, "p2", task_name=s, message=s, agent_type="poller"),
            fn_out(b + 11, "p2", f"agent path `/root/{s}` already exists"),
            *(exec_fn(b + 12, "p3", f"git push origin {s}"), fn_out(b + 13, "p3", running(77, s))),
            *(stdin_fn(b + 14, "p4", 77), fn_out(b + 15, "p4", exited(1, s))),
            exec_fn(b + 16, "p5", f"coderabbit review --agent --base {s}"),
            fn_out(
                b + 17,
                "p5",
                exited(
                    0,
                    cr_line("finding", severity="major", fileName=s)
                    + "\n"
                    + cr_line("complete", status="review_completed"),
                ),
            ),
            exec_js(b + 18, "p6", f'const r = await tools.exec_command({{cmd: "just check {s}"}}); text(r);'),
            js_out(b + 19, "p6", s, status="Script failed"),
            user_item(b + 19.5, f"# AGENTS.md instructions for /work/{s}", f"please also {s}"),
            exec_js(
                b + 19.6,
                "p7",
                f'const r = await tools.spawn_agent({{task_name: "{s}", model: "gpt-6-sol", message: `{s}`}}); text(JSON.stringify(r));',
            ),
            js_out(b + 19.7, "p7", {"task_name": f"/root/{s}"}),
            codex(
                b + 20,
                "event_msg",
                {
                    "type": "token_count",
                    "info": None,
                    "rate_limits": {
                        "limit_id": s,
                        "primary": {"used_percent": 1.0, "window_minutes": 300, "resets_at": 1},
                    },
                },
            ),
        ],
    )
    eff.claude_file(
        "sess-private.jsonl",
        [
            claude(b, "user", {"role": "user", "content": s}),
            claude_tool(b + 1, "mp", "tp", "Bash", {"command": f"sleep 5; echo {s}"}),
            claude_result(b + 2, "tp", s),
            claude(b + 3, "user", {"role": "user", "content": s}, origin={"kind": "human"}),
            claude_tool(b + 4, "mq", "tq", "Bash", {"command": f"git push {s}"}),
            claude_result(b + 5, "tq", s, is_error=True),
            claude_tool(b + 6, "mr", "tr", "Agent", {"prompt": s, "subagent_type": s}),
            claude_result(b + 7, "tr", s, toolUseResult={"agentId": "sentinel-agent", "status": "async_launched"}),
            claude(b + 8, "system", subtype="turn_duration", durationMs=1),
        ],
    )
    raw = eff.collect(eff.now)
    eff.monkeypatch.setattr("agent_history.metrics.efficiency.time.time", lambda: eff.now)
    bridge = _Bridge()
    collection = Collection(
        [EfficiencyCollector(eff.config(), eff.state_dir)], State(eff.root / "counter-state"), bridge=bridge
    )
    rendered = collection.metrics()
    names = {name for name, _ in raw}
    for series in (
        "agent_efficiency_tool_calls_total",
        "agent_efficiency_spawns_by_route_total",
        "agent_efficiency_spawn_errors_total",
        "agent_efficiency_git_pushes_total",
        "agent_efficiency_coderabbit_findings_total",
        "agent_efficiency_gate_runs_total",
        "agent_efficiency_tool_failures_total",
        "agent_efficiency_interventions_total",
        "agent_efficiency_rate_limit_used_percent",
        "agent_efficiency_root_llm_calls_by_protocol_total",
        "agent_efficiency_root_time_seconds_by_protocol_total",
        "agent_efficiency_roots_without_lanes",
        "agent_efficiency_root_no_lane_seconds",
    ):
        assert series in names, series
        assert series in rendered, series
    streams = capsys.readouterr()
    persisted = [path.read_text(encoding="utf-8") for path in (eff.root / "state").rglob("*") if path.is_file()]
    persisted += [
        path.read_text(encoding="utf-8") for path in (eff.root / "counter-state").rglob("*") if path.is_file()
    ]
    assert persisted, "the collector and the counter state must both persist something"
    for surface in (
        repr(sorted(raw.items(), key=repr)),
        rendered,
        repr(bridge.published),
        streams.err,
        streams.out,
        *persisted,
    ):
        assert s not in surface
        assert s.lower() not in surface.lower()


# -- loop attribution ---------------------------------------------------------------------------------

LOOP16 = "example-repo/loop16"
LOOP17 = "example-repo/loop17"
# Every value the label may take: the two reserved values, or "<safe slug>/<loop|wave><number or launch minute>".
LOOP_VALUE = re.compile(r"none|other|[A-Za-z0-9._-]{1,48}/(?:loop|wave)(?:\d{1,6}|-\d{8}T\d{4})")


def test_loop_label_value_scheme_and_fallbacks():
    launch = 1790000000.0  # 2026-09-21T14:13:20Z
    assert loop_label_value("example-repo", "2026-09-25", "loop", 16, launch) == LOOP16
    assert loop_label_value("other-repo", None, "wave", 12, launch) == "other-repo/wave12"
    assert loop_label_value(None, "2026-09-17-campaign", None, 3, launch) == "2026-09-17-campaign/loop3"
    assert loop_label_value(None, None, "loop", 3, launch) == "unknown/loop3"
    assert loop_label_value("example-kit", None, "loop", None, launch) == "example-kit/loop-20260921T1413"
    # The repository name wins over the campaign slug.
    assert loop_label_value("example-infra", "2026-09-24-campaign", "loop", 11, launch) == "example-infra/loop11"
    unsafe = loop_label_value('../we ird\nrepo"x' + "y" * 80, None, "bogus", 2, launch)
    assert LOOP_VALUE.fullmatch(unsafe)
    assert unsafe.endswith("/loop2")


def test_efficiency_session_keys_match_catalogue_natural_keys():
    assert efficiency_session_keys(f"{CLAUDE}/projects/-p/sess-1.jsonl", {}) == ("claude\tsess-1\t", None)
    assert efficiency_session_keys(f"{CLAUDE}/projects/-p/sess-1/subagents/agent-a1.jsonl", {}) == (
        "claude\tsess-1\ta1",
        "claude\tsess-1\t",
    )
    assert efficiency_session_keys(f"{CODEX}/sessions/2026/09/25/r.jsonl", {"tid": "t-2", "parent": "t-1"}) == (
        "codex\tt-2\t",
        "codex\tt-1\t",
    )
    assert efficiency_session_keys(f"{CODEX}/sessions/r.jsonl", {"tid": None}) == (None, None)


@pytest.fixture
def looped(tmp_path: Path, monkeypatch) -> Harness:
    harness = Harness(tmp_path, monkeypatch, time.time() - 5000)
    harness.now = harness.baseline + 5000
    harness.b = harness.baseline + 100
    return harness


def loop_rows(h: Harness) -> tuple[list, list]:
    """Catalogue rows: loop16 ends with its report write; loop17 has no end evidence and its root went quiet."""
    b = h.b
    roots = [
        ("codex", "root-1", "", "example-repo", "2026-09-25", "loop", 16, b + 100, b + 400, "report_write"),
        ("codex", "root-1", "", "example-repo", "2026-09-25", "loop", 17, b + 600, b + 700, "root_last_event"),
        ("claude", "sess-1", "", "example-repo", "2026-09-25", "loop", 16, b + 100, b + 400, "report_write"),
    ]
    members = [
        ("codex", "lane-1", "", "example-repo", "2026-09-25", "loop", 16, b + 100),
        ("claude", "sess-1", "a1", "example-repo", "2026-09-25", "loop", 16, b + 100),
    ]
    return roots, members


def write_loop_threads(h: Harness) -> None:
    b = h.b
    h.codex_file(
        "rollout-root.jsonl",
        [
            codex_meta(b, "root-1"),
            record(b + 10, "root-1"),  # before the first launch: none
            record(b + 200, "root-1"),  # inside loop16
            record(b + 300, "root-1"),  # inside loop16
            record(b + 500, "root-1"),  # after loop16's report write, before loop17: none
            record(b + 700, "root-1"),  # loop17
            record(b + 2600, "root-1"),  # beyond loop17's last event plus the refresh grace: none
        ],
    )
    h.codex_file("rollout-lane-1.jsonl", [codex_meta(b + 150, "lane-1", parent="root-1"), record(b + 250, "lane-1")])
    # Not in the catalogue yet: inherits its parent root's loop at the event time.
    h.codex_file("rollout-lane-2.jsonl", [codex_meta(b + 150, "lane-2", parent="root-1"), record(b + 260, "lane-2")])
    # An unrelated interactive thread at the same time: none.
    h.codex_file("rollout-solo.jsonl", [codex_meta(b + 150, "solo-1"), record(b + 250, "solo-1")])
    use = {"input_tokens": 10, "output_tokens": 1}
    assistant = lambda ts, mid: claude(
        ts, "assistant", {"id": mid, "model": "claude-test", "role": "assistant", "usage": use, "content": []}
    )
    h.claude_file("sess-1.jsonl", [assistant(b + 5, "m0"), assistant(b + 200, "m1")])
    subagents = h.claude_dir / "sess-1" / "subagents"
    subagents.mkdir(parents=True)
    (subagents / "agent-a1.jsonl").write_text(assistant(b + 210, "w1"), encoding="utf-8")


def by_loop(samples: dict, name: str) -> dict[str, float]:
    totals: dict[str, float] = {}
    for (sample, labels), number in samples.items():
        if sample == name:
            loop = dict(labels)["loop"]
            totals[loop] = totals.get(loop, 0) + number
    return totals


def test_events_are_attributed_to_the_loop_window_lane_or_none(looped: Harness):
    write_loop_threads(looped)
    samples = looped.collect(looped.now, loop_rows=loop_rows(looped))
    n = lambda **labels: total(samples, "agent_efficiency_llm_calls_total", **labels)
    assert n(agent="codex", namespace=CODEX, loop=LOOP16) == 4  # root x2, linked lane, unlinked lane
    assert n(agent="codex", namespace=CODEX, loop=LOOP16, role="worker") == 2
    assert n(agent="codex", namespace=CODEX, loop=LOOP17) == 1
    assert n(agent="codex", namespace=CODEX, loop="none") == 4  # root x3, unrelated solo thread
    assert n(agent="claude", namespace=CLAUDE, loop=LOOP16) == 2  # root in window, subagent member
    assert n(agent="claude", namespace=CLAUDE, loop="none") == 1  # root before launch
    seen = set()
    for name, labels in samples:
        if not name.startswith("agent_efficiency_") or UNLOOPED.match(name):
            continue
        plain = dict(labels)
        assert "loop" in plain, name
        assert LOOP_VALUE.fullmatch(plain["loop"]), plain["loop"]
        seen.add(plain["loop"])
    assert seen == {"none", LOOP16, LOOP17}
    assert samples[("agent_efficiency_loop_labels", frozenset())] == 2
    assert samples[("agent_efficiency_loop_map_loops", frozenset())] == 2


def test_cached_loop_map_bridges_an_outage_then_expires(looped: Harness):
    write_loop_threads(looped)
    looped.collect(looped.now, loop_rows=loop_rows(looped))
    lane = looped.codex_dir / "rollout-lane-1.jsonl"
    with lane.open("a", encoding="utf-8") as handle:
        handle.write(record(looped.b + 300, "lane-1"))
    samples = looped.collect(looped.now + 60, loop_error=OSError("connection refused"))  # catalogue unreachable
    assert by_loop(samples, "agent_efficiency_llm_calls_total")[LOOP16] == 7  # 6 + the new lane call
    assert "loop_map" in looped.saved()
    later = looped.now + LOOP_MAP_MAX_AGE_SECONDS + 120
    with lane.open("a", encoding="utf-8") as handle:
        handle.write(record(looped.b + 350, "lane-1"))
    samples = looped.collect(later, loop_error=OSError("connection refused"))
    assert by_loop(samples, "agent_efficiency_llm_calls_total")[LOOP16] == 7  # map expired: the new call is none
    assert ("agent_efficiency_loop_map_age_seconds", frozenset()) not in samples
    assert samples[("agent_efficiency_loop_map_loops", frozenset())] == 0
    assert "loop_map" not in looped.saved()


def test_loop_series_are_pruned_after_retention(looped: Harness):
    """Mapped loops are not capped (tests/test_efficiency_collector.py); they retire after the retention window."""
    write_loop_threads(looped)
    samples = looped.collect(looped.now, loop_rows=loop_rows(looped))
    assert set(by_loop(samples, "agent_efficiency_llm_calls_total")) == {"none", LOOP16, LOOP17}
    later = looped.b + 2600 + EFFICIENCY_LOOP_RETAIN_SECONDS + 60
    samples = looped.collect(later)
    assert set(by_loop(samples, "agent_efficiency_llm_calls_total")) == {"none"}
    assert samples[("agent_efficiency_loop_labels", frozenset())] == 0


# -- pi -------------------------------------------------------------------------------------------------

PI = "pi-local"
PI_ROOT = "11111111-1111-4111-8111-111111111111"
PI_CHILDREN = ("22222222-2222-4222-8222-222222222222", "33333333-3333-4333-8333-333333333333")
PI_ROOT_BASE = f"2026-09-28T07-27-09-000Z_{PI_ROOT}"
PI_LOOP = "example-repo/loop18"


def pi_record(ts: float, kind: str, **fields: object) -> str:
    return json.dumps({"type": kind, "timestamp": iso(ts), **fields}) + "\n"


def pi_message(ts: float, ident: str, role: str, **fields: object) -> str:
    return pi_record(ts, "message", id=ident, message={"role": role, "timestamp": iso(ts), **fields})


def pi_call(
    ts: float,
    ident: str,
    response: str,
    tool_id: str | None = None,
    name: str = "",
    arguments: dict | None = None,
    **fields,
):
    content = [{"type": "toolCall", "id": tool_id, "name": name, "arguments": arguments or {}}] if tool_id else []
    stop = "toolUse" if tool_id else "stop"
    return pi_message(
        ts,
        ident,
        "assistant",
        model="test-model",
        responseId=response,
        stopReason=stop,
        usage={"input": 1, "output": 1},
        content=content,
        **fields,
    )


def pi_result(ts: float, ident: str, tool_id: str, name: str, is_error: bool = False, **fields) -> str:
    return pi_message(
        ts, ident, "toolResult", toolCallId=tool_id, toolName=name, isError=is_error, content=[], **fields
    )


def test_pi_root_children_wait_status_spawn_and_loop(eff: Harness):
    start = eff.now - 500
    eff.baseline = start - 10
    directory = eff.hot / PI / "sessions" / "-synthetic-"
    directory.mkdir(parents=True)
    tasks = {"tasks": [{"agent": "mapper", "task": "one"}, {"agent": "worker", "task": "two"}]}
    results = {
        "results": [{"agent": "mapper", "index": 0, "exitCode": 0}, {"agent": "worker", "index": 1, "exitCode": 0}]
    }
    (directory / f"{PI_ROOT_BASE}.jsonl").write_text(
        "".join(
            [
                pi_record(start - 2, "session", id=PI_ROOT, cwd="/synthetic/cwd", version=3),
                pi_message(start + 1, "u1", "user", content=[{"type": "text", "text": "synthetic loop root"}]),
                pi_call(start + 2, "a1", "r1", "spawn", "subagent", tasks),
                pi_result(start + 3, "s1", "spawn", "subagent", details=results),
                pi_call(start + 4, "a2", "r2", "wait", "bash", {"command": "sleep 1"}),
                pi_result(start + 5, "w1", "wait", "bash"),
                pi_record(start + 6, "custom_message", id="watch", customType="loop-watch", content="status"),
                pi_call(start + 7, "a3", "r3"),
            ]
        ),
        encoding="utf-8",
    )
    for index, child in enumerate(PI_CHILDREN):
        child_dir = directory / PI_ROOT_BASE / child / f"run-{index}"
        child_dir.mkdir(parents=True)
        (child_dir / "session.jsonl").write_text(
            pi_record(start + 3, "session", id=child, cwd="/synthetic/cwd", version=3)
            + pi_message(start + 4, "u1", "user", content=[{"type": "text", "text": "synthetic child brief"}])
            + pi_call(start + 5, "a1", f"child-{index}"),
            encoding="utf-8",
        )
    rows = (
        [("pi", PI_ROOT, "", "example-repo", None, "loop", 18, start - 1, start + 100, "report_write")],
        [("pi", child, "", "example-repo", None, "loop", 18, start) for child in PI_CHILDREN],
    )
    samples = eff.collect(eff.now, loop_rows=rows)
    n = lambda name, **labels: total(samples, name, agent="pi", loop=PI_LOOP, **labels)
    assert n("agent_efficiency_llm_calls_total", role="root") == 3
    assert n("agent_efficiency_llm_calls_total", role="worker") == 2
    assert n("agent_efficiency_spawns_total") == 2
    assert n("agent_efficiency_poll_calls_total") >= 1
    # The loop-watch custom message wakes the root as an event, never as a status poll.
    assert n("agent_efficiency_llm_calls_total", role="root", trigger="event") == 1
    assert n("agent_efficiency_llm_calls_total", role="root", trigger="status") == 0
    assert n("agent_efficiency_active_threads", role="root") == 1
    assert n("agent_efficiency_active_threads", role="worker") == 2
    assert n("agent_efficiency_active_roots_with_idle_workers") == 0
    storage = ArchiveCollector(eff.hot, eff.root / "cold", None, None).collect()
    pi_hot = [
        s.value
        for f in storage
        if f.name == "agent_sessions_storage_files"
        for s in f.samples
        if {("namespace", PI), ("tier", "hot")} <= set(s.labels)
    ]
    assert sum(pi_hot) == 3


def pi_parse(tmp_path: Path, lines: list[str], skip: float) -> EfficiencyParser:
    relative = f"{PI}/sessions/-synthetic-/{PI_ROOT_BASE}.jsonl"
    state = read_efficiency_state(tmp_path / "absent-state.json", skip)
    parser = EfficiencyParser(EfficiencyRun(state), efficiency_file_state(relative, skip), relative)
    for line in lines:
        parser.line(line.encode())
    return parser


def delta(parser: EfficiencyParser, metric: str, fragment: str = "") -> float:
    return sum(v for (name, series), v in parser.run.delta.items() if name == metric and fragment in series)


def test_pi_async_launch_arms_a_wake_without_counting_a_poll_or_spawn(tmp_path: Path):
    start = time.time() - 100
    parser = pi_parse(
        tmp_path,
        [
            pi_record(start, "session", id=PI_ROOT, cwd="/synthetic/cwd", version=3),
            pi_message(start + 1, "u", "user", content=[]),
            pi_call(start + 2, "a1", "r1", "arm", "watch_start"),
            pi_result(start + 3, "arm-result", "arm", "watch_start"),
            pi_call(start + 4, "a2", "r2", "async", "subagent", {"async": True, "workflowScript": "synthetic"}),
            pi_result(start + 5, "async-result", "async", "subagent", details={"runId": PI_CHILDREN[0]}),
            pi_call(start + 6, "a3", "r3"),
        ],
        start - 10,
    )
    assert parser.s["role"] == "root"
    assert delta(parser, "agent_efficiency_llm_calls_total", "\troot\t") == 1
    assert delta(parser, "agent_efficiency_poll_calls_total") == 0
    assert delta(parser, "agent_efficiency_spawns_total") == 0
    assert delta(parser, "agent_efficiency_first_spawn_seconds_count") == 1


def test_pi_failed_launch_does_not_create_a_root_or_spawn(tmp_path: Path):
    start = time.time() - 100
    parser = pi_parse(
        tmp_path,
        [
            pi_record(start, "session", id=PI_ROOT, cwd="/synthetic/cwd", version=3),
            pi_call(start + 1, "a", "r", "failed", "subagent", {"action": "run", "agent": "worker"}),
            pi_result(start + 2, "result", "failed", "subagent", is_error=True, details={}),
        ],
        start - 10,
    )
    assert parser.s["role"] == "solo"
    assert (
        delta(parser, "agent_efficiency_spawns_total") + delta(parser, "agent_efficiency_first_spawn_seconds_count")
        == 0
    )


def test_pi_subagent_status_result_remains_a_status_trigger(tmp_path: Path):
    start = time.time() - 100
    parser = pi_parse(
        tmp_path,
        [
            pi_record(start, "session", id=PI_ROOT, cwd="/synthetic/cwd", version=3),
            pi_call(start + 1, "a1", "r1", "status", "subagent", {"action": "status"}),
            pi_result(start + 2, "result", "status", "subagent"),
            pi_call(start + 3, "a2", "r2"),
        ],
        start - 10,
    )
    assert delta(parser, "agent_efficiency_llm_calls_total", "\tstatus\t") == 1

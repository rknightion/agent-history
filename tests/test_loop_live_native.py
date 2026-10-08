"""Synthetic analogues of retained native command and watcher shapes, not source content."""

import json
from datetime import timedelta

import pytest

from agent_history import loop_live, loops
from agent_history.config import Config, LoopLive, parse_config
from test_loop_live import AT, event

REPORT = "/tmp/synthetic/codex/report-synthetic-loop1.md"
TARGET = "codex/state-synthetic-loop1.jsonl"


def append(ev, fields="", target=TARGET):
    return f"loop-state append {target} {ev} {fields}".strip()


def project_command(command, output, **kw):
    return loop_live.project(loop_live.append_events(command, "/tmp/synthetic", REPORT, AT, output, **kw), AT)


def test_native_semicolon_appends_quoted_operators_and_seq_correlation():
    command = (
        append("gate", "scope=composed exit=1 cmd=\"bash -c 'build && check'\"")
        + "; "
        + append("judgement", "'text=Inspect the unchanged candidate; do not guess success.'")
    )
    events = loop_live.append_events(command, "/tmp/synthetic", REPORT, AT, "seq=20\nseq=21\n")
    assert [e["ev"] for e in events] == ["gate", "judgement"]
    assert events[0]["cmd"] == "bash -c 'build && check'"
    assert loop_live.project(events, AT)["last_gate"]["exit"] == 1
    assert loop_live.project(events, AT)["last_judgement"].endswith("do not guess success.")


def test_native_multiple_appends_before_other_command_capture_each_admission():
    command = "; ".join([append("admit", f"task=TASK-{n}") for n in range(1, 6)]) + "; backlog task list --plain"
    state = project_command(command, "seq=15\nseq=16\nseq=17\nseq=18\nseq=19\nTask list\n")
    assert state["tasks_admitted"] == 5
    assert state["tasks_landed"] is None  # no recorded open, never fabricated zero


def test_native_absolute_final_append_after_chain_and_heredoc_ignores_body():
    absolute = "/tmp/synthetic/" + TARGET
    command = (
        "cd /tmp/worktree && git status --short; python3 - <<'PY'\n"
        + append("land", "task=NOT-A-COMMAND", absolute)
        + "\nPY\n"
        + append("judgement", "'text=Check the candidate.'", absolute)
    )
    events = loop_live.append_events(
        command, "/tmp/synthetic", REPORT, AT, "synthetic-tree\nseq=25\n", call_uid="synthetic-call"
    )
    # The final literal writer is still captured. Opaque preceding programs now separately
    # propagate completeness uncertainty; their heredoc body must never become a land event.
    captured = [e for e in events if e["ev"] != "uncertain"]
    assert [e["ev"] for e in captured] == ["judgement"]
    assert captured[0]["text"] == "Check the candidate."
    state = loop_live.project(events, AT)
    assert state["last_judgement"] == "Check the candidate."
    assert state["tasks_landed"] is None


@pytest.mark.parametrize(
    "prefix", ["cd /tmp/worktree; ", "source /tmp/opaque; ", "opaque_command; ", "eval 'cd /tmp/worktree'; "]
)
def test_unresolved_cwd_never_resolves_relative_append(prefix):
    state = project_command(prefix + append("land", "task=TASK-1"), "seq=3\n")
    assert state["tasks_landed"] is None
    assert not any(
        e["ev"] == "land"
        for e in loop_live.append_events(prefix + append("land", "task=TASK-1"), "/tmp/synthetic", REPORT, AT, "seq=3")
    )


@pytest.mark.parametrize(
    "command,output,kw",
    [
        (append("admit", "task=TASK-1"), "", {}),
        (append("admit", "task=TASK-1"), "seq=1\nseq=2\n", {}),
        (append("admit", "task=TASK-1"), "seq=1\nseq=0\n", {}),
        (append("admit", "task=TASK-1"), "seq=" + "9" * 5000, {}),
        (append("admit", "task=TASK-1"), "seq=1", {"successful": False}),
        (append("admit", "task=TASK-1"), "seq=1", {"output_truncated": True}),
        (append("admit", "task=$TASK"), "seq=1", {}),
        (append("admit", "task=TASK-*"), "seq=1", {}),
        ("if true; then " + append("admit", "task=TASK-1") + "; fi", "seq=1", {}),
        ("( " + append("admit", "task=TASK-1") + " )", "seq=1", {}),
    ],
)
def test_missing_ambiguous_opaque_or_expanding_evidence_does_not_count(command, output, kw):
    events = [event("open")] + loop_live.append_events(command, "/tmp/synthetic", REPORT, AT, output, **kw)
    state = loop_live.project(events, AT)
    assert state["tasks_admitted"] is None
    assert not any(e["ev"] == "admit" for e in events)


def test_quotes_are_literal_data_not_operators_expansions_or_commands():
    # A quoted mention is demonstrably not an append, so it cannot make a known open uncertain.
    mentioned = project_command("echo '" + append("admit", "task=TASK-1") + "'", "seq=1")
    assert mentioned["tasks_admitted"] is None  # no open or actual admission in this command
    assert (
        loop_live.project(
            [event("open")]
            + loop_live.append_events(
                "echo '" + append("admit", "task=TASK-1") + "'", "/tmp/synthetic", REPORT, AT, "seq=1"
            ),
            AT,
        )["tasks_admitted"]
        == 0
    )
    events = loop_live.append_events(
        append("judgement", "'text=$literal | && ; > (not a command)'"), "/tmp/synthetic", REPORT, AT, "seq=8"
    )
    assert events[0]["text"] == "$literal | && ; > (not a command)"
    assert (
        loop_live.append_events(
            append("admit", "task=TASK-1", "/tmp/other/state.jsonl"), "/tmp/synthetic", REPORT, AT, "seq=8"
        )
        == []
    )


def test_uncertain_expanding_task_cannot_be_resolved_by_a_later_literal_spelling():
    events = [event("open"), event("admit", task="$TASK")]
    events += loop_live.append_events(append("admit", "task=$TASK"), "/tmp/synthetic", REPORT, AT, "seq=1")
    assert loop_live.project(events, AT)["tasks_admitted"] is None
    assert loop_live.project([event("judgement", text="Activity alone is not preparation.")], AT)["live_phase"] is None


def test_native_watch_acceptance_and_terminal_receipt_use_exact_id_not_label():
    args = {"command": "synthetic gate", "deadline_s": 900, "interval_s": 60, "label": "Gate proof"}
    records = [(7, "watch_start", args, {"id": "watch-one"}, AT, AT + timedelta(seconds=1))]
    events = loop_live.watch_events(records, [], AT, None)
    assert events[0]["at"] == AT
    assert events[0]["deadline_basis"] == "call_entry_plus_deadline_s"
    assert loop_live.project(events, AT + timedelta(minutes=5))["live_phase"] == "gating"
    receipt = {
        "id": "watch-one",
        "receipt": {
            "phase": "done",
            "last_observed_at": (AT + timedelta(minutes=4)).isoformat(),
            "deadline": (AT + timedelta(minutes=16)).isoformat(),
            "result": {"exit_code": 0, "deadline_hit": False, "signal": None},
            "label": "Gate proof",
            "tail": [],
        },
    }
    hooks = [
        (
            7,
            AT + timedelta(minutes=6),
            "WATCH watch-one (Gate proof): phase=done exit_code=0 deadline_hit=false",
            {"source": "loop-watch", "details": receipt},
        )
    ]
    events = loop_live.watch_events(records, hooks, AT, None)
    assert [e["op"] for e in events] == ["start", "stop"]
    assert events[-1]["at"] == AT + timedelta(minutes=6)  # hook observation, not receipt start or wall-clock liveness
    assert not loop_live.project(events, AT + timedelta(minutes=7))["phase_input"]["watch_active"]
    assert not loop_live.project(loop_live.watch_events(records, [], AT, None), AT + timedelta(minutes=16))[
        "phase_input"
    ]["watch_active"]
    mismatch = [(8, *hooks[0][1:])]
    assert loop_live.project(loop_live.watch_events(records, mismatch, AT, None), AT + timedelta(minutes=7))[
        "phase_input"
    ]["watch_active"]


def test_native_oversized_receipt_text_fallback_and_wake_is_not_heartbeat():
    records = [(7, "watch_start", {"deadline_s": 600, "label": "Proof"}, {"id": "watch-two"}, AT, AT)]
    hooks = [
        (
            7,
            AT + timedelta(minutes=1),
            "WATCH watch-two (Proof): phase=failed exit_code=1 deadline_hit=false",
            {"source": "loop-watch"},
        ),
        (
            7,
            AT + timedelta(minutes=2),
            "WAKE timer-one: Backstop",
            {"source": "loop-wake", "details": {"id": "timer-one", "reason": "Backstop"}},
        ),
    ]
    events = loop_live.watch_events(records, hooks, AT, None)
    assert [e["ev"] for e in events] == ["watch", "watch", "wake"]
    assert events[1]["op"] == "stop"
    assert not any(e["ev"] == "heartbeat" for e in events)
    assert loop_live.watch_events([], hooks[:1], AT, None) == []  # terminal observation never invents a start


def test_frozen_explicit_heartbeat_at_is_not_a_claim_of_historical_native_heartbeat():
    hooks = [
        (7, AT + timedelta(seconds=10), "Heartbeat", {"source": "loop-heartbeat", "details": {"at": AT.isoformat()}})
    ]
    assert loop_live.watch_events([], hooks, AT, None) == [event("heartbeat")]


def heartbeat_log(at=AT, heartbeat=True):
    rows = [
        {
            "v": 1,
            "seq": 1,
            "ts": AT.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "by": "root",
            "ev": "open",
            "goal_sha256": "a" * 64,
            "tier": "guarded",
            "root": "llm",
            "root_model": "synthetic",
            "envelope": [],
        }
    ]
    if heartbeat:
        rows.append(
            {
                "v": 1,
                "seq": 2,
                # Append observation is not root activity time.
                "ts": (AT + timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "by": "ext",
                "ev": "heartbeat",
                "at": at,
            }
        )
    return "".join(json.dumps(row) + "\n" for row in rows)


def test_state_heartbeat_only_root_has_live_phase_without_invented_structure():
    recorded = AT + timedelta(hours=2, milliseconds=123)
    parsed = loops.state_progress(heartbeat_log(recorded.isoformat().replace("+00:00", "Z")))
    assert parsed is not None
    beats = [e for e in parsed[2] if e["ev"] == "heartbeat"]
    assert len(beats) == 1 and beats[0]["at"] == recorded
    state = loop_live.project(beats, recorded + timedelta(minutes=1))
    assert state["live_phase"] == "preparing"
    assert state["evidence_at"] == recorded.isoformat()
    assert state["active_lanes"] is None
    assert state["tasks_admitted"] is None
    stale_lane = [event("dispatch", lane="one", agent="worker")]
    assert loop_live.project(stale_lane, recorded)["live_phase"] == "waiting"
    assert loop_live.project(stale_lane + beats, recorded)["live_phase"] == "working"


@pytest.mark.parametrize("dispatched", [False, True])
def test_state_heartbeat_phase_survives_unscoped_uncertainty_without_watch(dispatched):
    recorded = AT + timedelta(hours=2)
    parsed = loops.state_progress(heartbeat_log(recorded.isoformat().replace("+00:00", "Z")))
    assert parsed is not None
    beats = [e for e in parsed[2] if e["ev"] == "heartbeat"]
    uncertain = loop_live.append_events("python3 -c 'print(1)'", "/tmp/synthetic", REPORT, recorded, "1\n")
    assert any(e["ev"] == "uncertain" and e.get("for_ev") is None for e in uncertain)
    events = ([event("dispatch", lane="one", agent="worker")] if dispatched else []) + beats + uncertain
    state = loop_live.project(events, recorded + timedelta(minutes=1))
    assert state["live_phase"] == ("working" if dispatched else "preparing")
    assert state["evidence_at"] == recorded.isoformat()
    assert not state["phase_input"]["watch_active"]
    assert state["active_lanes"] is None
    assert state["parks_total"] is None
    assert state["phase_input"]["park_events_so_far"] is None
    assert state["tasks_admitted"] is None
    assert state["tasks_landed"] is None


@pytest.mark.parametrize("at", [None, 1, True, "invalid", "2026-01-01T00:00:00", "2026-01-01T00:00:00+00:00"])
def test_invalid_state_heartbeat_does_not_supply_activity_or_change_counts(at):
    parsed = loops.state_progress(heartbeat_log(at))
    assert parsed is not None  # Invalid heartbeat alone must not invalidate older count evidence.
    assert not any(e["ev"] == "heartbeat" for e in parsed[2])
    assert loop_live.project(parsed[2], AT)["live_phase"] is None
    older = loops.state_progress(heartbeat_log(heartbeat=False))
    assert older is not None and older[2] == []


def test_file_binding_requires_sanitised_selector_and_uses_existing_consumer(tmp_path, monkeypatch):
    token = tmp_path / "synthetic-token"
    token.write_text("synthetic-file-token\n")
    config = parse_config(
        {"sources": {}, "loop_live": {"api_key_env": "SYNTHETIC_LOOP_TOKEN", "api_key_file": str(token)}}
    )
    monkeypatch.setenv("SYNTHETIC_LOOP_TOKEN", "synthetic-env-token")
    assert config.loop_live.token() == "synthetic-env-token"
    monkeypatch.delenv("SYNTHETIC_LOOP_TOKEN")
    assert config.loop_live.token() == "synthetic-file-token"


@pytest.mark.parametrize("stop", ["jobs", "deadline", "empty"])
def test_independent_module_drainer_checks_job_limit_and_deadline_before_admission(monkeypatch, stop):
    calls = []
    config = Config(dsn="synthetic", loop_live=LoopLive(enabled=True))
    monkeypatch.setattr(loop_live, "load_config", lambda: config)
    monkeypatch.delenv("AGENT_HISTORY_DSN", raising=False)
    ticks = iter([0] + ([0, 61] if stop == "deadline" else [0] * 21))
    monkeypatch.setattr(loop_live.time, "monotonic", lambda: next(ticks))

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

    monkeypatch.setattr(loop_live.psycopg, "connect", lambda dsn, **kw: Connection())
    monkeypatch.setattr(loop_live, "drain_one", lambda *_: calls.append(True) is None and stop != "empty")
    loop_live.main([])
    assert len(calls) == (20 if stop == "jobs" else 1)

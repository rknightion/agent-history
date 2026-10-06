"""Exercise the real pi loader/post-pass and independent queue drain on a disposable database."""

from __future__ import annotations

import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from agent_history import load, loop_live
from agent_history.config import Config, LoopLive

psycopg = pytest.importorskip("psycopg")


def scratch_database(dsn):
    try:
        return psycopg.conninfo.conninfo_to_dict(dsn).get("dbname") == "agent_history_test"
    except psycopg.ProgrammingError:
        return False


DSN = os.environ.get("AGENT_HISTORY_TEST_DSN", "")
ADMIN_DSN = os.environ.get("AGENT_HISTORY_TEST_ADMIN_DSN", "")
pytestmark = pytest.mark.skipif(not scratch_database(DSN), reason="disposable agent_history_test database required")


@pytest.fixture
def clean():
    with load.connect(DSN) as conn:
        load.apply_schema(conn, force=True)
        conn.commit()
        conn.execute("TRUNCATE " + ",".join(f"ah.{table}" for table in load.DATA_TABLES) + " RESTART IDENTITY CASCADE")
        conn.execute("TRUNCATE ah.loop_live_job,ah.loop_live_cache,ah.loop_live_reservation,ah.loop_live_budget")
        conn.execute("DELETE FROM ah.meta WHERE key='loops_live_projection_v1'")
        conn.commit()
        yield conn
        conn.rollback()


@pytest.fixture
def live_config(monkeypatch):
    monkeypatch.setenv("SYNTHETIC_LOOP_TOKEN", "synthetic")
    config = LoopLive(
        True,
        "https://example.invalid/jev",
        "https://example.invalid/compat/chat/completions",
        "SYNTHETIC_LOOP_TOKEN",
        reasoning="off",
    )
    monkeypatch.setattr(loop_live, "load_config", lambda: Config(loop_live=config))
    return config


@pytest.fixture
def transcript(tmp_path):
    at = datetime.now(timezone.utc) - timedelta(minutes=2)
    source = tmp_path / "pi"
    path = source / "sessions" / "-synthetic-" / "synthetic.jsonl"
    path.parent.mkdir(parents=True)
    records = [
        {
            "type": "session",
            "id": "11111111-1111-4111-8111-111111111111",
            "timestamp": at.isoformat(),
            "cwd": "/tmp/synthetic",
            "version": 3,
        },
        {
            "type": "message",
            "id": "launch",
            "timestamp": at.isoformat(),
            "message": {"role": "user", "content": "You are the root. Write codex/report-synthetic-loop1.md."},
        },
    ]

    def append(command, ok=True, tool="bash", details=None, output="synthetic result"):
        n = len(records)
        ts = (at + timedelta(seconds=n)).isoformat()
        call = f"call-{n}"
        arguments = {"command": command} if tool == "bash" else command
        records.append(
            {
                "type": "message",
                "id": f"assistant-{n}",
                "timestamp": ts,
                "message": {
                    "role": "assistant",
                    "timestamp": ts,
                    "model": "test",
                    "stopReason": "toolUse",
                    "content": [{"type": "toolCall", "id": call, "name": tool, "arguments": arguments}],
                },
            }
        )
        records.append(
            {
                "type": "message",
                "id": f"result-{n}",
                "timestamp": ts,
                "message": {
                    "role": "toolResult",
                    "timestamp": ts,
                    "toolCallId": call,
                    "toolName": tool,
                    "isError": not ok,
                    "content": [{"type": "text", "text": output}],
                    "details": details or {"exitCode": 0 if ok else 1},
                },
            }
        )
        path.write_text("".join(json.dumps(row) + "\n" for row in records))

    sequence = 0

    def state(ev, fields="", ok=True):
        nonlocal sequence
        sequence += 1
        # Native seq receipts are the per-append proof, not a successful enclosing shell alone.
        append(
            f"loop-state append codex/state-synthetic-loop1.jsonl {ev} {fields}",
            ok=ok,
            output=f"seq={sequence}\n" if ok else "synthetic failure",
        )

    state("open")
    return {
        "sources": {"pi-test": source},
        "append": append,
        "state": state,
        "records": records,
        "at": at,
        "path": path,
    }


def refresh(conn, transcript):
    stats = load.refresh(conn, sources=transcript["sources"], textfile=None, log=lambda *_: None)
    assert stats.errors == 0
    return conn.execute("SELECT launch_uid,live_phase,active_lanes,summary_error FROM ah.loops").fetchone()


def fake_infer(config, kind, state):
    if kind == "jev":
        return {
            "state": "Completed",
            "result": {
                "model": loop_live.JEV_MODEL,
                "answers": {
                    **{key: {"type": "noul", "noul": 0.0} for key in loop_live.HYBRID_QUESTIONS},
                    "phase": {
                        "type": "choice",
                        "choice": "waiting",
                        "probabilities": {phase: float(phase == "waiting") for phase in loop_live.PHASES},
                    },
                },
                "usage": {"input_tokens": 1000, "output_tokens": 100},
            },
        }
    return {
        "choices": [
            {
                "finish_reason": "stop",
                "message": {
                    "content": json.dumps(
                        {
                            "headline": "Work in progress",
                            "summary": "The implementation lane is working. A gate remains pending.",
                        }
                    )
                },
            }
        ],
        "usage": {"prompt_tokens": 1000, "completion_tokens": 100},
    }


@pytest.mark.parametrize("jev_shape", ["legacy", "cf-direct", "cf-run-record"])
def test_real_collector_structure_summary_history_and_failed_call(clean, transcript, live_config, jev_shape):
    def documented_infer(config, kind, state):
        response = fake_infer(config, kind, state)
        if kind == "jev" and jev_shape != "legacy":
            result = response["result"] if jev_shape == "cf-direct" else response
            return {"success": True, "result": result, "errors": [], "messages": []}
        return response

    transcript["state"]("admit", "task=TASK-1")
    transcript["state"]("dispatch", "lane=one task=TASK-1 agent=complex-worker run=synthetic-run")
    transcript["state"]("gate", "scope=composed sha=synthetic exit=1")
    transcript["state"]("park", "task=TASK-2 needs=owner 'reason=Owner decision needed'")
    transcript["state"]("judgement", "'text=The root is checking the candidate.'")
    uid, phase, lanes, error = refresh(clean, transcript)
    assert (phase, len(lanes), error) == ("working", 1, None)
    assert clean.execute(
        "SELECT tasks_admitted,tasks_landed,parks_total,last_gate,last_park FROM ah.loops"
    ).fetchone() == (
        1,
        0,
        1,
        {
            "sha": "synthetic",
            "scope": "composed",
            "exit": 1,
            "at": clean.execute("SELECT last_gate->>'at' FROM ah.loops").fetchone()[0],
        },
        {"task": "TASK-2", "needs": "owner", "reason": "Owner decision needed"},
    )
    before = clean.execute("SELECT phase_since FROM ah.loops").fetchone()[0]
    clean.commit()
    with psycopg.connect(DSN, autocommit=True) as worker:
        assert loop_live.drain_one(worker, live_config, documented_infer)
        assert not loop_live.drain_one(worker, live_config, documented_infer)
    refresh(clean, transcript)
    assert clean.execute(
        "SELECT jev_phase,live_phase,headline,summary_model,final_summary FROM ah.loops"
    ).fetchone() == ("waiting", "working", "Work in progress", loop_live.SUMMARY_MODEL, False)
    assert clean.execute("SELECT phase_since FROM ah.loops").fetchone()[0] == before
    assert clean.execute("SELECT count(*) FROM ah.loop_phase_event").fetchone()[0] == 2
    refresh(clean, transcript)
    assert clean.execute("SELECT count(*) FROM ah.loop_phase_event").fetchone()[0] == 2
    # A retained cache reply that fails validation must not make the collector fail or erase text.
    clean.execute("UPDATE ah.loop_live_cache SET response='{}'::jsonb WHERE kind='summary'")
    clean.commit()
    refresh(clean, transcript)
    assert clean.execute("SELECT headline,summary_error FROM ah.loops").fetchone() == (
        "Work in progress",
        "incomplete_summary",
    )
    clean.execute(
        "UPDATE ah.loop_live_cache SET response=%s WHERE kind='summary'",
        (psycopg.types.json.Jsonb(fake_infer(live_config, "summary", {})),),
    )
    clean.execute("UPDATE ah.loop_live_cache SET response='{}'::jsonb WHERE kind='jev'")
    clean.commit()
    refresh(clean, transcript)
    assert clean.execute("SELECT live_phase,jev_phase,summary_error FROM ah.loops").fetchone() == (
        "working",
        None,
        "invalid_jev_response",
    )
    clean.execute(
        "UPDATE ah.loop_live_cache SET response=%s WHERE kind='jev'",
        (psycopg.types.json.Jsonb(fake_infer(live_config, "jev", {})),),
    )
    clean.commit()
    # Failed appends do not prove an event or an exact zero. A subsequent receipt for the
    # same task resolves this distinct-task uncertainty, without counting the failed call.
    transcript["state"]("land", "task=TASK-1", ok=False)
    refresh(clean, transcript)
    assert clean.execute("SELECT tasks_landed FROM ah.loops").fetchone()[0] is None
    transcript["state"]("land", "task=TASK-1")
    refresh(clean, transcript)
    clean.commit()

    def fail(*_):
        raise TimeoutError("synthetic provider timeout")

    with psycopg.connect(DSN, autocommit=True) as worker:
        assert loop_live.drain_one(worker, live_config, fail)
    refresh(clean, transcript)
    assert clean.execute("SELECT tasks_landed,headline,summary_error FROM ah.loops").fetchone() == (
        1,
        "Work in progress",
        "timeout",
    )
    assert clean.execute("SELECT error FROM ah.loop_live_cache WHERE error IS NOT NULL").fetchall() == [
        ("TimeoutError: synthetic provider timeout",),
        ("TimeoutError: synthetic provider timeout",),
    ]
    assert clean.execute("SELECT count(*) FROM ah.loop_live_reservation").fetchone()[0] == 4
    assert clean.execute("SELECT reserved_usd FROM ah.loop_live_budget").fetchone()[0] == 2 * (
        loop_live.reservation("jev") + loop_live.reservation("summary")
    )


def test_slow_worker_does_not_hold_collector_and_close_is_once(clean, transcript, live_config):
    transcript["state"]("dispatch", "lane=one task=TASK-1 agent=complex-worker run=one")
    refresh(clean, transcript)
    clean.commit()
    started, release = threading.Event(), threading.Event()

    def slow(config, kind, state):
        started.set()
        if not release.wait(10):
            raise TimeoutError("synthetic test deadline")
        return fake_infer(config, kind, state)

    def work():
        with psycopg.connect(DSN, autocommit=True) as worker:
            loop_live.drain_one(worker, live_config, slow)

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(work)
        try:
            assert started.wait(5)
            transcript["state"]("park", "task=TASK-2 needs=dependency reason=Pending")
            before = time.monotonic()
            refresh(clean, transcript)
            assert time.monotonic() - before < 5
            assert clean.execute("SELECT parks_total FROM ah.loops").fetchone()[0] == 1
            clean.commit()
        finally:
            release.set()
        future.result(timeout=10)
    transcript["state"]("return", "run=one")
    transcript["state"]("close", "reason=blocked")
    refresh(clean, transcript)
    clean.commit()
    with psycopg.connect(DSN, autocommit=True) as worker:
        assert loop_live.drain_one(worker, live_config, fake_infer)
    refresh(clean, transcript)
    assert clean.execute("SELECT live_phase,final_summary FROM ah.loops").fetchone() == ("closing", True)
    clean.commit()
    with psycopg.connect(DSN, autocommit=True) as worker:
        assert not loop_live.drain_one(worker, live_config, fake_infer)


def test_global_atomic_budget_survives_rebuild_and_missing_usage(clean, transcript, live_config):
    refresh(clean, transcript)
    clean.commit()

    def no_usage(config, kind, state):
        response = fake_infer(config, kind, state)
        response.pop("usage", None)
        response.get("result", {}).pop("usage", None)
        return response

    with psycopg.connect(DSN, autocommit=True) as worker:
        assert loop_live.drain_one(worker, live_config, no_usage)
    refresh(clean, transcript)
    initial = clean.execute("SELECT reserved_usd FROM ah.loop_live_budget").fetchone()[0]
    assert initial == loop_live.reservation("jev") + loop_live.reservation("summary")
    clean.commit()

    def reserve(index):
        with psycopg.connect(DSN, autocommit=True) as connection:
            return loop_live.reserve(connection, f"synthetic-{index}", "key", "summary")

    with ThreadPoolExecutor(max_workers=8) as executor:
        admitted = list(executor.map(reserve, range(25)))
    amount = loop_live.reservation("summary")
    spent = initial + sum(admitted) * amount
    assert spent <= Decimal("5") < spent + amount
    assert clean.execute("SELECT reserved_usd FROM ah.loop_live_budget").fetchone()[0] == spent
    clean.commit()
    before = clean.execute("SELECT reserved_usd FROM ah.loop_live_budget").fetchone()[0]
    cache = clean.execute("SELECT count(*) FROM ah.loop_live_cache").fetchone()[0]
    clean.commit()
    assert load.rebuild(clean, sources=transcript["sources"], textfile=None, log=lambda *_: None).errors == 0
    assert clean.execute("SELECT reserved_usd FROM ah.loop_live_budget").fetchone()[0] == before
    assert clean.execute("SELECT count(*) FROM ah.loop_live_cache").fetchone()[0] == cache
    assert clean.execute("SELECT headline FROM ah.loops").fetchone()[0] == "Work in progress"
    clean.commit()
    with psycopg.connect(DSN, autocommit=True) as worker:
        assert not loop_live.drain_one(worker, live_config, fake_infer)


def test_spawn_and_append_exact_run_dedup_title_and_return(clean, transcript, live_config):
    transcript["append"](
        {
            "agent": "security-reviewer",
            "async": True,
            "task": "Lane: review · Task: TASK-1 (Review the synthetic candidate)",
        },
        tool="subagent",
        details={"mode": "async", "runId": "synthetic-review", "results": []},
    )
    transcript["state"]("dispatch", "lane=review task=TASK-1 agent=security-reviewer run=synthetic-review")
    refresh(clean, transcript)
    row = clean.execute("SELECT live_phase,active_lanes FROM ah.loops").fetchone()
    assert row[0] == "reviewing" and len(row[1]) == 1
    assert row[1][0]["title"] == "Review the synthetic candidate"
    transcript["state"]("return", "run=synthetic-review")
    refresh(clean, transcript)
    assert clean.execute("SELECT active_lanes FROM ah.loops").fetchone()[0] == []


def test_native_batch_watch_and_parser_spawn_rows_at_catalogue_surface(clean, transcript, live_config):
    # Native default-async evidence must cross the real parser/Writer boundary, not a seeded
    # spawn row. The source contract requires matching UUIDs, not an arbitrary run label.
    native_run = "22222222-2222-4222-8222-222222222222"
    n = len(transcript["records"])
    call_uid = f"call-{n}"
    transcript["append"](
        {"agent": "complex-worker", "task": "Lane: BUILD · Task: TASK-1 (Repair synthetic gate)"},
        tool="subagent",
        details={"mode": "single", "runId": native_run, "asyncId": native_run, "results": []},
    )
    # Preserve the distinction between message generation, entry dispatch and accepted result.
    entry = datetime.fromisoformat(transcript["records"][-2]["timestamp"])
    transcript["records"][-2]["message"]["timestamp"] = int((entry - timedelta(seconds=6)).timestamp() * 1000)
    transcript["records"][-1]["timestamp"] = (entry + timedelta(milliseconds=139)).isoformat()
    transcript["path"].write_text("".join(json.dumps(r) + "\n" for r in transcript["records"]))
    transcript["append"](
        "loop-state append codex/state-synthetic-loop1.jsonl admit task=TASK-1; "
        "loop-state append codex/state-synthetic-loop1.jsonl admit task=TASK-2; backlog task list --plain",
        output="seq=12\nseq=13\nSynthetic list",
    )
    transcript["append"](
        'loop-state append codex/state-synthetic-loop1.jsonl gate scope=composed exit=1 cmd="build && check"; '
        "loop-state append codex/state-synthetic-loop1.jsonl judgement 'text=Check the unchanged candidate.'",
        output="seq=14\nseq=15\n",
    )
    transcript["append"](
        {"command": "synthetic gate", "deadline_s": 600, "interval_s": 60, "label": "Gate proof"},
        tool="watch_start",
        details={"id": "native-watch"},
    )
    uid, phase, _, _ = refresh(clean, transcript)
    assert phase == "gating"
    assert clean.execute(
        "SELECT spawned_at,workflow_id,requested_type,launch_status FROM ah.subagent_spawn WHERE spawn_uid=%s",
        (call_uid,),
    ).fetchone() == (entry, native_run, "complex-worker", "launched")
    fields = clean.execute(
        "SELECT active_lanes,tasks_admitted,tasks_landed,last_gate,last_judgement FROM ah.loops"
    ).fetchone()
    assert fields[0] == [
        {
            "lane": "BUILD",
            "task": "TASK-1",
            "title": "Repair synthetic gate",
            "agent": "complex-worker",
            "started_at": entry.isoformat(timespec="microseconds").replace("+00:00", "Z"),
        }
    ]
    assert fields[1:3] == (2, 0)
    assert fields[3]["exit"] == 1
    assert fields[4] == "Check the unchanged candidate."
    # Truncated successful registrations cannot be discarded as if no watcher existed.
    clean.execute("UPDATE ah.tool_io SET output_truncated=true WHERE tool_name='watch_start'")
    loop_live.refresh(clean)
    assert clean.execute("SELECT live_phase,tasks_admitted FROM ah.loops").fetchone() == (None, 2)
    clean.execute("UPDATE ah.tool_io SET output_truncated=false WHERE tool_name='watch_start'")
    loop_live.refresh(clean)
    assert clean.execute("SELECT live_phase FROM ah.loops").fetchone()[0] == "gating"
    at = transcript["at"] + timedelta(seconds=len(transcript["records"]) + 1)
    transcript["records"].append(
        {
            "type": "custom_message",
            "id": "terminal",
            "timestamp": at.isoformat(),
            "customType": "loop-watch",
            "content": "WATCH native-watch (Gate proof): phase=failed exit_code=1 deadline_hit=false",
            "details": {
                "id": "native-watch",
                "receipt": {
                    "phase": "failed",
                    "observations": 1,
                    "last_observed_at": (at - timedelta(seconds=3)).isoformat(),
                    "deadline": (at + timedelta(minutes=9)).isoformat(),
                    "result": {"exit_code": 1, "deadline_hit": False, "signal": None},
                    "pid": 123,
                    "command": "synthetic " * 250,
                    "interval_s": 60,
                    "label": "Gate proof",
                    "tail": [],
                },
            },
        }
    )
    transcript["records"].append(
        {
            "type": "custom_message",
            "id": "wake",
            "timestamp": (at + timedelta(seconds=1)).isoformat(),
            "customType": "loop-wake",
            "content": "WAKE timer-one: Backstop",
            "details": {"id": "timer-one", "reason": "Backstop"},
        }
    )
    transcript["path"].write_text("".join(json.dumps(r) + "\n" for r in transcript["records"]))
    assert refresh(clean, transcript)[1] == "working"
    target = clean.execute(
        "SELECT id,root_session_id,launch_ts,report_path FROM ah.loop_run WHERE launch_uid=%s", (uid,)
    ).fetchone()
    events = loop_live._events(clean, target[0], target[1], target[2], None, target[3])
    assert any(e["ev"] == "wake" for e in events)
    assert not any(e["ev"] == "heartbeat" for e in events)
    assert any(e.get("op") == "stop" and e["at"] == at for e in events)
    assert (
        clean.execute("SELECT detail ? 'details' FROM ah.message WHERE event_uid LIKE '%%:terminal'").fetchone()[0]
        is False
    )


def test_real_collector_nulls_nonconforming_state_values_without_enrichment(
    clean, transcript, live_config, monkeypatch
):
    def no_paid_calls(*args, **kwargs):
        pytest.fail("structural collection attempted paid inference")

    monkeypatch.setattr(loop_live, "request", no_paid_calls)
    monkeypatch.setattr(loop_live, "drain_one", no_paid_calls)
    transcript["state"]("dispatch", 'lane=one task=TASK-1 agent=complex-worker run=one \'title={"text":"invalid"}\'')
    transcript["state"]("gate", "sha=synthetic scope=composed exit=2147483648")
    uid, phase, lanes, error = refresh(clean, transcript)
    assert phase == "working" and error is None
    assert lanes[0]["title"] is None
    gate = clean.execute("SELECT last_gate FROM ah.loops WHERE launch_uid=%s", (uid,)).fetchone()[0]
    assert gate["exit"] is None
    assert gate["sha"] == "synthetic" and gate["scope"] == "composed"
    assert gate["at"].endswith("Z") and len(gate["at"]) == 27
    assert clean.execute("SELECT count(*) FROM ah.loop_live_reservation").fetchone()[0] == 0
    assert clean.execute("SELECT count(*) FROM ah.loop_live_cache").fetchone()[0] == 0
    transcript["state"]("gate", "sha=synthetic scope=composed exit=0")
    refresh(clean, transcript)
    assert clean.execute("SELECT last_gate->'exit' FROM ah.loops").fetchone()[0] == 0


@pytest.mark.parametrize("acceptance", ["valid", "failed", "conflicting"])
def test_native_dispatch_requires_matching_acceptance_at_incremental_boundary(
    clean, transcript, live_config, acceptance
):
    run = "33333333-3333-4333-8333-333333333333"
    transcript["append"](
        {"agent": "complex-worker", "task": "Lane: BUILD · Task: TASK-1 (Build synthetic candidate)"},
        tool="subagent",
        ok=acceptance != "failed",
        details={
            "mode": "single",
            "runId": run,
            "asyncId": run if acceptance != "conflicting" else "44444444-4444-4444-8444-444444444444",
            "results": [],
        },
    )
    result = transcript["records"].pop()
    entry = datetime.fromisoformat(transcript["records"][-1]["timestamp"])
    transcript["path"].write_text("".join(json.dumps(r) + "\n" for r in transcript["records"]))
    # Fail-first causal boundary: a request alone cannot supply a timed/typed launched lane.
    assert refresh(clean, transcript)[1:3] == ("preparing", [])
    assert clean.execute("SELECT count(*) FROM ah.subagent_spawn").fetchone()[0] == 0
    transcript["records"].append(result)
    transcript["path"].write_text("".join(json.dumps(r) + "\n" for r in transcript["records"]))
    _, phase, lanes, _ = refresh(clean, transcript)
    if acceptance == "valid":
        assert phase == "working"
        assert lanes == [
            {
                "lane": "BUILD",
                "task": "TASK-1",
                "title": "Build synthetic candidate",
                "agent": "complex-worker",
                "started_at": entry.isoformat(timespec="microseconds").replace("+00:00", "Z"),
            }
        ]
        assert clean.execute("SELECT spawned_at FROM ah.subagent_spawn").fetchone()[0] == entry
    else:
        assert phase == "preparing" and lanes == []
        assert clean.execute("SELECT count(*) FROM ah.subagent_spawn").fetchone()[0] == 0
    assert clean.execute("SELECT count(*) FROM ah.loop_live_reservation").fetchone()[0] == 0


def test_explicit_heartbeat_payload_crosses_real_parser_and_projection(clean, transcript, live_config, monkeypatch):
    # Historical heartbeats are not inferred. This independently exercises the explicit payload
    # seam on a synthetic root whose timestamps are all recorded.
    monkeypatch.setattr(loop_live, "load_config", lambda: Config(loop_live=LoopLive(enabled=False)))
    transcript["state"]("dispatch", "lane=one agent=complex-worker run=one")
    at = transcript["at"] + timedelta(minutes=60)
    transcript["records"].append(
        {
            "type": "custom_message",
            "id": "heartbeat",
            "timestamp": at.isoformat(),
            "customType": "loop-heartbeat",
            "content": "Heartbeat",
            "details": {"at": at.isoformat()},
        }
    )
    transcript["path"].write_text("".join(json.dumps(r) + "\n" for r in transcript["records"]))
    uid, _, _, _ = refresh(clean, transcript)
    target = clean.execute(
        "SELECT id,root_session_id,launch_ts,report_path FROM ah.loop_run WHERE launch_uid=%s", (uid,)
    ).fetchone()
    events = loop_live._events(clean, *target[:3], None, target[3])
    assert [e for e in events if e["ev"] == "heartbeat"] == [{"ev": "heartbeat", "at": at}]
    assert loop_live.project(events, at + timedelta(minutes=1))["live_phase"] == "working"
    assert (
        loop_live.project([e for e in events if e["ev"] != "heartbeat"], at + timedelta(minutes=1))["live_phase"]
        == "waiting"
    )
    assert clean.execute("SELECT count(*) FROM ah.loop_live_reservation").fetchone()[0] == 0


@pytest.mark.skipif(not scratch_database(ADMIN_DSN), reason="disposable admin database DSN required for role proof")
def test_grant_preservation_and_finished_summary_application(clean, transcript, live_config):
    refresh(clean, transcript)
    clean.commit()
    with psycopg.connect(ADMIN_DSN) as admin:
        admin.execute("CREATE ROLE synthetic_phase_reader")
    try:
        clean.execute("GRANT SELECT ON ah.loops TO synthetic_phase_reader")
        clean.commit()
        transcript["state"]("close", "reason=owner-stop")
        refresh(clean, transcript)
        clean.execute("UPDATE ah.loop_run SET end_evidence='next_launch',end_ts=now()")
        clean.commit()
        load.post_passes(clean)
        clean.commit()
        with psycopg.connect(DSN, autocommit=True) as worker:
            assert loop_live.drain_one(worker, live_config, fake_infer)
        load.post_passes(clean)
        assert clean.execute("SELECT status,final_summary FROM ah.loops").fetchone() == ("finished", True)
        clean.commit()
        load.apply_schema(clean, force=True)
        clean.commit()
        assert load.rebuild(clean, sources=transcript["sources"], textfile=None, log=lambda *_: None).errors == 0
        assert clean.execute("SELECT has_table_privilege('synthetic_phase_reader','ah.loops','SELECT')").fetchone()[0]
        assert not clean.execute(
            "SELECT has_table_privilege('synthetic_phase_reader','ah.loop_live_cache','SELECT')"
        ).fetchone()[0]
    finally:
        clean.rollback()
        with psycopg.connect(ADMIN_DSN) as admin:
            admin.execute("DROP OWNED BY synthetic_phase_reader")
            admin.execute("DROP ROLE synthetic_phase_reader")

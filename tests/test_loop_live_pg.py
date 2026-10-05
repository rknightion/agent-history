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

    def append(command, ok=True, tool="bash", details=None):
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
                    "content": [{"type": "text", "text": "synthetic result"}],
                    "details": details or {"exitCode": 0 if ok else 1},
                },
            }
        )
        path.write_text("".join(json.dumps(row) + "\n" for row in records))

    def state(ev, fields="", ok=True):
        append(f"loop-state append codex/state-synthetic-loop1.jsonl {ev} {fields}", ok=ok)

    state("open")
    return {"sources": {"pi-test": source}, "append": append, "state": state, "records": records, "at": at}


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
    # A failed recorded append is not structured evidence.
    transcript["state"]("land", "task=TASK-1", ok=False)
    refresh(clean, transcript)
    assert clean.execute("SELECT tasks_landed FROM ah.loops").fetchone()[0] == 0
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

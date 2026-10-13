"""Real loader boundary: live checkpoints are replacement snapshots, not append-only sessions."""

import json
import os
import subprocess
from pathlib import Path

import pytest

from agent_history import load
from test_parse_pi_progress import BASE_MS, CTX, POS, REQUEST, SID, assistant, progress
from agent_history.model import SessionKey

DSN = os.environ.get("AGENT_HISTORY_TEST_DSN", "")
pytestmark = pytest.mark.skipif(not DSN or "agent_history_test" not in DSN, reason="Disposable PostgreSQL DSN required")


@pytest.fixture
def conn():
    connection = load.connect(DSN)
    load.apply_schema(connection, force=True)
    connection.execute("TRUNCATE " + ", ".join(f"ah.{t}" for t in load.DATA_TABLES) + " RESTART IDENTITY CASCADE")
    connection.commit()
    yield connection
    connection.close()


def write_checkpoint(home, record):
    path = home / "progress" / f"{SID}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    # Atomic rename is the producer's real surface, not a transcript-only parser shortcut.
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(record) + "\n")
    temporary.replace(path)
    return path


def refresh(conn, home, *, archive=False):
    if archive:
        return load.refresh(conn, hot=home.parent, cold=None, textfile=None, log=lambda *_: None)
    return load.refresh(conn, sources={"pi-toy": home}, textfile=None, log=lambda *_: None)


def read(conn):
    row = conn.execute(
        "SELECT request_uid, state, model_events, keepalives, last_model_event_at, "
        "last_keepalive_at, response_id, effort FROM ah.request_progress"
    ).fetchone()
    conn.commit()
    return row


def test_precompletion_replacement_duplicates_stale_final_and_resume(conn, tmp_path):
    home = tmp_path / "pi-toy"
    write_checkpoint(home, progress())
    assert refresh(conn, home).errors == 0
    initial = read(conn)
    assert initial[:4] == (REQUEST, "active", 0, None)
    assert initial[4:] == (None, None, None, None)
    assert conn.execute("SELECT count(*) FROM ah.llm_call").fetchone()[0] == 0
    assert conn.execute("SELECT first_event_at,last_event_at FROM ah.session").fetchone() == (None, None)
    newer = progress(
        writtenAt=BASE_MS + 10000,
        ageMs=10000,
        modelEvents=2,
        firstModelEventAt=BASE_MS + 7000,
        lastModelEventAt=BASE_MS + 8000,
        providerEvents=10,
        keepalives=3,
        lastKeepaliveAt=BASE_MS + 9000,
    )
    write_checkpoint(home, newer)
    assert refresh(conn, home, archive=True).errors == 0
    updated = read(conn)
    assert updated[2:4] == (2, 3) and updated[4] is not None and updated[5] is not None
    for record in (
        newer,
        progress(),
        progress(writtenAt=BASE_MS + 10000, modelEvents=999),
        progress(writtenAt=BASE_MS + 11000, ageMs=11000),  # regressed counters, newer stamp
        newer | {"writtenAt": BASE_MS + 11000, "ageMs": 11000, "lastModelEventAt": BASE_MS + 7500},
        newer | {"writtenAt": BASE_MS + 11000, "ageMs": 11000, "pid": 999},
    ):
        write_checkpoint(home, record)
        assert refresh(conn, home).errors == 0
        assert read(conn) == updated
    final = newer | {
        "state": "ended",
        "endedAt": BASE_MS + 12000,
        "writtenAt": BASE_MS + 12000,
        "ageMs": 12000,
        "stopReason": "stop",
    }
    write_checkpoint(home, final)
    refresh(conn, home)
    session = home / "sessions" / "toy" / "toy.jsonl"
    session.parent.mkdir(parents=True)
    header = {"type": "session", "id": SID, "timestamp": "2026-09-21T14:13:20Z"}
    session.write_text(json.dumps(header) + "\n" + json.dumps(assistant(final, thinkingLevel="high")) + "\n")
    assert refresh(conn, home).errors == 0
    ended = read(conn)
    assert ended[1] == "ended" and ended[6:] == ("toy-response", "high")
    write_checkpoint(home, progress(writtenAt=BASE_MS + 20000, ageMs=20000))
    refresh(conn, home)
    assert read(conn) == ended  # A later active snapshot cannot resurrect a terminal request.
    # Resume may reset ordinal and pid. A new UUID is a distinct request, not a replaced attempt.
    next_request = "44444444-4444-4444-8444-444444444444"
    write_checkpoint(home, progress(request=next_request, pid=456, turn=1))
    refresh(conn, home)
    assert conn.execute("SELECT count(*) FROM ah.request_progress").fetchone()[0] == 2
    # Removing a checkpoint cannot delete already observed history.
    (home / "progress" / f"{SID}.json").unlink()
    refresh(conn, home)
    assert conn.execute("SELECT count(*) FROM ah.request_progress").fetchone()[0] == 2


@pytest.mark.parametrize("terminal", ["ended", "abandoned"])
@pytest.mark.parametrize("via_envelope", [False, True])
def test_equal_stamp_terminal_preserves_transcript_batch(conn, tmp_path, terminal, via_envelope):
    home = tmp_path / "pi-toy"
    write_checkpoint(home, progress())
    assert refresh(conn, home).errors == 0
    final = progress(state=terminal, endedAt=BASE_MS + 5000, stopReason="stop")
    if via_envelope:
        session = home / "sessions" / "toy" / "toy.jsonl"
        session.parent.mkdir(parents=True)
        header = {"type": "session", "id": SID, "timestamp": "2026-09-21T14:13:20Z"}
        session.write_text(json.dumps(header) + "\n" + json.dumps(assistant(final, thinkingLevel="high")) + "\n")
    else:
        write_checkpoint(home, final)
    assert refresh(conn, home).errors == 0
    assert read(conn)[1] == terminal
    assert conn.execute("SELECT ended_at=written_at FROM ah.request_progress").fetchone() == (True,)
    if via_envelope:
        assert conn.execute("SELECT count(*) FROM ah.llm_call").fetchone() == (1,)
        assert read(conn)[6:] == ("toy-response", "high")


@pytest.mark.parametrize("stamp", [5000, 10000])
def test_stale_or_equal_activity_cannot_fill_first_fields(conn, tmp_path, stamp):
    home = tmp_path / "pi-toy"
    write_checkpoint(home, progress(writtenAt=BASE_MS + 10000, ageMs=10000))
    assert refresh(conn, home).errors == 0
    old = progress(
        writtenAt=BASE_MS + stamp,
        modelEvents=1,
        firstModelEventAt=BASE_MS + 1000,
        lastModelEventAt=BASE_MS + 1000,
        providerResponses=1,
        firstResponseAt=BASE_MS + 1000,
        **{
            group: {"started": 1, "deltas": 0, "completed": 0, "firstAt": BASE_MS + 1000, "lastAt": BASE_MS + 1000}
            for group in ("reasoning", "text", "toolCalls")
        },
    )
    # A stale envelope may link identity and effort, but not its activity snapshot.
    session = home / "sessions" / "toy" / "toy.jsonl"
    session.parent.mkdir(parents=True)
    header = {"type": "session", "id": SID, "timestamp": "2026-09-21T14:13:20Z"}
    session.write_text(json.dumps(header) + "\n" + json.dumps(assistant(old, thinkingLevel="high")) + "\n")
    assert refresh(conn, home).errors == 0
    assert conn.execute(
        "SELECT model_events,first_model_event_at,last_model_event_at,provider_responses,first_response_at,"
        "reasoning_started,reasoning_first_at,text_started,text_first_at,tool_calls_started,tool_calls_first_at "
        "FROM ah.request_progress"
    ).fetchone() == (0, None, None, 0, None, 0, None, 0, None, 0, None)
    assert read(conn)[6:] == ("toy-response", "high")


def test_terminal_snapshot_is_immutable_but_envelope_can_link(conn, tmp_path):
    home = tmp_path / "pi-toy"
    final = progress(state="ended", endedAt=BASE_MS + 5000, stopReason="stop")
    write_checkpoint(home, final)
    assert refresh(conn, home).errors == 0
    query = "SELECT state,ended_at,written_at,model_events,first_model_event_at FROM ah.request_progress"
    expected = conn.execute(query).fetchone()
    conn.commit()
    newer = final | {
        "writtenAt": BASE_MS + 10000,
        "modelEvents": 1,
        "firstModelEventAt": BASE_MS + 1000,
        "lastModelEventAt": BASE_MS + 1000,
    }
    session = home / "sessions" / "toy" / "toy.jsonl"
    session.parent.mkdir(parents=True)
    header = {"type": "session", "id": SID, "timestamp": "2026-09-21T14:13:20Z"}
    session.write_text(json.dumps(header) + "\n" + json.dumps(assistant(newer, thinkingLevel="high")) + "\n")
    assert refresh(conn, home).errors == 0
    assert conn.execute(query).fetchone() == expected
    assert read(conn)[6:] == ("toy-response", "high")


def test_checkpoint_validation_and_path_boundary(conn, tmp_path):
    home = tmp_path / "pi-toy"
    path = write_checkpoint(home, progress(v=2))
    refresh(conn, home)
    assert read(conn) is None
    write_checkpoint(home, progress(session="other"))
    refresh(conn, home)
    assert read(conn) is None
    path.unlink()
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps(progress()))
    path.symlink_to(outside)
    refresh(conn, home)
    assert read(conn) is None
    path.unlink()
    write_checkpoint(home, progress())
    refresh(conn, home)
    assert read(conn) is not None


def test_recovery_and_bounded_diagnostics(conn, tmp_path):
    home = tmp_path / "pi-toy"
    session = home / "sessions" / "toy" / "toy.jsonl"
    session.parent.mkdir(parents=True)
    final = progress(
        state="ended",
        endedAt=BASE_MS + 5000,
        stopReason="error",
        modelEvents=2,
        firstModelEventAt=BASE_MS + 1,
        lastModelEventAt=BASE_MS + 2,
        reasoning={"started": 1, "deltas": 0, "completed": 1, "firstAt": BASE_MS + 1, "lastAt": BASE_MS + 2},
    )
    record = assistant(
        final,
        thinkingLevel="high",
        loopPiRecovery={
            "attemptId": "33333333-3333-4333-8333-333333333333",
            "code": "stream_incomplete",
            "status": 502,
            "disposition": "stop",
            "reason": "protected-request",
            "outputObserved": True,
            "requestPortable": False,
            "toolsExecuted": False,
        },
    )
    records = [
        {"type": "session", "id": SID, "timestamp": "2026-09-21T14:13:20Z"},
        record,
        {
            "type": "custom_message",
            "id": "incident",
            "timestamp": "2026-09-21T14:13:25Z",
            "customType": "loop-request-incident",
            "content": "toy",
            "details": {"kind": "wall-clock", "attempt": 1, "exhausted": False, "elapsedMs": 840001},
        },
    ]
    session.write_text("\n".join(json.dumps(r) for r in records) + "\n")
    assert refresh(conn, home).errors == 0
    observed = conn.execute(
        "SELECT model, effort, transport, reasoning_items, recovery_refusals "
        "FROM ah.request_progress_diagnostics('pi-toy', to_timestamp(%s), 20)",
        (BASE_MS / 1000,),
    ).fetchall()
    assert ("toy-model", "high", "openai-responses", 1, 1) in observed
    assert (
        conn.execute(
            "SELECT count(*) FROM ah.request_incident WHERE timeout_source="
            "'request-wall-clock-ceiling' AND request_uid IS NULL AND wall_clock_limit_ms IS NULL"
        ).fetchone()[0]
        == 1
    )
    before = conn.execute("SELECT * FROM ah.request_recovery").fetchall()
    conn.commit()
    refresh(conn, home)
    assert conn.execute("SELECT * FROM ah.request_recovery").fetchall() == before
    # A forced source rewrite does not purge newer progress from independent checkpoints.
    write_checkpoint(home, final | {"writtenAt": BASE_MS + 6000})
    refresh(conn, home)
    session.write_text("\n".join(json.dumps(r) for r in [records[0], record]) + "\n")
    refresh(conn, home)
    assert conn.execute("SELECT count(*) FROM ah.request_progress").fetchone()[0] == 1


def test_heartbeat_silence_reasoning_unknown_and_bounded_sql(conn, tmp_path):
    home = tmp_path / "pi-toy"
    write_checkpoint(home, progress(providerEvents=8, keepalives=4, lastKeepaliveAt=BASE_MS + 4000))
    refresh(conn, home)
    query = (
        "SELECT reasoning_items,heartbeat_only_requests,silent_requests "
        "FROM ah.request_progress_diagnostics('pi-toy',to_timestamp(%s),1)"
    )
    assert conn.execute(query, (BASE_MS / 1000,)).fetchone() == (0, 1, 1)
    conn.commit()
    write_checkpoint(
        home,
        progress(
            writtenAt=BASE_MS + 10000,
            ageMs=10000,
            request="55555555-5555-4555-8555-555555555555",
            api="unsupported-api",
        ),
    )
    refresh(conn, home)
    records = conn.execute(
        "SELECT transport,heartbeat_only_requests FROM "
        "ah.request_progress_diagnostics('pi-toy',to_timestamp(%s),10000)",
        (BASE_MS / 1000,),
    ).fetchall()
    assert ("unsupported-api", None) in records
    assert (
        conn.execute(
            "SELECT count(*) FROM ah.request_progress_diagnostics('pi-toy',to_timestamp(%s),0)", (BASE_MS / 1000,)
        ).fetchone()[0]
        == 0
    )


def test_readonly_exact_version_natural_key_recipe(conn, tmp_path):
    reader_dsn = os.environ.get("AGENT_HISTORY_TEST_READER_DSN")
    if not reader_dsn:
        pytest.skip("Disposable read-only role DSN required")
    home = tmp_path / "pi-toy"
    write_checkpoint(home, progress())
    refresh(conn, home)
    expected = load.schema_hash()
    conn.commit()
    env = os.environ | {"AGENT_HISTORY_READER_DSN": reader_dsn}
    command = ["just", "verify-request-progress-catalogue", "pi-toy", SID, REQUEST]
    cwd = Path(__file__).resolve().parents[1]
    wrong = subprocess.run(command + ["0" * 64], cwd=cwd, env=env, capture_output=True, text=True, timeout=30)
    assert wrong.returncode != 0 and "version mismatch" in wrong.stderr
    result = subprocess.run(command + [expected], cwd=cwd, env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    output = json.loads(result.stdout)
    assert output["schema_hash"] == expected
    assert output["request_evidence"][0] == REQUEST and output["request_evidence"][3] == "active"
    assert "text" not in output and "content" not in output


def test_recovery_out_of_order_and_explicit_episode_limit(conn, tmp_path):
    home = tmp_path / "pi-toy"
    session = home / "sessions" / "toy" / "toy.jsonl"
    session.parent.mkdir(parents=True)
    header = {"type": "session", "id": SID, "timestamp": "2026-09-21T14:13:20Z"}
    newest = assistant(loopPiRecovery={"disposition": "stop", "reason": "episode-expired", "toolsExecuted": False})
    stale = assistant(loopPiRecovery={"disposition": "retry", "reason": "unstarted-request", "toolsExecuted": True})
    stale["id"] = "older-copy"
    stale["timestamp"] = "2026-09-21T14:13:24Z"
    notice = {
        "type": "custom_message",
        "id": "exhausted",
        "timestamp": "2026-09-21T14:13:25Z",
        "customType": "loop-recovery-exhausted",
        "content": "toy notice",
        "details": {"episodeMs": 1200000},
    }
    session.write_text("\n".join(json.dumps(r) for r in (header, newest, stale, notice)) + "\n")
    assert refresh(conn, home).errors == 0
    assert conn.execute(
        "SELECT disposition,reason,tools_executed,timeout_source,attempt_count,episode_limit_ms "
        "FROM ah.request_recovery"
    ).fetchone() == ("stop", "episode-expired", False, "recovery-episode", None, None)
    assert conn.execute("SELECT episode_limit_ms,request_uid,follow_up_count FROM ah.request_incident").fetchone() == (
        1200000,
        None,
        None,
    )


def test_sql_input_scan_is_limited_and_rebuild_tables_include_progress(conn, tmp_path):
    from agent_history.parse_pi import progress_row

    home = tmp_path / "pi-toy"
    write_checkpoint(home, progress())
    refresh(conn, home)
    source_id = conn.execute("SELECT source_id FROM ah.request_progress").fetchone()[0]
    rows = [
        progress_row(progress(request=f"{number:08x}-0000-4000-8000-000000000000"), SessionKey("pi", SID), POS)
        for number in range(513)
    ]
    assert all(row is not None for row in rows)
    load.Writer(conn).write(rows, source_id, CTX)
    result = conn.execute(
        "SELECT requests FROM ah.request_progress_diagnostics('pi-toy',to_timestamp(%s),100)", (BASE_MS / 1000,)
    ).fetchall()
    assert result == [(512,)]
    assert {"request_progress", "request_recovery", "request_incident"} <= set(load.DATA_TABLES)
    assert not {"request_progress", "request_recovery", "request_incident"} & set(load.OWNED_TABLES)


def test_checkpoint_inventory_is_bounded_to_exact_files(tmp_path):
    home = tmp_path / "pi-toy"
    write_checkpoint(home, progress())
    (home / "progress" / f"{SID}.json.123.tmp").write_text("{}")
    assert any(entry.role == "pi_progress" for entry in load.inventory_map({"pi-toy": home}).values())
    assert len(load.inventory_map({"pi-toy": home})) == 1

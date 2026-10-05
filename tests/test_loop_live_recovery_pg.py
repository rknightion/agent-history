"""Real role ACLs and deterministic paid-job recovery/application boundaries."""

import copy
import json
import os
from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from agent_history import load, loop_live
from agent_history.config import Config
from test_loop_live_pg import (
    DSN,
    clean as _clean_fixture,
    fake_infer,
    live_config as _config_fixture,
    psycopg,
    refresh,
    scratch_database,
    transcript as _transcript_fixture,
)

clean = _clean_fixture
live_config = _config_fixture
transcript = _transcript_fixture

READER_DSN = os.environ.get("AGENT_HISTORY_TEST_READER_DSN", "")
pytestmark = pytest.mark.skipif(not scratch_database(DSN), reason="disposable agent_history_test database required")
PAID = ("loop_live_job", "loop_live_cache", "loop_live_budget", "loop_live_reservation")


def closed_job(conn, transcript):
    transcript["state"]("dispatch", "lane=one task=TASK-1 agent=complex-worker run=one")
    transcript["state"]("return", "run=one")
    transcript["state"]("close", "reason=blocked")
    uid = refresh(conn, transcript)[0]
    conn.execute("UPDATE ah.loop_run SET end_evidence='next_launch',end_ts=now()")
    conn.commit()
    load.post_passes(conn)
    conn.commit()
    assert conn.execute("SELECT status FROM ah.loops").fetchone()[0] == "finished"
    conn.commit()
    return uid


def clone_source(transcript, label, uid, days=0):
    records = copy.deepcopy(transcript["records"])
    records[0]["id"] = uid
    for record in records:
        record["timestamp"] = (datetime.fromisoformat(record["timestamp"]) - timedelta(days=days)).isoformat()
        if record.get("type") == "message":
            record["id"] = label + "-" + record["id"]
            message = record["message"]
            if "timestamp" in message:
                message["timestamp"] = record["timestamp"]
            if "toolCallId" in message:
                message["toolCallId"] = label + "-" + message["toolCallId"]
            for part in message.get("content", []):
                if isinstance(part, dict) and part.get("type") == "toolCall":
                    part["id"] = label + "-" + part["id"]
    source = next(iter(transcript["sources"].values()))
    path = source / "sessions" / "-synthetic-" / (label + ".jsonl")
    path.write_text("".join(json.dumps(row) + "\n" for row in records))


@pytest.mark.skipif(not scratch_database(READER_DSN), reason="real scratch ah_reader DSN required")
def test_real_default_acl_does_not_disclose_any_paid_table(clean, live_config, transcript):
    # pg-test provisions the actual repository roles.sql before apply_schema, not a bespoke reader.
    clean.execute("CREATE TABLE ah.synthetic_default_acl (value integer)")
    try:
        assert clean.execute("SELECT has_table_privilege('ah_reader','ah.synthetic_default_acl','SELECT')").fetchone()[
            0
        ]
        assert clean.execute("SELECT has_table_privilege('ah_reader','ah.loops','SELECT')").fetchone()[0]
        for name in PAID:
            assert not clean.execute("SELECT has_table_privilege('ah_reader',%s,'SELECT')", ("ah." + name,)).fetchone()[
                0
            ]
        clean.commit()
        with psycopg.connect(READER_DSN) as reader:
            reader.execute("SELECT summary_error FROM ah.loops LIMIT 1")
            reader.rollback()
            for name in PAID:
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    reader.execute("SELECT * FROM ah." + name + " LIMIT 1")
                reader.rollback()
        # Analytics re-application and rebuild must not widen the restricted ACLs.
        refresh(clean, transcript)
        clean.commit()
        load.apply_schema(clean, force=True)
        clean.commit()
        assert load.rebuild(clean, sources=transcript["sources"], textfile=None, log=lambda *_: None).errors == 0
        for name in PAID:
            assert not clean.execute("SELECT has_table_privilege('ah_reader',%s,'SELECT')", ("ah." + name,)).fetchone()[
                0
            ]
    finally:
        clean.rollback()
        clean.execute("DROP TABLE IF EXISTS ah.synthetic_default_acl")
        clean.commit()


def test_finished_completion_between_cache_read_and_apply_marker_is_not_lost(clean, transcript, live_config):
    uid = closed_job(clean, transcript)
    # Force the last historical pass of this finished row; no dirty/running selection remains.
    clean.execute("DELETE FROM ah.meta WHERE key='loops_live_projection_v1'")
    clean.commit()
    fired = []

    def final_infer(config, kind, state):
        response = fake_infer(config, kind, state)
        if kind == "summary":
            response["choices"][0]["message"]["content"] = json.dumps(
                {"headline": "Loop closed", "summary": "The loop has closed. Work remains parked."}
            )
        return response

    class Interleaved:
        def __getattr__(self, key):
            return getattr(clean, key)

        def execute(self, query, *args, **kwargs):
            result = clean.execute(query, *args, **kwargs)
            if "SELECT response,generated_at,model,final_summary" in query and not fired:
                # The statement's snapshot has no final reply. Commit it immediately afterwards.
                with psycopg.connect(DSN, autocommit=True) as worker:
                    assert loop_live.drain_one(worker, live_config, final_infer)
                fired.append(True)
            return result

    load.post_passes(Interleaved())
    clean.commit()
    assert fired
    assert clean.execute(
        "SELECT finished_at IS NOT NULL,applied_at IS NULL FROM ah.loop_live_job WHERE launch_uid=%s", (uid,)
    ).fetchone() == (True, True)
    clean.commit()
    load.post_passes(clean)
    assert clean.execute("SELECT status,headline,final_summary FROM ah.loops").fetchone() == (
        "finished",
        "Loop closed",
        True,
    )
    clean.commit()
    assert clean.execute("SELECT applied_at IS NOT NULL FROM ah.loop_live_job WHERE launch_uid=%s", (uid,)).fetchone()[
        0
    ]


def test_historical_projection_and_rebuild_admit_only_live_work_and_prioritise_close(clean, transcript, live_config):
    transcript["state"]("dispatch", "lane=one task=TASK-1 agent=complex-worker run=one")
    clone_source(transcript, "current-working", "22222222-2222-4222-8222-222222222222")
    transcript["state"]("return", "run=one")
    transcript["state"]("close", "reason=blocked")
    clone_source(transcript, "archive-a", "00000000-0000-4000-8000-000000000001", days=40)
    clone_source(transcript, "archive-b", "00000000-0000-4000-8000-000000000002", days=41)
    stats = load.refresh(clean, sources=transcript["sources"], textfile=None, log=lambda *_: None)
    assert stats.errors == 0 and stats.files_parsed == 4
    assert (
        clean.execute("SELECT count(*) FROM ah.loops WHERE status='stale' AND live_phase='closing'").fetchone()[0] == 2
    )
    assert clean.execute("SELECT count(*) FROM ah.loop_live_job").fetchone()[0] == 2
    assert clean.execute("SELECT count(*) FROM ah.loop_live_reservation").fetchone()[0] == 0
    clean.execute("UPDATE ah.loop_live_job SET queued_at=queued_at-interval '1 day' WHERE NOT close_requested")
    clean.commit()
    seen = []

    def recorder(config, kind, state):
        seen.append(state["close_recorded"])
        return fake_infer(config, kind, state)

    with psycopg.connect(DSN, autocommit=True) as worker:
        assert loop_live.drain_one(worker, live_config, recorder)
    assert seen == [True, True]  # New close wins even over an older current-working queue row.
    assert clean.execute("SELECT count(*) FROM ah.loop_live_cache WHERE final_summary").fetchone()[0] == 1
    clean.commit()
    assert load.rebuild(clean, sources=transcript["sources"], textfile=None, log=lambda *_: None).errors == 0
    assert clean.execute("SELECT count(*) FROM ah.loop_live_job").fetchone()[0] == 2
    assert clean.execute("SELECT count(*) FROM ah.loop_live_reservation").fetchone()[0] == 2


@pytest.mark.parametrize("auth_source", ["environment", "file", "bad-encoding"])
def test_unpaid_auth_deferral_recovers_only_when_the_actual_auth_prerequisite_changes(
    clean, transcript, live_config, monkeypatch, tmp_path, auth_source
):
    config = live_config
    if auth_source != "environment":
        key = tmp_path / "synthetic-key"
        if auth_source == "bad-encoding":
            key.write_bytes(bytes([255]))
        config = replace(config, api_key_env=None, api_key_file=str(key))
        monkeypatch.setattr(loop_live, "load_config", lambda: Config(loop_live=config))
    uid = closed_job(clean, transcript)
    if auth_source == "environment":
        monkeypatch.delenv("SYNTHETIC_LOOP_TOKEN")
    seen = []

    def recorder(config, kind, state):
        assert "_defer" not in state
        seen.append(kind)
        return fake_infer(config, kind, state)

    with psycopg.connect(DSN, autocommit=True) as worker:
        assert loop_live.drain_one(worker, config, recorder)
        assert worker.execute("SELECT count(*) FROM ah.loop_live_cache").fetchone()[0] == 0
        assert worker.execute("SELECT count(*) FROM ah.loop_live_reservation").fetchone()[0] == 0
        assert not loop_live.drain_one(worker, config, recorder)
    assert not seen
    load.post_passes(clean)
    assert clean.execute("SELECT summary_error FROM ah.loops").fetchone()[0] == "auth_unavailable"
    clean.commit()
    # Rebuild must retain the recoverable deferral without silently retrying it.
    assert load.rebuild(clean, sources=transcript["sources"], textfile=None, log=lambda *_: None).errors == 0
    clean.commit()
    with psycopg.connect(DSN, autocommit=True) as worker:
        assert not loop_live.drain_one(worker, config, recorder)
    if auth_source == "environment":
        monkeypatch.setenv("SYNTHETIC_LOOP_TOKEN", "synthetic-repaired")
    else:
        key.write_text("synthetic-repaired\n")
    with psycopg.connect(DSN, autocommit=True) as worker:
        assert loop_live.drain_one(worker, config, recorder)
        assert not loop_live.drain_one(worker, config, recorder)
    assert seen == ["jev", "summary"]
    load.post_passes(clean)
    assert clean.execute("SELECT headline,final_summary,summary_error FROM ah.loops").fetchone() == (
        "Work in progress",
        True,
        None,
    )
    assert clean.execute("SELECT digest_sha256 FROM ah.loop_live_job WHERE launch_uid=%s", (uid,)).fetchone()


@pytest.mark.parametrize("budget_state", ["all-refused", "jev-only"])
def test_daily_budget_deferral_recovers_next_utc_day_without_repeating_paid_jev(
    clean, transcript, live_config, monkeypatch, budget_state
):
    closed_job(clean, transcript)
    today = clean.execute("SELECT (clock_timestamp() AT TIME ZONE 'UTC')::date").fetchone()[0]
    clean.execute(
        "INSERT INTO ah.loop_live_budget(day,reserved_usd) VALUES (%s,%s)",
        (
            today,
            loop_live.DAILY_CAP
            if budget_state == "all-refused"
            else loop_live.DAILY_CAP - loop_live.reservation("jev"),
        ),
    )
    clean.commit()
    seen = []

    def recorder(config, kind, state):
        seen.append(kind)
        return fake_infer(config, kind, state)

    with psycopg.connect(DSN, autocommit=True) as worker:
        assert loop_live.drain_one(worker, live_config, recorder)
        assert worker.execute("SELECT kind FROM ah.loop_live_cache").fetchall() == (
            [] if budget_state == "all-refused" else [("jev",)]
        )
        assert not loop_live.drain_one(worker, live_config, recorder)
    assert seen == ([] if budget_state == "all-refused" else ["jev"])
    load.post_passes(clean)
    clean.commit()
    assert clean.execute("SELECT summary_error FROM ah.loops").fetchone()[0] == "daily_budget_cap"
    clean.commit()
    tomorrow = today + timedelta(days=1)
    monkeypatch.setattr(loop_live, "_utc_day", lambda _: tomorrow, raising=False)
    with psycopg.connect(DSN, autocommit=True) as worker:
        assert loop_live.drain_one(worker, live_config, recorder)
        assert not loop_live.drain_one(worker, live_config, recorder)
        assert (
            worker.execute("SELECT reserved_usd FROM ah.loop_live_budget WHERE day=%s", (today,)).fetchone()[0]
            == loop_live.DAILY_CAP
        )
        assert worker.execute("SELECT reserved_usd FROM ah.loop_live_budget WHERE day=%s", (tomorrow,)).fetchone()[
            0
        ] == loop_live.reservation("summary") + (loop_live.reservation("jev") if budget_state == "all-refused" else 0)
    assert seen == ["jev", "summary"]
    load.post_passes(clean)
    assert clean.execute("SELECT final_summary,summary_error FROM ah.loops").fetchone() == (True, None)
    assert clean.execute("SELECT count(*) FROM ah.loop_live_reservation").fetchone()[0] == 2


@pytest.mark.parametrize("uncertain", ["reservation", "timeout"])
def test_ambiguous_paid_outcomes_never_retry_on_day_or_auth_change(
    clean, transcript, live_config, monkeypatch, uncertain
):
    uid = closed_job(clean, transcript)
    key = clean.execute("SELECT digest_sha256 FROM ah.loop_live_job WHERE launch_uid=%s", (uid,)).fetchone()[0]
    today = clean.execute("SELECT (clock_timestamp() AT TIME ZONE 'UTC')::date").fetchone()[0]
    clean.commit()
    seen = []

    def infer(config, kind, state):
        seen.append(kind)
        if uncertain == "timeout":
            raise TimeoutError("synthetic unacknowledged provider call")
        return fake_infer(config, kind, state)

    with psycopg.connect(DSN, autocommit=True) as worker:
        if uncertain == "reservation":
            assert loop_live.reserve(worker, uid, key, "jev")
        assert loop_live.drain_one(worker, live_config, infer)
        spent = worker.execute("SELECT reserved_usd FROM ah.loop_live_budget").fetchone()[0]
        count = worker.execute("SELECT count(*) FROM ah.loop_live_cache").fetchone()[0]
        assert not loop_live.drain_one(worker, live_config, infer)
    assert seen == (["summary"] if uncertain == "reservation" else ["jev", "summary"])
    monkeypatch.setenv("SYNTHETIC_LOOP_TOKEN", "synthetic-changed")
    monkeypatch.setattr(loop_live, "_utc_day", lambda _: today + timedelta(days=1), raising=False)
    load.post_passes(clean)
    clean.commit()
    with psycopg.connect(DSN, autocommit=True) as worker:
        assert not loop_live.drain_one(worker, live_config, infer)
        assert worker.execute("SELECT sum(reserved_usd) FROM ah.loop_live_budget").fetchone()[0] == spent
        assert worker.execute("SELECT count(*) FROM ah.loop_live_cache").fetchone()[0] == count
    assert spent == loop_live.reservation("jev") + loop_live.reservation("summary")


def test_public_error_is_bounded_while_private_cache_preserves_full_provider_output(clean, transcript, live_config):
    closed_job(clean, transcript)
    detail = "provider_http_400: synthetic-sensitive-detail at /tmp/synthetic/private-key"

    def fail(*_):
        raise ValueError(detail)

    with psycopg.connect(DSN, autocommit=True) as worker:
        assert loop_live.drain_one(worker, live_config, fail)
        assert all(row[0] == "ValueError: " + detail for row in worker.execute("SELECT error FROM ah.loop_live_cache"))
    load.post_passes(clean)
    assert clean.execute("SELECT summary_error FROM ah.loops").fetchone()[0] == "provider_http_400"

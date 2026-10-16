"""Real role ACLs and retained paid-job history after worker retirement."""

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
    historical_cache,
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


def test_finished_historical_completion_between_cache_read_and_apply_marker_is_not_lost(clean, transcript, live_config):
    uid = closed_job(clean, transcript)
    clean.execute("DELETE FROM ah.meta WHERE key='loops_live_projection_v1'")
    # A job admitted before retirement can still have a late result committed by an old process.
    clean.execute(
        "INSERT INTO ah.loop_live_job(launch_uid,digest_sha256,state,close_requested) VALUES (%s,'retained','{}',true)",
        (uid,),
    )
    clean.commit()
    fired = []

    class Interleaved:
        def __getattr__(self, key):
            return getattr(clean, key)

        def execute(self, query, *args, **kwargs):
            result = clean.execute(query, *args, **kwargs)
            if "SELECT response,generated_at,model,final_summary" in query and not fired:
                with psycopg.connect(DSN, autocommit=True) as writer:
                    response = fake_infer(live_config, "summary", {})
                    response["choices"][0]["message"]["content"] = json.dumps(
                        {"headline": "Loop closed", "summary": "The loop has closed. Work remains parked."}
                    )
                    writer.execute(
                        "INSERT INTO ah.loop_live_cache(launch_uid,digest_sha256,kind,response,model,final_summary,requested_at) "
                        "VALUES (%s,'retained','summary',%s,%s,true,now())",
                        (uid, psycopg.types.json.Jsonb(response), loop_live.SUMMARY_MODEL),
                    )
                    writer.execute("UPDATE ah.loop_live_job SET finished_at=now() WHERE launch_uid=%s", (uid,))
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
    assert clean.execute("SELECT applied_at IS NOT NULL FROM ah.loop_live_job WHERE launch_uid=%s", (uid,)).fetchone()[
        0
    ]


def test_historical_projection_and_rebuild_never_admit_live_or_close_work(clean, transcript, live_config):
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
    for name in PAID:
        assert clean.execute("SELECT count(*) FROM ah." + name).fetchone()[0] == 0
    clean.commit()
    assert not loop_live.drain_one(clean, live_config, lambda *_: pytest.fail("retired provider"))
    assert load.rebuild(clean, sources=transcript["sources"], textfile=None, log=lambda *_: None).errors == 0
    assert clean.execute("SELECT count(*) FROM ah.loops").fetchone()[0] == 4
    for name in PAID:
        assert clean.execute("SELECT count(*) FROM ah." + name).fetchone()[0] == 0


@pytest.mark.parametrize("auth_source", ["environment", "file", "bad-encoding"])
def test_retired_auth_deferral_never_recovers_on_credential_change(
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
    clean.execute(
        "INSERT INTO ah.loop_live_job(launch_uid,digest_sha256,state,close_requested,error) "
        "VALUES (%s,'retained',%s,true,'auth_unavailable')",
        (uid, psycopg.types.json.Jsonb({"_defer": {"reason": "auth", "prerequisite": "old"}})),
    )
    clean.commit()
    if auth_source == "environment":
        monkeypatch.delenv("SYNTHETIC_LOOP_TOKEN")
    assert not loop_live.drain_one(clean, config, lambda *_: pytest.fail("retired provider"))
    load.post_passes(clean)
    assert clean.execute("SELECT summary_error FROM ah.loops").fetchone()[0] == "auth_unavailable"
    clean.commit()
    assert load.rebuild(clean, sources=transcript["sources"], textfile=None, log=lambda *_: None).errors == 0
    if auth_source == "environment":
        monkeypatch.setenv("SYNTHETIC_LOOP_TOKEN", "synthetic-repaired")
    else:
        key.write_text("synthetic-repaired\n")
    assert not loop_live.drain_one(clean, config, lambda *_: pytest.fail("retired provider"))
    assert clean.execute("SELECT digest_sha256,claimed_at,finished_at,error FROM ah.loop_live_job").fetchone() == (
        "retained",
        None,
        None,
        "auth_unavailable",
    )
    assert clean.execute("SELECT count(*) FROM ah.loop_live_cache").fetchone()[0] == 0
    assert clean.execute("SELECT count(*) FROM ah.loop_live_reservation").fetchone()[0] == 0


@pytest.mark.parametrize("budget_state", ["all-refused", "jev-only"])
def test_retired_budget_deferral_never_recovers_next_utc_day(clean, transcript, live_config, monkeypatch, budget_state):
    uid = closed_job(clean, transcript)
    today = clean.execute("SELECT CURRENT_DATE").fetchone()[0]
    clean.execute("INSERT INTO ah.loop_live_budget(day,reserved_usd) VALUES (%s,5)", (today,))
    clean.execute(
        "INSERT INTO ah.loop_live_job(launch_uid,digest_sha256,state,close_requested,error) "
        "VALUES (%s,'retained',%s,true,'daily_budget_cap')",
        (uid, psycopg.types.json.Jsonb({"_defer": {"reason": "budget", "day": today.isoformat()}})),
    )
    if budget_state == "jev-only":
        clean.execute(
            "INSERT INTO ah.loop_live_cache(launch_uid,digest_sha256,kind,response,model,requested_at) "
            "VALUES (%s,'retained','jev',%s,%s,now())",
            (uid, psycopg.types.json.Jsonb(fake_infer(live_config, "jev", {})), loop_live.JEV_MODEL),
        )
    clean.commit()
    monkeypatch.setattr(loop_live, "_utc_day", lambda _: today + timedelta(days=1))
    assert not loop_live.drain_one(clean, live_config, lambda *_: pytest.fail("retired provider"))
    load.post_passes(clean)
    assert clean.execute("SELECT summary_error FROM ah.loops").fetchone()[0] == "daily_budget_cap"
    assert clean.execute("SELECT day,reserved_usd FROM ah.loop_live_budget").fetchall() == [
        (today, loop_live.DAILY_CAP)
    ]
    assert clean.execute("SELECT count(*) FROM ah.loop_live_cache").fetchone()[0] == (budget_state == "jev-only")
    assert clean.execute("SELECT count(*) FROM ah.loop_live_reservation").fetchone()[0] == 0


@pytest.mark.parametrize("uncertain", ["reservation", "timeout"])
def test_historical_ambiguous_paid_outcomes_never_retry_on_day_or_auth_change(
    clean, transcript, live_config, monkeypatch, uncertain
):
    uid = closed_job(clean, transcript)
    historical_cache(
        clean,
        uid,
        live_config,
        errors={"jev": "reservation_uncertain" if uncertain == "reservation" else "timeout"},
        final=True,
    )
    today = clean.execute("SELECT CURRENT_DATE").fetchone()[0]
    spent = clean.execute("SELECT sum(reserved_usd) FROM ah.loop_live_budget").fetchone()[0]
    count = clean.execute("SELECT count(*) FROM ah.loop_live_cache").fetchone()[0]
    monkeypatch.setenv("SYNTHETIC_LOOP_TOKEN", "synthetic-changed")
    monkeypatch.setattr(loop_live, "_utc_day", lambda _: today + timedelta(days=1))
    load.post_passes(clean)
    assert not loop_live.drain_one(clean, live_config, lambda *_: pytest.fail("retired provider"))
    assert clean.execute("SELECT sum(reserved_usd) FROM ah.loop_live_budget").fetchone()[0] == spent
    assert clean.execute("SELECT count(*) FROM ah.loop_live_cache").fetchone()[0] == count
    assert spent == loop_live.reservation("jev") + loop_live.reservation("summary")


def test_public_error_is_bounded_while_private_cache_preserves_full_provider_output(clean, transcript, live_config):
    uid = closed_job(clean, transcript)
    detail = "provider_http_400: synthetic-sensitive-detail at /tmp/synthetic/private-key"
    historical_cache(
        clean,
        uid,
        live_config,
        {"jev": None, "summary": None},
        {kind: "ValueError: " + detail for kind in ("jev", "summary")},
        final=True,
    )
    assert not loop_live.drain_one(clean, live_config, lambda *_: pytest.fail("retired provider"))
    assert all(row[0] == "ValueError: " + detail for row in clean.execute("SELECT error FROM ah.loop_live_cache"))
    load.post_passes(clean)
    assert clean.execute("SELECT summary_error FROM ah.loops").fetchone()[0] == "provider_http_400"

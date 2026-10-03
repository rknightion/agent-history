"""Loop-linking integration tests against a disposable Postgres database.

Skipped unless AGENT_HISTORY_TEST_DSN points at a scratch database the tests may TRUNCATE.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

import pytest

DSN = os.environ.get("AGENT_HISTORY_TEST_DSN", "")
pytestmark = pytest.mark.skipif(
    not DSN or "agent_history_test" not in DSN, reason="AGENT_HISTORY_TEST_DSN (a *_test database) not set"
)

psycopg = pytest.importorskip("psycopg")

from agent_history import load, loops  # noqa: E402


@pytest.fixture(scope="module")
def conn():
    connection = load.connect(DSN)
    load.apply_schema(connection, force=True)
    connection.commit()
    yield connection
    connection.close()


@pytest.fixture
def clean(conn):
    conn.execute("TRUNCATE " + ", ".join(f"ah.{table}" for table in load.DATA_TABLES) + " RESTART IDENTITY CASCADE")
    conn.execute("DELETE FROM ah.loop_receipt")
    conn.commit()
    yield conn
    conn.rollback()


@pytest.fixture
def launched(clean, tmp_path):
    """A real indexed launch, old enough to exercise silence without sleeping."""
    at = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
    source = tmp_path / "sessions"
    project = source / "projects" / "synthetic-project"
    project.mkdir(parents=True)
    path = project / "11111111-1111-4111-8111-111111111111.jsonl"
    path.write_text(
        json.dumps(
            {
                "type": "user",
                "uuid": "synthetic-launch",
                "sessionId": "synthetic-live-root",
                "timestamp": at,
                "cwd": "/tmp/synthetic-project",
                "message": {"role": "user", "content": "You are the root. Write codex/report-synthetic-loop1.md."},
            },
            separators=(",", ":"),
        )
        + "\n"
    )
    sources = {"claude-test": source}
    stats = load.refresh(clean, sources=sources, textfile=None, log=lambda *_: None)
    assert stats.errors == 0 and stats.files_parsed == 1
    clean.execute("UPDATE ah.session SET last_event_at = now()")
    clean.commit()
    load.post_passes(clean)
    clean.commit()
    return sources


def test_live_loop_report_write_is_not_terminal_but_next_launch_is(clean, launched):
    assert clean.execute("SELECT status, end_ts FROM ah.loops").fetchone() == ("running", None)
    root_id = clean.execute("SELECT id FROM ah.session").fetchone()[0]
    clean.execute(
        "INSERT INTO ah.artifact (agent, event_uid, session_id, ts, kind, action, path, "
        "evidence_type, source_id, byte_offset) VALUES ('claude', 'synthetic-report', %s, now(), "
        "'file', 'write', '/tmp/synthetic-project/codex/report-synthetic-loop1.md', 'tool', 0, 0)",
        (root_id,),
    )
    clean.execute("INSERT INTO ah.dirty_session (session_id) VALUES (%s) ON CONFLICT DO NOTHING", (root_id,))
    clean.commit()
    load.post_passes(clean)
    row = clean.execute("SELECT status, end_ts FROM ah.loops").fetchone()
    # Writing the report is not a delivered completion notification; only a receipt finishes it.
    assert row == ("running", None)
    # Supersession is terminal evidence, not an inferred stale failure.
    clean.execute("UPDATE ah.loop_run SET end_evidence = 'next_launch'")
    clean.commit()
    load.post_passes(clean)
    assert clean.execute("SELECT status FROM ah.loops").fetchone() == ("finished",)


def test_silent_loop_expires_without_dirty_sessions_and_can_resume(clean, launched):
    assert clean.execute("SELECT status, end_ts FROM ah.loops").fetchone() == ("running", None)
    clean.execute("UPDATE ah.session SET last_event_at = now() - interval '25 hours'")
    clean.commit()
    assert load.post_passes(clean)["dirty_sessions"] == 0
    row = clean.execute("SELECT status, end_ts FROM ah.loops").fetchone()
    assert row[0] == "stale" and row[1] is not None
    clean.execute("UPDATE ah.session SET last_event_at = now()")
    clean.commit()
    load.post_passes(clean)
    assert clean.execute("SELECT status, end_ts FROM ah.loops").fetchone() == ("running", None)


def test_analytics_reapply_and_rebuild_preserve_live_relation_and_grant(clean, launched):
    # Grant as the owner, to an absent-by-default consumer role in the disposable database.
    with psycopg.connect(os.environ["AGENT_HISTORY_TEST_ADMIN_DSN"]) as admin:
        admin.execute("CREATE ROLE synthetic_loop_reader")
    try:
        clean.execute("GRANT SELECT ON ah.loops TO synthetic_loop_reader")
        clean.commit()
        before = clean.execute("SELECT launch_uid FROM ah.loops").fetchall()
        clean.commit()
        load.apply_schema(clean, force=True)
        clean.commit()
        assert clean.execute("SELECT launch_uid FROM ah.loops").fetchall() == before
        clean.commit()
        assert load.rebuild(clean, sources=launched, textfile=None, log=lambda *_: None).errors == 0
        assert clean.execute("SELECT launch_uid, status FROM ah.loops").fetchall() == [(before[0][0], "stale")]
        assert clean.execute(
            "SELECT has_table_privilege('synthetic_loop_reader', 'ah.loops', 'SELECT')"
        ).fetchone() == (True,)
    finally:
        clean.rollback()
        with psycopg.connect(os.environ["AGENT_HISTORY_TEST_ADMIN_DSN"]) as admin:
            admin.execute("DROP OWNED BY synthetic_loop_reader")
            admin.execute("DROP ROLE synthetic_loop_reader")


def test_xreview_does_not_link_a_codex_exec_session_to_a_loop(clean):
    start, end = "2026-09-20 12:00:00+00", "2026-09-20 12:10:00+00"
    root_id = clean.execute(
        "INSERT INTO ah.session (agent, session_uid, is_stub, namespace, cwd, first_event_at, last_event_at) "
        "VALUES ('claude', 'synthetic-loop-root', false, 'claude-test', '/tmp/synthetic-project', %s, %s) "
        "RETURNING id",
        (start, end),
    ).fetchone()[0]
    member_id = clean.execute(
        "INSERT INTO ah.session (agent, session_uid, is_stub, namespace, cwd, entrypoint, first_event_at, last_event_at) "
        "VALUES ('codex', 'synthetic-xreview-child', false, 'codex-test', '/tmp/synthetic-project', 'codex_exec', "
        "%s::timestamptz + interval '1 minute', %s::timestamptz + interval '2 minutes') RETURNING id",
        (start, start),
    ).fetchone()[0]
    loop_id = clean.execute(
        "INSERT INTO ah.loop_run (launch_uid, root_session_id, status, launch_ts, end_ts) "
        "VALUES ('synthetic-loop:xreview', %s, 'resolved', %s, %s) RETURNING id",
        (root_id, start, end),
    ).fetchone()[0]
    clean.execute(
        "INSERT INTO ah.tool_call (agent, call_uid, session_id, tool_name, started_at, ended_at, meta, "
        "source_id, byte_offset) VALUES ('claude', 'synthetic-xreview-call', %s, 'Bash', %s, %s, "
        '\'{"cmd_verb": "xreview"}\'::jsonb, 0, 0)',
        (root_id, start, end),
    )
    try:
        loops._tag_tree(clean, loop_id, root_id, start, end)
        linked = clean.execute("SELECT loop_run_id FROM ah.session WHERE id = %s", (member_id,)).fetchone()[0]
        assert linked is None
    finally:
        clean.rollback()

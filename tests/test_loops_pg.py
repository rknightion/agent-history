"""Loop-linking integration tests against a disposable Postgres database.

Skipped unless AGENT_HISTORY_TEST_DSN points at a scratch database the tests may TRUNCATE.
"""

from __future__ import annotations

import os

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
    conn.commit()
    return conn


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

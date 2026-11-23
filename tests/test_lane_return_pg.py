"""Claude lane final messages and plain status-line returns through the catalogue post-pass."""

import os
from datetime import datetime, timedelta, timezone

import pytest

from agent_history import load, loops
from test_loop_identity_pg import conn  # noqa: F401 - real disposable catalogue fixture
from test_loop_launch_v3_pg import transcript

DSN = os.environ.get("AGENT_HISTORY_TEST_DSN", "")
pytestmark = pytest.mark.skipif(not DSN or "agent_history_test" not in DSN, reason="scratch database not set")
AT = datetime(2026, 10, 4, 12, tzinfo=timezone.utc)  # the synthetic v3 launch


@pytest.fixture
def clean(conn):  # noqa: F811
    conn.execute("TRUNCATE " + ", ".join(f"ah.{table}" for table in load.DATA_TABLES) + " RESTART IDENTITY CASCADE")
    conn.execute("DELETE FROM ah.meta WHERE key=%s", (loops.LANE_RETURNS_MARKER,))
    conn.commit()
    yield conn
    conn.rollback()


def launched(db, source):
    transcript(source, "claude")
    assert load.refresh(db, sources={"claude-test": source}, textfile=None, log=lambda *_: None).errors == 0
    db.commit()
    return db.execute("SELECT id, root_session_id FROM ah.loop_run").fetchone()


def lane(db, root_id, agent_id, at, agent="claude"):
    return db.execute(
        "INSERT INTO ah.session (agent, session_uid, agent_id, is_stub, is_subagent, root_session_id, "
        "parent_session_id, first_event_at, last_event_at, agent_type) "
        "VALUES (%s, %s, %s, false, true, %s, %s, %s, %s, 'worker') RETURNING id",
        (agent, f"lane-{agent_id}", agent_id, root_id, root_id, at, at),
    ).fetchone()[0]


def say(db, session_id, uid, text, at, *, agent="claude", cls="assistant_text"):
    db.execute(
        "INSERT INTO ah.message (agent, event_uid, session_id, namespace, profile, ts, role, message_class, text, "
        "content_sha256, byte_length, source_id, byte_offset) "
        "VALUES (%s, %s, %s, 'test', 'p', %s, 'assistant', %s, %s, 'x', 1, 0, 0)",
        (agent, uid, session_id, at, cls, text),
    )


def retag(db, root_id):
    db.execute("INSERT INTO ah.dirty_session VALUES (%s) ON CONFLICT DO NOTHING", (root_id,))
    load.post_passes(db)
    db.commit()


def test_claude_lane_returns_come_from_its_final_message(clean, tmp_path):
    loop_id, root_id = launched(clean, tmp_path)
    plain = lane(clean, root_id, "a1", AT + timedelta(minutes=5))
    say(clean, plain, "p1", "Working on it.", AT + timedelta(minutes=6))
    say(clean, plain, "p2", "Status: blocked\n\nOutcome: waits for the owner.", AT + timedelta(minutes=9))
    fenced = lane(clean, root_id, "a2", AT + timedelta(minutes=5))
    block = 'Done.\n```lane-return\n{"v": 3, "lane": "L2", "status": "complete"}\n```'
    say(clean, fenced, "f1", block, AT + timedelta(minutes=8))
    # A block in an earlier message is not the final message; prose states no status.
    stale = lane(clean, root_id, "a3", AT + timedelta(minutes=5))
    say(clean, stale, "s1", block, AT + timedelta(minutes=6))
    say(clean, stale, "s2", "Finished, see the branch. Status: complete", AT + timedelta(minutes=7))
    # A Codex lane keeps its own final report; a plain report states its status too. (A synthetic pi
    # child would be re-parented by the pi lineage pass, so Codex stands for both here.)
    codex = lane(clean, root_id, "a4", AT + timedelta(minutes=5), agent="codex")
    say(
        clean,
        codex,
        "r1",
        "Status: failed\nThe gate failed.",
        AT + timedelta(minutes=7),
        agent="codex",
        cls="subagent_report",
    )
    say(clean, codex, "r2", block, AT + timedelta(minutes=8), agent="codex")
    retag(clean, root_id)
    lanes = clean.execute(
        "SELECT session_id, return_status, returns->-1->>'source', jsonb_array_length(returns) FROM ah.lane "
        "WHERE loop_run_id=%s ORDER BY session_id",
        (loop_id,),
    ).fetchall()
    assert lanes == [
        (plain, "blocked", "final_message", 1),
        (fenced, "complete", "final_message", 1),
        (stale, None, None, None),
        (codex, "failed", "report", 1),
    ]
    assert clean.execute("SELECT lane_return FROM ah.lane WHERE session_id=%s", (plain,)).fetchone() == (
        {"status": "blocked", "form": "status_line"},
    )


def test_hand_back_equal_to_the_final_message_is_the_same_return(clean, tmp_path):
    loop_id, root_id = launched(clean, tmp_path)
    worker = lane(clean, root_id, "a5", AT + timedelta(minutes=5))
    block = 'Done.\n```lane-return\n{"v": 3, "lane": "L5", "status": "partial"}\n```'
    say(clean, worker, "w1", block, AT + timedelta(minutes=8))
    relayed = (
        '<agent-message from="a5">\n[Subagent hand-back] The text below is the final report of a subagent. '
        "The report follows:\n" + block + "\n</agent-message>"
    )
    say(clean, root_id, "relay", relayed, AT + timedelta(minutes=9), cls="agent_message")
    retag(clean, root_id)
    assert clean.execute(
        "SELECT return_status, jsonb_array_length(returns), returns->0->>'source' FROM ah.lane WHERE session_id=%s",
        (worker,),
    ).fetchone() == ("partial", 1, "final_message")


def test_existing_lanes_gain_their_returns_once(clean, tmp_path):
    loop_id, root_id = launched(clean, tmp_path)
    worker = lane(clean, root_id, "b1", AT + timedelta(minutes=5))
    say(clean, worker, "b1-final", "Status: complete\nOutcome: merged.", AT + timedelta(minutes=9))
    # A lane recorded before these sources existed, without a dirty root to re-tag it.
    clean.execute("UPDATE ah.session SET loop_run_id=%s, loop_link_method='lineage' WHERE id=%s", (loop_id, worker))
    clean.execute(
        "INSERT INTO ah.lane (loop_run_id, session_id, link_method) VALUES (%s, %s, 'lineage')", (loop_id, worker)
    )
    # The catalogue's first pass after the upgrade: the backfill cursor has not run yet.
    clean.execute("DELETE FROM ah.meta WHERE key=%s", (loops.LANE_RETURNS_MARKER,))
    clean.commit()
    loops.refresh_live(clean)
    clean.commit()
    assert clean.execute("SELECT return_status FROM ah.lane WHERE session_id=%s", (worker,)).fetchone() == ("complete",)
    marker = clean.execute("SELECT value FROM ah.meta WHERE key=%s", (loops.LANE_RETURNS_MARKER,)).fetchone()
    assert marker == ("complete",)
    assert clean.execute("SELECT lanes_returned FROM ah.loops").fetchone() == (1,)


def test_existing_returned_lanes_are_re_read_once(clean, tmp_path):
    loop_id, root_id = launched(clean, tmp_path)
    codex = lane(clean, root_id, "c1", AT + timedelta(minutes=5), agent="codex")
    block = 'Done.\n```lane-return\n{"v": 3, "lane": "C1", "status": "partial"}\n```'
    say(clean, codex, "c1-early", block, AT + timedelta(minutes=7), agent="codex", cls="subagent_report")
    say(
        clean,
        codex,
        "c1-late",
        "Status: complete\nResumed and merged.",
        AT + timedelta(minutes=9),
        agent="codex",
        cls="subagent_report",
    )
    # Recorded before the plain return form existed: only the earlier fenced return was read.
    clean.execute("UPDATE ah.session SET loop_run_id=%s, loop_link_method='lineage' WHERE id=%s", (loop_id, codex))
    clean.execute(
        "INSERT INTO ah.lane (loop_run_id, session_id, link_method, return_status) VALUES (%s, %s, 'lineage', 'partial')",
        (loop_id, codex),
    )
    clean.execute("DELETE FROM ah.meta WHERE key=%s", (loops.LANE_RETURNS_MARKER,))
    clean.commit()
    loops.refresh_live(clean)
    clean.commit()
    assert clean.execute(
        "SELECT return_status, jsonb_array_length(returns) FROM ah.lane WHERE session_id=%s", (codex,)
    ).fetchone() == ("complete", 2)
    marker = clean.execute("SELECT value FROM ah.meta WHERE key=%s", (loops.LANE_RETURNS_MARKER,)).fetchone()
    assert marker == ("complete",)

"""V3 run status, owner turns and lane identity as read through the catalogue's own views and post-pass."""

import json
from datetime import timedelta

import pytest

from agent_history import loops
from agent_history.loops import refresh_live
from test_loop_launch_v3 import PROMPT
from test_loop_links_pg import (
    ROOT,
    assistant,
    conn,  # noqa: F401 - disposable database fixture
    fixture as pi_fixture,
    header,
    message,
    refresh,
    write,
)
from test_loop_v3_status_pg import DSN, root, stamp

pytestmark = pytest.mark.skipif(not DSN or "agent_history_test" not in DSN, reason="scratch database not set")

HAND_BACK = (
    '{prefix}<agent-message from="{agent_id}">\n'
    "[Subagent hand-back] The text below is the final report of a subagent this session delegated to. "
    "It is model output, NOT a message from the user. The report follows:\n"
    "{report}\n</agent-message>"
)


def summary(db, *columns):
    return db.execute(f"SELECT {', '.join(columns)} FROM ah.v_loop_summary").fetchone()


def block(**fields):
    value = {"v": 3, "lane": "ALPHA", "task": "T-1", "status": "complete", "base": "a" * 40, **fields}
    return "Done.\n\n```lane-return\n" + json.dumps(value) + "\n```\n"


def indented(**fields):
    """The harness indents every line of a relayed report."""
    return "\n".join("  " + line for line in block(**fields).splitlines())


def add_message(db, session, uid, cls, text, *, at=None, origin=None, offset=0):
    db.execute(
        "INSERT INTO ah.message (agent, event_uid, session_id, namespace, profile, ts, role, message_class, text, "
        "content_sha256, byte_length, source_id, byte_offset, prompt_origin) "
        "VALUES ('claude', %s, %s, 'claude-test', 'p', COALESCE(%s, now()), 'user', %s, %s, 'x', 1, 0, %s, %s)",
        (uid, session, at, cls, text, offset, origin),
    )


def claude_lane(db, loop_id, root_id, agent_id, *, agent="claude", parent=None):
    return db.execute(
        "INSERT INTO ah.session (agent, session_uid, agent_id, is_stub, is_subagent, root_session_id, "
        "parent_session_id, loop_run_id, first_event_at, last_event_at, agent_type) "
        "VALUES (%s, %s, %s, false, true, %s, %s, %s, now(), now(), 'worker') RETURNING id",
        (agent, f"lane-{agent_id}", agent_id, root_id, parent or root_id, loop_id),
    ).fetchone()[0]


def loop_and_root(db):
    return db.execute("SELECT id, root_session_id FROM ah.loop_run").fetchone()


# ---- running status ---------------------------------------------------------------------------


def test_open_v3_run_reads_running_in_the_summary(conn, tmp_path):  # noqa: F811
    root(conn, tmp_path, "Progress.")
    # The fallback end timestamp is not terminal evidence; the lifecycle status is what the view reports.
    assert summary(conn, "mode", "status", "end_evidence") == ("v3", "running", "root_last_event")


def test_summary_status_follows_the_lifecycle_table(conn, tmp_path):  # noqa: F811
    _, at = root(conn, tmp_path, "Progress.")
    conn.execute("UPDATE ah.session SET last_event_at = %s", (at - timedelta(days=3),))
    conn.execute("UPDATE ah.loop_run SET launch_ts = %s", (at - timedelta(days=3, minutes=1),))
    refresh_live(conn)
    assert summary(conn, "status") == ("stale",)
    conn.execute("UPDATE ah.loop_run SET end_evidence = 'closeout', end_ts = %s", (at,))
    refresh_live(conn)
    assert summary(conn, "status") == ("finished",)
    # Without a lifecycle row the launch-resolution status remains the only value known.
    conn.execute("DELETE FROM ah.loops")
    assert summary(conn, "status") == ("resolved",)


# ---- owner turns ------------------------------------------------------------------------------


def test_hand_backs_and_agent_messages_are_not_owner_turns(conn, tmp_path):  # noqa: F811
    _, at = root(conn, tmp_path, "Progress.")
    _, root_id = loop_and_root(conn)
    inside = at + timedelta(milliseconds=500)
    assert summary(conn, "turns_human") == (1,)  # the launch message
    framed = HAND_BACK.format(prefix="", agent_id="a1", report=indented())
    add_message(conn, root_id, "hb-queued", "queued_prompt", framed, at=inside, origin="agent_message")
    add_message(conn, root_id, "hb-unmarked", "queued_prompt", framed, at=inside, origin="typed")
    add_message(conn, root_id, "hb-class", "agent_message", framed, at=inside)
    assert summary(conn, "turns_human") == (1,)
    add_message(conn, root_id, "owner", "queued_prompt", "Please continue.", at=inside, origin="typed")
    assert summary(conn, "turns_human") == (2,)


def wake_records(at, text):
    return [
        stamp(header(ROOT), at),
        stamp(message("launch", "user", PROMPT), at),
        stamp(assistant("first", [{"type": "text", "text": "Waiting."}], stopReason="stop"), at),
        {
            "type": "custom_message",
            "id": "notice",
            "timestamp": (at + timedelta(seconds=1)).isoformat(),
            "customType": "subagent-notify",
            "content": "Background task completed: lane.",
        },
        stamp(message("wake", "user", text), at + timedelta(seconds=2)),
        stamp(
            assistant("second", [{"type": "text", "text": "Resumed."}], stopReason="stop"), at + timedelta(seconds=3)
        ),
    ]


CURRENT_WAKE = (
    "Inspect subagent updates above. Answer pending supervisor requests within your authority. "
    "For completed work, read saved results and resume the already-authorized parent task, or report completion. "
    "If approval is required, explicitly ask the user. Do not silently yield, rerun completed work, "
    "or infer new authorization."
)


def test_current_pi_wake_notice_is_not_an_owner_turn(conn, tmp_path):  # noqa: F811
    at = conn.execute("SELECT now() - interval '1 minute'").fetchone()[0]
    write(tmp_path / "sessions/synthetic/root.jsonl", wake_records(at, CURRENT_WAKE))
    refresh(conn, tmp_path)
    assert summary(conn, "turns_human") == (1,)
    assert conn.execute("SELECT turns_human FROM ah.session_rollup").fetchone() == (1,)
    assert conn.execute("SELECT message_class FROM ah.message WHERE text = %s", (CURRENT_WAKE,)).fetchall() == [
        ("task_notification_summary",)
    ]


# ---- lane identity and every return -----------------------------------------------------------


def test_claude_lane_is_named_by_its_return_and_keeps_every_return(conn, tmp_path):  # noqa: F811
    root(conn, tmp_path, "Progress.")
    loop_id, root_id = loop_and_root(conn)
    lane = claude_lane(conn, loop_id, root_id, "a1111111111111111")
    base = conn.execute("SELECT now()").fetchone()[0]
    add_message(conn, lane, "r1", "subagent_report", block(status="blocked"), at=base, offset=1)
    add_message(
        conn, lane, "r2", "subagent_report", block(status="partial", base="b" * 40), at=base + timedelta(minutes=5)
    )
    loops._lane(conn, loop_id, lane, "lineage")
    name, status, current, returns = conn.execute(
        "SELECT lane_name, return_status, lane_return, returns FROM ah.lane WHERE session_id = %s", (lane,)
    ).fetchone()
    assert name == "ALPHA"
    assert status == "partial" and current["base"] == "b" * 40
    assert [(r["status"], r["source"]) for r in returns] == [("blocked", "report"), ("partial", "report")]
    assert returns[0]["return"]["status"] == "blocked"


def test_return_relayed_as_an_agent_message_is_parsed(conn, tmp_path):  # noqa: F811
    root(conn, tmp_path, "Progress.")
    loop_id, root_id = loop_and_root(conn)
    lane = claude_lane(conn, loop_id, root_id, "a1111111111111111")
    other = claude_lane(conn, loop_id, root_id, "a9999999999999999")
    base = conn.execute("SELECT now()").fetchone()[0]
    add_message(conn, lane, "r1", "subagent_report", block(status="blocked"), at=base)
    # Resumed after a one-shot hand-back: the next return only reaches the parent as an agent message.
    later = indented(status="complete", base="c" * 40)
    add_message(
        conn,
        root_id,
        "hb1",
        "queued_prompt",
        HAND_BACK.format(prefix="", agent_id="a1111111111111111", report=later),
        at=base + timedelta(minutes=10),
        origin="agent_message",
    )
    # The same hand-back mirrored in the other delivery class, a relay for another lane and an
    # unframed mention are not additional returns of this lane.
    add_message(
        conn,
        root_id,
        "hb2",
        "agent_message",
        HAND_BACK.format(prefix="Another Claude session sent a message:\n", agent_id="a1111111111111111", report=later),
        at=base + timedelta(minutes=10, seconds=1),
    )
    add_message(
        conn,
        root_id,
        "hb3",
        "agent_message",
        HAND_BACK.format(prefix="", agent_id="a9999999999999999", report=indented(lane="BETA")),
        at=base + timedelta(minutes=11),
    )
    add_message(
        conn,
        root_id,
        "quote",
        "agent_message",
        'Quoting <agent-message from="a1111111111111111"> in prose.\n' + block(status="failed"),
        at=base + timedelta(minutes=12),
    )
    loops._lane(conn, loop_id, lane, "lineage")
    loops._lane(conn, loop_id, other, "lineage")
    returns, status, current = conn.execute(
        "SELECT returns, return_status, lane_return FROM ah.lane WHERE session_id = %s", (lane,)
    ).fetchone()
    assert [(r["status"], r["source"]) for r in returns] == [("blocked", "report"), ("complete", "hand_back")]
    assert status == "complete" and current["base"] == "c" * 40
    assert conn.execute("SELECT lane_name, return_status FROM ah.lane WHERE session_id = %s", (other,)).fetchone() == (
        "BETA",
        "complete",
    )


def test_lane_without_a_return_keeps_its_spawn_derived_name(conn, tmp_path):  # noqa: F811
    root(conn, tmp_path, "Progress.")
    loop_id, root_id = loop_and_root(conn)
    lane = claude_lane(conn, loop_id, root_id, "aaaaaaaaaaaaaaaaa")
    conn.execute("UPDATE ah.session SET agent_path = 'workers/QUIET' WHERE id = %s", (lane,))
    loops._lane(conn, loop_id, lane, "lineage")
    assert conn.execute(
        "SELECT lane_name, return_status, lane_return, returns FROM ah.lane WHERE session_id = %s", (lane,)
    ).fetchone() == ("QUIET", None, None, None)


def test_pi_lane_is_named_by_its_return_through_the_real_pipeline(conn, tmp_path):  # noqa: F811
    pi_fixture(tmp_path, role="lane-worker-low")
    refresh(conn, tmp_path)
    name, role, returns = conn.execute("SELECT lane_name, role, returns FROM ah.lane").fetchone()
    assert (name, role) == ("synthetic", "lane-worker-low")  # the agent type is the role, never the lane
    assert [r["status"] for r in returns] == ["complete"]


def test_resumed_pi_lane_keeps_each_return(conn, tmp_path):  # noqa: F811
    from test_loop_links_pg import CHILD, RUN

    base, root_base = pi_fixture(tmp_path)

    def final(uid, **fields):
        return assistant(uid, [{"type": "text", "text": block(**fields)}], stopReason="stop")

    write(
        base / root_base / RUN / "session.jsonl",
        [
            header(CHILD),
            message("brief", "user", "Lane: synthetic"),
            final("first", status="blocked"),
            message("again", "user", "Retry with the new base."),
            final("second", status="complete", base="d" * 40),
        ],
    )
    refresh(conn, tmp_path)
    name, status, returns = conn.execute("SELECT lane_name, return_status, returns FROM ah.lane").fetchone()
    assert (name, status) == ("ALPHA", "complete")
    assert [r["status"] for r in returns] == ["blocked", "complete"]

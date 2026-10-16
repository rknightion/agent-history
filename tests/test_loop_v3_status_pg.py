"""V3 lifecycle helper over real Writer/refresh rows in disposable PostgreSQL."""

import json
import os
from datetime import timedelta
from pathlib import Path

import psycopg
import pytest

from agent_history.loop_v3_status import refresh as refresh_status
from test_loop_launch_v3 import PROMPT
from test_loop_links_pg import (
    ROOT,
    assistant,
    conn,  # noqa: F401 - disposable database fixture
    header,
    message,
    refresh,
    write,
)

DSN = os.environ.get("AGENT_HISTORY_TEST_DSN", "")
pytestmark = pytest.mark.skipif(not DSN or "agent_history_test" not in DSN, reason="scratch database not set")
MIGRATION = Path(__file__).resolve().parents[1] / "src/agent_history/sql/migrations/039_loop_wait_status.sql"


def stamp(record, at):
    record["timestamp"] = at.isoformat()
    if "message" in record:
        record["message"]["timestamp"] = at.isoformat()
    return record


def root(db, source, text, *, prompt=PROMPT, thinking=False):
    at = db.execute("SELECT now() - interval '1 minute'").fetchone()[0]
    path = source / "sessions/synthetic/root.jsonl"
    content = ([{"type": "thinking", "thinking": "Consider the next step."}] if thinking else []) + [
        {"type": "text", "text": text}
    ]
    write(
        path,
        [
            stamp(header(ROOT), at),
            stamp(message("launch", "user", prompt), at),
            stamp(assistant("status", content, stopReason="stop"), at + timedelta(seconds=1)),
        ],
    )
    refresh(db, source)
    return path, at


def live(db):
    return db.execute("SELECT status,end_ts,waiting_until FROM ah.loops").fetchone()


def append(path, record, at):
    with path.open("a") as stream:
        stream.write(json.dumps(stamp(record, at)) + "\n")


def test_enter_exit_expiration_and_terminal_precedence(conn, tmp_path):  # noqa: F811
    until = conn.execute("SELECT now() + interval '1 hour'").fetchone()[0]
    path, at = root(conn, tmp_path, "Progress.\nWAITING: review until " + until.isoformat())
    assert live(conn) == ("waiting", None, until)
    assert refresh_status(conn) == 0  # public refresh already applied it; a repeat is idempotent
    # Same timestamp, later source position: a user message invalidates the old line.
    append(path, message("continue", "user", "Continue now."), at + timedelta(seconds=1))
    refresh(conn, tmp_path)
    refresh_status(conn)
    assert live(conn) == ("running", None, None)
    append(
        path,
        assistant("pause", [{"type": "text", "text": "PAUSED: owner decision"}], stopReason="stop"),
        at + timedelta(seconds=2),
    )
    refresh(conn, tmp_path)
    refresh_status(conn)
    assert live(conn) == ("paused", None, None)
    # A tool observation, even without another assistant text row, resumes ordinary status.
    conn.execute("UPDATE ah.session SET last_event_at=%s", (at + timedelta(seconds=3),))
    refresh_status(conn)
    assert live(conn) == ("running", None, None)
    # Expiry is evaluated on collector-only passes, not just dirty-root refreshes.
    conn.execute("UPDATE ah.session SET last_event_at=%s", (at + timedelta(seconds=2),))
    conn.execute(
        "UPDATE ah.message SET text=%s WHERE event_uid LIKE '%%:pause'", ("WAITING: review until " + until.isoformat(),)
    )
    refresh_status(conn)
    assert live(conn)[0] == "waiting"
    expired = at - timedelta(days=2)
    conn.execute(
        "UPDATE ah.message SET ts=%s,text=%s WHERE event_uid LIKE '%%:pause'",
        (expired, "WAITING: review until " + at.isoformat()),
    )
    conn.execute("UPDATE ah.loop_run SET launch_ts=%s", (expired - timedelta(minutes=1),))
    conn.execute("UPDATE ah.message SET ts=%s WHERE event_uid NOT LIKE '%%:pause'", (expired - timedelta(seconds=1),))
    conn.execute("UPDATE ah.session SET last_event_at=%s", (expired,))
    refresh_status(conn)
    assert live(conn) == ("stale", expired, None)
    conn.execute("UPDATE ah.message SET text='PAUSED: owner decision' WHERE event_uid LIKE '%:pause'")
    refresh_status(conn)
    assert live(conn) == ("paused", None, None)
    for evidence in ("completion_receipt", "next_launch", "report_write", "state_close"):
        conn.execute("UPDATE ah.loop_run SET end_evidence=%s,end_ts=%s", (evidence, at))
        conn.execute("UPDATE ah.loops SET status='paused',end_ts=NULL")
        if evidence == "state_close":
            # A qualified close is freshly projected by _refresh_state_counts.
            conn.execute("UPDATE ah.loops SET status='finished',end_ts=%s,waiting_until=%s", (at, until))
        refresh_status(conn)
        assert live(conn) == ("finished", at, None)


def test_removed_state_close_does_not_resurrect_terminal_metadata(conn, tmp_path):  # noqa: F811
    root(conn, tmp_path, "PAUSED: owner decision")
    conn.execute("UPDATE ah.loop_run SET end_evidence='state_close',end_ts=now()")
    # No qualifying state snapshot remains. The ordinary post-pass ignores old metadata.
    from agent_history.loops import refresh_live

    refresh_live(conn)
    refresh_status(conn)
    assert live(conn) == ("paused", None, None)


def test_exact_root_relaunch_membership_and_v2_unchanged(conn, tmp_path):  # noqa: F811
    path, at = root(conn, tmp_path, "Still working.")
    resume = "ffffffff-ffff-4fff-8fff-ffffffffffff"
    relaunch = tmp_path / "sessions/synthetic/resume.jsonl"
    write(
        relaunch,
        [
            stamp(header(resume), at + timedelta(seconds=2)),
            stamp(
                message("resume", "user", "/tmp/synthetic-project/codex/launch-synthetic-loop14.txt"),
                at + timedelta(seconds=2),
            ),
            stamp(
                assistant("resume-pause", [{"type": "text", "text": "PAUSED: decision"}], stopReason="stop"),
                at + timedelta(seconds=3),
            ),
        ],
    )
    refresh(conn, tmp_path)
    assert conn.execute("SELECT loop_link_method FROM ah.session WHERE session_uid=%s", (resume,)).fetchone() == (
        "relaunch",
    )
    refresh_status(conn)
    assert live(conn) == ("paused", None, None)
    # A lane is not a root, even if its text and activity are newer.
    conn.execute("UPDATE ah.session SET loop_link_method='lineage' WHERE session_uid=%s", (resume,))
    refresh_status(conn)
    assert live(conn) == ("running", None, None)
    # A newer root tool/activity observation invalidates the relaunch's prior line.
    conn.execute("UPDATE ah.session SET loop_link_method='relaunch' WHERE session_uid=%s", (resume,))
    conn.execute("UPDATE ah.session SET last_event_at=%s WHERE session_uid=%s", (at + timedelta(seconds=4), ROOT))
    refresh_status(conn)
    assert live(conn) == ("running", None, None)
    conn.execute("UPDATE ah.loop_run SET mode='v2'")
    before = conn.execute("SELECT * FROM ah.loops").fetchone()
    append(
        path,
        assistant("v2-pause", [{"type": "text", "text": "PAUSED: ignored"}], stopReason="stop"),
        at + timedelta(seconds=5),
    )
    # Updating retained evidence alone must not write a historical v2 lifecycle row.
    conn.execute("UPDATE ah.message SET text='PAUSED: ignored'")
    assert refresh_status(conn) == 0
    assert conn.execute("SELECT * FROM ah.loops").fetchone() == before


@pytest.mark.parametrize("status", ["paused", "waiting"])
def test_cross_session_tied_relaunch_activity_withholds_marker(conn, tmp_path, status):  # noqa: F811
    until = conn.execute("SELECT date_trunc('minute', now()) + interval '1 hour'").fetchone()[0]
    text = "PAUSED: owner decision" if status == "paused" else "WAITING: review until " + until.isoformat()
    _, at = root(conn, tmp_path, text)
    refresh_status(conn)
    assert live(conn) == (status, None, until if status == "waiting" else None)
    # The original marker's seq is greater than the relaunch prompt's seq, but
    # per-session sequence numbers cannot establish order between these roots.
    tied = at + timedelta(seconds=1)
    resume = "ffffffff-ffff-4fff-8fff-ffffffffffff"
    write(
        tmp_path / "sessions/synthetic/resume.jsonl",
        [
            stamp(header(resume), tied),
            stamp(message("resume", "user", "/tmp/synthetic-project/codex/launch-synthetic-loop14.txt"), tied),
        ],
    )
    refresh(conn, tmp_path)
    assert conn.execute("SELECT loop_link_method FROM ah.session WHERE session_uid=%s", (resume,)).fetchone() == (
        "relaunch",
    )
    refresh_status(conn)
    assert live(conn) == ("running", None, None)


def test_minute_only_deadline_projects_waiting(conn, tmp_path):  # noqa: F811
    until = conn.execute("SELECT date_trunc('minute', now()) + interval '1 hour'").fetchone()[0]
    root(conn, tmp_path, "WAITING: review until " + until.strftime("%Y-%m-%dT%H:%MZ"))
    refresh_status(conn)
    assert live(conn) == ("waiting", None, until)


@pytest.mark.parametrize("status", ["paused", "waiting"])
def test_thinking_and_final_marker_in_same_pi_record(conn, tmp_path, status):  # noqa: F811
    until = conn.execute("SELECT date_trunc('minute', now()) + interval '1 hour'").fetchone()[0]
    text = "PAUSED: owner decision" if status == "paused" else "WAITING: review until " + until.isoformat()
    root(conn, tmp_path, text, thinking=True)
    rows = conn.execute(
        "SELECT message_class,ts,source_id,byte_offset,seq FROM ah.message "
        "WHERE message_class IN ('assistant_text','reasoning') ORDER BY seq"
    ).fetchall()
    # Real PiParser/Writer rows collide on the old selector's entire order key.
    # The established per-record semantic order is reasoning before final text.
    assert [row[0] for row in rows] == ["reasoning", "assistant_text"]
    assert rows[0][1:4] == rows[1][1:4]
    print("same-record collision:", rows)
    refresh_status(conn)
    assert live(conn) == (status, None, until if status == "waiting" else None)


def test_new_launch_window_does_not_inherit_old_marker(conn, tmp_path):  # noqa: F811
    path, at = root(conn, tmp_path, "PAUSED: decision")
    refresh_status(conn)
    append(path, message("new-launch", "user", PROMPT.replace("loop14", "loop15")), at + timedelta(seconds=5))
    refresh(conn, tmp_path)
    refresh_status(conn)
    assert conn.execute("SELECT status,waiting_until FROM ah.loops ORDER BY launch_ts").fetchall() == [
        ("finished", None),
        ("running", None),
    ]


def test_real_v2_launch_is_untouched(conn, tmp_path):  # noqa: F811
    root(
        conn, tmp_path, "PAUSED: historical marker", prompt="You are the root. Write codex/report-synthetic-loop14.md."
    )
    assert conn.execute("SELECT mode FROM ah.loop_run").fetchone() == ("v2",)
    before = conn.execute("SELECT * FROM ah.loops").fetchone()
    assert refresh_status(conn) == 0
    assert conn.execute("SELECT * FROM ah.loops").fetchone() == before


def test_same_timestamp_newer_tool_activity_invalidates_marker(conn, tmp_path):  # noqa: F811
    path, at = root(conn, tmp_path, "PAUSED: decision")
    refresh_status(conn)
    assert live(conn) == ("paused", None, None)
    same_time = at + timedelta(seconds=1)
    append(
        path,
        assistant(
            "work",
            [{"type": "toolCall", "id": "work", "name": "bash", "arguments": {"command": "true"}}],
            stopReason="toolUse",
        ),
        same_time,
    )
    append(path, message("result", "toolResult", [], toolCallId="work", toolName="bash", isError=False), same_time)
    refresh(conn, tmp_path)
    refresh_status(conn)
    assert live(conn) == ("running", None, None)


@pytest.mark.parametrize("upgrade", [False, True])
def test_fresh_and_upgraded_checks_preserve_existing_rows(conn, tmp_path, upgrade):  # noqa: F811
    root(conn, tmp_path, "Working.")
    if upgrade:
        conn.execute("ALTER TABLE ah.loops DROP CONSTRAINT loops_status_check, DROP CONSTRAINT loops_check")
        conn.execute(
            "ALTER TABLE ah.loops ADD CONSTRAINT loops_status_check CHECK (status IN ('running','finished','stale')), ADD CONSTRAINT loops_check CHECK ((status='running')=(end_ts IS NULL))"
        )
        # Red SQL proof: the actual old constraints reject the new open observation.
        with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
            conn.execute("UPDATE ah.loops SET status='waiting',end_ts=NULL")
        before = conn.execute("SELECT * FROM ah.loops").fetchone()
        conn.execute(MIGRATION.read_text())
        assert conn.execute("SELECT * FROM ah.loops").fetchone() == before
    for status in ("running", "waiting", "paused"):
        conn.execute("UPDATE ah.loops SET status=%s,end_ts=NULL", (status,))
        with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
            conn.execute("UPDATE ah.loops SET end_ts=now()")
    for status in ("finished", "stale"):
        conn.execute("UPDATE ah.loops SET status=%s,end_ts=now()", (status,))
        with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
            conn.execute("UPDATE ah.loops SET end_ts=NULL")
    with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
        conn.execute("UPDATE ah.loops SET status='invented'")

"""Synthetic v3 launches through real parsers, Writer and the refresh post-pass."""

import json
import os

import pytest

from agent_history import load
from test_loop_links_pg import (
    AT,
    CWD,
    ROOT,
    assistant,
    conn,  # noqa: F401 - disposable database fixture
    header,
    message,
    refresh,
    write,
)
from test_loop_launch_v3 import CHARTER, CLOSEOUT, PROMPT

DSN = os.environ.get("AGENT_HISTORY_TEST_DSN", "")
pytestmark = pytest.mark.skipif(not DSN or "agent_history_test" not in DSN, reason="scratch database not set")
LAUNCH = CWD + "/codex/launch-synthetic-loop14.txt"


def transcript(source, agent, prompt=PROMPT):
    if agent == "claude":
        path = source / "projects" / "synthetic" / f"{ROOT}.jsonl"
        records = [
            {
                "type": "user",
                "uuid": "launch",
                "sessionId": ROOT,
                "timestamp": AT,
                "cwd": CWD,
                "message": {"role": "user", "content": prompt},
            }
        ]
    elif agent == "codex":
        path = source / "sessions" / "2026" / "10" / "04" / f"rollout-{ROOT}.jsonl"
        records = [
            {"type": "session_meta", "timestamp": AT, "payload": {"id": ROOT, "source": "cli", "cwd": CWD}},
            {"type": "event_msg", "timestamp": AT, "payload": {"type": "user_message", "message": prompt}},
            {
                "type": "response_item",
                "timestamp": AT,
                "payload": {
                    "type": "custom_tool_call",
                    "call_id": "closeout",
                    "name": "apply_patch",
                    "input": "*** Begin Patch\n*** Add File: " + CLOSEOUT + "\n+Synthetic closeout.\n*** End Patch",
                },
            },
            {
                "type": "response_item",
                "timestamp": AT,
                "payload": {
                    "type": "custom_tool_call_output",
                    "call_id": "closeout",
                    "output": "Success. Updated the following files:\nA " + CLOSEOUT,
                },
            },
        ]
    else:
        path = source / "sessions" / "synthetic" / f"timestamp_{ROOT}.jsonl"
        records = [header(ROOT), message("launch", "user", prompt)]
    write(path, records)
    return path


@pytest.mark.parametrize("agent", ["claude", "codex", "pi"])
def test_v3_launch_creates_run_linked_to_root_and_v2_rows_stay_unchanged(conn, tmp_path, agent):  # noqa: F811
    source = tmp_path / agent
    path = transcript(source, agent, "You are the root. Write codex/report-synthetic-loop14.md.")
    sources = {agent + "-test": source}
    assert load.refresh(conn, sources=sources, textfile=None, log=lambda *_: None).errors == 0
    v2 = conn.execute("SELECT launch_uid, mode, report_path, goal_path FROM ah.loop_run").fetchone()
    assert v2[1:] == ("v2", CWD + "/codex/report-synthetic-loop14.md", CWD + "/codex/goal-synthetic-loop14.md")
    conn.commit()
    # A second operator launch is appended, rather than changing the retained first prompt.
    if agent == "claude":
        record = {
            "type": "user",
            "uuid": "launch-v3",
            "sessionId": ROOT,
            "timestamp": "2026-10-04T13:00:00Z",
            "cwd": CWD,
            "message": {"role": "user", "content": PROMPT},
        }
    elif agent == "codex":
        record = {
            "type": "event_msg",
            "timestamp": "2026-10-04T13:00:00Z",
            "payload": {"type": "user_message", "message": PROMPT},
        }
    else:
        record = message("launch-v3", "user", PROMPT)
        record["timestamp"] = "2026-10-04T13:00:00Z"
        record["message"]["timestamp"] = record["timestamp"]
    with path.open("a") as stream:
        stream.write(json.dumps(record) + "\n")
    assert load.refresh(conn, sources=sources, textfile=None, log=lambda *_: None).errors == 0
    assert (
        conn.execute("SELECT launch_uid, mode, report_path, goal_path FROM ah.loop_run ORDER BY launch_ts").fetchall()[
            0
        ]
        == v2
    )
    row = conn.execute(
        "SELECT l.mode,l.report_path,l.goal_path,l.loop_number,s.agent,s.loop_run_id=l.id "
        "FROM ah.loop_run l JOIN ah.session s ON s.id=l.root_session_id WHERE l.mode='v3'"
    ).fetchone()
    # The root's single membership remains the first run; each launch still has its exact owner.
    assert row[:5] == ("v3", CLOSEOUT, CHARTER, 14, agent)
    assert conn.execute("SELECT count(*) FROM ah.loop_run").fetchone() == (2,)
    if agent == "codex":
        assert conn.execute("SELECT path FROM ah.file_touch WHERE op='create'").fetchall() == [(CLOSEOUT,)]


def test_bare_v3_resolves_only_same_stem_captured_target(conn, tmp_path):  # noqa: F811
    path = transcript(tmp_path, "pi", LAUNCH)
    records = [json.loads(line) for line in path.read_text().splitlines()]
    records += [
        assistant(
            "read-call",
            [{"type": "toolCall", "id": "read-launch", "name": "read", "arguments": {"path": LAUNCH}}],
            stopReason="toolUse",
        ),
        message(
            "read-result",
            "toolResult",
            [{"type": "text", "text": PROMPT}],
            toolCallId="read-launch",
            toolName="read",
            isError=False,
        ),
    ]
    write(path, records)
    refresh(conn, tmp_path)
    assert conn.execute("SELECT status,mode,report_path,goal_path FROM ah.loop_run").fetchone() == (
        "resolved",
        "v3",
        CLOSEOUT,
        CHARTER,
    )
    conn.execute(
        "UPDATE ah.tool_io SET output_text=%s WHERE tool_name='read'",
        (PROMPT.replace("synthetic-loop14", "unrelated-loop14"),),
    )
    conn.execute("INSERT INTO ah.dirty_session SELECT id FROM ah.session ON CONFLICT DO NOTHING")
    load.post_passes(conn)
    assert conn.execute("SELECT status,launch_sha256,report_path FROM ah.loop_run").fetchone() == (
        "unresolved_path",
        None,
        CLOSEOUT,
    )


@pytest.mark.parametrize("evidence", ["charter", "launch", "state", "ambiguous", "contradictory"])
def test_v3_relaunch_requires_one_unambiguous_existing_target(conn, tmp_path, evidence):  # noqa: F811
    transcript(tmp_path, "pi")
    refresh(conn, tmp_path)
    if evidence == "ambiguous":
        # Same-stem v2 and v3 runs: launch/state evidence must not choose either version.
        other = tmp_path / "sessions" / "synthetic" / "earlier.jsonl"
        uid = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
        write(
            other,
            [header(uid), message("v2-launch", "user", "You are the root. Write codex/report-synthetic-loop14.md.")],
        )
        refresh(conn, tmp_path)
    resume = "ffffffff-ffff-4fff-8fff-ffffffffffff"
    prompt = CHARTER if evidence in ("charter", "contradictory") else LAUNCH if evidence == "launch" else "Continue."
    records = [header(resume), message("resume", "user", prompt)]
    if evidence in ("state", "ambiguous", "contradictory"):
        stem = "unrelated" if evidence == "contradictory" else "synthetic"
        records += [
            assistant(
                "append-call",
                [
                    {
                        "type": "toolCall",
                        "id": "append",
                        "name": "bash",
                        "arguments": {
                            "command": f"loop-state append {CWD}/codex/state-{stem}-loop14.jsonl judgement text=continue"
                        },
                    }
                ],
                stopReason="toolUse",
            ),
            message("append-result", "toolResult", [], toolCallId="append", toolName="bash", isError=False),
        ]
    for record in records:
        record["timestamp"] = "2026-10-04T14:00:00Z"
        if "message" in record:
            record["message"]["timestamp"] = record["timestamp"]
    write(tmp_path / "sessions" / "synthetic" / "resume.jsonl", records)
    refresh(conn, tmp_path)
    method = conn.execute("SELECT loop_link_method FROM ah.session WHERE session_uid=%s", (resume,)).fetchone()[0]
    assert method == (None if evidence in ("ambiguous", "contradictory") else "relaunch")
    assert conn.execute(
        "SELECT count(*) FROM ah.loop_run WHERE root_session_id=(SELECT id FROM ah.session WHERE session_uid=%s)",
        (resume,),
    ).fetchone() == (0,)


def test_historical_mixed_v2_backfill_preserves_existing_projection(conn, tmp_path):  # noqa: F811
    text = (
        "You are the root. Read codex/goal-synthetic-loop14.md. "
        "Write codex/report-synthetic-loop14.md. Background notes: codex/closeout-notes-loop1.md."
    )
    transcript(tmp_path, "pi", text)
    refresh(conn, tmp_path)
    report = CWD + "/codex/report-synthetic-loop14.md"
    goal = CWD + "/codex/goal-synthetic-loop14.md"
    # Seed the retained fields emitted by the report-only projection, independently of the
    # current parser. The new historical pass must preserve them, not follow the incidental token.
    conn.execute(
        "UPDATE ah.loop_run SET mode='v2',report_path=%s,goal_path=%s,loop_number=14,"
        "campaign_slug='synthetic',repo_slug='synthetic-project',naming='loop'",
        (report, goal),
    )
    projection = (
        "SELECT launch_uid,mode,report_path,goal_path,loop_number,campaign_slug,repo_slug,naming FROM ah.loop_run"
    )
    before = conn.execute(projection).fetchall()
    assert len(before) == 1
    assert before[0][1:] == ("v2", report, goal, 14, "synthetic", "synthetic-project", "loop")
    messages = conn.execute("SELECT text FROM ah.message ORDER BY id").fetchall()
    conn.execute("DELETE FROM ah.dirty_session")
    conn.execute(
        "INSERT INTO ah.meta VALUES ('loops_links_projection_v2','complete') "
        "ON CONFLICT (key) DO UPDATE SET value='complete'"
    )
    conn.execute("DELETE FROM ah.meta WHERE key='loops_links_projection_v3'")
    conn.commit()
    assert load.post_passes(conn)["dirty_sessions"] == 1
    assert conn.execute(projection).fetchall() == before
    assert conn.execute("SELECT text FROM ah.message ORDER BY id").fetchall() == messages


def test_completed_v2_cursor_does_not_prevent_v3_historical_tagging(conn, tmp_path):  # noqa: F811
    transcript(tmp_path, "pi")
    refresh(conn, tmp_path)
    original = conn.execute("SELECT text FROM ah.message ORDER BY id").fetchall()
    conn.execute("UPDATE ah.session SET loop_run_id=NULL,loop_link_method=NULL")
    conn.execute("DELETE FROM ah.loop_run")
    conn.execute("DELETE FROM ah.dirty_session")
    conn.execute(
        "INSERT INTO ah.meta VALUES ('loops_links_projection_v2','complete') ON CONFLICT (key) DO UPDATE SET value='complete'"
    )
    conn.execute("DELETE FROM ah.meta WHERE key='loops_links_projection_v3'")
    conn.commit()
    assert load.post_passes(conn)["dirty_sessions"] == 1
    assert conn.execute("SELECT mode,report_path,goal_path FROM ah.loop_run").fetchone() == ("v3", CLOSEOUT, CHARTER)
    assert conn.execute("SELECT text FROM ah.message ORDER BY id").fetchall() == original

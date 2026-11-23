"""The run-folder dialect (protocol v5) and Claude lane returns through the real refresh post-pass."""

import hashlib
import os
from datetime import datetime, timedelta, timezone

import pytest

from agent_history import load, loops
from test_loop_identity_pg import conn  # noqa: F401 - real disposable catalogue fixture
from test_loop_identity_v3 import AT as V3_AT, CHARTER, CLOSEOUT as V3_CLOSEOUT, DIGEST, HEADER as V3_HEADER
from test_loop_launch_v3_pg import transcript
from test_loop_links_pg import CWD, ROOT, assistant, header, message, write

DSN = os.environ.get("AGENT_HISTORY_TEST_DSN", "")
pytestmark = pytest.mark.skipif(not DSN or "agent_history_test" not in DSN, reason="scratch database not set")

# A run long past, so its lifecycle reads stale rather than running whenever the suite runs.
T0 = datetime(2026, 10, 4, 9, tzinfo=timezone.utc)
RUN = "run-20261123-1"
AUTHORITY = f"{CWD}/codex/{RUN}/AUTHORITY.md"
CLOSEOUT = f"{CWD}/codex/{RUN}/CLOSEOUT.md"
AUTHORITY_BODY = f"# Authority: project {RUN}\nprotocol: v5\n\nScope and stop conditions.\n"
CLOSEOUT_HEADER = f"# Closeout: project {RUN}"
CLOSEOUT_BODY = (
    CLOSEOUT_HEADER + "\nprotocol: v5\n\n## Landed\n- #11 First\n- #12 Second\n\n## Parked\n- #13 Waits\n\n"
    "## Decisions needed\n\n## Spend by role\n"
)
MARKERS = ("loops_identity_v3_projection_v1", "loops_run_projection_v1", loops.LANE_RETURNS_MARKER)


def wipe(db):
    db.execute("DELETE FROM ah.loop_run_metadata")
    db.execute("DELETE FROM ah.loop_receipt")
    db.execute("DELETE FROM ah.meta WHERE key = ANY(%s)", (list(MARKERS),))
    db.commit()


@pytest.fixture
def clean(conn):  # noqa: F811
    conn.execute("TRUNCATE " + ", ".join(f"ah.{table}" for table in load.DATA_TABLES) + " RESTART IDENTITY CASCADE")
    wipe(conn)
    yield conn
    conn.rollback()
    wipe(conn)


def stamp(record, at):
    record["timestamp"] = at.isoformat()
    if "message" in record:
        record["message"]["timestamp"] = at.isoformat()
    return record


def tool_write(uid, at, path, content, *, tool="write", arguments=None):
    """A pi tool call and its successful result, one second apart."""
    return [
        stamp(
            assistant(
                f"{uid}-call",
                [
                    {
                        "type": "toolCall",
                        "id": uid,
                        "name": tool,
                        "arguments": arguments or {"path": path, "content": content},
                    }
                ],
                stopReason="toolUse",
            ),
            at,
        ),
        stamp(
            message(
                f"{uid}-result",
                "toolResult",
                [{"type": "text", "text": "Done."}],
                toolCallId=uid,
                toolName=tool,
                isError=False,
            ),
            at + timedelta(seconds=1),
        ),
    ]


def run_root(source, *more, authority=AUTHORITY_BODY, tool="write", arguments=None):
    records = [stamp(header(ROOT), T0), stamp(message("ask", "user", "Run the ready queue."), T0)]
    records += tool_write("authority", T0 + timedelta(seconds=10), AUTHORITY, authority, tool=tool, arguments=arguments)
    records += [record for group in more for record in group]
    records.append(
        stamp(assistant("idle", [{"type": "text", "text": "Waiting."}], stopReason="stop"), T0 + timedelta(hours=3))
    )
    write(source / "sessions" / "synthetic" / "run-root.jsonl", records)


def refresh(db, source=None):
    if source is not None:
        assert load.refresh(db, sources={"pi-test": source}, textfile=None, log=lambda *_: None).errors == 0
    else:
        load.post_passes(db)
    db.commit()


def run_row(db):
    return db.execute(
        "SELECT l.mode,l.protocol,l.naming,l.goal_path,l.report_path,l.launch_ts,l.loop_number,l.end_evidence,"
        "r.loop,r.status FROM ah.loop_run l JOIN ah.loops r USING(launch_uid)"
    ).fetchall()


def counts(db):
    return db.execute(
        "SELECT r.tasks_landed,r.tasks_done,r.parks_total,r.status,l.end_evidence,l.end_ts "
        "FROM ah.loop_run l JOIN ah.loops r USING(launch_uid)"
    ).fetchone()


def collect(
    db, *, machine="machine-a", body=CLOSEOUT_BODY, line1=CLOSEOUT_HEADER, protocol="v5", when=None, counts=(2, 1)
):
    db.execute(
        "INSERT INTO ah.loop_run_metadata(machine,path,kind,sha256,line1,file_mtime,repo_origin,landed_count,"
        "parked_count,protocol) VALUES (%s,%s,'run_closeout',%s,%s,%s,'example/project',%s,%s,%s)",
        (
            machine,
            CLOSEOUT,
            hashlib.sha256(body.encode()).hexdigest(),
            line1,
            when or T0 + timedelta(hours=2, seconds=2),
            *counts,
            protocol,
        ),
    )
    db.commit()


def claude_lane(db, root_id, agent_id, at):
    return db.execute(
        "INSERT INTO ah.session (agent, session_uid, agent_id, is_stub, is_subagent, root_session_id, "
        "parent_session_id, first_event_at, last_event_at, agent_type) "
        "VALUES ('claude', %s, %s, false, true, %s, %s, %s, %s, 'worker') RETURNING id",
        (f"lane-{agent_id}", agent_id, root_id, root_id, at, at),
    ).fetchone()[0]


def final_text(db, session_id, uid, text, at):
    db.execute(
        "INSERT INTO ah.message (agent, event_uid, session_id, namespace, profile, ts, role, message_class, text, "
        "content_sha256, byte_length, source_id, byte_offset) "
        "VALUES ('claude', %s, %s, 'claude-test', 'p', %s, 'assistant', 'assistant_text', %s, 'x', 1, 0, 0)",
        (uid, session_id, at, text),
    )


def test_authority_write_starts_a_v5_run_and_its_lanes_return(clean, tmp_path):
    run_root(tmp_path)
    refresh(clean, tmp_path)
    assert run_row(clean) == [
        ("run", "v5", "run", AUTHORITY, CLOSEOUT, T0 + timedelta(seconds=10), None, "root_last_event", RUN, "stale")
    ]
    loop_id, root_id = clean.execute("SELECT id, root_session_id FROM ah.loop_run").fetchone()
    assert clean.execute("SELECT loop_run_id FROM ah.session WHERE id=%s", (root_id,)).fetchone() == (loop_id,)
    # A Claude lane of that root, and its plain or fenced final message.
    plain = claude_lane(clean, root_id, "a1", T0 + timedelta(minutes=5))
    final_text(clean, plain, "p1", "Working on it.", T0 + timedelta(minutes=6))
    final_text(clean, plain, "p2", "Status: blocked\n\nOutcome: waits for the owner.", T0 + timedelta(minutes=9))
    fenced = claude_lane(clean, root_id, "a2", T0 + timedelta(minutes=5))
    final_text(
        clean,
        fenced,
        "f1",
        'Done.\n```lane-return\n{"v": 3, "lane": "L2", "status": "complete"}\n```',
        T0 + timedelta(minutes=8),
    )
    silent = claude_lane(clean, root_id, "a3", T0 + timedelta(minutes=5))
    final_text(clean, silent, "s1", "Finished, see the branch.", T0 + timedelta(minutes=7))
    early = claude_lane(clean, root_id, "a4", T0)  # started before the run: not its lane
    clean.execute("INSERT INTO ah.dirty_session VALUES (%s) ON CONFLICT DO NOTHING", (root_id,))
    refresh(clean)
    lanes = clean.execute(
        "SELECT session_id, lane_name, return_status, returns->-1->>'source' FROM ah.lane WHERE loop_run_id=%s ORDER BY session_id",
        (loop_id,),
    ).fetchall()
    assert lanes == [
        (plain, None, "blocked", "final_message"),
        (fenced, "L2", "complete", "final_message"),
        (silent, None, None, None),
    ]
    assert clean.execute("SELECT loop_run_id FROM ah.session WHERE id=%s", (early,)).fetchone() == (None,)
    assert clean.execute("SELECT lanes_total, lanes_returned FROM ah.loops").fetchone() == (3, 2)


def test_closeout_write_finishes_the_run_and_collected_counts_apply(clean, tmp_path):
    run_root(tmp_path, tool_write("closeout", T0 + timedelta(hours=2), CLOSEOUT, CLOSEOUT_BODY))
    refresh(clean, tmp_path)
    assert counts(clean)[3:] == ("finished", "closeout", T0 + timedelta(hours=2, seconds=1))
    assert counts(clean)[:3] == (None, None, None)
    collect(clean)
    for _ in range(2):
        refresh(clean)
        assert counts(clean) == (2, 2, 1, "finished", "closeout", T0 + timedelta(hours=2, seconds=1))
    assert clean.execute("SELECT repo FROM ah.loops").fetchone() == ("example/project",)
    # A second machine's different bytes revoke the closeout end and its counts.
    collect(clean, machine="machine-b", body=CLOSEOUT_BODY + "More.\n", counts=(5, 0))
    refresh(clean)
    landed, done, parked, status, evidence, _ = counts(clean)
    assert (landed, parked, evidence) == (None, None, "root_last_event") and status != "finished"


def test_collected_closeout_alone_finishes_the_run(clean, tmp_path):
    run_root(tmp_path)
    refresh(clean, tmp_path)
    collect(clean, when=T0 + timedelta(hours=2))
    refresh(clean)
    assert counts(clean) == (2, 2, 1, "finished", "closeout", T0 + timedelta(hours=2))


@pytest.mark.parametrize("line1, protocol", [(None, "v5"), (CLOSEOUT_HEADER, None), (CLOSEOUT_HEADER, "v4")])
def test_unqualified_collected_closeout_does_not_finish(clean, tmp_path, line1, protocol):
    run_root(tmp_path)
    refresh(clean, tmp_path)
    collect(clean, line1=line1, protocol=protocol)
    refresh(clean)
    assert counts(clean)[3:5] != ("finished", "closeout")


@pytest.mark.parametrize(
    "variant",
    ["protocol_v4", "other_run", "no_protocol", "edit"],
)
def test_unqualified_authority_writes_start_no_run(clean, tmp_path, variant):
    body = {
        "protocol_v4": AUTHORITY_BODY.replace("protocol: v5", "protocol: v4"),
        "other_run": AUTHORITY_BODY.replace(RUN, "run-20261123-2"),
        "no_protocol": AUTHORITY_BODY.replace("protocol: v5\n", ""),
        "edit": AUTHORITY_BODY,
    }[variant]
    if variant == "edit":
        run_root(tmp_path, tool="edit", arguments={"path": AUTHORITY, "oldText": "x", "newText": body})
    else:
        run_root(tmp_path, authority=body)
    refresh(clean, tmp_path)
    assert clean.execute("SELECT count(*) FROM ah.loop_run").fetchone() == (0,)


def test_old_dialect_run_is_identified_as_before_and_takes_its_declared_protocol(clean, tmp_path):
    transcript(tmp_path, "claude")
    refresh_sources = {"claude-test": tmp_path}
    assert load.refresh(clean, sources=refresh_sources, textfile=None, log=lambda *_: None).errors == 0
    clean.commit()

    def observe(kind, path, sha, line1, protocol, machine="machine-a"):
        clean.execute(
            "INSERT INTO ah.loop_run_metadata(machine,path,kind,sha256,line1,file_mtime,repo_origin,protocol) "
            "VALUES (%s,%s,%s,%s,%s,%s,'example/project',%s)",
            (machine, path, kind, sha, line1, V3_AT + timedelta(minutes=1), protocol),
        )
        clean.commit()

    def identity():
        return clean.execute(
            "SELECT l.mode,l.protocol,l.report_path,l.goal_path,r.repo,r.loop,r.goal_sha256,r.status,l.end_evidence "
            "FROM ah.loop_run l JOIN ah.loops r USING(launch_uid)"
        ).fetchall()

    observe("closeout", V3_CLOSEOUT, "b" * 64, V3_HEADER, None)
    refresh(clean)
    expected = ("v3", V3_CLOSEOUT, CHARTER, "example/project", "loop14", DIGEST, "finished", "closeout")
    assert identity() == [(expected[0], None, *expected[1:])]
    observe("charter", CHARTER, DIGEST, None, "v4")
    refresh(clean)
    assert identity() == [(expected[0], "v4", *expected[1:])]
    # Two declared tokens are no protocol; identity and completion are unchanged by either.
    observe("closeout", V3_CLOSEOUT, "b" * 64, V3_HEADER, "v5", machine="machine-b")
    refresh(clean)
    assert identity() == [(expected[0], None, *expected[1:])]

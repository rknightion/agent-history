"""A run planned in one session and run by a pi root, and a pi root's codemode writes (protocol v5)."""

import os
from datetime import timedelta

import pytest

from test_loop_identity_pg import conn  # noqa: F401 - real disposable catalogue fixture
from test_loop_links_pg import assistant, header, message, write
from test_loop_run_dialect_pg import (
    AUTHORITY,
    AUTHORITY_BODY,
    CWD,
    RUN,
    T0,
    clean as _clean,  # noqa: F401 - re-exported under the name the tests request
    refresh,
    run_root,
    stamp,
    tool_write,
)

DSN = os.environ.get("AGENT_HISTORY_TEST_DSN", "")
pytestmark = pytest.mark.skipif(not DSN or "agent_history_test" not in DSN, reason="scratch database not set")

clean = _clean  # noqa: F811

EXECUTOR = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"


def codemode(uid, at, nested, *, name="codemode"):
    """A pi codemode call and its result, whose `nestedCalls` record the tools the script ran."""
    calls = [
        {"id": f"{uid}/{n}", "name": tool, "status": "ok", "durationMs": 5, "arguments": arguments}
        for n, (tool, arguments) in enumerate(nested, 1)
    ]
    return [
        stamp(
            assistant(
                f"{uid}-call",
                [{"type": "toolCall", "id": uid, "name": name, "arguments": {"code": "await tools.read(...)"}}],
                stopReason="toolUse",
            ),
            at,
        ),
        stamp(
            message(
                f"{uid}-result",
                "toolResult",
                [{"type": "text", "text": "Script completed"}],
                toolCallId=uid,
                toolName=name,
                isError=False,
                details={"calls": []},
                nestedCalls={"calls": calls, "complete": True},
            ),
            at + timedelta(seconds=1),
        ),
    ]


def lane_file(source, stem, run_id, uid, at):
    """A pi child transcript under its parent's folder, the layout the loader reads as a lane."""
    write(
        source / "sessions" / "synthetic" / stem / run_id / "session.jsonl",
        [
            stamp(header(uid), at),
            stamp(message("brief", "user", "Lane: synthetic"), at),
            stamp(assistant("done", [{"type": "text", "text": "Done."}], stopReason="stop"), at + timedelta(minutes=1)),
        ],
    )


def executor(source, *, reads=AUTHORITY, at=T0 + timedelta(minutes=10), lanes=3):
    stem = f"2026-10-04T09-10-00Z_{EXECUTOR}"
    records = [stamp(header(EXECUTOR), at), stamp(message("ask", "user", "Run the plan."), at)]
    records += codemode("rd", at + timedelta(seconds=5), [("read", {"path": reads})])
    records.append(
        stamp(assistant("idle", [{"type": "text", "text": "Waiting."}], stopReason="stop"), at + timedelta(hours=2))
    )
    write(source / "sessions" / "synthetic" / (stem + ".jsonl"), records)
    for n in range(lanes):
        lane_file(
            source,
            stem,
            f"5f000000-0000-4000-8000-00000000000{n}",
            f"1a000000-0000-4000-8000-00000000000{n}",
            at + timedelta(minutes=10 + n),
        )


def session_id(db, uid):
    return db.execute("SELECT id FROM ah.session WHERE session_uid = %s", (uid,)).fetchone()[0]


def run_state(db):
    return db.execute(
        "SELECT l.id, l.root_session_id, l.launch_uid, l.protocol FROM ah.loop_run l WHERE l.mode = 'run'"
    ).fetchall()


def test_pi_root_that_runs_a_planned_run_takes_the_run_and_its_lanes(clean, tmp_path):
    run_root(tmp_path)  # the planning session writes AUTHORITY.md and dispatches nothing
    executor(tmp_path)
    refresh(clean, tmp_path)
    ((run_id, pi, launch_uid, protocol),) = run_state(clean)
    assert pi == session_id(clean, EXECUTOR) and protocol == "v5"
    planner = session_id(clean, "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
    methods = dict(clean.execute("SELECT id, loop_link_method FROM ah.session WHERE loop_run_id = %s", (run_id,)))
    lanes = [r[0] for r in clean.execute("SELECT id FROM ah.session WHERE is_subagent AND root_session_id = %s", (pi,))]
    assert len(lanes) == 3
    assert methods[planner] == "plan" and methods[pi] == "launch"
    assert {methods[lane] for lane in lanes} == {"lineage"}
    assert sorted(
        r[0] for r in clean.execute("SELECT session_id FROM ah.lane WHERE loop_run_id = %s", (run_id,))
    ) == sorted(lanes)
    assert clean.execute("SELECT lanes_total FROM ah.loops").fetchone() == (3,)
    # a further pass, including one that revisits the planner, changes nothing
    clean.execute("INSERT INTO ah.dirty_session VALUES (%s) ON CONFLICT DO NOTHING", (planner,))
    refresh(clean)
    assert run_state(clean) == [(run_id, pi, launch_uid, "v5")]
    assert (
        dict(clean.execute("SELECT id, loop_link_method FROM ah.session WHERE loop_run_id = %s", (run_id,))) == methods
    )


def test_a_pi_session_that_never_read_the_authority_file_does_not_take_the_run(clean, tmp_path):
    run_root(tmp_path)
    executor(tmp_path, reads=f"{CWD}/codex/{RUN}/PLAN.md")
    refresh(clean, tmp_path)
    assert run_state(clean)[0][1] != session_id(clean, EXECUTOR)


def test_a_planner_that_dispatched_lanes_keeps_its_run(clean, tmp_path):
    run_root(tmp_path)
    stem = "2026-10-04T09-00-00Z_aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    (tmp_path / "sessions" / "synthetic" / "run-root.jsonl").rename(
        tmp_path / "sessions" / "synthetic" / (stem + ".jsonl")
    )
    lane_file(
        tmp_path,
        stem,
        "5f000000-0000-4000-8000-0000000000aa",
        "1a000000-0000-4000-8000-0000000000aa",
        T0 + timedelta(minutes=2),
    )
    executor(tmp_path)
    refresh(clean, tmp_path)
    planner = session_id(clean, "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
    assert (
        clean.execute(
            "SELECT count(*) FROM ah.session WHERE is_subagent AND root_session_id = %s", (planner,)
        ).fetchone()[0]
        == 1
    )
    assert run_state(clean)[0][1] == planner


def test_a_pi_root_that_writes_the_authority_file_in_codemode_starts_the_run(clean, tmp_path):
    records = [stamp(header(EXECUTOR), T0), stamp(message("ask", "user", "Plan and run."), T0)]
    records += codemode(
        "wr",
        T0 + timedelta(seconds=10),
        [
            ("write", {"path": AUTHORITY, "content": AUTHORITY_BODY}),
            ("write", {"path": AUTHORITY + ".bak", "content": "x"}),
        ],
    )
    write(tmp_path / "sessions" / "synthetic" / "executor.jsonl", records)
    refresh(clean, tmp_path)
    pi = session_id(clean, EXECUTOR)
    rows = clean.execute("SELECT root_session_id, protocol, goal_path, launch_ts FROM ah.loop_run").fetchall()
    assert rows == [(pi, "v5", AUTHORITY, T0 + timedelta(seconds=10))]
    assert (
        clean.execute("SELECT loop_run_id, loop_link_method FROM ah.session WHERE id = %s", (pi,)).fetchone()[1]
        == "launch"
    )


def test_lanes_that_arrive_after_the_handover_join_the_run(clean, tmp_path):
    run_root(tmp_path)
    executor(tmp_path, lanes=1)
    refresh(clean, tmp_path)
    ((run_id, pi, _, _),) = run_state(clean)
    assert pi == session_id(clean, EXECUTOR)
    stem = f"2026-10-04T09-10-00Z_{EXECUTOR}"
    lane_file(
        tmp_path,
        stem,
        "5f000000-0000-4000-8000-0000000000bb",
        "1a000000-0000-4000-8000-0000000000bb",
        T0 + timedelta(minutes=40),
    )
    refresh(clean, tmp_path)
    lanes = [r[0] for r in clean.execute("SELECT id FROM ah.session WHERE is_subagent AND root_session_id = %s", (pi,))]
    assert len(lanes) == 2
    assert sorted(
        r[0] for r in clean.execute("SELECT session_id FROM ah.lane WHERE loop_run_id = %s", (run_id,))
    ) == sorted(lanes)
    assert {r[0] for r in clean.execute("SELECT loop_run_id FROM ah.session WHERE id = ANY(%s)", (lanes,))} == {run_id}


def test_a_handed_over_run_does_not_keep_the_planners_next_run_as_its_end(clean, tmp_path):
    other = "run-20261123-2"
    run_root(
        tmp_path,
        tool_write(
            "authority-two",
            T0 + timedelta(minutes=1),
            AUTHORITY.replace(RUN, other),
            AUTHORITY_BODY.replace(RUN, other),
        ),
    )
    executor(tmp_path)
    refresh(clean, tmp_path)
    first = clean.execute(
        "SELECT root_session_id, end_evidence, end_ts FROM ah.loop_run WHERE goal_path = %s", (AUTHORITY,)
    ).fetchone()
    assert first[0] == session_id(clean, EXECUTOR)
    assert first[1] == "root_last_event"
    assert first[2] > T0 + timedelta(minutes=1)
    status = clean.execute(
        "SELECT l.status FROM ah.loops l JOIN ah.loop_run r USING (launch_uid) WHERE r.goal_path = %s", (AUTHORITY,)
    ).fetchone()
    assert status[0] != "finished"

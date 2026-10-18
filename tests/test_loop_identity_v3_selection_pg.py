"""Incremental v3 selection and SQL window bounds on a real disposable catalogue."""

import hashlib
import json
from datetime import timedelta

from agent_history import loop_identity_v3 as v3
from test_loop_identity_v3 import AT, CWD, HEADER
from test_loop_identity_v3_pg import clean, conn, pytestmark  # noqa: F401 - disposable fixtures and DSN guard


def history(db, total=130):
    root = db.execute(
        "INSERT INTO ah.session(agent,session_uid,cwd,git_remote_url,first_event_at,last_event_at) "
        "VALUES('pi','synthetic-bounded-root',%s,'https://example.org/example/project.git',%s,%s) RETURNING id",
        (CWD, AT, AT + timedelta(minutes=total)),
    ).fetchone()[0]
    for index in range(total):
        uid = f"bounded-{index:04}"
        started = AT + timedelta(minutes=index)
        closeout = CWD + f"/codex/closeout-bounded-{index}-loop14.md"
        charter = closeout.replace("closeout-", "charter-")
        db.execute(
            "INSERT INTO ah.loop_run(launch_uid,root_session_id,mode,loop_number,naming,goal_path,report_path,launch_ts,end_ts,end_evidence) "
            "VALUES(%s,%s,'v3',14,'loop',%s,%s,%s,%s,'next_launch')",
            (uid, root, charter, closeout, started, started + timedelta(seconds=50)),
        )
        db.execute(
            "INSERT INTO ah.loops(launch_uid,status,launch_ts,end_ts,observed_at) VALUES(%s,'finished',%s,%s,now())",
            (uid, started, started + timedelta(seconds=50)),
        )
        db.execute(
            "INSERT INTO ah.tool_call(agent,call_uid,session_id,tool_name,started_at,ended_at,outcome,source_id,byte_offset) "
            "VALUES('pi',%s,%s,'write',%s,%s,'ok',0,0)",
            (uid, root, started + timedelta(seconds=1), started + timedelta(seconds=2)),
        )
        db.execute(
            "INSERT INTO ah.tool_io(agent,io_uid,session_id,namespace,profile,kind,tool_name,call_uid,ts,input_text,source_id,byte_offset) "
            "VALUES('pi',%s,%s,'pi-test','test','call','write',%s,%s,%s,0,0)",
            (
                uid,
                root,
                uid,
                started + timedelta(seconds=1),
                json.dumps({"path": closeout, "content": HEADER + "\nbody\n"}),
            ),
        )
    db.execute("CREATE TEMP TABLE IF NOT EXISTS dirty_now(session_id bigint PRIMARY KEY) ON COMMIT DELETE ROWS")
    db.commit()
    return root


def test_historical_cohort_is_128_transactional_and_never_transfers_later_run_bodies(clean, monkeypatch):  # noqa: F811
    history(clean)
    parsed = []
    original = v3.structured_closeout

    def observe(*args, **kwargs):
        parsed.append(args[2])
        return original(*args, **kwargs)

    monkeypatch.setattr(v3, "structured_closeout", observe)
    first = v3.projections(clean)
    assert len(first) == 128
    assert len(parsed) == 128  # exactly one in-window call per selected run, not later runs' inputs
    clean.rollback()
    assert clean.execute("SELECT value FROM ah.meta WHERE key=%s", (v3.MARKER,)).fetchone() is None
    parsed.clear()
    retried = v3.projections(clean)
    assert retried.keys() == first.keys() and len(parsed) == 128
    clean.commit()
    second = v3.projections(clean)
    assert len(second) == 2 and not first.keys() & second.keys()
    clean.commit()
    assert v3.projections(clean) == {}
    clean.commit()
    assert v3.projections(clean) == {}  # unchanged closed history stays out after catch-up
    clean.commit()
    clean.execute("UPDATE ah.loops SET status='running',end_ts=NULL WHERE launch_uid='bounded-0129'")
    clean.commit()
    assert set(v3.projections(clean)) == {"bounded-0129"}


def test_metadata_changes_and_late_dirty_lane_reselect_closed_owner(clean):  # noqa: F811
    root = history(clean, total=2)
    v3.projections(clean)
    clean.commit()
    assert v3.projections(clean) == {}
    clean.commit()
    closeout = CWD + "/codex/closeout-bounded-0-loop14.md"
    clean.execute(
        "INSERT INTO ah.loop_run_metadata(machine,path,kind,sha256,line1,file_mtime,repo_origin) "
        "VALUES('machine-a',%s,'closeout',%s,%s,%s,'example/project')",
        (closeout, hashlib.sha256((HEADER + "\nbody\n").encode()).hexdigest(), HEADER, AT + timedelta(seconds=2)),
    )
    clean.commit()
    changed = v3.projections(clean)
    assert set(changed) == {"bounded-0000"} and changed["bounded-0000"].end is not None
    clean.commit()
    lane = clean.execute(
        "INSERT INTO ah.session(agent,session_uid,root_session_id,first_event_at) "
        "VALUES('pi','synthetic-bounded-lane',%s,%s) RETURNING id",
        (root, AT + timedelta(seconds=5)),
    ).fetchone()[0]
    clean.execute("INSERT INTO dirty_now VALUES(%s)", (lane,))
    changed = v3.projections(clean)
    assert changed["bounded-0000"].end is None


def test_precomputed_projection_is_shared_by_both_writers(clean, monkeypatch):  # noqa: F811
    history(clean, total=2)
    count = 0
    original = v3.projections

    def observe(db):
        nonlocal count
        count += 1
        return original(db)

    monkeypatch.setattr(v3, "projections", observe)
    projected = v3.projections(clean)
    v3.refresh_completion(clean, projected)
    v3.refresh_identity(clean, projected)
    assert count == 1


def test_edit_with_truncated_output_still_blocks_an_earlier_write(clean):  # noqa: F811
    root = history(clean, total=2)
    closeout = CWD + "/codex/closeout-bounded-0-loop14.md"
    clean.execute(
        "INSERT INTO ah.tool_call(agent,call_uid,session_id,tool_name,started_at,ended_at,outcome,source_id,byte_offset) "
        "VALUES('pi','bounded-edit',%s,'edit',%s,%s,'ok',0,0)",
        (root, AT + timedelta(seconds=3), AT + timedelta(seconds=4)),
    )
    clean.execute(
        "INSERT INTO ah.tool_io(agent,io_uid,session_id,namespace,profile,kind,tool_name,call_uid,ts,input_text,"
        "output_truncated,source_id,byte_offset) "
        "VALUES('pi','bounded-edit',%s,'pi-test','test','call','edit','bounded-edit',%s,%s,true,0,0)",
        (root, AT + timedelta(seconds=3), json.dumps({"path": closeout, "edits": []})),
    )
    clean.commit()
    assert v3.projections(clean)["bounded-0000"].end is None
    clean.commit()
    clean.execute(
        "INSERT INTO ah.loop_run_metadata(machine,path,kind,sha256,line1,file_mtime,repo_origin) "
        "VALUES('machine-a',%s,'closeout',%s,%s,%s,'example/project')",
        (closeout, "e" * 64, HEADER, AT + timedelta(seconds=5)),
    )
    clean.commit()
    assert v3.projections(clean)["bounded-0000"].end == AT + timedelta(seconds=4)

"""Observed v3 Task-trailer counts through the real catalogue post-pass."""

from datetime import timedelta

from agent_history import load
from test_loop_identity_pg import GOAL, index_launch
from test_loop_links_pg import ROOT, assistant, header, message, write
from test_loop_outcomes import AT
import test_loop_identity_pg as helpers

clean = helpers.clean
conn = helpers.conn
pytestmark = helpers.pytestmark


def setup_run(db, tmp_path, mode="v3"):
    if mode == "v3":
        source = tmp_path / "pi"
        activity = assistant("working", [{"type": "text", "text": "Working."}], stopReason="stop")
        activity["timestamp"] = activity["message"]["timestamp"] = (AT + timedelta(seconds=30)).isoformat()
        write(
            source / "sessions/synthetic/root.jsonl",
            [
                {**header(ROOT), "cwd": "/tmp/synthetic"},
                message("launch", "user", "You are the root. Write codex/closeout-synthetic-loop1.md."),
                activity,
            ],
        )
        assert load.refresh(db, sources={"pi-test": source}, textfile=None, log=lambda *_: None).errors == 0
        assert db.execute("SELECT mode FROM ah.loop_run").fetchone() == ("v3",)
        # Synthetic input at the metadata boundary. Real file/hash collection is
        # exercised separately by the genuine v3 integrated collector regression.
        db.execute(
            "INSERT INTO ah.loop_run_metadata(machine,path,kind,sha256,file_mtime,repo_origin) "
            "SELECT 'trailer-tests',goal_path,'charter',%s,%s,'example/project' FROM ah.loop_run "
            "ON CONFLICT(machine,kind,path) DO UPDATE SET sha256=EXCLUDED.sha256,"
            "file_mtime=EXCLUDED.file_mtime,repo_origin=EXCLUDED.repo_origin,seen_at=now()",
            (GOAL, AT - timedelta(seconds=60)),
        )
    else:
        index_launch(db, tmp_path, f"# Loop: example/project loop1 · Goal: {GOAL}")
    root, run, at = db.execute("SELECT root_session_id,id,launch_ts FROM ah.loop_run").fetchone()
    db.execute("TRUNCATE ah.git_commit, ah.git_commit_file")
    return root, run, at


def commit(db, sha, at, tasks, repo="machine/example/project", on_default=True):
    db.execute(
        "INSERT INTO ah.git_commit (repo_slug,sha,context,committed_at,author_is_owner,subject,parent_count,"
        "files_changed,insertions,deletions,on_default,task_ids) VALUES (%s,%s,'default',%s,true,'s',1,0,0,0,%s,%s)",
        (repo, sha * 40, at, on_default, tasks),
    )


def landed(db):
    load.post_passes(db)
    return db.execute("SELECT tasks_landed FROM ah.loops ORDER BY launch_ts").fetchone()[0]


def test_observed_repo_set_deduplicates_and_bounds_commit_evidence(clean, tmp_path):
    root, run, at = setup_run(clean, tmp_path)
    child = clean.execute(
        "INSERT INTO ah.session (agent,session_uid,root_session_id,loop_run_id) "
        "VALUES ('pi','synthetic-trailer-child',%s,%s) RETURNING id",
        (root, run),
    ).fetchone()[0]
    # Only an in-window captured event supplies the second repository, not a basename.
    for n, sid, offset, remote in [
        (1, child, 1, "https://example.com/example/secondary.git"),
        (2, root, -1, "https://example.com/example/unrelated.git"),
    ]:
        clean.execute(
            "INSERT INTO ah.git_event (agent,event_uid,session_id,ts,op,remote_url,evidence,source_id,byte_offset) "
            "VALUES ('pi',%s,%s,%s,'push',%s,'command',0,0)",
            (f"synthetic-remote-{n}", sid, at + timedelta(seconds=offset), remote),
        )
    commit(clean, "a", at, ["TASK-1001"])
    commit(clean, "b", at + timedelta(seconds=2), ["TASK-1001", "TASK-1002"], "other/example/secondary")
    commit(clean, "c", at + timedelta(seconds=2), ["TASK-9999"], "machine/example/unrelated")
    commit(clean, "d", at - timedelta(seconds=1), ["TASK-9998"])
    commit(clean, "e", at + timedelta(seconds=3), ["TASK-9997"], on_default=False)
    assert landed(clean) == 2
    # A finished boundary is exclusive for commits and remote observations alike.
    clean.execute("UPDATE ah.loop_run SET end_ts=%s,end_evidence='next_launch'", (at + timedelta(seconds=2),))
    assert landed(clean) == 1
    # A following launch of the same recorded run path also bounds a still-open row.
    clean.execute("UPDATE ah.loop_run SET end_ts=NULL,end_evidence='root_last_event'")
    clean.execute(
        "INSERT INTO ah.loop_run (launch_uid,root_session_id,launch_ts,report_path,mode) "
        "SELECT 'synthetic-following-launch',root_session_id,%s,report_path,'v3' FROM ah.loop_run",
        (at + timedelta(seconds=2),),
    )
    assert landed(clean) == 1


def test_unknown_empty_and_late_collector_evidence(clean, tmp_path):
    _, _, at = setup_run(clean, tmp_path)
    assert landed(clean) is None
    commit(clean, "a", at, [])
    assert landed(clean) == 0
    commit(clean, "b", at, ["TASK-1001"])
    assert landed(clean) == 1
    commit(clean, "c", at, None)
    assert landed(clean) is None
    clean.execute("UPDATE ah.git_commit SET task_ids=ARRAY['TASK-1002'] WHERE task_ids IS NULL")
    assert landed(clean) == 2
    clean.execute("UPDATE ah.loop_run SET mode='v2'")
    assert landed(clean) is None  # v2 remains state-only, never trailer-derived


def test_v3_state_outcomes_keep_precedence_over_positive_trailers(clean, tmp_path):
    from test_loop_outcomes import encoded, framed, outcome_events
    from test_loop_outcomes_pg import collect, surface

    _, _, at = setup_run(clean, tmp_path)
    commit(clean, "a", at, ["TASK-1001", "TASK-1002"])
    collect(clean, tmp_path, encoded(framed()))
    assert landed(clean) == 0  # a qualifying observed state open has a known zero
    collect(clean, tmp_path, encoded(outcome_events()))
    assert landed(clean) == 1
    assert surface(clean) == [("finished", 2, 1, 3, 1)]
    assert clean.execute("SELECT tasks_done FROM ah.loops").fetchone() == (1,)

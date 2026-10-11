"""Synthetic state collection through the real PostgreSQL loader boundary."""

from __future__ import annotations

import json
import subprocess
import time
from datetime import timedelta

import pytest

from agent_history import collect_git, collect_receipts, load
from test_loop_outcomes import AT, GOAL, encoded, framed, outcome_events
import test_loops_pg as helpers

clean = helpers.clean
conn = helpers.conn
pytestmark = helpers.pytestmark


def surface(db):
    return db.execute(
        "SELECT status,tasks_admitted,tasks_landed,parks_total,lanes_accepted FROM ah.loops ORDER BY launch_ts"
    ).fetchall()


def launch(db, tmp_path, *, second=False):
    source = tmp_path / "pi"
    path = source / "sessions" / "-synthetic-" / "synthetic.jsonl"
    path.parent.mkdir(parents=True)
    rows = [
        {
            "type": "session",
            "id": "synthetic-outcome-root",
            "version": 3,
            "timestamp": AT.isoformat(),
            "cwd": "/tmp/synthetic",
        }
    ]
    for n in range(2 if second else 1):
        at = AT + timedelta(seconds=n * 60)
        rows.append(
            {
                "type": "message",
                "id": f"launch-{n}",
                "timestamp": at.isoformat(),
                "message": {
                    "role": "user",
                    "content": "You are the root. Write codex/report-synthetic-loop1.md.\n"
                    f"# Loop: example/project loop1 · Goal: {GOAL}",
                },
            }
        )
    path.write_text(encoded(rows))
    sources = {"pi-test": source}
    assert load.refresh(db, sources=sources, textfile=None, log=lambda *_: None).errors == 0
    return sources


def collect(db, tmp_path, content, *, copies=1):
    repo = tmp_path / "reconciled"
    (repo / "codex").mkdir(parents=True, exist_ok=True)
    if not (repo / ".git").exists():
        subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True, timeout=10)
        subprocess.run(
            ["git", "-C", str(repo), "remote", "add", "origin", "https://example.invalid/example/project.git"],
            check=True,
            timeout=10,
        )
    (repo / "codex/state-synthetic-loop1.jsonl").write_text(content)
    for n in range(copies):
        collector = collect_git.Collector(db, False, time.monotonic() + 30)
        collect_receipts.collect(collector, [repo], f"synthetic-{n}")
        assert collector.errors == []
    db.commit()


def test_complete_outcomes_survive_live_phase_and_idle_refresh(clean, tmp_path):
    launch(clean, tmp_path)
    collect(clean, tmp_path, encoded(outcome_events()), copies=2)
    assert load.post_passes(clean)["dirty_sessions"] == 0
    assert surface(clean) == [("finished", 2, 1, 3, 1)]
    assert clean.execute("SELECT live_phase FROM ah.loops").fetchone() == ("closing",)
    assert clean.execute("SELECT repo,loop,goal_sha256 FROM ah.loops").fetchone() == ("example/project", "loop1", GOAL)
    clean.commit()
    # A collector-only pass after bounded live backfill completed must retain outcomes.
    assert load.post_passes(clean)["dirty_sessions"] == 0
    assert surface(clean) == [("finished", 2, 1, 3, 1)]
    clean.commit()
    load.apply_schema(clean, force=True)
    clean.commit()
    load.post_passes(clean)
    assert surface(clean) == [("finished", 2, 1, 3, 1)]


@pytest.mark.parametrize("case", ["absent", "open", "torn", "invalid", "wrong-goal", "ambiguous", "worker-land"])
def test_zero_absent_invalid_and_attribution(clean, tmp_path, case):
    launch(clean, tmp_path, second=case == "ambiguous")
    rows = framed()
    if case == "invalid":
        rows += [{"v": 1, "seq": 2, "ts": "2026-10-04T12:00:02Z", "by": "root", "ev": "close", "reason": "invalid"}]
    elif case == "wrong-goal":
        rows = outcome_events()
        rows[0]["goal_sha256"] = "f" * 64
    elif case == "worker-land":
        rows = framed(
            {"ev": "return", "lane": "one", "run": "one", "status": "complete", "landed": True},
            {"ev": "land", "by": "ext", "task": "EX-0001", "sha": "b" * 40, "gate": "green", "mode": "after-green"},
        )
    if case != "absent":
        collect(clean, tmp_path, encoded(rows) + ('{"ev":"close"\n' if case == "torn" else ""))
    load.post_passes(clean)
    values = (0, 0, 0, 0) if case in ("open", "torn", "worker-land") else (None,) * 4
    actual = surface(clean)
    assert all(row[1:] == values for row in actual)
    if case != "ambiguous":
        assert actual[0][0] == "stale"


def test_unresolved_accept_and_launch_cutoff(clean, tmp_path):
    launch(clean, tmp_path)
    rows = framed(
        {"ev": "accept", "task": "EX-0001", "accepted": True, "reason": "green"}, {"ev": "close", "reason": "blocked"}
    )
    collect(clean, tmp_path, encoded(rows))
    load.post_passes(clean)
    assert surface(clean) == [("finished", 0, 0, 0, None)]


def test_invalid_newer_machine_and_removed_state_retract_outcomes(clean, tmp_path):
    launch(clean, tmp_path)
    collect(clean, tmp_path, encoded(outcome_events()))
    load.post_passes(clean)
    assert surface(clean) == [("finished", 2, 1, 3, 1)]
    rows = outcome_events()
    rows[1].pop("accept")
    clean.execute(
        "INSERT INTO ah.loop_state (machine,path,loop,repo_origin,content,state_mtime) "
        "VALUES ('synthetic-invalid','/tmp/synthetic/state','loop1','example/project',%s,now())",
        (encoded(rows),),
    )
    clean.commit()
    load.post_passes(clean)
    assert surface(clean) == [("stale", None, None, None, None)]
    clean.execute("DELETE FROM ah.loop_state")
    clean.commit()
    load.post_passes(clean)
    assert surface(clean) == [("stale", None, None, None, None)]


def test_launch_window_excludes_later_close_and_lands(clean, tmp_path):
    launch(clean, tmp_path, second=True)
    rows = framed(
        {"ev": "land", "task": "EX-0001", "sha": "b" * 40, "gate": "green", "mode": "after-green"},
        {"ev": "close", "reason": "owner-stop"},
    )
    rows[0]["ts"] = (AT - timedelta(seconds=61)).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows[1]["ts"] = (AT + timedelta(seconds=60)).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows[2]["ts"] = (AT + timedelta(seconds=61)).strftime("%Y-%m-%dT%H:%M:%SZ")
    collect(clean, tmp_path, encoded(rows))
    load.post_passes(clean)
    assert surface(clean) == [("finished", 0, 0, 0, 0), ("stale", None, None, None, None)]


@pytest.mark.parametrize("case", ["close-return", "park-return", "accept-reject", "root-ext-close", "before-launch"])
@pytest.mark.parametrize("reverse", [False, True])
def test_complete_copied_event_conflicts_fail_closed_per_field(clean, tmp_path, case, reverse):
    launch(clean, tmp_path)
    returned = {"ev": "return", "lane": "one", "run": "run-one", "status": "complete"}
    if case == "close-return":
        left = framed({"ev": "close", "reason": "owner-stop"})
        right = framed(returned)
        expected = ("stale", 0, 0, 0, 0)
    elif case == "park-return":
        left = framed({"ev": "park", "task": "EX-0001", "reason": "decision", "needs": "owner"})
        right = framed(returned)
        expected = ("stale", 0, 0, None, 0)
    elif case == "accept-reject":
        left = framed({"ev": "accept", "task": "EX-0001", "lane": "one", "accepted": True, "reason": "green"})
        right = framed({"ev": "accept", "task": "EX-0001", "lane": "one", "accepted": False, "reason": "red"})
        expected = ("stale", 0, 0, 0, None)
    elif case == "root-ext-close":
        left = framed({"ev": "close", "reason": "owner-stop"})
        right = framed({"ev": "close", "by": "ext", "reason": "owner-stop"})
        expected = ("stale", 0, 0, 0, 0)
    else:
        left = framed(
            {"ev": "watch", "op": "start", "what": "worker", "deadline": "later"},
            {"ev": "close", "reason": "owner-stop"},
        )
        right = framed(returned, {"ev": "close", "reason": "owner-stop"})
        # Both divergent seq=2 observations precede launch and are not projected.
        # Their complete ledger still prevents seq=3 from proving a trustworthy end.
        for rows in (left, right):
            rows[0]["ts"] = (AT - timedelta(seconds=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
            rows[1]["ts"] = (AT - timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        expected = ("stale", 0, 0, 0, 0)
    if reverse:
        left, right = right, left
    collect(clean, tmp_path, encoded(left))
    clean.execute(
        "INSERT INTO ah.loop_state (machine,path,loop,repo_origin,content,state_mtime) "
        "VALUES ('synthetic-conflict','/tmp/synthetic/state','loop1','example/project',%s,now())",
        (encoded(right),),
    )
    clean.commit()
    load.post_passes(clean)
    assert surface(clean) == [expected]


@pytest.mark.parametrize("case", ["unknown", "invalid-dispatch"])
def test_invalid_complete_frame_cannot_prove_zero_close_or_acceptance(clean, tmp_path, case):
    launch(clean, tmp_path)
    invalid = {"ev": "unknown"} if case == "unknown" else {"ev": "dispatch", "task": "EX-0001", "lane": "one"}
    rows = framed(
        invalid,
        {"ev": "accept", "task": "EX-0001", "accepted": True, "reason": "green"},
        {"ev": "close", "reason": "owner-stop"},
    )
    collect(clean, tmp_path, encoded(rows))
    load.post_passes(clean)
    assert surface(clean) == [("stale", None, None, None, None)]


def test_missing_state_does_not_take_transcript_outcomes(clean, tmp_path):
    launch(clean, tmp_path)
    root = clean.execute("SELECT id FROM ah.session").fetchone()[0]
    command = "loop-state append codex/state-synthetic-loop1.jsonl admit task=EX-0001"
    clean.execute(
        "INSERT INTO ah.tool_call (agent,call_uid,session_id,tool_name,started_at,ended_at,outcome,source_id,byte_offset) "
        "VALUES ('pi','synthetic-append',%s,'bash',%s,%s,'ok',0,0)",
        (root, AT, AT),
    )
    clean.execute(
        "INSERT INTO ah.tool_io (agent,io_uid,session_id,namespace,profile,kind,tool_name,call_uid,ts,input_text,output_text,source_id,byte_offset) "
        "VALUES ('pi','synthetic-append',%s,'pi-test','test','call','bash','synthetic-append',%s,%s,'seq=1',0,0)",
        (root, AT, json.dumps({"command": command})),
    )
    clean.execute("INSERT INTO ah.dirty_session VALUES (%s) ON CONFLICT DO NOTHING", (root,))
    clean.commit()
    load.post_passes(clean)
    assert surface(clean) == [("stale", None, None, None, None)]

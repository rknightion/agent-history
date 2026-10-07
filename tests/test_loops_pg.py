"""Loop-linking integration tests against a disposable Postgres database.

Skipped unless AGENT_HISTORY_TEST_DSN points at a scratch database the tests may TRUNCATE.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

import pytest

DSN = os.environ.get("AGENT_HISTORY_TEST_DSN", "")
pytestmark = pytest.mark.skipif(
    not DSN or "agent_history_test" not in DSN, reason="AGENT_HISTORY_TEST_DSN (a *_test database) not set"
)

psycopg = pytest.importorskip("psycopg")

from agent_history import load, loop_live, loops  # noqa: E402


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
    conn.execute("DELETE FROM ah.loop_receipt")
    conn.execute("DELETE FROM ah.loop_state")
    conn.commit()
    yield conn
    conn.rollback()


@pytest.fixture
def launched(clean, tmp_path):
    """A real indexed launch, old enough to exercise silence without sleeping."""
    at = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
    source = tmp_path / "sessions"
    project = source / "projects" / "synthetic-project"
    project.mkdir(parents=True)
    path = project / "11111111-1111-4111-8111-111111111111.jsonl"
    path.write_text(
        json.dumps(
            {
                "type": "user",
                "uuid": "synthetic-launch",
                "sessionId": "synthetic-live-root",
                "timestamp": at,
                "cwd": "/tmp/synthetic-project",
                "message": {"role": "user", "content": "You are the root. Write codex/report-synthetic-loop1.md."},
            },
            separators=(",", ":"),
        )
        + "\n"
    )
    sources = {"claude-test": source}
    stats = load.refresh(clean, sources=sources, textfile=None, log=lambda *_: None)
    assert stats.errors == 0 and stats.files_parsed == 1
    clean.execute("UPDATE ah.session SET last_event_at = now()")
    clean.commit()
    load.post_passes(clean)
    clean.commit()
    return sources


@pytest.mark.parametrize(
    "case", ["heartbeat", "duplicate-copy", "older", "wrong-goal", "wrong-origin", "no-identity", "ambiguous-open"]
)
def test_authoritative_collected_root_heartbeat_phase(clean, tmp_path, case):
    """Index only a pi launch, collect state JSONL, then read the real live projection."""
    import subprocess
    import time

    from agent_history import collect_git, collect_receipts
    from test_loop_live_native import heartbeat_log

    started = datetime.now(timezone.utc) - timedelta(hours=2)
    activity = datetime.now(timezone.utc) - timedelta(minutes=1)
    goal = "a" * 64
    source = tmp_path / "pi"
    path = source / "sessions" / "-synthetic-" / "synthetic.jsonl"
    path.parent.mkdir(parents=True)
    records = [
        {
            "type": "session",
            "id": "synthetic-heartbeat-root",
            "version": 3,
            "timestamp": started.isoformat(),
            "cwd": "/tmp/synthetic",
        },
        {
            "type": "message",
            "id": "launch",
            "timestamp": started.isoformat(),
            "message": {
                "role": "user",
                "content": "You are the root. Write codex/report-synthetic-loop1.md."
                + ("" if case == "no-identity" else "\n# Loop: example/project loop1 · Goal: " + goal),
            },
        },
    ]
    if case == "ambiguous-open":
        records.append(
            {**records[1], "id": "second-launch", "timestamp": (started + timedelta(seconds=60)).isoformat()}
        )
    path.write_text("".join(json.dumps(row) + "\n" for row in records))
    sources = {"pi-test": source}
    assert load.refresh(clean, sources=sources, textfile=None, log=lambda *_: None).errors == 0
    assert clean.execute("SELECT live_phase FROM ah.loops").fetchall() == [(None,)] * (len(records) - 1)
    assert clean.execute("SELECT count(*) FROM ah.tool_call").fetchone() == (0,)
    assert clean.execute("SELECT count(*) FROM ah.subagent_spawn").fetchone() == (0,)
    clean.commit()

    repo = tmp_path / "reconciled"
    (repo / "codex").mkdir(parents=True)
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True, timeout=10)
    origin = "other/project" if case == "wrong-origin" else "example/project"
    subprocess.run(
        ["git", "-C", str(repo), "remote", "add", "origin", "https://example.invalid/" + origin + ".git"],
        check=True,
        timeout=10,
    )
    rows = [
        json.loads(line)
        for line in heartbeat_log(activity.isoformat().replace("+00:00", "Z"), heartbeat=case != "older").splitlines()
    ]
    rows[0]["ts"] = started.strftime("%Y-%m-%dT%H:%M:%SZ")
    if case == "wrong-goal":
        rows[0]["goal_sha256"] = "b" * 64
    if len(rows) > 1:
        # CLI framing is append time; activity's independent milliseconds must survive.
        rows[1]["ts"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    (repo / "codex/state-synthetic-loop1.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows))
    for machine in ["synthetic-one", "synthetic-two"] if case == "duplicate-copy" else ["synthetic-one"]:
        collector = collect_git.Collector(clean, False, time.monotonic() + 60)
        collect_receipts.collect(collector, [repo], machine)
        assert collector.errors == []
    clean.commit()
    assert load.post_passes(clean)["dirty_sessions"] == 0
    expected = "preparing" if case in ("heartbeat", "duplicate-copy") else None
    assert clean.execute("SELECT live_phase FROM ah.loops ORDER BY launch_ts").fetchall() == [(expected,)] * (
        len(records) - 1
    )
    target = clean.execute(
        "SELECT id,root_session_id,launch_ts,NULL,report_path FROM ah.loop_run ORDER BY launch_ts LIMIT 1"
    ).fetchone()
    events = loop_live._events(clean, *target)
    if expected:
        assert [(e["ev"], e["at"]) for e in events] == [("heartbeat", activity)]
        # Collector state does not manufacture open/dispatch, terminal status or root activity.
        assert clean.execute("SELECT active_lanes,tasks_admitted,status FROM ah.loops").fetchone() == (
            None,
            None,
            "running",
        )
        assert clean.execute("SELECT last_event_at FROM ah.session").fetchone() == (started,)
    else:
        assert events == []
    clean.commit()
    assert load.rebuild(clean, sources=sources, textfile=None, log=lambda *_: None).errors == 0
    assert clean.execute("SELECT live_phase FROM ah.loops ORDER BY launch_ts").fetchall() == [(expected,)] * (
        len(records) - 1
    )


def test_live_loop_report_write_is_not_terminal_but_next_launch_is(clean, launched):
    assert clean.execute("SELECT status, end_ts FROM ah.loops").fetchone() == ("running", None)
    root_id = clean.execute("SELECT id FROM ah.session").fetchone()[0]
    clean.execute(
        "INSERT INTO ah.artifact (agent, event_uid, session_id, ts, kind, action, path, "
        "evidence_type, source_id, byte_offset) VALUES ('claude', 'synthetic-report', %s, now(), "
        "'file', 'write', '/tmp/synthetic-project/codex/report-synthetic-loop1.md', 'tool', 0, 0)",
        (root_id,),
    )
    clean.execute("INSERT INTO ah.dirty_session (session_id) VALUES (%s) ON CONFLICT DO NOTHING", (root_id,))
    clean.commit()
    load.post_passes(clean)
    row = clean.execute("SELECT status, end_ts FROM ah.loops").fetchone()
    # Writing the report is not a delivered completion notification; only a receipt finishes it.
    assert row == ("running", None)
    # Supersession is terminal evidence, not an inferred stale failure.
    clean.execute("UPDATE ah.loop_run SET end_evidence = 'next_launch'")
    clean.commit()
    load.post_passes(clean)
    assert clean.execute("SELECT status FROM ah.loops").fetchone() == ("finished",)


def test_silent_loop_expires_without_dirty_sessions_and_can_resume(clean, launched):
    assert clean.execute("SELECT status, end_ts FROM ah.loops").fetchone() == ("running", None)
    clean.execute("UPDATE ah.session SET last_event_at = now() - interval '25 hours'")
    clean.commit()
    assert load.post_passes(clean)["dirty_sessions"] == 0
    row = clean.execute("SELECT status, end_ts FROM ah.loops").fetchone()
    assert row[0] == "stale" and row[1] is not None
    clean.execute("UPDATE ah.session SET last_event_at = now()")
    clean.commit()
    load.post_passes(clean)
    assert clean.execute("SELECT status, end_ts FROM ah.loops").fetchone() == ("running", None)


def test_analytics_reapply_and_rebuild_preserve_live_relation_and_grant(clean, launched):
    # Grant as the owner, to an absent-by-default consumer role in the disposable database.
    with psycopg.connect(os.environ["AGENT_HISTORY_TEST_ADMIN_DSN"]) as admin:
        admin.execute("CREATE ROLE synthetic_loop_reader")
    try:
        clean.execute("GRANT SELECT ON ah.loops TO synthetic_loop_reader")
        clean.commit()
        before = clean.execute("SELECT launch_uid FROM ah.loops").fetchall()
        clean.commit()
        load.apply_schema(clean, force=True)
        clean.commit()
        assert clean.execute("SELECT launch_uid FROM ah.loops").fetchall() == before
        clean.commit()
        assert load.rebuild(clean, sources=launched, textfile=None, log=lambda *_: None).errors == 0
        assert clean.execute("SELECT launch_uid, status FROM ah.loops").fetchall() == [(before[0][0], "stale")]
        assert clean.execute(
            "SELECT has_table_privilege('synthetic_loop_reader', 'ah.loops', 'SELECT')"
        ).fetchone() == (True,)
    finally:
        clean.rollback()
        with psycopg.connect(os.environ["AGENT_HISTORY_TEST_ADMIN_DSN"]) as admin:
            admin.execute("DROP OWNED BY synthetic_loop_reader")
            admin.execute("DROP ROLE synthetic_loop_reader")


@pytest.mark.parametrize(
    "terminal",
    [None, "completion_receipt", "next_launch", "wrong-goal", "before-launch", "before-open", "wrong-origin"],
)
def test_state_close_is_analytical_end_only(clean, tmp_path, terminal):
    """Collected close beats root activity, but never changes receiver lifecycle semantics."""
    import subprocess
    import time

    from agent_history import collect_git, collect_receipts

    at = (datetime.now(timezone.utc) - timedelta(minutes=10)).replace(microsecond=0)
    closed = at + timedelta(minutes=2)
    last = at + timedelta(minutes=5)
    goal = "a" * 64
    source = tmp_path / "sessions"
    project = source / "projects" / "synthetic-project"
    project.mkdir(parents=True)
    path = project / "11111111-1111-4111-8111-111111111111.jsonl"
    launch = {
        "type": "user",
        "uuid": "synthetic-launch",
        "sessionId": "synthetic-close-root",
        "timestamp": at.isoformat(),
        "cwd": "/tmp/synthetic-project",
        "message": {
            "role": "user",
            "content": "You are the root. Write codex/report-synthetic-loop1.md.\n"
            + "# Loop: example/project loop1 · Goal: "
            + goal,
        },
    }
    activity = {
        **launch,
        "uuid": "synthetic-activity",
        "timestamp": last.isoformat(),
        "message": {"role": "user", "content": "Continue the audit."},
    }
    if terminal == "next_launch":
        activity["message"] = {"role": "user", "content": "You are the root. Write codex/report-synthetic-loop2.md."}
    path.write_text(json.dumps(launch) + "\n" + json.dumps(activity) + "\n")
    sources = {"claude-test": source}
    assert load.refresh(clean, sources=sources, textfile=None, log=lambda *_: None).errors == 0
    repo = tmp_path / "reconciled"
    (repo / "codex").mkdir(parents=True)
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "remote", "add", "origin", "https://example.invalid/example/project.git"], check=True
    )
    events = [
        {
            "v": 1,
            "seq": 1,
            "ts": at.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "by": "root",
            "ev": "open",
            "goal_sha256": goal,
            "tier": "routine",
            "root": "llm",
            "root_model": "synthetic",
            "envelope": [],
        },
        {"v": 1, "seq": 2, "ts": closed.strftime("%Y-%m-%dT%H:%M:%SZ"), "by": "root", "ev": "close"},
    ]
    if terminal == "wrong-goal":
        events[0]["goal_sha256"] = "e" * 64
    elif terminal == "before-launch":
        events[0]["ts"] = (at - timedelta(seconds=60)).strftime("%Y-%m-%dT%H:%M:%SZ")
        events[1]["ts"] = (at - timedelta(seconds=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    elif terminal == "before-open":
        events.reverse()
        for seq, event in enumerate(events, 1):
            event["seq"] = seq
    elif terminal == "wrong-origin":
        subprocess.run(
            ["git", "-C", str(repo), "remote", "set-url", "origin", "https://example.invalid/example/other.git"],
            check=True,
        )
    state = repo / "codex/state-synthetic-loop1.jsonl"
    state.write_text("".join(json.dumps(event) + "\n" for event in events))
    collector = collect_git.Collector(clean, False, time.monotonic() + 60)
    collect_receipts.collect(collector, [repo], "synthetic-one")
    assert collector.errors == []
    receipt_at = closed + timedelta(seconds=30)
    if terminal == "completion_receipt":
        clean.execute(
            "INSERT INTO ah.loop_receipt (machine, kind, path, content, receipt_mtime, target_exists) "
            "VALUES ('synthetic-one', 'notified', '/tmp/synthetic-project/codex/report-synthetic-loop1.md', "
            "'request synthetic', %s, true)",
            (receipt_at,),
        )
    clean.commit()
    assert load.post_passes(clean)["dirty_sessions"] == 0
    expected = {
        None: (closed, "state_close"),
        "completion_receipt": (receipt_at, "completion_receipt"),
        "next_launch": (last, "next_launch"),
        "wrong-goal": (last, "root_last_event"),
        "before-launch": (last, "root_last_event"),
        "before-open": (last, "root_last_event"),
        "wrong-origin": (last, "root_last_event"),
    }[terminal]
    assert clean.execute("SELECT end_ts, end_evidence FROM ah.loop_run WHERE loop_number = 1").fetchone() == expected
    lifecycle = ("finished", expected[0]) if terminal in ("completion_receipt", "next_launch") else ("running", None)
    assert clean.execute("SELECT status, end_ts FROM ah.loops WHERE loop = 'loop1'").fetchone() == lifecycle
    clean.commit()
    # Retagging/rebuild must reapply the same collected evidence without finishing the receiver.
    assert load.rebuild(clean, sources=sources, textfile=None, log=lambda *_: None).errors == 0
    assert clean.execute("SELECT end_ts, end_evidence FROM ah.loop_run WHERE loop_number = 1").fetchone() == expected
    assert clean.execute("SELECT status, end_ts FROM ah.loops WHERE loop = 'loop1'").fetchone() == lifecycle


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


@pytest.mark.parametrize(
    ("case", "expected"),
    [
        ("explicit", (2, 2)),
        ("unique", (2, 2)),
        ("mixed", (None, 2)),
        ("missing", (None, 2)),
        ("future", (None, 2)),
        ("different-task", (None, 2)),
        ("explicit-overrides", (2, 2)),
        ("conflicting-copy", (None, 2)),
        ("no-accept", (0, 2)),
        ("overlapping-launches", (None, None)),
    ],
)
def test_collected_state_counts_distinct_accepted_lanes_and_landed_tasks(clean, tmp_path, case, expected):
    """Exercise the real collector/refresh seam, including reconciled paths and optional lanes."""
    import subprocess
    import time

    from agent_history import collect_git, collect_receipts

    at = datetime.now(timezone.utc) - timedelta(minutes=10)
    goal = "a" * 64
    source = tmp_path / "sessions"
    project = source / "projects" / "synthetic-project"
    project.mkdir(parents=True)
    path = project / "11111111-1111-4111-8111-111111111111.jsonl"
    path.write_text(
        json.dumps(
            {
                "type": "user",
                "uuid": "synthetic-launch",
                "sessionId": "synthetic-count-root",
                "timestamp": at.isoformat(),
                "cwd": "/tmp/synthetic-project",
                "message": {
                    "role": "user",
                    "content": "You are the root. Write codex/report-synthetic-loop1.md.\n"
                    + "# Loop: example/project loop1 · Goal: "
                    + goal,
                },
            }
        )
        + "\n"
    )
    if case == "overlapping-launches":
        second = {
            **json.loads(path.read_text()),
            "uuid": "synthetic-second-launch",
            "timestamp": (at + timedelta(seconds=60)).isoformat(),
        }
        with path.open("a") as handle:
            handle.write(json.dumps(second) + "\n")
    copies = 2 if case == "overlapping-launches" else 1
    sources = {"claude-test": source}
    assert load.refresh(clean, sources=sources, textfile=None, log=lambda *_: None).errors == 0
    repo = tmp_path / "reconciled"
    (repo / "codex").mkdir(parents=True)
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "remote", "add", "origin", "https://example.invalid/example/project.git"], check=True
    )
    events = [
        {
            "ev": "open",
            "goal_sha256": goal,
            "tier": "guarded",
            "root": "llm",
            "root_model": "synthetic",
            "envelope": ["TASK-1", "TASK-2"],
        },
        {"ev": "accept", "task": "TASK-1", "lane": "one", "accepted": True, "reason": "gate green"},
        {"ev": "accept", "task": "TASK-1", "lane": "one", "accepted": True, "reason": "duplicate"},
        {"ev": "accept", "task": "TASK-2", "lane": "two", "accepted": False, "reason": "gate red"},
        {"ev": "accept", "task": "TASK-2", "lane": "three", "accepted": True, "reason": "gate green"},
        {"ev": "accept", "task": "TASK-3", "accepted": True, "reason": "no lane recorded"},
        {"ev": "accept", "task": "TASK-1", "lane": "one", "accepted": False, "reason": "later rejection"},
        {"ev": "return", "lane": "four", "run": "synthetic-run", "status": "complete", "landed": True},
        {"ev": "land", "task": "TASK-1", "sha": "b" * 40, "gate": "green", "mode": "after-green"},
        {"ev": "land", "task": "TASK-1", "sha": "c" * 40, "gate": "green", "mode": "after-green"},
        {"ev": "land", "task": "TASK-2", "sha": "d" * 40, "gate": "green", "mode": "after-green"},
    ]
    if case in ("explicit", "explicit-overrides"):
        events[5]["lane"] = "three"
    elif case in ("unique", "conflicting-copy", "future", "different-task"):
        for event in events:
            if event["ev"] == "accept":
                event.pop("lane", None)
    elif case == "no-accept":
        for event in events:
            if event["ev"] == "accept":
                event["accepted"] = False

    dispatches = [
        {"ev": "dispatch", "task": "TASK-1", "lane": "one"},
        {"ev": "dispatch", "task": "TASK-1", "lane": "one"},  # retry/copy is still one lane
        {"ev": "dispatch", "task": "TASK-2", "lane": "three"},
        {"ev": "dispatch", "task": "TASK-3", "lane": "three"},
    ]
    if case == "future":
        events.append(dispatches.pop())  # a later dispatch cannot identify an earlier accept
    elif case == "different-task":
        dispatches[-1]["task"] = "TASK-3-other"  # exact task keys, not aliases
    elif case in ("mixed", "explicit-overrides"):
        dispatches.append({"ev": "dispatch", "task": "TASK-3", "lane": "reviewer"})
        dispatches.append({"ev": "return", "lane": "reviewer", "status": "complete"})
    if case != "missing":
        events[1:1] = dispatches
    framed = [
        {"v": 1, "seq": n, "ts": (at + timedelta(seconds=n)).strftime("%Y-%m-%dT%H:%M:%SZ"), "by": "root", **event}
        for n, event in enumerate(events, 1)
    ]
    state = repo / "codex/state-synthetic-loop1.jsonl"
    state.write_text(json.dumps(framed[0]) + "\n")
    collector = collect_git.Collector(clean, False, time.monotonic() + 60)
    collect_receipts.collect(collector, [repo], "synthetic-one")
    assert collector.errors == []
    clean.commit()
    load.post_passes(clean)
    initial = (None, None) if case == "overlapping-launches" else (0, 0)
    assert (
        clean.execute("SELECT lanes_accepted, tasks_done FROM ah.loops ORDER BY launch_ts").fetchall()
        == [initial] * copies
    )
    clean.commit()
    state.write_text("".join(json.dumps(event) + "\n" for event in framed))
    # Same label and origin but another explicit goal cannot contaminate this loop's counts.
    unrelated = [dict(event) for event in framed]
    unrelated[0]["goal_sha256"] = "e" * 64
    unrelated[1]["lane"] = "unrelated"
    unrelated[-1]["task"] = "TASK-UNRELATED"
    (repo / "codex/state-unrelated-loop1.jsonl").write_text("".join(json.dumps(event) + "\n" for event in unrelated))
    for machine in ("synthetic-one", "synthetic-two"):
        if case == "conflicting-copy" and machine == "synthetic-two":
            conflicting = [dict(event) for event in framed]
            conflicting[1]["lane"] = "other"
            state.write_text("".join(json.dumps(event) + "\n" for event in conflicting))
        collector = collect_git.Collector(clean, False, time.monotonic() + 60)
        collect_receipts.collect(collector, [repo], machine)
        assert collector.errors == []
    clean.commit()
    assert load.post_passes(clean)["dirty_sessions"] == 0
    assert (
        clean.execute("SELECT lanes_accepted, tasks_done FROM ah.loops ORDER BY launch_ts").fetchall()
        == [expected] * copies
    )
    clean.commit()
    # Collector evidence must survive a content rebuild, and no duplicate machine inflates it.
    assert load.rebuild(clean, sources=sources, textfile=None, log=lambda *_: None).errors == 0
    assert (
        clean.execute("SELECT lanes_accepted, tasks_done FROM ah.loops ORDER BY launch_ts").fetchall()
        == [expected] * copies
    )
    clean.execute("DELETE FROM ah.loop_state")
    clean.commit()

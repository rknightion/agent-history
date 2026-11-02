"""A no-change refresh leaves ah.loops rows alone, apart from the contract's once-per-refresh stamp.

`observed_at` is stamped once per row per refresh by the lifecycle upsert (CONTRACT.md, "Current
lifecycle status"). Every other statement that writes ah.loops must write only when a projected value
actually differs, so a quiet catalogue does not pay a row version per loop per pass for rewriting values
it already holds.

The fixture is one catalogue holding the four shapes that write ah.loops from several places:

* a loop closed by its attributed state log, which also has state cohorts for the outcome counts
* a v3 loop finished by its closeout, where the closeout counts and the Done-flip counts disagree
* a v3 loop whose last root line is a future wait
* an active loop with a linked lane and model usage

A test-only AFTER UPDATE row trigger (created and dropped here, never in a migration) logs the launch_uid of
every row version written, so the count is exact per launch rather than inferred from table statistics.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from psycopg.types.json import Jsonb

from agent_history import load
from test_loop_closeout_counts_pg import DSN, SLUG, observe, wipe
from test_loop_identity_pg import GOAL, conn  # noqa: F401 - disposable catalogue fixture
from test_loop_identity_v3 import AT
from test_loop_launch_v3 import PROMPT as V3_PROMPT
from test_loop_launch_v3_pg import transcript
from test_loop_outcomes import encoded, outcome_events
from test_loop_outcomes_pg import collect, launch
from test_loop_v3_status_pg import stamp

pytestmark = pytest.mark.skipif(not DSN or "agent_history_test" not in DSN, reason="scratch database not set")

CURSORS = ("loops_progress_projection_v1", "loops_live_projection_v1")
WAIT_ROOT = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
LANE_ROOT = "22222222-2222-4222-8222-222222222222"
STATE_NAME = "report-synthetic-loop1.md"
CLOSEOUT_NAME = "closeout-synthetic-loop14.md"
WAIT_NAME = "closeout-synthetic-loop15.md"
ACTIVE_NAME = "report-synthetic-loop2.md"
# Captured from the code before the guards existed (the same fixture, three passes after the cursors completed).
EXPECTED = {
    "closeout-synthetic-loop14.md": {
        "end_ts": datetime(2026, 10, 4, 12, 1, tzinfo=UTC),
        "goal_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "last_activity_at": datetime(2026, 10, 4, 12, 0, tzinfo=UTC),
        "launch_ts": datetime(2026, 10, 4, 12, 0, tzinfo=UTC),
        "launch_uid": "claude/aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa/:launch",
        "live_phase": "closing",
        "loop": "loop14",
        "parks_total": 0,
        "phase_since": "<now>",
        "repo": "example/project",
        "root_agent": "claude",
        "status": "finished",
        "tasks_done": 7,
        "tasks_landed": 7,
    },
    "closeout-synthetic-loop15.md": {
        "api_errors": 0,
        "cache_read": 0,
        "cache_write": 0,
        "input_uncached": 1,
        "last_activity_at": "<ts>",
        "launch_ts": "<ts>",
        "launch_uid": "pi/dddddddd-dddd-4ddd-8ddd-dddddddddddd/:dddddddd-dddd-4ddd-8ddd-dddddddddddd:launch",
        "llm_calls": 1,
        "loop": "loop15",
        "output": 1,
        "root_agent": "pi",
        "status": "waiting",
        "waiting_until": "<ts>",
    },
    "report-synthetic-loop1.md": {
        "end_ts": datetime(2026, 10, 4, 12, 0, 17, tzinfo=UTC),
        "goal_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "lanes_accepted": 1,
        "last_activity_at": datetime(2026, 10, 4, 12, 0, tzinfo=UTC),
        "launch_ts": datetime(2026, 10, 4, 12, 0, tzinfo=UTC),
        "launch_uid": "pi/synthetic-outcome-root/:synthetic-outcome-root:launch-0",
        "live_phase": "closing",
        "loop": "loop1",
        "parks_total": 3,
        "phase_since": "<now>",
        "repo": "example/project",
        "root_agent": "pi",
        "status": "finished",
        "tasks_admitted": 2,
        "tasks_done": 1,
        "tasks_landed": 1,
    },
    "report-synthetic-loop2.md": {
        "api_errors": 0,
        "cache_read": 24,
        "cache_write": 54,
        "final_summary": False,
        "goal_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "headline": "Synthetic progress",
        "input_uncached": 22,
        "lanes_total": 1,
        "last_activity_at": "<ts>",
        "launch_ts": "<ts>",
        "launch_uid": "claude/22222222-2222-4222-8222-222222222222/:synthetic-launch",
        "llm_calls": 2,
        "loop": "loop2",
        "output": 30,
        "repo": "example/project",
        "root_agent": "claude",
        "status": "running",
        "summary": "The loop is working. One lane is attached.",
        "summary_generated_at": "<ts>",
        "summary_model": "synthetic-model",
        "tasks_done": 0,
    },
}

# Flip-flops the guards deliberately leave alone: two statements assign different values to the same
# columns of the same row on every pass, and neither can be dropped without restructuring the write order.
# Each entry is the evidence that puts a row in the class and the columns the class may rewrite.
STATE_CLOSE = {"status", "end_ts"}  # upsert writes running/stale, the state outcome pass writes finished
WAITING = {"status"}  # upsert writes running/stale, the v3 status pass writes waiting
# The delivery counts have one writer per source and the last one wins: the Done-flip count, the state
# outcome reset, the Task trailer count, the state close and the closeout count.
COUNT_CHAIN = {"tasks_done", "tasks_landed", "parks_total"}
UPSERT = {"status", "end_ts", "observed_at"}  # what the lifecycle upsert may change on a no-change pass
CHAIN_WRITERS = {"closeout": 4}  # writers that disagree for a row of that end evidence


def reset(db):
    db.execute("TRUNCATE " + ", ".join(f"ah.{table}" for table in load.DATA_TABLES) + " RESTART IDENTITY CASCADE")
    wipe(db)
    for table in ("loop_state", "loop_live_cache", "loop_live_job"):
        db.execute(f"DELETE FROM ah.{table}")
    db.execute("DELETE FROM ah.meta WHERE key LIKE 'loops\\_%'")
    db.commit()


@pytest.fixture
def clean(conn):  # noqa: F811
    reset(conn)
    yield conn
    conn.rollback()
    reset(conn)


def waiting_root(db, source):
    at = db.execute("SELECT now() - interval '1 minute'").fetchone()[0]
    until = db.execute("SELECT now() + interval '1 hour'").fetchone()[0]
    cwd = "/tmp/synthetic-project-wait"
    prompt = V3_PROMPT.replace("loop14", "loop15")
    records = [
        {"type": "session", "id": WAIT_ROOT, "cwd": cwd, "version": 3},
        {"type": "message", "id": "launch", "message": {"role": "user", "content": prompt}},
        {
            "type": "message",
            "id": "status",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "Progress.\nWAITING: review until " + until.isoformat()}],
                "model": "test",
                "responseId": "status",
                "usage": {"input": 1, "output": 1, "cacheRead": 0, "cacheWrite": 0},
                "stopReason": "stop",
            },
        },
    ]
    path = source / "sessions" / "synthetic-wait" / "root.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(
        "".join(json.dumps(stamp(record, at + timedelta(seconds=n))) + "\n" for n, record in enumerate(records))
    )


def active_root(source, now):
    project = source / "projects" / "synthetic-active"
    project.mkdir(parents=True)
    text = f"You are the root. Write codex/report-synthetic-loop2.md.\n# Loop: example/project loop2 · Goal: {GOAL}"
    record = {
        "type": "user",
        "uuid": "synthetic-launch",
        "sessionId": "synthetic-guards-active-root",
        "timestamp": now.isoformat(),
        "cwd": "/tmp/synthetic-project-active",
        "message": {"role": "user", "content": text},
    }
    (project / f"{LANE_ROOT}.jsonl").write_text(json.dumps(record) + "\n")


def add_lane(db):
    root, loop_id = db.execute(
        "SELECT r.root_session_id, r.id FROM ah.loop_run r JOIN ah.session s ON s.id = r.root_session_id "
        "WHERE s.session_uid = %s",
        (LANE_ROOT,),
    ).fetchone()
    child = db.execute(
        "INSERT INTO ah.session (agent,session_uid,is_stub,root_session_id,loop_run_id,first_event_at,"
        "last_event_at) VALUES ('claude','synthetic-guards-child',false,%s,%s,now(),now() + interval '1 minute') "
        "RETURNING id",
        (root, loop_id),
    ).fetchone()[0]
    db.execute("INSERT INTO ah.lane (loop_run_id,session_id,link_method) VALUES (%s,%s,'lineage')", (loop_id, child))
    for sid, uid in ((root, "synthetic-root-usage"), (child, "synthetic-child-usage")):
        db.execute(
            "INSERT INTO ah.llm_call (agent,response_id,session_id,ts,model,input_uncached,cache_read,"
            "cache_write_5m,cache_write_1h,output,source_id,byte_offset) "
            "VALUES ('claude',%s,%s,now(),'synthetic-unpriced',11,12,13,14,15,0,0)",
            (uid, sid),
        )
    db.execute("INSERT INTO ah.dirty_session (session_id) VALUES (%s)", (child,))
    # A cached summary for the active loop, so the headline and summary projection has a value to hold.
    reply = {"headline": "Synthetic progress", "summary": "The loop is working. One lane is attached."}
    db.execute(
        "INSERT INTO ah.loop_live_cache(launch_uid,digest_sha256,kind,response,model,final_summary,requested_at) "
        "SELECT launch_uid,%s,'summary',%s,'synthetic-model',false,now() FROM ah.loop_run WHERE id=%s",
        ("e" * 64, Jsonb(summary_reply(reply)), loop_id),
    )


def summary_reply(content):
    return {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(content)}}]}


def done_flips(db, *, state_loop_agrees):
    """Three Done flips and a Task trailer commit. The closeout run's window is a minute, the state run's 17s.

    When `state_loop_agrees` only one flip falls in the state-closed run's window, which is the number its
    state log publishes, so the Done-flip count and the state close count do not compete for that run.
    """
    offsets = (30, 31, 5) if state_loop_agrees else (1, 2, 3)
    db.execute("INSERT INTO ah.backlog_done_scan (repo_slug,widened_at) VALUES (%s,now())", (SLUG,))
    for n, (task, offset) in enumerate(zip(("ABC-0001", "ABC-0002", "ABC-0003"), offsets)):
        db.execute(
            "INSERT INTO ah.backlog_done_event (repo_slug,task_key,sha,done_at,from_status) "
            "VALUES (%s,%s,%s,%s,'To Do')",
            (SLUG, task, str(n) * 40, AT + timedelta(seconds=offset)),
        )
    db.execute(
        "INSERT INTO ah.git_commit (repo_slug,sha,context,committed_at,author_is_owner,subject,parent_count,"
        "files_changed,insertions,deletions,on_default,task_ids) VALUES (%s,%s,'default',%s,true,'s',1,0,0,0,true,%s)",
        ("machine/example/project", "c" * 40, AT + timedelta(seconds=40), ["ABC-0001", "ABC-0002"]),
    )


def build(db, tmp_path, *, state_loop_agrees=False):
    """Index all four shapes, then run passes until both bounded backfill cursors read complete."""
    now = db.execute("SELECT now()").fetchone()[0]
    launch(db, tmp_path / "state", second=False)  # state-closed pi loop (loop1)
    collect(db, tmp_path / "state", encoded(outcome_events()), copies=2)
    claude = tmp_path / "closeout"
    transcript(claude, "claude")  # v3 launch, loop14
    pi = tmp_path / "wait"
    waiting_root(db, pi)
    active = tmp_path / "active"
    active_root(active, now)
    sources = {
        "pi-test": tmp_path / "state",
        "claude-test": claude,
        "pi-wait-test": pi,
        "claude-active-test": active,
    }
    assert load.refresh(db, sources=sources, textfile=None, log=lambda *_: None).errors == 0
    db.commit()
    done_flips(db, state_loop_agrees=state_loop_agrees)
    observe(db, 7, 0)  # the closeout says 7 landed, disagreeing with the Done flips
    add_lane(db)
    db.commit()
    for _ in range(6):
        load.post_passes(db)
        db.commit()
        done = db.execute("SELECT key, value FROM ah.meta WHERE key = ANY(%s)", (list(CURSORS),)).fetchall()
        if len(done) == 2 and {value for _, value in done} == {"complete"}:
            break
    else:
        raise AssertionError("bounded backfill cursors did not complete")
    if state_loop_agrees:
        # The Done-flip pass runs before the state close and sees the lifecycle upsert's end (the root's last
        # activity), so give the state-closed root activity up to the 20th second: one flip is in that window.
        db.execute(
            "UPDATE ah.session SET last_event_at = %s WHERE session_uid = 'synthetic-outcome-root'",
            (AT + timedelta(seconds=20),),
        )
        db.commit()
        for _ in range(3):
            load.post_passes(db)
            db.commit()


def names(db):
    return {
        uid: name
        for uid, name in db.execute(
            "SELECT l.launch_uid, regexp_replace(r.report_path, '^.*/', '') FROM ah.loops l "
            "JOIN ah.loop_run r USING (launch_uid)"
        )
    }


def snapshot(db):
    """Every projected column of every loop, with wall-clock readings reduced to a marker."""
    cursor = db.execute(
        "SELECT regexp_replace(r.report_path, '^.*/', '') AS name, l.* FROM ah.loops l "
        "JOIN ah.loop_run r USING (launch_uid) ORDER BY 1"
    )
    columns = [column.name for column in cursor.description]
    result = {}
    for row in cursor.fetchall():
        values = dict(zip(columns, row))
        name = values.pop("name")
        values.pop("observed_at")  # stamped once per refresh by contract
        relative = name in (WAIT_NAME, ACTIVE_NAME)
        values = {
            key: "<now>"
            if key == "phase_since" and value is not None
            else "<ts>"
            if relative and isinstance(value, datetime)
            else value
            for key, value in values.items()
        }
        # Only the columns that hold a value: the rest stay NULL, which the equality below also pins.
        result[name] = {key: value for key, value in values.items() if value is not None}
    return result


def count_updates(db):
    """Run one more refresh with no new evidence; return the columns each write changed.

    Two dicts keyed by launch_uid: the ah.loops writes in order, and the ah.loop_run writes in order.
    """
    db.execute(
        "CREATE TABLE ah.guard_update_log (n bigserial, tbl text NOT NULL, launch_uid text NOT NULL, "
        "changed text[] NOT NULL)"
    )
    db.execute(
        "CREATE FUNCTION ah.guard_update_log_fn() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN "
        "INSERT INTO ah.guard_update_log(tbl, launch_uid, changed) SELECT TG_TABLE_NAME, NEW.launch_uid, "
        "coalesce(array_agg(k), ARRAY[]::text[]) FROM jsonb_each(to_jsonb(NEW)) e(k, v) "
        "WHERE to_jsonb(OLD) -> k IS DISTINCT FROM v; RETURN NULL; END $$"
    )
    for table in ("loops", "loop_run"):
        db.execute(
            f"CREATE TRIGGER guard_update_log_trg AFTER UPDATE ON ah.{table} FOR EACH ROW "
            "EXECUTE FUNCTION ah.guard_update_log_fn()"
        )
    try:
        assert load.post_passes(db)["dirty_sessions"] == 0
        rows = db.execute("SELECT tbl, launch_uid, changed FROM ah.guard_update_log ORDER BY n").fetchall()
    finally:
        db.rollback()  # the triggers, function and table all lived in this transaction
    writes: dict[str, dict[str, list[set[str]]]] = {"loops": {}, "loop_run": {}}
    for table, uid, changed in rows:
        writes[table].setdefault(uid, []).append(set(changed))
    return writes["loops"], writes["loop_run"]


@pytest.mark.parametrize("state_loop_agrees", [False, True], ids=["done-flips-compete", "done-flips-agree"])
def test_no_change_refresh_writes_each_loop_only_for_its_named_flip_flops(clean, tmp_path, state_loop_agrees):
    build(clean, tmp_path, state_loop_agrees=state_loop_agrees)
    by_uid = names(clean)
    assert sorted(by_uid.values()) == sorted([STATE_NAME, CLOSEOUT_NAME, WAIT_NAME, ACTIVE_NAME])
    kinds = {
        by_uid[uid]: (evidence, status)
        for uid, evidence, status in clean.execute(
            "SELECT launch_uid, r.end_evidence, l.status FROM ah.loops l JOIN ah.loop_run r USING (launch_uid)"
        )
    }
    # The fixture really holds each shape, or the bounds below would prove nothing.
    assert kinds == {
        STATE_NAME: ("state_close", "finished"),
        CLOSEOUT_NAME: ("closeout", "finished"),
        WAIT_NAME: ("root_last_event", "waiting"),
        ACTIVE_NAME: ("root_last_event", "running"),
    }
    loop_writes, run_writes = count_updates(clean)
    writes = {by_uid[uid]: w for uid, w in loop_writes.items()}
    assert set(writes) <= set(by_uid.values())
    problems = []
    for name, (evidence, status) in kinds.items():
        row = writes.get(name, [])
        allowed, bound = set(UPSERT), 1
        if evidence == "state_close":
            allowed |= STATE_CLOSE
            bound += 1
            if not state_loop_agrees:  # the Done-flip count and the state close count write in turn
                allowed |= COUNT_CHAIN
                bound += 2
        if status == "waiting":
            allowed |= WAITING
            bound += 1
        if evidence in CHAIN_WRITERS:
            allowed |= COUNT_CHAIN
            bound += CHAIN_WRITERS[evidence]
        if len(row) > bound:
            problems.append(f"{name}: {len(row)} writes, at most {bound} are named: {row}")
        # No write changes a column outside the loop's named classes, and the stamp lands once.
        if not all(columns <= allowed for columns in row):
            problems.append(f"{name}: a write changed a column outside {sorted(allowed)}: {row}")
        if sum("observed_at" in columns for columns in row) > 1:
            problems.append(f"{name}: observed_at stamped more than once: {row}")
    # State close, completion receipt and closeout rewrite a run's recorded end only when it differs.
    for uid, row in run_writes.items():
        if row:
            problems.append(f"{by_uid.get(uid, uid)}: an unchanged pass rewrote its loop_run row: {row}")
    assert not problems, "\n".join(problems)
    # A loop with no flip-flop is written exactly once: the contract's observed_at stamp.
    assert writes[ACTIVE_NAME] == [{"observed_at"}]


def test_guards_leave_every_projected_value_as_unguarded_code_wrote_it(clean, tmp_path):
    build(clean, tmp_path)
    first = snapshot(clean)
    assert first == EXPECTED
    for _ in range(3):
        load.post_passes(clean)
        clean.commit()
        assert snapshot(clean) == first


def test_a_changed_input_still_writes_its_projection(clean, tmp_path):
    build(clean, tmp_path)
    uid, root = clean.execute(
        "SELECT r.launch_uid, r.root_session_id FROM ah.loop_run r JOIN ah.session s ON s.id = r.root_session_id "
        "WHERE s.session_uid = %s",
        (LANE_ROOT,),
    ).fetchone()
    clean.execute(
        "INSERT INTO ah.llm_call (agent,response_id,session_id,ts,model,input_uncached,cache_read,"
        "cache_write_5m,cache_write_1h,output,source_id,byte_offset) "
        "VALUES ('claude','synthetic-late-usage',%s,now(),'synthetic-unpriced',1,1,1,1,1,0,0)",
        (root,),
    )
    clean.execute("INSERT INTO ah.dirty_session (session_id) VALUES (%s)", (root,))
    clean.execute(
        "UPDATE ah.loop_live_cache SET response=%s, requested_at=now() + interval '1 second' WHERE kind='summary'",
        (Jsonb(summary_reply({"headline": "Later progress", "summary": "A later summary. Two sentences."})),),
    )
    clean.commit()
    load.post_passes(clean)
    clean.commit()
    assert clean.execute(
        "SELECT llm_calls, headline, summary FROM ah.loops WHERE launch_uid=%s", (uid,)
    ).fetchone() == (
        3,
        "Later progress",
        "A later summary. Two sentences.",
    )


def test_state_close_on_an_already_finished_run_writes_nothing(clean, tmp_path):
    build(clean, tmp_path, state_loop_agrees=True)
    uid = next(uid for uid, name in names(clean).items() if name == STATE_NAME)
    # A replacement launch also ended the run, so the upsert already says finished and the state log's close
    # has nothing to add: neither its status/end_ts write nor its run write may happen.
    clean.execute("UPDATE ah.loop_run SET end_evidence='next_launch' WHERE launch_uid=%s", (uid,))
    clean.commit()
    for _ in range(2):
        load.post_passes(clean)
        clean.commit()
    loop_writes, run_writes = count_updates(clean)
    assert loop_writes[uid] == [{"observed_at"}]
    assert uid not in run_writes

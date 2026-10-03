"""Nullable progress through the catalogue's actual post-pass and rebuild boundary."""

from __future__ import annotations

import json
import os
from datetime import timedelta
from decimal import Decimal

import pytest

from agent_history import load
import test_loop_identity_pg as identity_helpers
from test_loop_identity_pg import GOAL, index_launch, record_report

clean = identity_helpers.clean
conn = identity_helpers.conn
pytestmark = identity_helpers.pytestmark


def progress(db):
    return db.execute(
        "SELECT lanes_total, lanes_returned, last_activity_at, llm_calls, input_uncached, "
        "cache_read, cache_write, output, priced_cost_usd, tool_errors, api_errors, "
        "commits, pushes, root_agent FROM ah.loops"
    ).fetchone()


def add_usage(db, sid, uid, model="synthetic-unpriced"):
    db.execute(
        "INSERT INTO ah.llm_call (agent,response_id,session_id,ts,model,input_uncached,cache_read,"
        "cache_write_5m,cache_write_1h,output,source_id,byte_offset) "
        "VALUES ('claude',%s,%s,now(),%s,11,12,13,14,15,0,0)",
        (uid, sid, model),
    )


def populated(db, tmp_path):
    index_launch(db, tmp_path, f"# Loop: example/project loop1 · Goal: {GOAL}")
    root, loop_id = db.execute("SELECT root_session_id,id FROM ah.loop_run").fetchone()
    child = db.execute(
        "INSERT INTO ah.session (agent,session_uid,is_stub,root_session_id,loop_run_id,"
        "first_event_at,last_event_at) VALUES ('claude','synthetic-progress-child',false,%s,%s,now(),"
        "now() + interval '1 minute') RETURNING id",
        (root, loop_id),
    ).fetchone()[0]
    db.execute("INSERT INTO ah.lane (loop_run_id,session_id,link_method) VALUES (%s,%s,'lineage')", (loop_id, child))
    add_usage(db, root, "synthetic-root-usage")
    add_usage(db, child, "synthetic-child-usage")
    db.commit()
    load.post_passes(db)
    db.commit()
    return root, child


def test_running_finished_and_unknown_returns(clean, tmp_path):
    root, child = populated(clean, tmp_path)
    row = progress(clean)
    assert row[:2] == (1, None)  # a lane without captured returns is not zero returned
    assert row[2] == clean.execute("SELECT last_event_at FROM ah.session WHERE id=%s", (child,)).fetchone()[0]
    assert row[3:9] == (2, 22, 24, 54, 30, None)  # unpriced never becomes zero
    assert row[9:] == (None, 0, None, None, "claude")
    clean.execute('UPDATE ah.lane SET lane_return = \'{"status":"complete"}\'')
    clean.execute("INSERT INTO ah.dirty_session (session_id) VALUES (%s)", (child,))
    clean.commit()
    record_report(clean, f"# Loop: example/project loop1 · Goal: {GOAL}")
    assert clean.execute("SELECT status FROM ah.loops").fetchone() == ("finished",)
    row = progress(clean)
    assert row[:2] == (1, 1)
    assert row[3:9] == (2, 22, 24, 54, 30, None)
    assert row[-1] == "claude"


def test_partial_usage_and_partial_pricing_are_unknown(clean, tmp_path):
    root, _ = populated(clean, tmp_path)
    clean.execute(
        "INSERT INTO ah.model_pricing (model,effective_from,input_per_mtok,cached_input_per_mtok,"
        "cache_write_per_mtok,cache_write_1h_per_mtok,output_per_mtok) "
        "VALUES ('synthetic-priced','2000-01-01',1,1,1,1,1) ON CONFLICT DO NOTHING"
    )
    clean.execute("UPDATE ah.llm_call SET model='synthetic-priced' WHERE session_id=%s", (root,))
    clean.commit()
    load.post_passes(clean)
    assert progress(clean)[8] is None  # never a misleading partial total
    clean.execute("UPDATE ah.llm_call SET model='synthetic-priced'")
    clean.commit()
    load.post_passes(clean)
    assert progress(clean)[8] == Decimal("0.000130")
    clean.execute("UPDATE ah.llm_call SET input_uncached=NULL WHERE session_id=%s", (root,))
    clean.commit()
    load.post_passes(clean)
    assert progress(clean)[4] is None
    assert progress(clean)[8] is None


def test_historical_backfill_then_bounded_finished_refresh(clean, tmp_path):
    populated(clean, tmp_path)
    record_report(clean, f"# Loop: example/project loop1 · Goal: {GOAL}")
    clean.execute("UPDATE ah.loops SET llm_calls=NULL")
    clean.execute("DELETE FROM ah.meta WHERE key='loops_progress_projection_v1'")
    clean.commit()
    assert load.post_passes(clean)["dirty_sessions"] == 0
    assert progress(clean)[3] == 2
    clean.execute("UPDATE ah.loops SET llm_calls=99")
    clean.commit()
    load.post_passes(clean)
    assert progress(clean)[3] == 99  # unchanged historical rows are not rescanned every pass
    root = clean.execute("SELECT root_session_id FROM ah.loop_run").fetchone()[0]
    clean.execute("INSERT INTO ah.dirty_session (session_id) VALUES (%s)", (root,))
    clean.commit()
    load.post_passes(clean)
    assert progress(clean)[3] == 2


def test_finished_heuristic_lane_dirty_refresh(clean, tmp_path):
    _, child = populated(clean, tmp_path)
    record_report(clean, f"# Loop: example/project loop1 · Goal: {GOAL}")
    assert progress(clean)[3] == 2
    clean.execute("UPDATE ah.session SET root_session_id=NULL,loop_link_method='heuristic' WHERE id=%s", (child,))
    add_usage(clean, child, "synthetic-late-child-usage")
    clean.execute("INSERT INTO ah.dirty_session (session_id) VALUES (%s)", (child,))
    clean.commit()
    load.post_passes(clean)
    assert progress(clean)[3] == 3


def test_backfill_is_bounded_and_finishes_without_rebuild(clean, tmp_path):
    index_launch(clean, tmp_path, "")
    root = clean.execute("SELECT root_session_id FROM ah.loop_run").fetchone()[0]
    clean.execute("DELETE FROM ah.meta WHERE key='loops_progress_projection_v1'")
    clean.execute("UPDATE ah.loop_run SET end_evidence='next_launch',end_ts=now()")
    clean.execute("UPDATE ah.loops SET root_agent=NULL")
    clean.execute(
        "INSERT INTO ah.loop_run (launch_uid,root_session_id,launch_ts,end_ts,end_evidence) "
        "SELECT 'synthetic-batch-' || lpad(n::text,3,'0'),%s,now(),now(),'next_launch' "
        "FROM generate_series(1,128) n",
        (root,),
    )
    clean.commit()
    load.post_passes(clean)
    assert clean.execute("SELECT count(*) FROM ah.loops WHERE root_agent IS NOT NULL").fetchone() == (128,)
    clean.commit()
    load.post_passes(clean)
    assert clean.execute("SELECT count(*) FROM ah.loops WHERE root_agent IS NOT NULL").fetchone() == (129,)


def test_root_window_lane_whole_and_recorded_actions(clean, tmp_path):
    root, child = populated(clean, tmp_path)
    clean.execute(
        "UPDATE ah.llm_call SET ts=(SELECT launch_ts - interval '1 second' FROM ah.loop_run) WHERE session_id=%s",
        (root,),
    )
    for sid, uid in [(root, "synthetic-root-error"), (child, "synthetic-child-error")]:
        clean.execute(
            "INSERT INTO ah.tool_call (agent,call_uid,session_id,tool_name,started_at,ended_at,"
            "outcome,source_id,byte_offset) VALUES ('claude',%s,%s,'Bash',now(),now(),'error',0,0)",
            (uid, sid),
        )
        for op in ("commit", "push"):
            clean.execute(
                "INSERT INTO ah.git_event (agent,event_uid,session_id,ts,op,evidence,source_id,byte_offset) "
                "VALUES ('claude',%s,%s,now(),%s,'tool',0,0)",
                (uid + op, sid, op),
            )
    clean.commit()
    load.post_passes(clean)
    row = progress(clean)
    assert row[3:8] == (1, 11, 12, 27, 15)
    assert row[9:13] == (2, 0, 2, 2)


def test_analytics_reapply_rebuild_keep_progress_and_foreign_grant(clean, tmp_path):
    sources = index_launch(clean, tmp_path, f"# Loop: example/project loop1 · Goal: {GOAL}")
    before = progress(clean)
    assert before[:2] == (None, None)
    assert before[3:13] == (None,) * 10
    psycopg = pytest.importorskip("psycopg")
    with psycopg.connect(os.environ["AGENT_HISTORY_TEST_ADMIN_DSN"]) as admin:
        admin.execute("CREATE ROLE synthetic_progress_reader")
    try:
        clean.execute("GRANT SELECT ON ah.loops TO synthetic_progress_reader")
        clean.commit()
        load.apply_schema(clean, force=True)
        clean.commit()
        assert progress(clean) == before
        clean.commit()
        assert load.rebuild(clean, sources=sources, textfile=None, log=lambda *_: None).errors == 0
        assert progress(clean) == before
        assert clean.execute(
            "SELECT has_table_privilege('synthetic_progress_reader','ah.loops','SELECT')"
        ).fetchone() == (True,)
    finally:
        clean.rollback()
        with psycopg.connect(os.environ["AGENT_HISTORY_TEST_ADMIN_DSN"]) as admin:
            admin.execute("DROP OWNED BY synthetic_progress_reader")
            admin.execute("DROP ROLE synthetic_progress_reader")


def test_progress_selection_reads_running_set_not_history(clean):
    """Cost discriminator: three running loops must not read 5,000 finished owners."""
    import json

    from agent_history.loops import PROGRESS_SELECTION_SQL

    clean.execute(
        "INSERT INTO ah.session (agent,session_uid,is_stub) "
        "SELECT 'pi','synthetic-cost-' || n,false FROM generate_series(1,5003) n"
    )
    clean.execute(
        "INSERT INTO ah.loop_run (launch_uid,root_session_id,launch_ts) SELECT session_uid,id,now() FROM ah.session"
    )
    clean.execute(
        "INSERT INTO ah.loops (launch_uid,status,launch_ts,end_ts,observed_at,repo) "
        "SELECT launch_uid,CASE WHEN root_session_id <= 5000 THEN 'finished' ELSE 'running' END,"
        "now(),CASE WHEN root_session_id <= 5000 THEN now() END,now(),'example/project' FROM ah.loop_run"
    )
    clean.execute("CREATE TEMP TABLE IF NOT EXISTS dirty_now (session_id bigint PRIMARY KEY)")
    clean.execute("TRUNCATE dirty_now")
    for table in ("loops", "loop_run", "session"):
        clean.execute(f"ANALYZE ah.{table}")

    def prove(query, params=None, expected=3):
        plan = clean.execute("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + query, params).fetchone()[0][0]
        print(json.dumps(plan, indent=2))
        nodes = []

        def visit(node):
            nodes.append(node)
            for child in node.get("Plans", []):
                visit(child)

        visit(plan["Plan"])
        reads = sum(
            (node.get("Actual Rows", 0) + node.get("Rows Removed by Filter", 0)) * node.get("Actual Loops", 0)
            for node in nodes
            if node.get("Relation Name") in ("loops", "loop_run", "session")
        )
        assert reads <= 30, f"selected three running loops but read {reads} catalogue rows"
        assert any("Index" in node["Node Type"] for node in nodes)
        assert len(clean.execute(query, params).fetchall()) == expected

    prove(PROGRESS_SELECTION_SQL)
    prove("SELECT launch_uid FROM ah.loops WHERE repo = %s AND status = 'running'", ("example/project",))
    # A dirty finished root also resolves through the ownership index, not all historical loops.
    clean.execute("INSERT INTO dirty_now VALUES (1)")
    prove(PROGRESS_SELECTION_SQL, expected=4)


def add_done(db, registry, repo_slug, task, sha, done_at):
    registry["done"].append((repo_slug, task, sha))
    db.execute(
        "INSERT INTO ah.backlog_done_event (repo_slug, task_key, sha, done_at) VALUES (%s,%s,%s,%s)",
        (repo_slug, task, sha, done_at),
    )
    db.commit()


def add_scan(db, registry, repo_slug):
    registry["scan"].append(repo_slug)
    db.execute("INSERT INTO ah.backlog_done_scan (repo_slug, widened_at) VALUES (%s, now())", (repo_slug,))
    db.commit()


def tasks_done(db):
    return db.execute("SELECT tasks_done FROM ah.loops").fetchone()[0]


@pytest.fixture
def collector_rows(clean):
    """Keys of the collector rows a test inserted; only those are removed (the tables survive the truncate)."""
    inserted = {"done": [], "task": [], "scan": []}
    yield inserted
    clean.rollback()
    for repo_slug in inserted["scan"]:
        clean.execute("DELETE FROM ah.backlog_done_scan WHERE repo_slug = %s", (repo_slug,))
    for repo_slug, task, sha in inserted["done"]:
        clean.execute(
            "DELETE FROM ah.backlog_done_event WHERE repo_slug = %s AND task_key = %s AND sha = %s",
            (repo_slug, task, sha),
        )
    for repo_slug, task in inserted["task"]:
        clean.execute("DELETE FROM ah.backlog_task WHERE repo_slug = %s AND task_key = %s", (repo_slug, task))
    clean.commit()


def test_tasks_done_counts_flips_in_the_loop_window_whatever_the_session(clean, collector_rows, tmp_path):
    populated(clean, tmp_path)
    collector_rows["task"].append(("github.com/example/project", "EX-0001"))
    assert tasks_done(clean) is None  # the collector has not read this repo's tracker
    clean.execute(
        "INSERT INTO ah.backlog_task (task_key, repo_slug, context, status) "
        "VALUES ('EX-0001','github.com/example/project','default','Done')"
    )
    clean.commit()
    load.post_passes(clean)
    assert tasks_done(clean) is None  # tracker read, but no Done scan of its history yet
    add_scan(clean, collector_rows, "github.com/example/project")
    load.post_passes(clean)
    assert tasks_done(clean) == 0  # a scanned repo with no flips is zero
    launch = clean.execute("SELECT launch_ts FROM ah.loops").fetchone()[0]
    day = launch.replace(microsecond=0)
    add_done(clean, collector_rows, "github.com/example/project", "EX-0001", "a" * 40, day + timedelta(hours=1))
    add_done(
        clean, collector_rows, "github.com/Example/Project", "EX-0001", "b" * 40, day + timedelta(hours=2)
    )  # reopened, again
    add_done(clean, collector_rows, "github.com/example/project", "EX-0002", "c" * 40, day + timedelta(hours=3))
    add_done(
        clean, collector_rows, "github.com/example/project", "EX-0003", "d" * 40, day - timedelta(days=1)
    )  # before launch
    add_done(
        clean, collector_rows, "github.com/example/other", "EX-0004", "e" * 40, day + timedelta(hours=1)
    )  # another repo
    load.post_passes(clean)
    assert tasks_done(clean) == 2  # distinct tasks; no session involved
    # a finished loop only counts flips before its end; late collector rows still reach it
    clean.execute(
        "UPDATE ah.loop_run SET end_evidence='next_launch', end_ts=%s", (day + timedelta(hours=2, minutes=30),)
    )
    clean.commit()
    load.post_passes(clean)
    assert tasks_done(clean) == 1


def test_tasks_done_window_of_a_stale_loop_ends_at_its_last_activity(clean, collector_rows, tmp_path):
    root, _ = populated(clean, tmp_path)
    add_scan(clean, collector_rows, "github.com/example/project")
    launch = clean.execute("SELECT launch_ts FROM ah.loops").fetchone()[0] - timedelta(days=5)
    clean.execute("UPDATE ah.loop_run SET launch_ts = %s", (launch,))
    clean.execute(
        "UPDATE ah.session SET last_event_at = %s WHERE id = %s OR root_session_id = %s",
        (launch + timedelta(hours=4), root, root),
    )
    clean.commit()
    add_done(clean, collector_rows, "github.com/example/project", "EX-0001", "a" * 40, launch + timedelta(hours=1))
    add_done(clean, collector_rows, "github.com/example/project", "EX-0002", "b" * 40, launch + timedelta(days=2))
    load.post_passes(clean)
    assert clean.execute("SELECT status, end_ts FROM ah.loops").fetchone() == ("stale", launch + timedelta(hours=4))
    assert tasks_done(clean) == 1  # the flip two days after the root fell silent is not this loop's


def test_lane_return_v2_fills_return_status_and_lane_return(clean, tmp_path):
    from agent_history import loops

    _, child = populated(clean, tmp_path)
    loop_id = clean.execute("SELECT id FROM ah.loop_run").fetchone()[0]
    v2 = {
        "v": 2,
        "lane": "H1",
        "status": "partial",
        "sha": None,
        "landed": False,
        "base": "a" * 40,
        "check": "just check",
        "exit": 1,
        "tail": "boom",
        "ci": None,
        "coderabbit": None,
        "questions": ["q"],
    }
    clean.execute(
        "INSERT INTO ah.message (agent, event_uid, session_id, namespace, profile, ts, role, message_class, text, "
        "content_sha256, byte_length, source_id, byte_offset) VALUES ('claude','lane-report',%s,'claude-test','p',"
        "now(),'assistant','subagent_report',%s,'x',1,0,0)",
        (child, f"summary\n```lane-return\n{json.dumps(v2)}\n```\n"),
    )
    loops._lane(clean, loop_id, child, "lineage")
    clean.commit()
    assert clean.execute("SELECT return_status, lane_return FROM ah.lane WHERE session_id=%s", (child,)).fetchone() == (
        "partial",
        v2,
    )

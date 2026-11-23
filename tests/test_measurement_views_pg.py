"""Read-side measurement: synthetic model calls, squash-merged pull requests, run protocol and run time.

Database tests against a disposable catalogue (just ci). Every row is synthetic.
"""

from datetime import datetime, timedelta, timezone

import pytest

from agent_history import load
from agent_history.metrics.loop_health import QUERY as HEALTH_QUERY
from test_loop_identity_pg import DSN, conn  # noqa: F401 - real disposable catalogue fixture

pytestmark = pytest.mark.skipif(not DSN or "agent_history_test" not in DSN, reason="scratch database not available")

AT = datetime(2031, 3, 3, 9, tzinfo=timezone.utc)
SYN = "<synthetic>"
PRICED = "synthetic-measure-priced"
REPO = "github.com/example-org/measure"
REPORT = "/synthetic/measure/codex/run-20310303-1/CLOSEOUT.md"


@pytest.fixture
def clean(conn):  # noqa: F811
    def wipe():
        conn.execute("TRUNCATE " + ", ".join(f"ah.{t}" for t in load.DATA_TABLES) + " RESTART IDENTITY CASCADE")
        conn.execute("DELETE FROM ah.git_commit WHERE lower(repo_slug) = %s", (REPO,))
        conn.execute("DELETE FROM ah.loop_run_metadata WHERE path = %s", (REPORT,))
        conn.execute("DELETE FROM ah.model_pricing WHERE model = %s", (PRICED,))
        conn.commit()

    wipe()
    conn.execute(
        "INSERT INTO ah.model_pricing (model,effective_from,input_per_mtok,output_per_mtok) VALUES (%s,'2000-01-01',1,2)",
        (PRICED,),
    )
    conn.commit()
    yield conn
    conn.rollback()
    wipe()


def session(db, uid, *, agent="claude", loop_id=None, remote=None):
    sid = db.execute(
        "INSERT INTO ah.session (agent,session_uid,is_stub,loop_run_id,loop_link_method,git_remote_url,"
        "first_event_at,last_event_at) VALUES (%s,%s,false,%s,%s,%s,%s,%s) RETURNING id",
        (agent, uid, loop_id, "lineage" if loop_id else None, remote, AT, AT + timedelta(hours=4)),
    ).fetchone()[0]
    db.execute("UPDATE ah.session SET root_session_id = id WHERE id = %s", (sid,))
    return sid


def call(db, sid, uid, at, model=PRICED, *, api_error=False, stop="end_turn", output=1000):
    db.execute(
        "INSERT INTO ah.llm_call (agent,response_id,session_id,ts,model,input_uncached,cache_read,cache_write_5m,"
        "cache_write_1h,output,is_api_error,stop_reason,source_id,byte_offset) "
        "VALUES ('claude',%s,%s,%s,%s,%s,0,0,0,%s,%s,%s,0,0)",
        (uid, sid, at, model, 0 if model == SYN else 1000, 0 if model == SYN else output, api_error, stop),
    )


def rollup(db, *ids):
    db.execute("CREATE TEMP TABLE IF NOT EXISTS dirty_now (session_id bigint PRIMARY KEY)")
    db.execute("DELETE FROM dirty_now")
    db.execute("INSERT INTO dirty_now SELECT unnest(%s::bigint[])", (list(ids),))
    db.execute(load.ROLLUP_SQL.replace("ah.dirty_now", "dirty_now"))
    db.execute("DROP TABLE dirty_now")


def one(db, sql, params=()):
    cur = db.execute(sql, params)
    names = [d.name for d in cur.description]
    row = cur.fetchone()
    return dict(zip(names, row)) if row else None


def synthetic_session(db, uid="measure-session"):
    sid = session(db, uid)
    call(db, sid, "real-a", AT + timedelta(minutes=1))
    call(db, sid, "real-b", AT + timedelta(minutes=2), stop=None)
    call(db, sid, "syn-a", AT + timedelta(minutes=3), SYN)
    call(db, sid, "syn-b", AT + timedelta(minutes=4), SYN, stop=None)
    call(db, sid, "syn-error", AT + timedelta(minutes=5), SYN, api_error=True)
    return sid


def test_rollup_counts_synthetic_records_apart_from_model_calls(clean):
    db = clean
    sid = synthetic_session(db)
    rollup(db, sid)
    r = one(db, "SELECT * FROM ah.session_rollup WHERE session_id = %s", (sid,))
    total = db.execute("SELECT count(*) FROM ah.llm_call WHERE session_id = %s", (sid,)).fetchone()[0]
    # Invariant: llm_calls + synthetic_calls is every llm_call row (the old llm_calls).
    assert (r["llm_calls"], r["synthetic_calls"]) == (2, 3) and r["llm_calls"] + r["synthetic_calls"] == total
    # api_errors keeps the synthetic API error; snapshot_calls and models never hold a synthetic row.
    assert r["api_errors"] == 1 and r["snapshot_calls"] == 1 and r["models"] == [PRICED]
    summary = one(db, "SELECT llm_calls, synthetic_calls FROM ah.v_session_summary WHERE session_id = %s", (sid,))
    assert summary == {"llm_calls": 2, "synthetic_calls": 3}
    tree = one(db, "SELECT llm_calls, synthetic_calls FROM ah.v_session_tree WHERE root_session_id = %s", (sid,))
    assert tree == {"llm_calls": 2, "synthetic_calls": 3}


def test_cost_usage_and_efficiency_views_do_not_count_synthetic_records(clean):
    db = clean
    sid = synthetic_session(db)
    db.commit()
    cost = one(db, "SELECT * FROM ah.v_session_cost WHERE session_id = %s", (sid,))
    # The unpriced '<synthetic>' group adds no unpriced call; the priced total is unchanged.
    assert (cost["llm_calls"], cost["synthetic_calls"], cost["unpriced_calls"]) == (2, 3, None)
    assert cost["snapshot_calls"] == 1 and cost["priced_cost_usd"] is not None
    usage = {
        r[0]: r[1:]
        for r in db.execute(
            "SELECT model, llm_calls, synthetic_calls FROM ah.v_daily_usage WHERE agent = 'claude'"
        ).fetchall()
    }
    # The synthetic model keeps its row, with no model calls.
    assert usage == {PRICED: (2, 0), SYN: (0, 3)}
    models = [r[0] for r in db.execute("SELECT model FROM ah.efficiency_calls(NULL, 'measure-session', '')")]
    assert sorted(models) == [PRICED, PRICED]


def loop(db, *, protocol="v5", mode="run", end=AT + timedelta(hours=2)):
    root = session(db, "measure-root")
    run = db.execute(
        "INSERT INTO ah.loop_run (launch_uid,root_session_id,mode,protocol,report_path,launch_ts,end_ts,end_evidence) "
        "VALUES ('measure:run',%s,%s,%s,%s,%s,%s,'root_last_event') RETURNING id",
        (root, mode, protocol, REPORT, AT, end),
    ).fetchone()[0]
    db.execute(
        "INSERT INTO ah.loops (launch_uid,repo,status,launch_ts,end_ts,observed_at,tasks_admitted,tasks_landed,"
        "tasks_done,parks_total) VALUES ('measure:run','example-org/measure','finished',%s,%s,now(),5,3,3,1)",
        (AT, end),
    )
    return root, run


def test_loop_summary_protocol_counters_and_synthetic_calls(clean):
    db = clean
    root, run = loop(db)
    call(db, root, "root-before", AT - timedelta(minutes=5))
    call(db, root, "root-a", AT + timedelta(minutes=1))
    call(db, root, "root-syn", AT + timedelta(minutes=2), SYN)
    lane = session(db, "measure-lane", loop_id=run)
    db.execute("INSERT INTO ah.lane (loop_run_id,session_id,link_method) VALUES (%s,%s,'lineage')", (run, lane))
    call(db, lane, "lane-a", AT + timedelta(minutes=3))
    call(db, lane, "lane-syn", AT + timedelta(minutes=4), SYN)
    rollup(db, lane)
    db.commit()
    s = one(db, "SELECT * FROM ah.v_loop_summary WHERE loop_run_id = %s", (run,))
    assert (s["mode"], s["protocol"]) == ("run", "v5")
    # Root calls inside the window plus the lane whole; the old count was 4.
    assert (s["llm_calls"], s["synthetic_calls"]) == (2, 2) and s["llm_calls"] + s["synthetic_calls"] == 4
    assert (s["tasks_admitted"], s["tasks_landed"], s["tasks_done"], s["parks_total"]) == (5, 3, 3, 1)
    lanes = one(db, "SELECT llm_calls, synthetic_calls, model FROM ah.loop_lanes(%s)", (run,))
    assert lanes == {"llm_calls": 1, "synthetic_calls": 1, "model": PRICED}


def test_loop_summary_closeout_time_and_active_time(clean):
    db = clean
    root, run = loop(db, protocol=None, mode="v3")
    lane = session(db, "measure-lane", loop_id=run)
    # Active spans: 0:01 to 0:10 (calls 9 minutes apart join), a 30-minute tool call from 0:40 to 1:10
    # with a call at 1:20 joining it, and a lone call at 1:50. Gaps of 30 and 40 minutes are idle; the
    # call before launch and the tool call running past the end are clipped to the window.
    call(db, root, "before", AT - timedelta(minutes=30))
    call(db, root, "a", AT + timedelta(minutes=1))
    call(db, lane, "b", AT + timedelta(minutes=10))
    call(db, root, "syn", AT + timedelta(minutes=25), SYN)
    db.execute(
        "INSERT INTO ah.tool_call (agent,call_uid,session_id,tool_name,started_at,ended_at,source_id,byte_offset) "
        "VALUES ('claude','t1',%s,'Bash',%s,%s,0,0), ('claude','t2',%s,'Bash',%s,%s,0,0)",
        (
            lane,
            AT + timedelta(minutes=40),
            AT + timedelta(minutes=70),
            root,
            AT + timedelta(minutes=110),
            AT + timedelta(hours=3),
        ),
    )
    call(db, lane, "c", AT + timedelta(minutes=80))
    db.execute(
        "INSERT INTO ah.loop_run_metadata (machine,path,kind,sha256,file_mtime) VALUES "
        "('m1',%s,'closeout','a',%s), ('m2',%s,'closeout','a',%s), ('m3',%s,'closeout','b',%s)",
        (REPORT, AT + timedelta(minutes=95), REPORT, AT + timedelta(minutes=100), REPORT, AT - timedelta(days=1)),
    )
    db.commit()
    s = one(db, "SELECT * FROM ah.v_loop_summary WHERE loop_run_id = %s", (run,))
    assert s["protocol"] is None and s["mode"] == "v3"
    assert s["wall_s"] == 7200 and s["end_ts"] == AT + timedelta(hours=2)  # end_ts is unchanged
    # Earliest collected closeout at or after launch; the stale copy from before launch is ignored.
    assert s["closeout_ts"] == AT + timedelta(minutes=95) and s["closeout_wall_s"] == 95 * 60
    # 9 min (0:01 to 0:10) + 40 min (0:40 to 1:20) + 10 min (1:50 to 2:00, the tool clipped at the end).
    assert s["active_s"] == (9 + 40 + 10) * 60
    assert 0 <= s["active_s"] <= s["wall_s"]


def test_squash_merged_pull_request_resolves_to_the_session(clean):
    db = clean
    sid = session(db, "measure-pr", remote="git@github.com:example-org/measure.git")

    def event(uid, op, at, **kw):
        db.execute(
            "INSERT INTO ah.git_event (agent,event_uid,session_id,ts,op,sha_short,pr_number,pr_url,pr_repo,evidence,"
            "source_id,byte_offset) VALUES ('claude',%s,%s,%s,%s,%s,%s,%s,%s,'gitOperation',0,0)",
            (uid, sid, at, op, kw.get("sha"), kw.get("pr"), kw.get("url"), kw.get("repo")),
        )

    def commit(sha, at, subject, lines=(10, 2)):
        db.execute(
            "INSERT INTO ah.git_commit (repo_slug,sha,context,committed_at,subject,insertions,deletions,on_default) "
            "VALUES (%s,%s,'local',%s,%s,%s,%s,true)",
            (REPO, sha, at, subject, *lines),
        )

    # Two branch commits, then PR 42 opened and squash-merged; the branch shas never reach main.
    event("c1", "commit", AT, sha="aaaaaaa1111")
    event("c2", "commit", AT + timedelta(minutes=5), sha="aaaaaaa2222")
    event("p42", "pr", AT + timedelta(minutes=6), pr=42, url="https://github.com/example-org/measure/pull/42")
    commit("f" * 40, AT + timedelta(minutes=30), "Add widget (#42)")
    commit("e" * 40, AT + timedelta(minutes=20), "Unrelated change (#421)")  # a longer number is not #42
    # A later commit, PR 43 closed unmerged: no squash commit, so it stays unresolved.
    event("c3", "commit", AT + timedelta(hours=1), sha="bbbbbbb3333")
    event("p43", "pr", AT + timedelta(hours=1, minutes=1), pr=43, repo="example-org/measure")
    # A commit that reached main by its own sha keeps resolving by sha.
    commit("c" * 40, AT + timedelta(hours=2, minutes=1), "Direct (#44)", lines=(1, 1))
    event("c4", "commit", AT + timedelta(hours=2), sha="c" * 12)
    db.commit()
    got = {
        r[0]: r[1:]
        for r in db.execute(
            "SELECT sha_short, resolved, resolved_by, pr_number, sha, lines_changed FROM ah.v_agent_commit_quality "
            "WHERE session_uid = 'measure-pr'"
        ).fetchall()
    }
    assert got == {
        "aaaaaaa1111": (True, "pr_number", 42, "f" * 40, 12),
        "aaaaaaa2222": (True, "pr_number", 42, "f" * 40, 12),
        "bbbbbbb3333": (False, None, None, None, 0),
        "c" * 12: (True, "sha", None, "c" * 40, 2),
    }
    # One row per commit event, none dropped; resolved = by sha + by pr; the squash commit's lines once.
    week = one(
        db,
        "SELECT sum(agent_commits) AS commits, sum(resolved) AS resolved, sum(resolved_by_sha) AS by_sha, "
        "sum(resolved_by_pr) AS by_pr, sum(pr_merge_commits) AS merges, sum(lines_changed) AS lines "
        "FROM ah.v_agent_commit_quality_weekly WHERE repo_slug = %s",
        (REPO,),
    )
    assert week == {"commits": 4, "resolved": 3, "by_sha": 1, "by_pr": 2, "merges": 1, "lines": 14}


def test_loop_health_root_calls_exclude_synthetic_records(clean):
    db = clean
    root, run = loop(db)
    call(db, root, "a", AT + timedelta(minutes=1))
    call(db, root, "b", AT + timedelta(minutes=2))
    for i in range(3):
        call(db, root, f"syn-{i}", AT + timedelta(minutes=3 + i), SYN)
    db.commit()
    cur = db.execute(HEALTH_QUERY)
    names = [d.name for d in cur.description]
    rows = [dict(zip(names, r)) for r in cur.fetchall()]
    assert [(r["harness"], r["root_model_calls"]) for r in rows] == [("claude", 2)]


def test_loop_health_window_is_not_ended_by_another_dialects_launch(clean):
    db = clean
    root, run = loop(db, protocol=None, mode="v3")
    # A run-folder run starting on the same root half an hour in does not end the v3 window.
    db.execute(
        "INSERT INTO ah.loop_run (launch_uid,root_session_id,mode,protocol,launch_ts) VALUES ('measure:folder',%s,'run','v5',%s)",
        (root, AT + timedelta(minutes=30)),
    )
    call(db, root, "early", AT + timedelta(minutes=1))
    call(db, root, "late", AT + timedelta(minutes=60))
    db.commit()
    cur = db.execute(HEALTH_QUERY)
    names = [d.name for d in cur.description]
    assert [dict(zip(names, r))["root_model_calls"] for r in cur.fetchall()] == [2]


def test_squash_resolution_matches_a_mixed_case_stored_slug(clean):
    db = clean
    sid = session(db, "measure-case", remote="https://github.com/Example-Org/Measure.git")
    db.execute(
        "INSERT INTO ah.git_event (agent,event_uid,session_id,ts,op,sha_short,pr_number,pr_url,evidence,source_id,"
        "byte_offset) VALUES ('claude','k1',%s,%s,'commit','ddddddd1111',NULL,NULL,'gitOperation',0,0),"
        "('claude','k2',%s,%s,'pr',NULL,7,'https://github.com/Example-Org/Measure/pull/7','gitOperation',0,0)",
        (sid, AT, sid, AT + timedelta(minutes=1)),
    )
    # The sha path compares lower(repo_slug); the pull request path must too.
    db.execute(
        "INSERT INTO ah.git_commit (repo_slug,sha,context,committed_at,subject,on_default) "
        "VALUES ('github.com/Example-Org/Measure',%s,'local',%s,'Fix (#7)',true)",
        ("d" * 40, AT + timedelta(minutes=9)),
    )
    db.commit()
    got = db.execute(
        "SELECT resolved_by, pr_number, sha FROM ah.v_agent_commit_quality WHERE session_uid = 'measure-case'"
    ).fetchall()
    assert got == [("pr_number", 7, "d" * 40)]


def test_migration_054_requeues_sessions_rolled_up_before_synthetic_calls(clean):
    db = clean
    sid = synthetic_session(db, "measure-stale")
    plain = session(db, "measure-plain")
    call(db, plain, "plain-a", AT)
    # As a catalogue that applied 053 with the old rollup: marks consumed, counts inflated.
    db.execute(
        "INSERT INTO ah.session_rollup (session_id,computed_at,llm_calls) VALUES (%s,now(),5), (%s,now(),1)",
        (sid, plain),
    )
    db.execute("DELETE FROM ah.dirty_session")
    db.execute("DELETE FROM ah.meta WHERE key = 'migration:054_requeue_synthetic_rollups.sql'")
    db.commit()
    load.apply_schema(db, force=True)
    db.commit()
    queued = [r[0] for r in db.execute("SELECT session_id FROM ah.dirty_session ORDER BY 1")]
    assert sid in queued and plain not in queued  # only sessions holding a synthetic record
    rollup(db, sid)
    r = one(db, "SELECT llm_calls, synthetic_calls FROM ah.session_rollup WHERE session_id = %s", (sid,))
    assert r == {"llm_calls": 2, "synthetic_calls": 3}

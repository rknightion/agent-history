"""`loop <closeout path> --usage` through the real reader (psql, ah_reader) against synthetic rows."""

import os
import shutil
from datetime import datetime, timedelta, timezone

import pytest

from agent_history import load, reader
from test_loop_identity_pg import conn  # noqa: F401 - real disposable catalogue fixture

DSN = os.environ.get("AGENT_HISTORY_TEST_DSN", "")
READER = os.environ.get("AGENT_HISTORY_TEST_READER_DSN", "")
pytestmark = pytest.mark.skipif(
    not (DSN and READER and "agent_history_test" in DSN and shutil.which(reader.PSQL)),
    reason="scratch database, reader DSN or psql not available",
)
AT = datetime(2026, 10, 4, 12, tzinfo=timezone.utc)
CLOSEOUT = "/synthetic/project/codex/closeout-synthetic-loop14.md"
PRICED = "synthetic-usage-priced"


@pytest.fixture
def clean(conn):  # noqa: F811
    conn.execute("TRUNCATE " + ", ".join(f"ah.{table}" for table in load.DATA_TABLES) + " RESTART IDENTITY CASCADE")
    conn.execute("DELETE FROM ah.model_pricing WHERE model=%s", (PRICED,))
    conn.commit()
    yield conn
    conn.rollback()
    conn.execute("DELETE FROM ah.model_pricing WHERE model=%s", (PRICED,))
    conn.commit()


def session(db, uid, *, loop_id=None, method=None):
    return db.execute(
        "INSERT INTO ah.session (agent,session_uid,is_stub,loop_run_id,loop_link_method,first_event_at,last_event_at) "
        "VALUES ('claude',%s,false,%s,%s,%s,%s) RETURNING id",
        (uid, loop_id, method, AT, AT + timedelta(hours=3)),
    ).fetchone()[0]


def launch(db, uid, root, start, end):
    loop_id = db.execute(
        "INSERT INTO ah.loop_run (launch_uid,root_session_id,loop_number,mode,report_path,launch_ts,end_ts,end_evidence) "
        "VALUES (%s,%s,14,'v3',%s,%s,%s,'closeout') RETURNING id",
        (uid, root, CLOSEOUT, start, end),
    ).fetchone()[0]
    db.execute(
        "INSERT INTO ah.loops (launch_uid,status,launch_ts,end_ts,observed_at) VALUES (%s,'finished',%s,%s,now())",
        (uid, start, end),
    )
    return loop_id


def call(db, sid, uid, at, model=PRICED):
    db.execute(
        "INSERT INTO ah.llm_call (agent,response_id,session_id,ts,model,input_uncached,cache_read,"
        "cache_write_5m,cache_write_1h,output,source_id,byte_offset) "
        "VALUES ('claude',%s,%s,%s,%s,1000000,0,0,0,1000000,0,0)",
        (uid, sid, at, model),
    )


def test_usage_block_for_a_relaunched_closeout(clean, monkeypatch, capsys):
    db = clean
    db.execute(
        "INSERT INTO ah.model_pricing (model,effective_from,input_per_mtok,output_per_mtok) VALUES (%s,'2026-01-01',1,2)",
        (PRICED,),
    )
    root = session(db, "synthetic-usage-root")
    first = launch(db, "usage:first", root, AT, AT + timedelta(hours=1))
    second = launch(db, "usage:second", root, AT + timedelta(hours=1), AT + timedelta(hours=2))
    # Root calls inside each window count; one before the first launch does not.
    call(db, root, "root-before", AT - timedelta(minutes=5))
    call(db, root, "root-first", AT + timedelta(minutes=5))
    call(db, root, "root-second", AT + timedelta(hours=1, minutes=5))
    worker = session(db, "synthetic-usage-worker", loop_id=first, method="lineage")
    db.execute(
        "INSERT INTO ah.lane (loop_run_id,session_id,lane_name,role,link_method) VALUES (%s,%s,'secret-lane','worker','lineage')",
        (first, worker),
    )
    call(db, worker, "worker-priced", AT + timedelta(minutes=10))
    call(db, worker, "worker-unpriced", AT + timedelta(minutes=11), model="synthetic-usage-unpriced")
    stray = session(db, "synthetic-usage-heuristic", loop_id=second, method="heuristic")
    call(db, stray, "stray", AT + timedelta(hours=1, minutes=30))
    db.commit()
    monkeypatch.setenv("AGENT_HISTORY_READER_DSN", READER)
    assert reader.main(["loop", CLOSEOUT, "--usage"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].startswith("Usage as of ") and lines[0].endswith("Z (agent-history catalogue, list prices)")
    assert lines[2:8] == [
        "- Wall time: 2h 0m",
        "- LLM calls: 5",
        "- List-price cost: $12.00",
        "- Launches: 2 share this closeout and are counted as one run",
        "- Unpriced calls: 1 (not in the cost total)",
        "- Heuristic-linked sessions: 1 (attributed by timing, not by an explicit link)",
    ]
    at = lines.index("| Role | Model | Calls | Cost |")
    assert lines[at - 1] == "" and lines[at : at + 6] == [
        "| Role | Model | Calls | Cost |",
        "| --- | --- | ---: | ---: |",
        f"| root | {PRICED} | 2 | $6.00 |",
        f"| unattributed | {PRICED} | 1 | $3.00 |",
        f"| worker | {PRICED} | 1 | $3.00 |",
        "| worker | synthetic-usage-unpriced | 1 | unpriced |",
    ]
    assert "secret-lane" not in "\n".join(lines)
    # The numeric form names one launch; an unknown path is one line and a non-zero exit.
    assert reader.main(["loop", str(first), "--usage"]) == 0
    assert "- LLM calls: 3" in capsys.readouterr().out.splitlines()
    # A running relaunch still counts its root's latest call at the recorded fallback end.
    db.execute("UPDATE ah.loops SET status='running',end_ts=NULL WHERE launch_uid='usage:second'")
    call(db, root, "root-latest", AT + timedelta(hours=2))
    db.commit()
    assert reader.main(["loop", CLOSEOUT, "--usage"]) == 0
    assert "- LLM calls: 6" in capsys.readouterr().out.splitlines()
    # Without --usage, a shared closeout shows every launch and says so.
    assert reader.main(["loop", CLOSEOUT]) == 0
    assert capsys.readouterr().out.startswith("2 launches share this closeout; showing them together as one run.")
    with pytest.raises(SystemExit) as raised:
        reader.main(["loop", "/synthetic/project/codex/closeout-other-loop1.md", "--usage"])
    assert str(raised.value.code).startswith("agent-history: no loop run has closeout path ")


def test_usage_block_numbers_from_catalogue_rows(clean, monkeypatch, capsys):
    db = clean
    db.execute(
        "INSERT INTO ah.model_pricing (model,effective_from,input_per_mtok,output_per_mtok) VALUES (%s,'2026-01-01',1,2)",
        (PRICED,),
    )
    root = session(db, "synthetic-v4-root")
    db.execute("UPDATE ah.session SET cli_version_last='2.1.4' WHERE id=%s", (root,))
    run = launch(db, "usage:v4", root, AT, AT + timedelta(hours=2))
    db.execute("UPDATE ah.loops SET tasks_landed=3, parks_total=1 WHERE launch_uid='usage:v4'")
    # Root context: the larger call is 1,500 tokens; a call before the launch is not in the window.
    call(db, root, "root-early", AT - timedelta(minutes=5))
    call(db, root, "root-a", AT + timedelta(minutes=5))
    call(db, root, "root-b", AT + timedelta(minutes=6))
    db.execute("UPDATE ah.llm_call SET input_uncached=1200, cache_read=300 WHERE response_id='root-b'")
    db.execute("UPDATE ah.llm_call SET input_uncached=800 WHERE response_id='root-a'")
    db.execute("UPDATE ah.llm_call SET input_uncached=9000000 WHERE response_id='root-early'")
    for n, (offset, trigger) in enumerate(((10, "manual"), (20, "auto"), (30, "auto"), (40, None), (-5, "manual"))):
        db.execute(
            "INSERT INTO ah.compaction (agent,event_uid,session_id,ts,trigger,source_id,byte_offset) "
            "VALUES ('claude',%s,%s,%s,%s,0,%s)",
            (f"synthetic-compaction-{n}", root, AT + timedelta(minutes=offset), trigger, n),
        )
    worker = session(db, "synthetic-v4-worker", loop_id=run, method="lineage")
    reviewer = session(db, "synthetic-v4-reviewer", loop_id=run, method="lineage")
    other = session(db, "synthetic-v4-other", loop_id=run, method="lineage")
    for sid, name, role, status in (
        (worker, "lane-a", "worker", "complete"),
        (reviewer, "lane-b", "reviewer", "complete"),
        (other, "lane-c", "worker", "failed"),
    ):
        db.execute(
            "INSERT INTO ah.lane (loop_run_id,session_id,lane_name,role,link_method,return_status) "
            "VALUES (%s,%s,%s,%s,'lineage',%s)",
            (run, sid, name, role, status),
        )
    # (session, tool, outcome, timed_out, denial, duration_ms, minutes after the launch)
    tools = [
        (root, "AskUserQuestion", "ok", None, None, 1_800_000, 15),
        (root, "AskUserQuestion", "ok", None, None, 3_600_000, 16),
        (root, "AskUserQuestion", "ok", None, None, 60_000, -30),
        (root, "Bash", "timeout", True, None, 1000, 17),
        (worker, "Bash", "timeout", True, None, 1000, 18),
        (worker, "Bash", "timeout", True, None, 1000, 19),
        (worker, "Bash", "ok", False, None, 1000, 20),
        (other, "bash", "timeout", True, None, 1000, 21),
        (other, "Read", "timeout", True, None, 1000, 22),
        (root, "Edit", "denied", None, "user-rejected", 10, 23),
        (worker, "Bash", "denied", None, "user-rejected", 10, 24),
        (worker, "Bash", "denied", None, "automode-blocked", 10, 25),
        (worker, "Write", "denied", None, None, 10, 26),
    ]
    for n, (sid, name, outcome, timed_out, denial, ms, offset) in enumerate(tools):
        db.execute(
            "INSERT INTO ah.tool_call (agent,call_uid,session_id,tool_name,started_at,duration_ms,outcome,"
            "timed_out,denial_kind,source_id,byte_offset) VALUES ('claude',%s,%s,%s,%s,%s,%s,%s,%s,0,%s)",
            (f"synthetic-tool-{n}", sid, name, AT + timedelta(minutes=offset), ms, outcome, timed_out, denial, n),
        )
    db.commit()
    monkeypatch.setenv("AGENT_HISTORY_READER_DSN", READER)
    assert reader.main(["loop", CLOSEOUT, "--usage"]) == 0
    out = capsys.readouterr().out
    lines = out.splitlines()
    assert "- Harness: claude 2.1.4" in lines
    assert "- Landed: 3 tasks (from the closeout)" in lines and "- Parked: 1 task (from the closeout)" in lines
    assert "- Root calls: 2" in lines
    assert "- Root calls per landed task: 0.7" in lines
    assert "- Peak root context: 1,500 tokens" in lines
    # Two automatic and one typed compaction inside the window, one with no trigger; the early one is out.
    assert "- Compactions: 4 (1 typed by the owner, 1 with no trigger recorded)" in lines
    assert "- Root question-tool calls: 2 (blocked 1.50 hours)" in lines
    # Lane shell timeouts only: not the root's, not a non-shell tool's.
    assert "- Lane Bash timeouts: 3" in lines
    at = lines.index("| Lane role | Lanes | Complete | Complete rate |")
    assert lines[at + 2 : at + 4] == ["| reviewer | 1 | 1 | 100% |", "| worker | 2 | 1 | 50% |"]
    at = lines.index("| Denial class | Denials |")
    assert lines[at + 2 : at + 5] == ["| user-rejected | 2 |", "| automode-blocked | 1 |", "| denied | 1 |"]
    assert "| Role | Cost | Share of cost |" in lines
    assert "secret" not in out

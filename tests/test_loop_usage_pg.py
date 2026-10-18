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
    assert lines[8:] == [
        "",
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

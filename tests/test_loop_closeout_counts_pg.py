"""A v3 run finished by its closeout takes its delivery counts from that closeout's collected integers."""

import os
from datetime import timedelta

import pytest

from agent_history import load
from test_loop_identity_pg import conn  # noqa: F401 - real disposable catalogue fixture
from test_loop_identity_v3 import AT, CLOSEOUT, DIGEST, HEADER
from test_loop_launch_v3_pg import transcript

DSN = os.environ.get("AGENT_HISTORY_TEST_DSN", "")
pytestmark = pytest.mark.skipif(not DSN or "agent_history_test" not in DSN, reason="scratch database not set")
SLUG = "github.com/example/project"
COLLECTOR = ("loop_run_metadata", "loop_receipt", "backlog_done_event", "backlog_done_scan", "git_commit")


def wipe(db):
    for table in COLLECTOR:
        db.execute(f"DELETE FROM ah.{table}")
    db.execute("DELETE FROM ah.meta WHERE key='loops_identity_v3_projection_v1'")
    db.commit()


@pytest.fixture
def clean(conn):  # noqa: F811
    conn.execute("TRUNCATE " + ", ".join(f"ah.{table}" for table in load.DATA_TABLES) + " RESTART IDENTITY CASCADE")
    wipe(conn)
    yield conn
    conn.rollback()
    wipe(conn)


def observe(db, landed, parked, *, machine="machine-a", sha="b" * 64, when=None):
    db.execute(
        "INSERT INTO ah.loop_run_metadata(machine,path,kind,sha256,line1,file_mtime,repo_origin,landed_count,parked_count) "
        "VALUES (%s,%s,'closeout',%s,%s,%s,'example/project',%s,%s)",
        (machine, CLOSEOUT, sha, HEADER, when or AT + timedelta(minutes=1), landed, parked),
    )
    db.commit()


def fallbacks(db):
    """Three Done flips and two Task trailers in the run window: today's competing counts."""
    db.execute("INSERT INTO ah.backlog_done_scan (repo_slug,widened_at) VALUES (%s,now())", (SLUG,))
    for n, task in enumerate(("ABC-0001", "ABC-0002", "ABC-0003")):
        db.execute(
            "INSERT INTO ah.backlog_done_event (repo_slug,task_key,sha,done_at,from_status) VALUES (%s,%s,%s,%s,'To Do')",
            (SLUG, task, str(n) * 40, AT + timedelta(seconds=n + 1)),
        )
    db.execute(
        "INSERT INTO ah.git_commit (repo_slug,sha,context,committed_at,author_is_owner,subject,parent_count,"
        "files_changed,insertions,deletions,on_default,task_ids) VALUES (%s,%s,'default',%s,true,'s',1,0,0,0,true,%s)",
        ("machine/example/project", "c" * 40, AT + timedelta(seconds=5), ["ABC-0001", "ABC-0002"]),
    )
    db.commit()


def refresh(db, sources=None):
    if sources is not None:
        assert load.refresh(db, sources=sources, textfile=None, log=lambda *_: None).errors == 0
    else:
        load.post_passes(db)
        db.commit()


def counts(db):
    return db.execute(
        "SELECT l.tasks_landed,l.tasks_done,l.parks_total,l.status,r.end_evidence "
        "FROM ah.loops l JOIN ah.loop_run r USING(launch_uid) ORDER BY r.launch_ts"
    ).fetchall()


def test_counted_closeout_takes_precedence_and_survives_every_pass(clean, tmp_path):
    transcript(tmp_path, "claude")
    refresh(clean, {"claude-test": tmp_path})
    fallbacks(clean)
    # An old collector row without counts finishes the run but keeps today's counts.
    observe(clean, None, None)
    refresh(clean)
    assert counts(clean) == [(2, 3, None, "finished", "closeout")]
    clean.execute("UPDATE ah.loop_run_metadata SET landed_count=7,parked_count=0")
    clean.commit()
    for _ in range(3):
        refresh(clean)
        assert counts(clean) == [(7, 7, 0, "finished", "closeout")]
    # A machine copy of the same snapshot, collected before counts existed, does not change them.
    observe(clean, None, None, machine="machine-b")
    refresh(clean)
    assert counts(clean) == [(7, 7, 0, "finished", "closeout")]
    # Only the Landed section present: parks keep their other evidence (none here).
    clean.execute("UPDATE ah.loop_run_metadata SET landed_count=4,parked_count=NULL")
    clean.commit()
    refresh(clean)
    assert counts(clean) == [(4, 4, None, "finished", "closeout")]


def test_invalidated_closeout_restores_the_fallbacks(clean, tmp_path):
    transcript(tmp_path, "claude")
    refresh(clean, {"claude-test": tmp_path})
    fallbacks(clean)
    refresh(clean)
    (baseline,) = counts(clean)
    observe(clean, 9, 2)
    refresh(clean)
    assert counts(clean) == [(9, 9, 2, "finished", "closeout")]
    # A conflicting snapshot revokes the closeout end, and with it the closeout counts.
    observe(clean, 1, 1, machine="machine-b", sha="d" * 64)
    refresh(clean)
    (row,) = counts(clean)
    assert row[4] == "root_last_event" and row[3] != "finished"
    assert row == baseline


def test_closeout_outside_the_window_is_not_counted(clean, tmp_path):
    transcript(tmp_path, "claude")
    refresh(clean, {"claude-test": tmp_path})
    fallbacks(clean)
    refresh(clean)
    baseline = counts(clean)
    observe(clean, 9, 2, when=AT - timedelta(seconds=1))
    refresh(clean)
    assert counts(clean) == baseline and baseline[0][3] != "finished"


def test_v2_run_is_untouched_by_closeout_counts(clean, tmp_path):
    prompt = f"You are the root. Write codex/report-synthetic-loop1.md.\n# Loop: example/project loop1 · Goal: {DIGEST}"
    transcript(tmp_path, "claude", prompt)
    refresh(clean, {"claude-test": tmp_path})
    assert clean.execute("SELECT mode IS DISTINCT FROM 'v3' FROM ah.loop_run").fetchone() == (True,)
    fallbacks(clean)
    # Even a forged closeout end and metadata counted at the run's exact terminal path change nothing.
    clean.execute("UPDATE ah.loop_run SET end_evidence='closeout',end_ts=%s", (AT + timedelta(minutes=1),))
    clean.commit()
    refresh(clean)
    before = counts(clean)
    assert before[0][4] == "closeout" and before[0][1] == 3
    path = clean.execute("SELECT report_path FROM ah.loop_run").fetchone()[0]
    assert path.endswith("/codex/report-synthetic-loop1.md")
    clean.execute(
        "INSERT INTO ah.loop_run_metadata(machine,path,kind,sha256,line1,file_mtime,repo_origin,landed_count,parked_count) "
        "VALUES ('machine-a',%s,'closeout',%s,%s,%s,'example/project',40,5)",
        (path, "b" * 64, HEADER, AT + timedelta(minutes=1)),
    )
    clean.commit()
    refresh(clean)
    assert counts(clean) == before


def test_collected_closeout_file_counts_reach_the_run(clean, tmp_path):
    import hashlib

    from agent_history import collect_loop_metadata
    from test_loop_links_pg import ROOT, assistant, header, message, write
    from test_loop_outcomes import AT as STARTED
    from test_loop_v3_integrated_pg import git, ingest

    repo = tmp_path / "catalogue"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "remote", "add", "origin", "https://github.com/example/catalogue.git")
    codex = repo / "codex"
    codex.mkdir()
    charter = codex / "charter-synthetic-loop14.md"
    charter.write_text("Synthetic integration charter.\n")
    os.utime(charter, (STARTED.timestamp() - 60, STARTED.timestamp() - 60))
    digest = hashlib.sha256(charter.read_bytes()).hexdigest()
    prompt = "You are the root. Read codex/charter-synthetic-loop14.md. Write codex/closeout-synthetic-loop14.md."
    activity = assistant("working", [{"type": "text", "text": "Working."}], stopReason="stop")
    activity["timestamp"] = activity["message"]["timestamp"] = (STARTED + timedelta(minutes=5)).isoformat()
    root = {**header(ROOT), "cwd": str(repo)}
    write(repo / "sessions/synthetic/root.jsonl", [root, message("launch", "user", prompt), activity])
    refresh(clean, {"pi-test": repo})
    closeout = codex / "closeout-synthetic-loop14.md"
    closeout.write_text(
        f"# Closeout: example/catalogue loop14 · Charter: {digest}\n\n## Landed\n\n"
        "| Task | SHA |\n| --- | --- |\n| ABC-0001 (synthetic) | `abc1234` |\n| ABC-0002.1 | `def5678` |\n\n"
        "## Parked\n\n- ABC-0003: needs the owner.\n\n## Notes\n\n- ABC-0004 was only discussed.\n"
    )
    at = (STARTED + timedelta(minutes=10)).timestamp()
    os.utime(closeout, (at, at))
    rows = list(collect_loop_metadata.metadata(repo, "integration"))
    assert {(r["kind"], r["landed_count"], r["parked_count"]) for r in rows} == {
        ("charter", None, None),
        ("closeout", 2, 1),
    }
    ingest(clean, "loop_run_metadata", collect_loop_metadata.COLS, collect_loop_metadata.KEY, rows)
    refresh(clean)
    assert counts(clean) == [(2, 2, 1, "finished", "closeout")]

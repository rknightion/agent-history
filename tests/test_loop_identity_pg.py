"""Exact loop identity across the public refresh/rebuild boundary."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone

import pytest

from agent_history import load

DSN = os.environ.get("AGENT_HISTORY_TEST_DSN", "")
psycopg = pytest.importorskip("psycopg")
TEST_DB = psycopg.conninfo.conninfo_to_dict(DSN).get("dbname") if DSN else None
pytestmark = pytest.mark.skipif(TEST_DB != "agent_history_test", reason="exact disposable catalogue required")
GOAL = "a" * 64
REPORT = "/tmp/synthetic-project/codex/report-synthetic-loop1.md"


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
    conn.commit()
    yield conn
    conn.rollback()


def index_launch(conn, tmp_path, identity):
    source = tmp_path / "sessions"
    project = source / "projects" / "synthetic-project"
    project.mkdir(parents=True)
    path = project / "11111111-1111-4111-8111-111111111111.jsonl"
    text = f"You are the root. Write codex/report-synthetic-loop1.md.\n{identity}"
    path.write_text(
        json.dumps(
            {
                "type": "user",
                "uuid": "synthetic-launch",
                "sessionId": "synthetic-identity-root",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "cwd": "/tmp/synthetic-project",
                "message": {"role": "user", "content": text},
            },
            separators=(",", ":"),
        )
        + "\n"
    )
    sources = {"claude-test": source}
    stats = load.refresh(conn, sources=sources, textfile=None, log=lambda *_: None)
    assert stats.errors == 0 and stats.files_parsed == 1
    return sources


def identity(conn):
    return conn.execute("SELECT repo, loop, goal_sha256 FROM ah.loops").fetchone()


def record_report(conn, text, *, success=True):
    root = conn.execute("SELECT id FROM ah.session").fetchone()[0]
    conn.execute(
        "INSERT INTO ah.tool_call (agent, call_uid, session_id, tool_name, started_at, ended_at, "
        "outcome, source_id, byte_offset) VALUES ('claude','identity-report',%s,'Write',now(),now(),%s,0,0)",
        (root, "ok" if success else "error"),
    )
    conn.execute(
        "INSERT INTO ah.tool_io (agent, io_uid, session_id, namespace, profile, kind, tool_name, call_uid, ts, "
        "input_text, source_id, byte_offset) "
        "VALUES ('claude','identity-report',%s,'claude-test','test','call','Write','identity-report',now(),%s,0,0)",
        (root, json.dumps({"file_path": REPORT, "content": text})),
    )
    conn.execute(
        "INSERT INTO ah.artifact (agent,event_uid,session_id,ts,kind,action,path,evidence_type,source_id,byte_offset) "
        "VALUES ('claude','identity-report-artifact',%s,now(),'file','write',%s,'tool',0,0)",
        (root, REPORT),
    )
    conn.execute("INSERT INTO ah.dirty_session (session_id) VALUES (%s) ON CONFLICT DO NOTHING", (root,))
    conn.commit()
    load.post_passes(conn)
    conn.commit()


def test_running_exact_launch_identity(clean, tmp_path):
    index_launch(clean, tmp_path, f"# Loop: example/project loop1 · Goal: {GOAL}")
    assert clean.execute("SELECT status FROM ah.loops").fetchone() == ("running",)
    assert identity(clean) == ("example/project", "loop1", GOAL)


def test_unknown_repo_preserves_exact_loop_and_goal_not_launch_hash(clean, tmp_path):
    index_launch(
        clean,
        tmp_path,
        f"Manifest:\n  {GOAL}  /tmp/synthetic-project/codex/goal-synthetic-loop1.md",
    )
    assert identity(clean) == (None, "loop1", GOAL)
    assert clean.execute("SELECT launch_sha256 FROM ah.loop_run").fetchone()[0] != GOAL


def test_no_manifest_never_uses_launch_hash_or_basename(clean, tmp_path):
    index_launch(clean, tmp_path, "")
    assert identity(clean) == (None, "loop1", None)


def test_finished_report_identity_matches_header_and_data(clean, tmp_path):
    index_launch(clean, tmp_path, "")
    data = {"repo": "example/project", "loop": "loop1", "goal_sha256": GOAL}
    report = f"# Loop: example/project loop1 · Goal: {GOAL}\n\n## Data\n```json\n{json.dumps(data)}\n```\n"
    record_report(clean, report)
    assert clean.execute("SELECT status FROM ah.loops").fetchone() == ("finished",)
    assert identity(clean) == (data["repo"], data["loop"], data["goal_sha256"])
    # Even a no-dirty scheduled pass must keep/re-project identity.
    assert load.post_passes(clean)["dirty_sessions"] == 0
    assert identity(clean) == (data["repo"], data["loop"], data["goal_sha256"])


@pytest.mark.parametrize("invalid", ["basename", "mismatch", "failed_write"])
def test_report_cannot_fabricate_identity(clean, tmp_path, invalid):
    index_launch(clean, tmp_path, "")
    repo = "project" if invalid == "basename" else "example/project"
    report = f"# Loop: {repo} loop1 · Goal: {GOAL}\n"
    if invalid == "mismatch":
        report += "\n## Data\n```json\n" + json.dumps({"repo": repo, "loop": "loop2", "goal_sha256": GOAL}) + "\n```\n"
    record_report(clean, report, success=invalid != "failed_write")
    expected = (None, "loop1", GOAL) if invalid == "basename" else (None, "loop1", None)
    assert identity(clean) == expected


def test_conflicting_goal_hash_does_not_erase_exact_repo(clean, tmp_path):
    index_launch(
        clean,
        tmp_path,
        f"# Loop: example/project loop1 · Goal: {GOAL}\n"
        + f"{'b' * 64}  /tmp/synthetic-project/codex/goal-synthetic-loop1.md",
    )
    assert identity(clean) == ("example/project", "loop1", None)


def test_data_heading_inside_example_is_not_a_data_section(clean, tmp_path):
    index_launch(clean, tmp_path, "")
    report = f"# Loop: example/project loop1 · Goal: {GOAL}\n\n```markdown\n## Data\n```\n"
    record_report(clean, report)
    assert identity(clean) == ("example/project", "loop1", GOAL)


def test_report_data_mismatch_with_cr_lines_is_unknown(clean, tmp_path):
    index_launch(clean, tmp_path, "")
    data = {"repo": "example/project", "loop": "loop2", "goal_sha256": GOAL}
    report = f"# Loop: example/project loop1 · Goal: {GOAL}\r\r## Data\r```json\r{json.dumps(data)}\r```\r"
    record_report(clean, report)
    assert identity(clean) == (None, "loop1", None)


def test_identity_rows_and_foreign_grant_survive_reapply_and_rebuild(clean, tmp_path):
    sources = index_launch(clean, tmp_path, f"# Loop: example/project loop1 · Goal: {GOAL}")
    with psycopg.connect(os.environ["AGENT_HISTORY_TEST_ADMIN_DSN"]) as admin:
        admin.execute("CREATE ROLE synthetic_identity_reader")
    try:
        clean.execute("GRANT SELECT ON ah.loops TO synthetic_identity_reader")
        clean.commit()
        before = clean.execute("SELECT launch_uid, repo, loop, goal_sha256 FROM ah.loops").fetchall()
        clean.commit()
        load.apply_schema(clean, force=True)
        clean.commit()
        assert clean.execute("SELECT launch_uid, repo, loop, goal_sha256 FROM ah.loops").fetchall() == before
        clean.commit()
        assert load.rebuild(clean, sources=sources, textfile=None, log=lambda *_: None).errors == 0
        assert clean.execute("SELECT launch_uid, repo, loop, goal_sha256 FROM ah.loops").fetchall() == before
        assert clean.execute(
            "SELECT has_table_privilege('synthetic_identity_reader','ah.loops','SELECT')"
        ).fetchone() == (True,)
    finally:
        clean.rollback()
        with psycopg.connect(os.environ["AGENT_HISTORY_TEST_ADMIN_DSN"]) as admin:
            admin.execute("DROP OWNED BY synthetic_identity_reader")
            admin.execute("DROP ROLE synthetic_identity_reader")

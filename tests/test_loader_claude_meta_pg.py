"""Real loader boundary for Claude subagent `*.meta.json` sidecars: whole-file snapshots rewritten in place."""

import json
import os
import shutil

import pytest

from agent_history import load

DSN = os.environ.get("AGENT_HISTORY_TEST_DSN", "")
pytestmark = pytest.mark.skipif(not DSN or "agent_history_test" not in DSN, reason="Disposable PostgreSQL DSN required")

SID = "11111111-1111-4111-8111-111111111111"
AGENT_ID = "a0000000000000009"


@pytest.fixture
def conn():
    connection = load.connect(DSN)
    load.apply_schema(connection, force=True)
    connection.execute("TRUNCATE " + ", ".join(f"ah.{t}" for t in load.DATA_TABLES) + " RESTART IDENTITY CASCADE")
    connection.commit()
    yield connection
    connection.close()


def write_sidecar(home, record, *, workflow=False):
    base = home / "projects" / "-home-tester-repo" / SID / "subagents"
    if workflow:
        base = base / "workflows" / "wf_abc"
    path = base / f"agent-{AGENT_ID}.meta.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(record))
    temporary.replace(path)
    return path


def refresh(conn, home):
    stats = load.refresh(conn, sources={"claude-toy": home}, textfile=None, log=lambda *_: None)
    conn.commit()
    return stats


def snapshot(conn):
    rows = conn.execute(
        "SELECT s.agent, s.session_uid, s.agent_id, s.spawn_depth, s.is_stub, m.agent_type, m.model, "
        "m.picked_effort_model, m.spawn_depth, m.request_shape, m.request_non_interactive, m.workflow_phase, "
        "m.unknown_keys, m.namespace FROM ah.subagent_meta m JOIN ah.session s ON s.id = m.session_id"
    ).fetchall()
    conn.commit()
    return rows


def test_sidecar_is_indexed_rewritten_in_place_and_skipped_when_unchanged(conn, tmp_path):
    home = tmp_path / "claude-toy"
    write_sidecar(
        home,
        {
            "agentType": "workflow-subagent",
            "workflowPhase": "Review",
            "spawnDepth": 1,
            "requestShape": "foreground",
            "requestNonInteractive": False,
            "model": "sonnet",
            "pickedEffortModel": "claude-opus-5-5",
            "futureKey": 1,
        },
        workflow=True,
    )
    assert refresh(conn, home).files_parsed == 1
    assert snapshot(conn) == [
        (
            "claude",
            SID,
            AGENT_ID,
            1,
            True,
            "workflow-subagent",
            "sonnet",
            "claude-opus-5-5",
            1,
            "foreground",
            False,
            "Review",
            ["futureKey"],
            "claude-toy",
        )
    ]
    assert not conn.execute("SELECT count(*) FROM ah.parse_issue").fetchone()[0]
    # Unchanged: skipped on its stat alone.
    assert refresh(conn, home).files_parsed == 0
    # The harness rewrites the file: the row is replaced, never duplicated, and a dropped key clears.
    write_sidecar(
        home,
        {
            "agentType": "workflow-subagent",
            "spawnDepth": 2,
            "requestShape": "background",
            "requestNonInteractive": True,
            "model": "opus",
            "finished": True,
        },
        workflow=True,
    )
    assert refresh(conn, home).files_parsed == 1
    assert snapshot(conn) == [
        (
            "claude",
            SID,
            AGENT_ID,
            2,
            True,
            "workflow-subagent",
            "opus",
            None,
            2,
            "background",
            True,
            None,
            [],
            "claude-toy",
        )
    ]
    row = conn.execute(
        "SELECT count(*), max(file_role), max(parser_version), max(status) FROM ah.source_file"
    ).fetchone()
    assert row == (1, "claude_subagent_meta", load.SUBAGENT_META_PARSER_VERSION, "indexed")
    assert "subagent_meta" in load.DATA_TABLES


def test_sidecar_joins_the_transcript_session_and_never_sets_its_type(conn, tmp_path):
    home = tmp_path / "claude-toy"
    write_sidecar(home, {"agentType": "general-purpose", "spawnDepth": 1})
    transcript = home / "projects" / "-home-tester-repo" / SID / "subagents" / f"agent-{AGENT_ID}.jsonl"
    transcript.write_text(
        json.dumps(
            {
                "type": "user",
                "sessionId": SID,
                "agentId": AGENT_ID,
                "uuid": "side-u",
                "isSidechain": True,
                "timestamp": "2026-10-01T10:00:00Z",
                "message": {"role": "user", "content": "Synthetic brief."},
            }
        )
        + "\n"
    )
    refresh(conn, home)
    sessions = conn.execute(
        "SELECT agent_id, spawn_depth, is_stub, agent_type FROM ah.session WHERE agent_id <> ''"
    ).fetchall()
    assert sessions == [(AGENT_ID, 1, False, None)]
    assert conn.execute("SELECT count(*) FROM ah.subagent_meta").fetchone()[0] == 1


@pytest.mark.parametrize("content", ["not json", "[1, 2]", "{" + '"a": "' + "x" * 70000 + '"}'])
def test_unusable_sidecar_is_skipped_without_rows(conn, tmp_path, content):
    home = tmp_path / "claude-toy"
    path = home / "projects" / "-home-tester-repo" / SID / "subagents" / f"agent-{AGENT_ID}.meta.json"
    path.parent.mkdir(parents=True)
    path.write_text(content)
    refresh(conn, home)
    assert conn.execute("SELECT count(*) FROM ah.subagent_meta").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM ah.source_file").fetchone()[0] == 0


def test_sidecar_tier_move_refreshes_its_source_row(conn, tmp_path, monkeypatch):
    hot, cold = tmp_path / "hot", tmp_path / "cold"
    write_sidecar(hot / "claude-toy", {"agentType": "general-purpose", "spawnDepth": 1})
    shutil.copytree(hot / "claude-toy", cold / "claude-toy")

    def archive_refresh():
        stats = load.refresh(conn, hot=hot, cold=cold, textfile=None, log=lambda *_: None)
        conn.commit()
        return stats

    def source():
        row = conn.execute("SELECT tier, available_hot, available_cold FROM ah.source_file").fetchone()
        conn.commit()
        return row

    monkeypatch.setattr(load, "cold_tier_ok", lambda *_args, **_kw: True)  # no archive receipts here
    archive_refresh()
    assert source() == ("hot", True, True)
    assert archive_refresh().files_parsed == 0  # unchanged in both tiers: stat skip
    shutil.rmtree(hot / "claude-toy")  # the hot copy ages out to the archive only
    archive_refresh()
    assert source() == ("cold", False, True)
    assert conn.execute("SELECT count(*) FROM ah.subagent_meta").fetchone()[0] == 1


def test_sidecar_with_escaped_nul_is_indexed(conn, tmp_path):
    home = tmp_path / "claude-toy"
    path = write_sidecar(home, {})
    path.write_text('{"description": "label\\u0000x", "model": "son\\u0000net", "new\\u0000Key": 1}')
    stats = refresh(conn, home)
    assert stats.errors == 0
    row = conn.execute("SELECT model, unknown_keys FROM ah.subagent_meta").fetchone()
    assert row == ("sonnet", ["newKey"])
    assert conn.execute("SELECT status FROM ah.source_file").fetchone() == ("indexed",)

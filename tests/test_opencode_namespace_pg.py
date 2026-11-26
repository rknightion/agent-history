"""An opencode-* namespace loads into the catalogue labelled 'opencode' (disposable database only)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from test_opencode_namespace import SID, spool

DSN = os.environ.get("AGENT_HISTORY_TEST_DSN", "")
pytestmark = pytest.mark.skipif(
    not DSN or "agent_history_test" not in DSN, reason="AGENT_HISTORY_TEST_DSN (a *_test database) not set"
)

psycopg = pytest.importorskip("psycopg")
from agent_history import load  # noqa: E402


@pytest.fixture
def conn():
    connection = load.connect(DSN)
    load.apply_schema(connection, force=True)
    connection.execute("TRUNCATE " + ", ".join(f"ah.{t}" for t in load.DATA_TABLES) + " RESTART IDENTITY CASCADE")
    connection.commit()
    yield connection
    connection.close()


def test_opencode_namespace_loads_with_agent_label(conn, tmp_path: Path):
    hot = tmp_path / "hot"
    spool(hot)
    result = load.refresh(conn, hot, None, log=lambda *_: None)
    assert result.errors == 0 and result.files_parsed == 1
    assert conn.execute("SELECT agent, namespace, file_role FROM ah.source_file").fetchall() == [
        ("opencode", "opencode-work", "main")
    ]
    assert conn.execute("SELECT agent, namespace, session_uid FROM ah.session").fetchall() == [
        ("opencode", "opencode-work", SID)
    ]
    assert conn.execute("SELECT DISTINCT agent FROM ah.message").fetchall() == [("opencode",)]
    assert conn.execute("SELECT count(*) FROM ah.llm_call WHERE agent = 'opencode'").fetchone() == (1,)
    assert conn.execute("SELECT count(*) FROM ah.tool_call WHERE agent = 'opencode'").fetchone() == (1,)
    assert conn.execute("SELECT count(*) FROM ah.parse_issue").fetchone() == (0,)
    again = load.refresh(conn, hot, None, log=lambda *_: None)
    assert again.files_parsed == 0 and again.errors == 0


# --- sub-agent linkage, sidecars and efficiency SQL for the opencode label ------------------------

import json  # noqa: E402

from test_opencode_namespace import PROJECT, base, line  # noqa: E402

CHILD = "a0000000000000042"


def family(home: Path, namespace: str, label: str) -> None:
    """A main session that spawns one subagent, its transcript and its meta sidecar."""
    root = home / namespace / "projects" / PROJECT
    (root / SID / "subagents").mkdir(parents=True)
    spawn = base(
        "assistant",
        "p-2",
        "2026-10-01T10:00:02.000Z",
        requestId="req_p2",
        message={
            "id": "msg_p2",
            "role": "assistant",
            "model": "synthetic-model",
            "content": [{"type": "tool_use", "id": "call_agent", "name": "Agent", "input": {"prompt": "go"}}],
            "usage": {"input_tokens": 10, "output_tokens": 3},
        },
    )
    done = base(
        "user",
        "p-3",
        "2026-10-01T10:00:09.000Z",
        toolUseResult={"status": "completed", "agentId": CHILD},
        message={"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call_agent", "content": "ok"}]},
    )
    prompt = base("user", "p-1", "2026-10-01T10:00:00.000Z", message={"role": "user", "content": "hello"})
    (root / f"{SID}.jsonl").write_text("".join(line(r) for r in (prompt, spawn, done)))
    child = [
        base(
            "user",
            "c-1",
            "2026-10-01T10:00:03.000Z",
            isSidechain=True,
            agentId=CHILD,
            message={"role": "user", "content": "child brief"},
        ),
        base(
            "assistant",
            "c-2",
            "2026-10-01T10:00:05.000Z",
            isSidechain=True,
            agentId=CHILD,
            requestId="req_c2",
            message={
                "id": "msg_c2",
                "role": "assistant",
                "model": "synthetic-model",
                "content": [{"type": "text", "text": "done"}],
                "usage": {"input_tokens": 5, "output_tokens": 2},
            },
        ),
    ]
    (root / SID / "subagents" / f"agent-{CHILD}.jsonl").write_text("".join(line(r) for r in child))
    (root / SID / "subagents" / f"agent-{CHILD}.meta.json").write_text(
        json.dumps({"agentType": "general", "description": label})
    )


def test_subagents_link_to_parents_and_spawns_within_the_same_agent(conn, tmp_path: Path):
    hot = tmp_path / "hot"
    family(hot, "opencode-work", "oc")
    family(hot, "claude-toy", "cl")  # same session ids under the Claude label must stay apart
    result = load.refresh(conn, hot, None, log=lambda *_: None)
    assert result.errors == 0
    rows = conn.execute(
        "SELECT c.agent, c.agent_id, p.agent, p.agent_id, r.agent, r.agent_id FROM ah.session c "
        "JOIN ah.session p ON p.id = c.parent_session_id JOIN ah.session r ON r.id = c.root_session_id "
        "WHERE c.agent_id <> '' ORDER BY c.agent"
    ).fetchall()
    assert rows == [("claude", CHILD, "claude", "", "claude", ""), ("opencode", CHILD, "opencode", "", "opencode", "")]
    spawns = conn.execute(
        "SELECT s.agent, c.agent FROM ah.subagent_spawn s JOIN ah.session c ON c.id = s.child_session_id "
        "ORDER BY s.agent"
    ).fetchall()
    assert spawns == [("claude", "claude"), ("opencode", "opencode")]


def test_sidecar_attaches_to_the_opencode_child_not_a_claude_stub(conn, tmp_path: Path):
    hot = tmp_path / "hot"
    family(hot, "opencode-work", "oc")
    assert load.refresh(conn, hot, None, log=lambda *_: None).errors == 0
    meta = conn.execute(
        "SELECT s.agent, s.is_stub, m.namespace FROM ah.subagent_meta m JOIN ah.session s ON s.id = m.session_id"
    ).fetchall()
    assert meta == [("opencode", False, "opencode-work")]
    assert conn.execute("SELECT count(*) FROM ah.session WHERE agent = 'claude'").fetchone() == (0,)


def test_efficiency_sql_classifies_opencode_like_claude(conn):
    args = ("TaskOutput", "{}", json.dumps({"task": {"status": "running"}}))
    got = conn.execute(
        "SELECT ah.eff_result('opencode', %s, %s, %s), ah.eff_result('claude', %s, %s, %s)", (*args, *args)
    ).fetchone()
    assert got == ("wait", "wait")


def test_efficiency_calls_classify_opencode_triggers_like_claude(conn, tmp_path: Path):
    def call(uuid, ts, content, tool_id=None):
        return base(
            "assistant",
            uuid,
            ts,
            requestId="req_" + uuid,
            message={
                "id": "msg_" + uuid,
                "role": "assistant",
                "model": "synthetic-model",
                "content": content,
                "usage": {"input_tokens": 10, "output_tokens": 3},
            },
        )

    records = [
        base("user", "e-1", "2026-10-01T10:00:00.000Z", message={"role": "user", "content": "go"}),
        call("e-2", "2026-10-01T10:00:02.000Z", [{"type": "tool_use", "id": "t1", "name": "TaskOutput", "input": {}}]),
        base(
            "user",
            "e-3",
            "2026-10-01T10:00:03.000Z",
            toolUseResult={"task": {"status": "running"}},
            message={"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "still"}]},
        ),
        call("e-4", "2026-10-01T10:00:05.000Z", [{"type": "text", "text": "waiting"}]),
    ]
    target = tmp_path / "hot" / "opencode-work" / "projects" / PROJECT
    target.mkdir(parents=True)
    (target / f"{SID}.jsonl").write_text("".join(line(r) for r in records))
    load.refresh(conn, tmp_path / "hot", None, log=lambda *_: None)
    rows = conn.execute("SELECT agent, trigger FROM ah.efficiency_calls(ARRAY['opencode-work']) ORDER BY ts").fetchall()
    assert rows == [("opencode", "user"), ("opencode", "wait")]

"""The SQL efficiency classifier (sql/efficiency.sql) over the synthetic pi fixtures.

Each expected trigger follows from the definitions in efficiency.sql: the event that last changed
the agent's state before the call. Skipped unless AGENT_HISTORY_TEST_DSN names a *_test database.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from test_loader_pg import DSN, clean, conn  # noqa: F401  (fixtures)

pytestmark = pytest.mark.skipif(
    not DSN or "agent_history_test" not in DSN, reason="AGENT_HISTORY_TEST_DSN (a *_test database) not set"
)

from agent_history import load  # noqa: E402
from agent_history.efficiency import classify_tool_result  # noqa: E402


@pytest.mark.parametrize(
    "agent,name,args,result",
    [
        ("codex", "functions.wait_agent", {"timeout_ms": 20}, {"timed_out": False}),
        ("codex", "functions.spawn_agent", {"agent_type": "worker"}, {}),
        ("codex", "functions.exec_command", {"other": "x" * 100}, {}),
        ("codex", "functions.exec_command", {"command": "sleep 60"}, {"exit_code": 0}),
        ("codex", "functions.exec_command", "sleep 60\n" + "x" * 80, '{"exit_code": 0}'),
        ("claude", "Bash", {"command": "git status"}, {}),
        ("claude", "TaskOutput", {"task_id": "x"}, {"status": "running"}),
    ],
)
def test_sql_tool_classifier_matches_public_python(conn, agent, name, args, result):  # noqa: F811
    try:
        actual = conn.execute(
            "SELECT ah.eff_result(%s, %s, %s, %s)",
            (
                agent,
                name,
                args if isinstance(args, str) else json.dumps(args),
                result if isinstance(result, str) else json.dumps(result),
            ),
        ).fetchone()[0]
    finally:
        conn.rollback()
    assert actual == classify_tool_result(agent, name, args, result)


FIX = Path(__file__).parent / "fixtures" / "pi"
ASYNC = "01900000-0000-7000-8000-00000000a001"
FAILED = "01900000-0000-7000-8000-00000000a002"
LISTED = "01900000-0000-7000-8000-00000000a003"
LOOP = "01900000-0000-7000-8000-00000000a004"
MAPPER = "01900000-0000-7000-8000-00000000b001"
WORKER = "01900000-0000-7000-8000-00000000b002"


def calls(conn, session_uid: str) -> list[tuple[str, str, str]]:  # noqa: F811
    rows = conn.execute(
        "SELECT response_id, role, trigger FROM ah.efficiency_calls(NULL, %s, '') ORDER BY ts, byte_offset",
        (session_uid,),
    ).fetchall()
    conn.rollback()
    return rows


@pytest.fixture
def loaded(clean, tmp_path):  # noqa: F811
    shutil.copytree(FIX, tmp_path / "pi-local")
    stats = load.refresh(
        clean, None, None, sources={"pi-local": tmp_path / "pi-local"}, textfile=None, log=lambda *_: None
    )
    assert stats.errors == 0
    return clean


def test_async_root_triggers(loaded):
    assert calls(loaded, ASYNC) == [
        ("resp_root_01", "root", "user"),  # the human prompt
        ("resp_root_02", "root", "status"),  # result of `git status`
        ("resp_root_03", "root", "agent_msg"),  # a child notice arrived after the workflow result
        ("resp_root_04", "root", "wait"),  # a subagent wait that returned no results
        ("resp_root_05", "root", "agent_msg"),  # woken by the completion notice
        ("resp_root_06", "root", "event"),  # a subagent wait that delivered results
        ("resp_root_07", "root", "model"),  # retry straight after a failed model call
    ]


def test_result_physical_offset_beats_recorded_timestamp(loaded):
    # The status result is physically between the prompt and the second model call, even if its
    # recorded timestamp predates both. Timestamp-first SQL would incorrectly report `user`.
    loaded.execute(
        "UPDATE ah.tool_io SET output_at = '2000-01-01'::timestamptz "
        "WHERE call_uid IN (SELECT call_uid FROM ah.tool_call "
        "WHERE session_id = (SELECT id FROM ah.session WHERE session_uid = %s AND agent_id = '') "
        "AND tool_name = 'bash')",
        (ASYNC,),
    )
    assert calls(loaded, ASYNC)[1] == ("resp_root_02", "root", "status")


def test_claude_ignores_attachment_notification_before_call(loaded):
    session_id = loaded.execute(
        "SELECT id FROM ah.session WHERE session_uid = %s AND agent_id = ''", (ASYNC,)
    ).fetchone()[0]
    first_call = loaded.execute(
        "SELECT id, byte_offset FROM ah.llm_call WHERE session_id = %s AND response_id = 'resp_root_01'",
        (session_id,),
    ).fetchone()
    loaded.execute("UPDATE ah.session SET agent = 'claude' WHERE id = %s", (session_id,))
    loaded.execute(
        "UPDATE ah.message SET raw_record_origin = 'user' WHERE session_id = %s AND message_class = 'human_prompt'",
        (session_id,),
    )
    loaded.execute(
        "INSERT INTO ah.message (agent, event_uid, session_id, namespace, profile, ts, role, "
        "message_class, text, content_sha256, byte_offset, byte_length, line_number, turn_key, "
        "is_sidechain, source_id, raw_record_origin, detail) "
        "SELECT agent, 'synthetic-attachment-notification', session_id, namespace, profile, ts, "
        "role, 'agent_message', '<task-notification>synthetic</task-notification>', content_sha256, "
        "%s, byte_length, line_number, turn_key, is_sidechain, source_id, 'attachment', "
        '\'{"source":"task-notification"}\'::jsonb '
        "FROM ah.message WHERE session_id = %s AND message_class = 'human_prompt' LIMIT 1",
        (first_call[1] - 1, session_id),
    )
    assert (
        loaded.execute(
            "SELECT trigger FROM ah.efficiency_calls(NULL, %s, '') WHERE llm_call_id = %s", (ASYNC, first_call[0])
        ).fetchone()[0]
        == "user"
    )
    loaded.rollback()


def test_children_are_workers(loaded):
    assert calls(loaded, MAPPER) == [("resp_mapper_01", "worker", "user"), ("resp_mapper_02", "worker", "work")]
    # a parent message after the tool result is the latest event before the second call
    assert calls(loaded, WORKER) == [("resp_worker_01", "worker", "user"), ("resp_worker_02", "worker", "user")]


def test_loop_root_injected_events(loaded):
    assert [t for _, _, t in calls(loaded, LOOP)] == [
        "user",
        "work",
        "orchestrate",
        "event",
        "orchestrate",
        "event",
        "orchestrate",
        "user",
    ]


def test_error_retries_and_status_list(loaded):
    assert [t for _, _, t in calls(loaded, FAILED)] == ["user", "model", "model", "model", "model"]
    assert [t for _, _, t in calls(loaded, LISTED)] == ["user", "status"]


def test_usage_and_compaction_rows_are_not_calls(loaded):
    total = loaded.execute(
        "SELECT count(*) FROM ah.llm_call l JOIN ah.session s ON s.id = l.session_id WHERE s.session_uid = %s", (LOOP,)
    ).fetchone()[0]
    assert total == 9 and len(calls(loaded, LOOP)) == 8  # one row is the compaction's usage


def test_per_session_totals(loaded):
    row = loaded.execute(
        "SELECT calls, user_calls, status_calls, wait_calls, event_calls, agent_msg_calls, "
        "model_calls, unclassified_calls, poll_share FROM ah.efficiency(NULL, %s, '')",
        (ASYNC,),
    ).fetchone()
    loaded.rollback()
    assert row[:8] == (7, 1, 1, 1, 1, 2, 1, 0)
    assert float(row[8]) == pytest.approx(2 / 7, abs=1e-4)


def test_namespace_filter(loaded):
    assert loaded.execute("SELECT count(*) FROM ah.efficiency_calls(ARRAY['claude-local'])").fetchone()[0] == 0
    assert loaded.execute("SELECT count(*) FROM ah.efficiency_calls(ARRAY['pi-local'])").fetchone()[0] == 26
    loaded.rollback()


@pytest.mark.parametrize(
    "command, expected",
    [
        ("", "noop"),
        ("ls", "noop"),
        ("tools.sleep(5)", "wait"),
        ('tools.exec({cmd: "git status"})', "status"),
        ('tools.exec({cmd: "git status"}); tools.exec({cmd: "make"})', "work"),
        ("tools.gh run watch 42", "wait"),
        ("tools.until [ -f done ]; do sleep 5; done", "wait"),
        ("tools.backlog task list --plain", "status"),
        ("tools.cat codex/state-loop3.json", "status"),
        ("tools.npm test", "work"),
    ],
)
def test_cls_exec(conn, command, expected):  # noqa: F811
    assert conn.execute("SELECT ah.eff_cls_exec(%s)", (command,)).fetchone()[0] == expected
    conn.rollback()


@pytest.mark.parametrize(
    "tool, arguments, result, expected",
    [
        ("subagent", {"action": "status"}, None, "status"),
        ("subagent", {"action": "wait"}, {"results": [{"agent": "x"}]}, "event"),
        ("subagent", {"action": "wait"}, {"results": []}, "wait"),
        ("subagent", {"tasks": [{"agent": "x", "task": "y"}]}, None, "orchestrate"),
        ("subagent", {"workflowScript": None}, None, "work"),
        ("wake_at", {}, None, "orchestrate"),
        ("read", {"path": "x"}, None, "work"),
    ],
)
def test_pi_result_classes(conn, tool, arguments, result, expected):  # noqa: F811
    got = conn.execute(
        "SELECT ah.eff_pi_result(%s, %s, %s)",
        (tool, json.dumps(arguments), json.dumps(result) if result is not None else None),
    ).fetchone()[0]
    conn.rollback()
    assert got == expected

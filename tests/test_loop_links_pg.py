"""SQL-ready linkage reproductions through the real refresh path and synthetic pi transcripts."""

from __future__ import annotations

import json
import os

import pytest

from agent_history import load

DSN = os.environ.get("AGENT_HISTORY_TEST_DSN", "")
pytestmark = pytest.mark.skipif(not DSN or "agent_history_test" not in DSN, reason="scratch database not set")


@pytest.fixture
def conn():
    conn = load.connect(DSN)
    load.apply_schema(conn, force=True)
    conn.execute("TRUNCATE " + ", ".join(f"ah.{t}" for t in load.DATA_TABLES) + " RESTART IDENTITY CASCADE")
    conn.execute("DELETE FROM ah.meta WHERE key LIKE 'loops_links_projection_v1%%'")
    conn.commit()
    yield conn
    conn.close()


ROOT = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
RUN = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
CHILD = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
AT = "2026-10-04T12:00:00Z"
CWD = "/tmp/synthetic-project"
LAUNCH = CWD + "/codex/launch-2026-10-04-loop14.txt"
REPORT = CWD + "/codex/report-2026-10-04-loop14.md"
PROMPT = "You are the root. Write " + REPORT + " as the terminal action."


def header(uid):
    return {"type": "session", "id": uid, "timestamp": AT, "cwd": CWD, "version": 3}


def message(uid, role, content, **fields):
    return {
        "type": "message",
        "id": uid,
        "timestamp": AT,
        "message": {"role": role, "content": content, "timestamp": AT, **fields},
    }


def assistant(uid, content, **fields):
    return message(
        uid,
        "assistant",
        content,
        model="test",
        responseId=uid,
        usage={"input": 1, "output": 1, "cacheRead": 0, "cacheWrite": 0},
        **fields,
    )


def write(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records))


def fixture(source, *, bare=True, role="lane-worker-low", direct_child=True):
    base = source / "sessions" / "slug"
    root_base = f"2026-10-04T12-00-00Z_{ROOT}"
    write(
        base / (root_base + ".jsonl"),
        [
            header(ROOT),
            message("launch", "user", LAUNCH if bare else PROMPT),
            assistant(
                "read-call",
                [{"type": "toolCall", "id": "read-launch", "name": "read", "arguments": {"path": LAUNCH}}],
                stopReason="toolUse",
            ),
            message(
                "read-result",
                "toolResult",
                [{"type": "text", "text": PROMPT}],
                toolCallId="read-launch",
                toolName="read",
                isError=False,
            ),
            assistant("done", [], stopReason="stop"),
        ],
    )
    child_path = base / root_base / RUN
    if not direct_child:
        child_path /= "run-0"
    write(
        child_path / "session.jsonl",
        [
            header(CHILD),
            message("brief", "user", "Lane: synthetic"),
            assistant(
                "child-response",
                [{"type": "text", "text": '```lane-return\n{"v":2,"lane":"synthetic","status":"complete"}\n```'}],
                stopReason="stop",
            ),
        ],
    )
    write(
        base / "subagent-artifacts" / f"{RUN}_{role}_transcript.jsonl",
        [{"version": 1, "runId": RUN, "agent": role, "message": {"responseId": "child-response"}}],
    )
    return base, root_base


def refresh(conn, source):
    stats = load.refresh(conn, sources={"pi-test": source}, textfile=None, log=lambda *_: None)
    assert stats.errors == 0
    conn.commit()
    return stats


@pytest.mark.parametrize("direct_child", [True, False])
def test_bare_launch_resolves_and_lane_role_counts_reproduce_from_sql(conn, tmp_path, direct_child):
    fixture(tmp_path, direct_child=direct_child, role="security-reviewer")
    refresh(conn, tmp_path)
    assert conn.execute("SELECT status, report_path, loop_number FROM ah.loop_run").fetchall() == [
        ("resolved", REPORT, 14)
    ]
    assert conn.execute("SELECT role, count(*) FROM ah.lane GROUP BY role").fetchall() == [("security-reviewer", 1)]
    assert conn.execute("SELECT lanes_total, lanes_returned FROM ah.loops").fetchone() == (1, 1)


def test_low_role_and_nested_lanes_link_via_real_refresh(conn, tmp_path):
    base, root_base = fixture(tmp_path)
    nested_run = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
    nested_uid = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"
    write(
        base / root_base / RUN / "session" / nested_run / "run-0" / "session.jsonl",
        [header(nested_uid), assistant("nested-response", [], stopReason="stop")],
    )
    write(
        base / "subagent-artifacts" / f"{nested_run}_custom-reviewer_transcript.jsonl",
        [{"version": 1, "runId": nested_run, "agent": "custom-reviewer", "message": {"responseId": "nested-response"}}],
    )
    refresh(conn, tmp_path)
    assert conn.execute("SELECT role FROM ah.lane ORDER BY role").fetchall() == [
        ("custom-reviewer",),
        ("lane-worker-low",),
    ]
    assert conn.execute(
        "SELECT p.session_uid FROM ah.session c JOIN ah.session p ON p.id=c.parent_session_id WHERE c.session_uid = %s",
        (nested_uid,),
    ).fetchone() == (CHILD,)
    assert conn.execute("SELECT count(DISTINCT loop_run_id) FROM ah.session").fetchone() == (1,)


@pytest.mark.parametrize(
    "prompt,append",
    [(LAUNCH, False), (CWD + "/codex/goal-2026-10-04-loop14.md", False), ("Continue the interrupted root.", True)],
)
def test_relaunched_root_links_same_run_not_a_lane(conn, tmp_path, prompt, append):
    base, _ = fixture(tmp_path, bare=False)
    refresh(conn, tmp_path)
    resume_uid = "ffffffff-ffff-4fff-8fff-ffffffffffff"
    records = [header(resume_uid), message("resume", "user", prompt)]
    if append:
        records += [
            assistant(
                "append-call",
                [
                    {
                        "type": "toolCall",
                        "id": "append",
                        "name": "bash",
                        "arguments": {
                            "command": "loop-state append "
                            + CWD
                            + "/codex/state-2026-10-04-loop14.jsonl judgement text=continue"
                        },
                    }
                ],
                stopReason="toolUse",
            ),
            message("append-result", "toolResult", [], toolCallId="append", toolName="bash", isError=False),
        ]
    records.append(assistant("resume-done", [], stopReason="stop"))
    # Relaunch is later, independently of source inventory ordering.
    for r in records:
        r["timestamp"] = "2026-10-04T13:00:00Z"
        if "message" in r:
            r["message"]["timestamp"] = r["timestamp"]
    write(base / ("2026-10-04T13-00-00Z_" + resume_uid + ".jsonl"), records)
    refresh(conn, tmp_path)
    assert conn.execute("SELECT count(*) FROM ah.loop_run").fetchone() == (1,)
    assert conn.execute(
        "SELECT s.loop_link_method, r.session_uid FROM ah.session s "
        "JOIN ah.session r ON r.id=s.root_session_id WHERE s.session_uid=%s",
        (resume_uid,),
    ).fetchone() == ("relaunch", ROOT)
    assert conn.execute("SELECT count(*) FROM ah.lane").fetchone() == (1,)
    assert conn.execute("SELECT last_activity_at FROM ah.loops").fetchone()[0].hour == 13


def test_foreground_direct_child_without_artifact_and_historical_spawn_path(conn, tmp_path):
    base, root_base = fixture(tmp_path, bare=False)
    artifact = next((base / "subagent-artifacts").glob("*.jsonl"))
    artifact.unlink()
    path = base / (root_base + ".jsonl")
    records = [json.loads(line) for line in path.read_text().splitlines()]
    records += [
        assistant(
            "spawn-call",
            [
                {
                    "type": "toolCall",
                    "id": "spawn",
                    "name": "subagent",
                    "arguments": {"agent": "lane-worker-low", "task": "synthetic"},
                }
            ],
            stopReason="toolUse",
        ),
        message(
            "spawn-result",
            "toolResult",
            [],
            toolCallId="spawn",
            toolName="subagent",
            isError=False,
            details={
                "results": [
                    {
                        "agent": "lane-worker-low",
                        "index": 0,
                        "exitCode": 0,
                        "sessionFile": str(base / root_base / RUN / "session.jsonl"),
                    }
                ]
            },
        ),
    ]
    write(path, records)
    refresh(conn, tmp_path)
    assert conn.execute("SELECT role FROM ah.lane").fetchone() == ("lane-worker-low",)
    assert conn.execute("SELECT child_task_name FROM ah.subagent_spawn").fetchone() == (RUN,)
    conn.execute("UPDATE ah.subagent_spawn SET child_task_name=NULL")
    conn.execute("DELETE FROM ah.meta WHERE key='loops_links_projection_v1'")
    conn.commit()
    load.post_passes(conn)
    assert conn.execute("SELECT child_task_name FROM ah.subagent_spawn").fetchone() == (RUN,)


def test_captured_relative_report_uses_launch_file_base_not_session_cwd(conn, tmp_path):
    fixture(tmp_path, direct_child=False)
    base = tmp_path / "sessions" / "slug"
    root_path = next(base.glob("*.jsonl"))
    records = [json.loads(line) for line in root_path.read_text().splitlines()]
    records[0]["cwd"] = CWD + "/subdirectory"
    for record in records:
        msg = record.get("message", {})
        if msg.get("role") == "toolResult":
            msg["content"][0]["text"] = "You are the root. Write codex/report-2026-10-04-loop14.md."
    write(root_path, records)
    refresh(conn, tmp_path)
    assert conn.execute("SELECT status, report_path FROM ah.loop_run").fetchone() == ("resolved", REPORT)


def test_artifact_upgrade_repairs_retained_low_roles_without_content_rebuild(conn, tmp_path):
    fixture(tmp_path, direct_child=False)
    refresh(conn, tmp_path)
    original = conn.execute("SELECT count(*) FROM ah.message").fetchone()
    conn.execute("UPDATE ah.source_file SET parser_version='6' WHERE file_role='pi_artifact'")
    conn.execute("UPDATE ah.pi_run_response SET agent_type=NULL")
    conn.execute("UPDATE ah.session SET agent_type=NULL WHERE is_subagent")
    conn.execute("UPDATE ah.subagent_spawn SET requested_type=NULL, requested_type_source=NULL")
    conn.execute("UPDATE ah.lane SET role=NULL")
    conn.execute("DELETE FROM ah.dirty_session")
    conn.commit()
    stats = refresh(conn, tmp_path)
    assert stats.files_parsed == 1
    assert conn.execute("SELECT role FROM ah.lane").fetchone() == ("lane-worker-low",)
    assert conn.execute("SELECT count(*) FROM ah.message").fetchone() == original


def test_partial_or_failed_launch_reads_remain_unresolved(conn, tmp_path):
    fixture(tmp_path, direct_child=False)
    refresh(conn, tmp_path)
    conn.execute("UPDATE ah.tool_io SET output_truncated=true")
    conn.execute("INSERT INTO ah.dirty_session SELECT id FROM ah.session ON CONFLICT DO NOTHING")
    conn.commit()
    load.post_passes(conn)
    assert conn.execute("SELECT status FROM ah.loop_run").fetchone() == ("unresolved_path",)
    conn.execute("UPDATE ah.tool_io SET output_truncated=false")
    conn.execute("UPDATE ah.tool_call SET outcome='error'")
    conn.execute("INSERT INTO ah.dirty_session SELECT id FROM ah.session ON CONFLICT DO NOTHING")
    conn.commit()
    load.post_passes(conn)
    assert conn.execute("SELECT status FROM ah.loop_run").fetchone() == ("unresolved_path",)


def test_idle_backfill_is_bounded_and_repairs_retained_roles_and_paths(conn, tmp_path):
    fixture(tmp_path, direct_child=False)
    refresh(conn, tmp_path)
    conn.execute("UPDATE ah.loop_run SET status = 'unresolved_path'")
    conn.execute("UPDATE ah.lane SET role = NULL")
    conn.execute("DELETE FROM ah.dirty_session")
    conn.execute("DELETE FROM ah.meta WHERE key LIKE 'loops_links_projection_v1%%'")
    conn.commit()
    assert load.post_passes(conn)["dirty_sessions"] == 2
    assert conn.execute("SELECT status FROM ah.loop_run").fetchone() == ("resolved",)
    assert conn.execute("SELECT role FROM ah.lane").fetchone() == ("lane-worker-low",)
    conn.commit()
    assert load.post_passes(conn)["dirty_sessions"] == 0
    conn.execute("DELETE FROM ah.meta WHERE key LIKE 'loops_links_projection_v1%%'")
    conn.execute(
        "INSERT INTO ah.session (agent, session_uid, namespace) "
        "SELECT 'pi', 'synthetic-' || i, 'pi-test' FROM generate_series(1, 260) i"
    )
    conn.commit()
    assert load.post_passes(conn)["dirty_sessions"] == 128
    conn.commit()
    assert load.post_passes(conn)["dirty_sessions"] == 128
    conn.commit()
    assert load.post_passes(conn)["dirty_sessions"] == 6
    conn.commit()
    assert load.post_passes(conn)["dirty_sessions"] == 0

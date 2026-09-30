"""pi ingestion through the loader against a disposable ParadeDB database (see test_loader_pg.py).

Skipped unless AGENT_HISTORY_TEST_DSN names a *_test database. Needs the schema to accept
agent 'pi' in ah.source_file and ah.session (their agent CHECK constraints).
"""

from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

import pytest

from test_loader_pg import DSN, clean, conn  # noqa: F401  (fixtures)

pytestmark = pytest.mark.skipif(not DSN or "agent_history_test" not in DSN,
                                reason="AGENT_HISTORY_TEST_DSN (a *_test database) not set")

from agent_history import load  # noqa: E402

FIX = Path(__file__).parent / "fixtures" / "pi"
ROOT_UID = "01900000-0000-7000-8000-00000000a001"


def build(root: Path) -> tuple[Path, Path]:
    hot, cold = root / "hot", root / "cold"
    shutil.copytree(FIX, hot / "pi-local")
    (cold / ".archive-receipts").mkdir(parents=True)
    (cold / ".archive-receipts" / "receipt.json").write_text("{}")
    return hot, cold


def test_pi_tree_loads_links_children_and_tags_the_launch(clean, tmp_path):  # noqa: F811
    hot, cold = build(tmp_path)
    stats = load.refresh(clean, hot, cold, textfile=None, log=lambda *_: None)
    assert stats.errors == 0 and stats.files_parsed == 8
    rows = clean.execute(
        "SELECT c.agent_type, p.session_uid, r.session_uid, c.spawn_depth "
        "FROM ah.session c JOIN ah.session p ON p.id = c.parent_session_id "
        "JOIN ah.session r ON r.id = c.root_session_id "
        "WHERE c.agent = 'pi' AND c.is_subagent ORDER BY c.agent_path").fetchall()
    # The artifact transcripts pair each run id with its child's response id: an exact link that
    # also supplies the child type.
    assert rows == [("mapper", ROOT_UID, ROOT_UID, 1), ("lane-worker", ROOT_UID, ROOT_UID, 1)]
    assert clean.execute("SELECT count(*) FROM ah.subagent_spawn WHERE agent='pi' "
                         "AND child_session_id IS NOT NULL").fetchone()[0] == 2
    orphans = clean.execute("SELECT count(*) FROM ah.session WHERE agent = 'pi' AND NOT is_stub "
                            "AND root_session_id IS NULL").fetchone()[0]
    assert orphans == 0
    loop = clean.execute("SELECT loop_number, status FROM ah.loop_run").fetchall()
    assert loop == [(1, "resolved")]   # the loop root's launch prompt
    clean.rollback()


def test_pi_refresh_is_idempotent(clean, tmp_path):  # noqa: F811
    hot, cold = build(tmp_path)
    load.refresh(clean, hot, cold, textfile=None, log=lambda *_: None)
    counts = [clean.execute(f"SELECT count(*) FROM ah.{t}").fetchone()[0] for t in ("llm_call", "message", "turn")]
    time.sleep(0.01)
    load.refresh(clean, hot, cold, textfile=None, log=lambda *_: None)
    assert [clean.execute(f"SELECT count(*) FROM ah.{t}").fetchone()[0] for t in ("llm_call", "message", "turn")] == counts
    clean.rollback()


def test_async_pi_child_type_uses_exact_run_and_response_evidence(clean, tmp_path):  # noqa: F811
    hot, cold = tmp_path / "hot", tmp_path / "cold"
    base = hot / "pi-local" / "sessions" / "-synthetic-"
    base.mkdir(parents=True)
    (cold / ".archive-receipts").mkdir(parents=True)
    (cold / ".archive-receipts" / "receipt.json").write_text("{}")
    root_uid = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    runs = ("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb", "cccccccc-cccc-4ccc-8ccc-cccccccccccc")
    dirs = ("dddddddd-dddd-4ddd-8ddd-dddddddddddd", "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",
            "ffffffff-ffff-4fff-8fff-ffffffffffff")
    types = ("mapper", "lane-worker")
    root_base = f"2026-09-28T07-27-09-000Z_{root_uid}"
    at = "2026-09-28T07:27:09Z"

    def write(path, records):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(row) + "\n" for row in records))

    root_records = [{"type": "session", "id": root_uid, "timestamp": at, "cwd": "/tmp/synthetic", "version": 3}]
    for index, (run, directory, agent_type) in enumerate(zip(runs, dirs, types)):
        root_records.extend([
            {"type": "message", "id": f"a{index}", "timestamp": at,
             "message": {"role": "assistant", "timestamp": at, "model": "test", "responseId": f"root-{index}",
                         "stopReason": "toolUse", "usage": {"input": 1, "output": 1},
                         "content": [{"type": "toolCall", "id": f"call-{index}", "name": "subagent",
                                      "arguments": {"agent": agent_type, "task": "synthetic brief", "async": True}}]}},
            {"type": "message", "id": f"r{index}", "timestamp": at,
             "message": {"role": "toolResult", "timestamp": at, "toolCallId": f"call-{index}",
                         "toolName": "subagent", "isError": False, "content": [],
                         "details": {"mode": "async", "runId": run, "results": []}}},
            {"type": "custom_message", "id": f"n{index}", "timestamp": at,
             "customType": "subagent-notify",
             "content": (f"run {run} at /{directory}/run-0/session.jsonl"
                         if index == 0 else f"run {run} finished")},
        ])
    write(base / f"{root_base}.jsonl", root_records)
    for index, directory in enumerate(dirs):
        write(base / root_base / directory / "run-0" / "session.jsonl", [
            {"type": "session", "id": f"00000000-0000-4000-8000-00000000000{index}",
             "timestamp": at, "cwd": "/tmp/synthetic", "version": 3},
            {"type": "session_info", "id": f"i{index}", "timestamp": at,
             "name": "WIRE-HARNESS: display label"},
            {"type": "message", "id": f"m{index}", "timestamp": at,
             "message": {"role": "assistant", "timestamp": at, "model": "test",
                         "responseId": f"child-{index}", "stopReason": "stop",
                         "usage": {"input": 1, "output": 1}, "content": []}},
        ])
    for index, (run, agent_type) in enumerate(zip(runs, types)):
        write(base / "subagent-artifacts" / f"{run}_{agent_type}_transcript.jsonl", [
            {"version": 1, "recordType": "message", "runId": run, "agent": agent_type,
             "timestamp": at, "message": {"role": "assistant", "responseId": f"child-{index}"}},
        ])
    stats = load.refresh(clean, hot, cold, textfile=None, log=lambda *_: None)
    assert stats.errors == 0 and stats.files_parsed == 6
    rows = clean.execute("SELECT agent_type, agent_type_source FROM ah.session "
                         "WHERE agent='pi' AND spawn_kind='pi_subagent' ORDER BY agent_type NULLS LAST").fetchall()
    assert rows == [("lane-worker", "explicit"), ("mapper", "explicit"), (None, None)]
    assert clean.execute("SELECT count(*), count(child_session_id) FROM ah.subagent_spawn "
                         "WHERE agent='pi'").fetchone() == (2, 2)
    assert clean.execute("SELECT count(*) FROM ah.pi_run_response").fetchone()[0] == 2
    assert clean.execute("SELECT count(*) FROM ah.llm_call WHERE agent='pi'").fetchone()[0] == 5
    conflicting_run = "99999999-9999-4999-8999-999999999999"
    for agent_type in ("mapper", "reviewer"):
        write(base / "subagent-artifacts" / f"{conflicting_run}_{agent_type}_transcript.jsonl", [
            {"version": 1, "recordType": "message", "runId": conflicting_run, "agent": agent_type,
             "timestamp": at, "message": {"role": "assistant", "responseId": "child-2"}},
        ])
    assert load.refresh(clean, hot, cold, textfile=None, log=lambda *_: None).errors == 0
    assert clean.execute("SELECT count(*) FROM ah.session WHERE agent='pi' AND spawn_kind='pi_subagent' "
                         "AND agent_type IS NULL").fetchone()[0] == 1
    assert clean.execute("SELECT count(*) FROM ah.subagent_spawn WHERE agent='pi'").fetchone()[0] == 2
    assert clean.execute("SELECT count(*) FROM ah.dirty_session").fetchone()[0] == 0
    clean.rollback()

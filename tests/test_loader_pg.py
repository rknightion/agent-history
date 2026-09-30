"""Loader integration tests against a disposable Postgres database (ParadeDB image).

Skipped unless AGENT_HISTORY_TEST_DSN points at a scratch database the tests may TRUNCATE, whose
name contains `agent_history_test`. Never a catalogue you use.
Run: just ci (starts a disposable ParadeDB and sets AGENT_HISTORY_TEST_DSN).
"""

from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path

import pytest

DSN = os.environ.get("AGENT_HISTORY_TEST_DSN", "")
pytestmark = pytest.mark.skipif(not DSN or "agent_history_test" not in DSN,
                                reason="AGENT_HISTORY_TEST_DSN (a *_test database) not set")

psycopg = pytest.importorskip("psycopg")

from agent_history import load  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"
COUNTED = ("session", "turn", "message", "llm_call", "tool_call", "tool_op", "subagent_spawn", "git_event",
           "artifact", "session_event", "compaction", "hook_event", "cost_state", "rate_limit_sample")


@pytest.fixture(scope="module")
def conn():
    connection = load.connect(DSN)
    load.apply_schema(connection, force=True)
    connection.commit()
    yield connection
    connection.close()


@pytest.fixture
def clean(conn):
    conn.execute("TRUNCATE " + ", ".join(f"ah.{t}" for t in load.DATA_TABLES) + " RESTART IDENTITY CASCADE")
    conn.commit()
    return conn


def build_tree(root: Path) -> tuple[Path, Path]:
    hot, cold = root / "hot", root / "cold"
    shutil.copytree(FIXTURES / "claude" / "projects", hot / "claude-local" / "projects")
    target = hot / "codex-local" / "sessions" / "2026" / "09" / "25"
    target.mkdir(parents=True)
    for index, path in enumerate(sorted((FIXTURES / "codex").glob("*.jsonl"))):
        shutil.copy(path, target / f"rollout-2026-09-25T00-00-0{index}-00000000-0000-4000-8000-00000000000{index}.jsonl")
    (cold / ".archive-receipts").mkdir(parents=True)
    (cold / ".archive-receipts" / "receipt.json").write_text("{}")
    return hot, cold


def snapshot(conn) -> dict[str, tuple]:
    out = {}
    for table in COUNTED:
        out[table] = conn.execute(f"SELECT count(*) FROM ah.{table}").fetchone()
    out["tokens"] = conn.execute(
        "SELECT sum(input_uncached), sum(cache_read), sum(output), count(DISTINCT response_id) FROM ah.llm_call").fetchone()
    out["outcomes"] = tuple(conn.execute(
        "SELECT outcome, count(*) FROM ah.tool_call GROUP BY 1 ORDER BY 1 NULLS FIRST").fetchall())
    conn.rollback()
    return out


def refresh(conn, hot, cold, textfile=None):
    return load.refresh(conn, hot, cold, textfile=textfile, log=lambda *_: None)


def test_second_refresh_is_a_noop(clean, tmp_path):
    hot, cold = build_tree(tmp_path)
    first = refresh(clean, hot, cold)
    assert first.errors == 0 and first.files_parsed > 0
    before = snapshot(clean)
    assert before["message"][0] > 0 and before["llm_call"][0] > 0
    offsets = clean.execute("SELECT f.rel_path, i.byte_offset, i.output_byte_offset "
                            "FROM ah.tool_io i JOIN ah.source_file f ON f.id = i.source_id "
                            "WHERE i.output_at IS NOT NULL ORDER BY f.rel_path, i.output_byte_offset").fetchall()
    assert offsets and any(output > call for _, call, output in offsets)
    for rel_path, _, output in offsets:
        line = (hot / rel_path).read_bytes()[output:].split(b"\n", 1)[0]
        assert json.loads(line)  # result is anchored to a complete source record
    second = refresh(clean, hot, cold)
    assert second.files_parsed == 0 and second.errors == 0
    assert snapshot(clean) == before


def test_claude_notification_origin_and_physical_order_survive_load(clean, tmp_path):
    hot, cold = build_tree(tmp_path)
    main = next((hot / "claude-local").rglob("11111111-1111-4111-8111-111111111111.jsonl"))
    sid = "11111111-1111-4111-8111-111111111111"
    # The notification is appended after the call, despite carrying an earlier timestamp.
    call = {"type": "assistant", "sessionId": sid, "uuid": "order-call",
            "timestamp": "2026-09-20T10:01:00.000Z",
            "message": {"id": "msg-order", "role": "assistant", "model": "synthetic-model",
                        "content": [{"type": "text", "text": "call anchor"}]}}
    body = "<task-notification><task-id>synthetic-order</task-id><status>completed</status></task-notification>"
    attachment = {"type": "attachment", "sessionId": sid, "uuid": "order-attachment",
                  "timestamp": "2026-09-20T10:00:58.000Z",
                  "attachment": {"type": "queued_command", "commandMode": "task-notification",
                                 "prompt": body}}
    user = {"type": "user", "sessionId": sid, "uuid": "order-user",
            "timestamp": "2026-09-20T10:00:59.000Z", "promptId": "order-prompt",
            "origin": {"kind": "task-notification"},
            "message": {"role": "user", "content": body + " user mirror"}}
    with main.open("ab") as f:
        call_offset = f.tell()
        for rec in (call, attachment, user):
            f.write((json.dumps(rec) + "\n").encode())
    stats = refresh(clean, hot, cold)
    assert stats.errors == 0
    rows = clean.execute(
        "SELECT m.raw_record_origin, m.byte_offset, m.ts, m.detail->>'source', s.rel_path "
        "FROM ah.message m JOIN ah.source_file s ON s.id=m.source_id "
        "WHERE m.event_uid LIKE 'tn:synthetic-order:%:text' ORDER BY m.byte_offset"
    ).fetchall()
    assert len(rows) == 2
    call_row = clean.execute("SELECT byte_offset, ts FROM ah.llm_call "
                             "WHERE agent='claude' AND response_id='msg-order'").fetchone()
    assert call_row and call_row[0] == call_offset
    assert [r[0] for r in rows] == ["attachment", "user"]
    assert all(r[1] > call_row[0] and r[2] < call_row[1] for r in rows)
    assert all(r[3] == "task-notification" for r in rows)
    assert all(json.loads((hot / r[4]).read_bytes()[r[1]:].split(b"\n", 1)[0])["type"] == r[0]
               for r in rows)


def test_append_in_two_runs_equals_one_shot(clean, tmp_path):
    hot, cold = build_tree(tmp_path)
    refresh(clean, hot, cold)
    full = snapshot(clean)
    clean.execute("TRUNCATE " + ", ".join(f"ah.{t}" for t in load.DATA_TABLES) + " RESTART IDENTITY CASCADE")
    clean.commit()
    big = max(hot.rglob("*.jsonl"), key=lambda p: p.stat().st_size)
    data = big.read_bytes()
    cut = data.index(b"\n", len(data) // 2) + 1
    big.write_bytes(data[:cut])
    refresh(clean, hot, cold)
    with big.open("ab") as handle:
        handle.write(data[cut:])
    stats = refresh(clean, hot, cold)
    assert stats.errors == 0 and stats.files_parsed == 1 and stats.files_rewritten == 0
    assert snapshot(clean) == full


def test_rewritten_file_is_purged_and_reparsed(clean, tmp_path):
    hot, cold = build_tree(tmp_path)
    refresh(clean, hot, cold)
    full = snapshot(clean)
    big = max(hot.rglob("*.jsonl"), key=lambda p: p.stat().st_size)
    original = big.read_bytes()
    big.write_bytes(b'{"type":"noise"}\n' + original)          # prefix changed: not an append
    stats = refresh(clean, hot, cold)
    assert stats.files_rewritten == 1 and stats.errors == 0
    assert clean.execute("SELECT count(*) FROM ah.parse_issue WHERE kind='source_rewritten'").fetchone()[0] == 1
    big.write_bytes(original)                                     # and back: purged again, same rows
    refresh(clean, hot, cold)
    after = snapshot(clean)
    assert after == full


def test_one_failing_file_does_not_stop_the_run(clean, tmp_path, monkeypatch):
    hot, cold = build_tree(tmp_path)
    real = load.parser_for

    def broken(agent, role=None):
        cls, version = real(agent, role)
        if agent != "codex":
            return cls, version

        class Exploding(cls):
            def flush(self):
                raise RuntimeError("boom")
        return Exploding, version

    monkeypatch.setattr(load, "parser_for", broken)
    stats = refresh(clean, hot, cold)
    assert stats.errors >= 1
    assert clean.execute("SELECT count(*) FROM ah.source_file WHERE agent='claude' AND status='indexed'").fetchone()[0] > 0
    assert clean.execute("SELECT count(*) FROM ah.parse_issue WHERE kind='load_error'").fetchone()[0] >= 1
    clean.rollback()


def test_lock_held_keeps_previous_success(clean, tmp_path):
    hot, cold = build_tree(tmp_path)
    textfile = tmp_path / "metrics" / "agent-history.prom"
    textfile.parent.mkdir()
    textfile.write_text("agent_history_last_success_timestamp_seconds 1000\n")
    other = load.connect(DSN)
    try:
        assert load.try_lock(other)
        stats = refresh(clean, hot, cold, textfile)
        assert stats.lock_held and stats.files_parsed == 0
        assert "agent_history_last_success_timestamp_seconds 1000" in textfile.read_text()
    finally:
        load.unlock(other)
        other.close()
    stats = refresh(clean, hot, cold, textfile)
    stamp = float([l for l in textfile.read_text().splitlines()
                   if l.startswith("agent_history_last_success_timestamp_seconds")][0].split()[1])
    assert time.time() - stamp < 60


def test_parents_resolve_when_parent_arrives_later(clean, tmp_path):
    hot, cold = build_tree(tmp_path)
    mains = [p for p in (hot / "claude-local").rglob("*.jsonl") if "subagents" not in p.parts]
    hidden = {p: p.read_bytes() for p in mains}
    for p in mains:
        p.unlink()
    refresh(clean, hot, cold)                                   # children only
    for p, data in hidden.items():
        p.write_bytes(data)
    refresh(clean, hot, cold)                                   # parents arrive later
    orphans = clean.execute("SELECT count(*) FROM ah.session WHERE agent='claude' AND agent_id <> '' "
                            "AND NOT is_stub AND root_session_id IS NULL").fetchone()[0]
    assert orphans == 0
    clean.rollback()


def test_cost_state_start_time_is_stored_and_backfilled_from_the_source_line(clean, tmp_path):
    hot, cold = build_tree(tmp_path)
    refresh(clean, hot, cold)
    before = clean.execute("SELECT id, start_time FROM ah.cost_state WHERE agent = 'claude' ORDER BY id").fetchall()
    assert before and all(start is not None for _, start in before)
    clean.execute("UPDATE ah.cost_state SET start_time = NULL")
    clean.commit()
    assert load.backfill_cost_start(clean, hot, cold) == {"updated": len(before), "unavailable": 0, "unreadable": 0}
    after = clean.execute("SELECT id, start_time FROM ah.cost_state WHERE agent = 'claude' ORDER BY id").fetchall()
    assert after == before
    assert load.backfill_cost_start(clean, hot, cold)["updated"] == 0


def test_cost_state_backfill_reads_the_cold_copy_when_the_hot_line_is_unusable(clean, tmp_path):
    hot, cold = build_tree(tmp_path)
    refresh(clean, hot, cold)
    clean.execute("UPDATE ah.cost_state SET start_time = NULL")
    clean.commit()
    main = next((hot / "claude-local").rglob("11111111-1111-4111-8111-111111111111.jsonl"))
    archived = cold / main.relative_to(hot)
    archived.parent.mkdir(parents=True)
    shutil.copy(main, archived)
    main.write_text("")
    got = load.backfill_cost_start(clean, hot, cold)
    assert got["updated"] > 0 and got["unreadable"] == 0


@pytest.mark.parametrize("column", ["total_cost_usd", "total_duration_ms"])
def test_cost_state_backfill_leaves_a_row_whose_source_line_no_longer_matches(clean, tmp_path, column):
    hot, cold = build_tree(tmp_path)
    refresh(clean, hot, cold)
    clean.execute(f"UPDATE ah.cost_state SET start_time = NULL, {column} = {column} + 1")
    clean.commit()
    got = load.backfill_cost_start(clean, hot, cold)
    assert got["updated"] == 0 and got["unreadable"] > 0
    assert clean.execute("SELECT count(*) FROM ah.cost_state WHERE start_time IS NOT NULL").fetchone()[0] == 0


def test_cost_state_backfill_counts_a_missing_source_as_unavailable(clean, tmp_path):
    hot, cold = build_tree(tmp_path)
    refresh(clean, hot, cold)
    clean.execute("UPDATE ah.cost_state SET start_time = NULL")
    clean.commit()
    shutil.rmtree(hot / "claude-local")
    got = load.backfill_cost_start(clean, hot, cold)
    assert got["updated"] == 0 and got["unavailable"] > 0


def test_rollup_counts_claude_snapshot_calls_and_leaves_codex_null(clean, tmp_path):
    hot, cold = build_tree(tmp_path)
    # a subagent call whose transcript kept only the streamed usage snapshot (no final stop_reason line)
    sub = next((hot / "claude-local").rglob("agent-a0000000000000001.jsonl"))
    rec = json.loads(sub.read_text().splitlines()[1])
    rec.update(uuid="s-009", requestId="req_msg_s9", timestamp="2026-09-20T10:00:40.000Z")
    rec["message"]["id"] = "msg_s9"
    assert rec["message"]["stop_reason"] is None
    with sub.open("a") as handle:
        handle.write(json.dumps(rec) + "\n")
    refresh(clean, hot, cold)
    got = clean.execute(
        "SELECT s.agent, r.snapshot_calls, (SELECT count(*) FROM ah.llm_call c WHERE c.session_id = s.id "
        "AND c.stop_reason IS NULL AND NOT c.is_api_error) FROM ah.session s "
        "JOIN ah.session_rollup r ON r.session_id = s.id").fetchall()
    claude = [g for g in got if g[0] == "claude"]
    codex = [g for g in got if g[0] == "codex"]
    assert claude and codex
    assert all(rollup == expected for _, rollup, expected in claude), claude
    assert any(rollup > 0 for _, rollup, _ in claude), claude
    assert all(rollup is None for _, rollup, _ in codex), codex

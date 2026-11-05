"""Parser-v4 structure post-passes (structure.py / structure.sql / search.sql) on a scratch database.

Rows are written through the loader's Writer with synthetic content, so these tests pin the SQL
contract independently of the parsers. Skipped unless AGENT_HISTORY_TEST_DSN names a *_test database.
"""

from __future__ import annotations

import os
import random
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

DSN = os.environ.get("AGENT_HISTORY_TEST_DSN", "")
pytestmark = pytest.mark.skipif(not DSN or "agent_history_test" not in DSN,
                                reason="AGENT_HISTORY_TEST_DSN (a *_test database) not set")

psycopg = pytest.importorskip("psycopg")

from agent_history import load, structure  # noqa: E402
from agent_history.parse_claude import ClaudeParser  # noqa: E402
from agent_history.model import (ContinuationRow, FileContext, MessageRow, SessionKey,  # noqa: E402
                                 LinePos, SessionRow, SubagentSpawnRow, ToolCallRow, ToolIoRow, ToolOpRow)

T0 = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def conn():
    connection = load.connect(DSN)
    load.apply_schema(connection, force=True)
    connection.commit()
    yield connection
    connection.close()


@pytest.fixture
def db(conn):
    conn.execute("TRUNCATE " + ", ".join(f"ah.{t}" for t in load.DATA_TABLES)
                 + ", ah.change_log, ah.refresh_log RESTART IDENTITY CASCADE")
    conn.execute("DELETE FROM ah.meta WHERE key = 'embedding_model'")
    conn.execute("DELETE FROM ah.embedding WHERE model = 'm'")   # the paid cache is never truncated
    conn.commit()
    yield conn
    conn.rollback()
    conn.execute("DELETE FROM ah.meta WHERE key = 'embedding_model'")   # do not leak a fake model to other files
    conn.commit()


def ctx(rel: str, agent: str = "claude") -> FileContext:
    return FileContext(path="/x/" + rel, rel_path=f"{agent}-local/{rel}", namespace=f"{agent}-local",
                       agent=agent, profile="local", machine=None, file_role="main")


def write(conn, rows, rel="projects/p/s.jsonl", agent="claude"):
    c = ctx(rel, agent)
    sid = conn.execute(
        "INSERT INTO ah.source_file (rel_path, namespace, agent, profile, file_role, tier) "
        "VALUES (%s,%s,%s,'local','main','hot') ON CONFLICT (rel_path) DO UPDATE SET tier = 'hot' RETURNING id",
        (c.rel_path, c.namespace, agent)).fetchone()[0]
    writer = load.Writer(conn)
    touched = writer.write(rows, sid, c)
    for s in touched:
        conn.execute("INSERT INTO ah.dirty_session VALUES (%s) ON CONFLICT DO NOTHING", (s,))
    conn.commit()


def msg(key, uid, off, cls="assistant_text", text="hello", origin=None, role="assistant"):
    return MessageRow(agent=key.agent, event_uid=uid, session=key, ts=T0 + timedelta(seconds=off), role=role,
                      message_class=cls, text=text, byte_offset=off * 100, byte_length=10, prompt_origin=origin)


def session_row(key, **kw):
    return SessionRow(session=key, first_event_at=T0, last_event_at=T0 + timedelta(minutes=5), **kw)


def test_seq_is_source_ordered_shared_with_tool_io_and_append_only(db):
    k = SessionKey("claude", "s1")
    rows = [session_row(k), msg(k, "u2", 2, "human_prompt", "go", "typed", "user"), msg(k, "u1", 1),
            ToolCallRow(agent="claude", call_uid="c1", session=k, tool_name="Bash", byte_offset=300,
                        started_at=T0 + timedelta(seconds=3)),
            ToolIoRow(agent="claude", io_uid="c1", session=k, ts=T0 + timedelta(seconds=3), byte_offset=300,
                      kind="call", tool_name="Bash", call_uid="c1", input_text='{"command": "ls"}')]
    random.shuffle(rows)
    write(db, rows)
    load.post_passes(db, None)
    db.commit()
    order = db.execute("SELECT event_uid, seq FROM ah.message ORDER BY seq").fetchall()
    assert [u for u, _ in order] == ["u1", "u2"]
    call_seq = db.execute("SELECT seq FROM ah.tool_call WHERE call_uid = 'c1'").fetchone()[0]
    io = db.execute("SELECT seq, input_bytes, input_sha256 FROM ah.tool_io WHERE io_uid = 'c1'").fetchone()
    assert call_seq == 3 and io[0] == call_seq and io[1] == len('{"command": "ls"}') and len(io[2]) == 64
    # a later line appends without renumbering
    write(db, [msg(k, "u4", 4, text="later")])
    load.post_passes(db, None)
    db.commit()
    assert db.execute("SELECT event_uid, seq FROM ah.message ORDER BY seq").fetchall() == order + [("u4", 4)]
    first = db.execute("SELECT first_prompt_event_uid FROM ah.session WHERE session_uid = 's1'").fetchone()[0]
    assert first == "u2"


def test_seq_append_sorts_only_new_rows(db):
    key = SessionKey("claude", "long-order-session")
    write(db, [session_row(key), *[msg(key, f"history-{n}", n) for n in range(1, 2001)]])
    load.post_passes(db, None)
    db.commit()
    before = db.execute("SELECT event_uid, seq FROM ah.message ORDER BY seq").fetchall()
    write(db, [msg(key, "appended", 2001)])
    db.execute("SELECT 1")  # keep the post-pass transaction open for its temporary relation
    load.post_passes(db, None)
    # seq_now is the input/output of the real PostgreSQL window ordering, before ON COMMIT DROP.
    assert db.execute("SELECT count(*) FROM seq_now").fetchone()[0] == 1
    assert db.execute("SELECT event_uid, seq FROM ah.message ORDER BY seq").fetchall() == before + [("appended", 2001)]
    db.commit()

    # All physical row kinds share the same append path; linked I/O adds no slot.
    write(db, [ToolCallRow(agent="claude", call_uid="new-call", session=key, tool_name="Read", byte_offset=200200),
               ToolIoRow(agent="claude", io_uid="new-call", session=key, ts=T0, byte_offset=200200,
                         kind="call", call_uid="new-call", input_text="read"),
               ToolOpRow(agent="claude", item_uid="new-op", session=key, item_type="Extension", byte_offset=200300),
               ToolIoRow(agent="claude", io_uid="item:new-op", session=key, ts=T0, byte_offset=200300,
                         kind="op", item_uid="new-op", output_text="done"),
               ToolIoRow(agent="claude", io_uid="orphan", session=key, ts=T0, byte_offset=200400,
                         kind="call", call_uid="orphan", output_text="unlinked")])
    db.execute("SELECT 1")
    load.post_passes(db, None)
    assert db.execute("SELECT count(*) FROM seq_now").fetchone()[0] == 3
    assert db.execute("SELECT io_uid, seq FROM ah.tool_io ORDER BY seq").fetchall() == [
        ("new-call", 2002), ("item:new-op", 2003), ("orphan", 2004)]
    db.commit()
    # Later results update content but must not read/order the old I/O history.
    write(db, [ToolIoRow(agent="claude", io_uid="new-call", session=key, ts=T0, byte_offset=200200,
                        kind="call", call_uid="new-call", output_text="finished")])
    db.execute("SELECT 1")
    load.post_passes(db, None)
    assert db.execute("SELECT count(*) FROM seq_now").fetchone()[0] == 0
    assert db.execute("SELECT count(*) FROM seq_io_now").fetchone()[0] == 0
    assert db.execute("SELECT seq FROM ah.tool_io WHERE io_uid = 'new-call'").fetchone()[0] == 2002
    db.commit()


def sequence_rows(db):
    return dict(db.execute("""
        SELECT 'message:' || event_uid, seq FROM ah.message
        UNION ALL SELECT 'call:' || call_uid, seq FROM ah.tool_call
        UNION ALL SELECT 'op:' || item_uid, seq FROM ah.tool_op
        UNION ALL SELECT 'io:' || io_uid, seq FROM ah.tool_io
    """))


def test_seq_late_rows_mutations_and_source_rename_rederive(db):
    key = SessionKey("claude", "reordered")
    write(db, [session_row(key), msg(key, "a", 1), msg(key, "b", 3)])
    load.post_passes(db, None)
    db.commit()
    assert sequence_rows(db) == {"message:a": 1, "message:b": 2}
    write(db, [msg(key, "late", 2)])
    load.post_passes(db, None)
    db.commit()
    assert sequence_rows(db) == {"message:a": 1, "message:late": 2, "message:b": 3}
    # Equal byte offsets use reasoning first, then natural UID, not insertion order.
    db.execute("UPDATE ah.message SET byte_offset = 200, message_class = 'reasoning' WHERE event_uid = 'b'")
    load.post_passes(db, None)
    db.commit()
    assert sequence_rows(db) == {"message:a": 1, "message:b": 2, "message:late": 3}
    db.execute("DELETE FROM ah.message WHERE event_uid = 'a'")
    load.post_passes(db, None)
    db.commit()
    assert sequence_rows(db) == {"message:b": 1, "message:late": 2}
    write(db, [msg(key, "other-file", 1)], rel="z.jsonl")
    load.post_passes(db, None)
    db.commit()
    assert sequence_rows(db) == {"message:b": 1, "message:late": 2, "message:other-file": 3}
    db.execute("UPDATE ah.source_file SET rel_path = 'claude-local/a.jsonl' WHERE rel_path = 'claude-local/z.jsonl'")
    load.post_passes(db, None)
    db.commit()
    assert sequence_rows(db) == {"message:other-file": 1, "message:b": 2, "message:late": 3}
    # Moving a row between source files changes the leading sort key too.
    db.execute("UPDATE ah.message SET source_id = (SELECT id FROM ah.source_file WHERE rel_path = 'claude-local/a.jsonl') "
               "WHERE event_uid = 'late'")
    load.post_passes(db, None)
    db.commit()
    assert sequence_rows(db) == {"message:other-file": 1, "message:late": 2, "message:b": 3}
    db.execute("TRUNCATE ah.message CASCADE")
    load.post_passes(db, None)
    db.commit()
    write(db, [msg(key, "after-truncate", 8)])
    load.post_passes(db, None)
    db.commit()
    assert sequence_rows(db) == {"message:after-truncate": 1}


def test_seq_orphan_linkage_changes_and_rollback(db):
    key = SessionKey("claude", "late-link")
    write(db, [session_row(key), msg(key, "before", 1), msg(key, "after", 3),
               ToolIoRow(agent="claude", io_uid="future", session=key, ts=T0, byte_offset=200,
                         kind="call", call_uid="future", output_text="result")])
    load.post_passes(db, None)
    db.commit()
    assert sequence_rows(db) == {"message:before": 1, "io:future": 2, "message:after": 3}
    write(db, [ToolCallRow(agent="claude", call_uid="future", session=key, tool_name="Read", byte_offset=400)])
    db.execute("SELECT 1")
    load.post_passes(db, None)
    assert sequence_rows(db) == {"message:before": 1, "message:after": 2, "call:future": 3, "io:future": 3}
    db.rollback()
    # A failed outer pass restores both the old order and the still-pending mutation.
    assert sequence_rows(db) == {"message:before": 1, "io:future": 2, "message:after": 3, "call:future": None}
    load.post_passes(db, None)
    db.commit()
    assert sequence_rows(db) == {"message:before": 1, "message:after": 2, "call:future": 3, "io:future": 3}
    db.execute("UPDATE ah.tool_call SET call_uid = 'renamed' WHERE call_uid = 'future'")
    load.post_passes(db, None)
    db.commit()
    assert sequence_rows(db) == {"message:before": 1, "io:future": 2, "message:after": 3, "call:renamed": 4}
    db.execute("UPDATE ah.tool_io SET call_uid = 'renamed' WHERE io_uid = 'future'")
    load.post_passes(db, None)
    db.commit()
    assert sequence_rows(db) == {"message:before": 1, "message:after": 2, "call:renamed": 3, "io:future": 3}
    db.execute("DELETE FROM ah.tool_call WHERE call_uid = 'renamed'")
    load.post_passes(db, None)
    db.commit()
    assert sequence_rows(db) == {"message:before": 1, "io:future": 2, "message:after": 3}


def test_error_class_and_excerpt(db):
    k = SessionKey("claude", "s2")
    out = "building\n" * 50 + "Error: ENOENT: no such file or directory, open 'x.json'\nmore"
    write(db, [session_row(k),
               ToolCallRow(agent="claude", call_uid="e1", session=k, tool_name="Bash", byte_offset=1,
                           outcome="error", is_error=True, exit_code=1),
               ToolIoRow(agent="claude", io_uid="e1", session=k, ts=T0, byte_offset=1, kind="call", call_uid="e1",
                         output_text=out),
               ToolCallRow(agent="claude", call_uid="e2", session=k, tool_name="Bash", byte_offset=2,
                           outcome="denied", is_error=True, denial_kind="user-rejected"),
               ToolCallRow(agent="claude", call_uid="ok", session=k, tool_name="Read", byte_offset=3, outcome="ok",
                           is_error=None),
               ToolOpRow(agent="claude", item_uid="op-quiet", session=k, item_type="Extension", byte_offset=4)])
    load.post_passes(db, None)
    db.commit()
    got = dict((u, (c, e)) for u, c, e in db.execute("SELECT call_uid, error_class, error_excerpt FROM ah.tool_call"))
    assert got["e1"][0] == "not_found" and got["e1"][1].startswith("Error: ENOENT")
    assert got["e2"][0] == "denied"
    assert got["ok"] == (None, None)
    # an op with no status, exit code or error flag is not a failure (NULLs must not classify it)
    assert db.execute("SELECT error_class FROM ah.tool_op WHERE item_uid = 'op-quiet'").fetchone()[0] is None


CELL_MISSING = "Script failed\nWall time 0.0 seconds\nOutput:\nScript error:\nexec cell {} not found"


def codex_wait(k, uid, off, cell, outcome="error", output=None):
    return [ToolCallRow(agent="codex", call_uid=uid, session=k, tool_name="wait", byte_offset=off,
                        started_at=T0 + timedelta(seconds=off), outcome=outcome, is_error=outcome == "error"),
            ToolIoRow(agent="codex", io_uid=uid, session=k, ts=T0 + timedelta(seconds=off), byte_offset=off,
                      kind="call", tool_name="wait", call_uid=uid,
                      input_text=f'{{"cell_id":"{cell}"}}', output_text=output or CELL_MISSING.format(cell))]


def test_fake_cell_wait_class_backfill_and_views(db):
    from agent_history.model import LlmCallRow
    root, lane = SessionKey("codex", "fc-root"), SessionKey("codex", "fc-lane")
    write(db, [session_row(root),
               ToolCallRow(agent="codex", call_uid="ex7", session=root, tool_name="exec", byte_offset=1,
                           started_at=T0 + timedelta(seconds=1), outcome="ok"),
               ToolIoRow(agent="codex", io_uid="ex7", session=root, ts=T0 + timedelta(seconds=1), byte_offset=1,
                         kind="call", tool_name="exec", call_uid="ex7",
                         output_text="Script running with cell ID 7\nWall time 1.0 seconds\nOutput:\n"),
               *codex_wait(root, "w-stale", 2, "7"),      # the session's own cell, since finished: not fake
               *codex_wait(root, "w-none", 3, "none"),
               *codex_wait(root, "w-524", 4, "524"),      # numeric but never returned by this session
               ToolCallRow(agent="codex", call_uid="wa1", session=root, tool_name="wait_agent", byte_offset=5,
                           started_at=T0 + timedelta(seconds=5), outcome="ok"),
               *[LlmCallRow(agent="codex", response_id=f"r{i}", session=root, ts=T0 + timedelta(seconds=i),
                            byte_offset=10 + i) for i in range(4)]],
          rel="sessions/fc-root.jsonl", agent="codex")
    write(db, [session_row(lane, is_subagent=True, root_session_uid="fc-root"), *codex_wait(lane, "w-lane", 1, "x")],
          rel="sessions/fc-lane.jsonl", agent="codex")
    load.post_passes(db, None)
    db.commit()
    cls = dict(db.execute("SELECT call_uid, error_class FROM ah.tool_call WHERE tool_name = 'wait'").fetchall())
    assert cls == {"w-stale": "not_found", "w-none": "fake_cell_wait", "w-524": "fake_cell_wait",
                   "w-lane": "fake_cell_wait"}

    # root sessions only: the lane's fake wait is classified but not counted against a root
    hourly = db.execute("SELECT session_uid, hour, fake_cell_waits, wait_agent_calls, root_llm_calls, "
                        "fake_per_1k_llm_calls FROM ah.v_fake_cell_wait_hourly").fetchall()
    assert hourly == [("fc-root", T0, 2, 1, 4, 500)]
    per_root = db.execute("SELECT session_uid, fake_cell_waits, wait_agent_calls, root_llm_calls, "
                          "fake_per_1k_llm_calls, peak_hour_fake_cell_waits FROM ah.v_fake_cell_wait_root").fetchall()
    assert per_root == [("fc-root", 2, 1, 4, 500, 2)]
    db.rollback()


def test_error_excerpts_are_embedded_and_searchable(db):
    from agent_history import embed
    from test_embed_pg import FakeProvider
    k = SessionKey("claude", "s9")
    write(db, [session_row(k),
               ToolCallRow(agent="claude", call_uid="x1", session=k, tool_name="Bash", byte_offset=1,
                           outcome="error", is_error=True, exit_code=2),
               ToolIoRow(agent="claude", io_uid="x1", session=k, ts=T0, byte_offset=1, kind="call", call_uid="x1",
                         stderr_text="fatal: unable to access remote repository gitea mirror handshake refused")])
    load.post_passes(db, None)
    db.commit()
    db.execute("TRUNCATE ah.embed_failure")
    db.execute("DELETE FROM ah.embedding WHERE model = 'fake-bow-1024'")
    db.execute("DELETE FROM ah.meta WHERE key LIKE 'embed%%' OR key = 'rebuild_in_progress'")
    db.commit()
    provider = FakeProvider()
    embed.run(db, provider, cap_tokens=0, daily_cap=0, log=lambda *_: None)
    assert db.execute("SELECT count(*) FROM ah.chunk WHERE tool_call_id IS NOT NULL").fetchone()[0] == 1
    qvec = embed.halfvec_literal(provider.embed(["remote repository handshake refused"], kind="query")[0])
    hits = db.execute("SELECT hit_kind, io_uid, vec_rank FROM ah.search_hybrid('zzzznomatch', qvec => %s::halfvec)",
                      (qvec,)).fetchall()
    db.rollback()
    assert ("tool_io", "x1", 1) in hits


def test_orchestration_tree(db):
    root, lane, poll, grand = (SessionKey("claude", "r"), SessionKey("claude", "r", "a1"),
                               SessionKey("claude", "r", "a2"), SessionKey("claude", "r", "a3"))
    plain, explore = SessionKey("claude", "p"), SessionKey("claude", "p", "b1")
    write(db, [session_row(root), msg(root, "l1", 1, "human_prompt", "You are the root. codex/launch-x-loop1.txt",
                                      "launch_message", "user"),
               SubagentSpawnRow(agent="claude", spawn_uid="sp1", session=root, byte_offset=2, child_session_uid="r",
                                child_agent_id="a1", requested_type="agent-workflows:lane-worker"),
               SubagentSpawnRow(agent="claude", spawn_uid="sp2", session=root, byte_offset=3, child_session_uid="r",
                                child_agent_id="a2", requested_type="agent-workflows:poller")])
    write(db, [session_row(lane, is_subagent=True, root_session_uid="r", parent_session_uid="r"),
               SubagentSpawnRow(agent="claude", spawn_uid="sp3", session=lane, byte_offset=4, child_session_uid="r",
                                child_agent_id="a3", requested_type="Explore")], rel="projects/p/r/subagents/agent-a1.jsonl")
    write(db, [session_row(poll, is_subagent=True, root_session_uid="r", parent_session_uid="r")],
          rel="projects/p/r/subagents/agent-a2.jsonl")
    write(db, [session_row(grand, is_subagent=True, root_session_uid="r", parent_session_uid="r")],
          rel="projects/p/r/subagents/agent-a3.jsonl")
    write(db, [session_row(plain), msg(plain, "h1", 1, "human_prompt", "fix it", "typed", "user"),
               SubagentSpawnRow(agent="claude", spawn_uid="sp4", session=plain, byte_offset=2, child_session_uid="p",
                                child_agent_id="b1", requested_type="Explore")], rel="projects/p/p.jsonl")
    write(db, [session_row(explore, is_subagent=True, root_session_uid="p", parent_session_uid="p")],
          rel="projects/p/p/subagents/agent-b1.jsonl")
    load.post_passes(db, None)
    db.commit()
    rows = {(u, a): (k, d, r) for u, a, k, d, r in db.execute(
        "SELECT session_uid, agent_id, orchestration_kind, is_orchestration_descendant, root_session_uid "
        "FROM ah.v_session_orchestration")}
    assert rows[("r", "")] == ("loop_root", False, "r")
    assert rows[("r", "a1")] == ("lane", True, "r")
    assert rows[("r", "a2")] == ("poller", True, "r")
    assert rows[("r", "a3")] == ("subagent", True, "r")
    assert rows[("p", "")] == ("none", False, None)
    assert rows[("p", "b1")] == ("subagent", False, None)


@pytest.mark.parametrize("agent", ["claude", "codex", "pi"])
def test_v3_role_names_and_codex_builtin_context(db, agent):
    roots = [SessionKey(agent, "loop-root"), SessionKey(agent, "plain-root")]
    for root in roots:
        rows = [session_row(root)]
        if root == roots[0]:
            rows.append(msg(root, "launch", 1, "human_prompt", "You are the root.", "launch_message", "user"))
        write(db, rows, rel=f"{root.session_uid}.jsonl", agent=agent)
    expected = {}
    names = ["worker", "senior", "senior-escalation", "reviewer", "scout", "explorer", "networker"]
    for root in roots:
        for i, name in enumerate(names):
            for j, prefix in enumerate(("", "agent-workflows:", "lane-", "agent-workflows:lane-")):
                uid = f"{root.session_uid}-{i}-{j}"
                if agent == "claude":
                    # Claude children share the root UID; the agent ID distinguishes them.
                    rel = f"projects/p/{root.session_uid}/subagents/agent-{uid}.jsonl"
                    parser = ClaudeParser(replace(ctx(rel, agent), file_role="subagent"), {})
                    list(parser.line({"type": "user", "uuid": uid, "sessionId": root.session_uid,
                                      "timestamp": T0.isoformat(), "message": {"role": "user", "content": "brief"}},
                                     LinePos(0, 100, 1)))
                    row = next(row for row in parser.flush() if isinstance(row, SessionRow))
                    child = row.session
                    assert child == SessionKey(agent, root.session_uid, uid)
                    row = replace(row, agent_type=prefix + name)
                else:
                    child = SessionKey(agent, uid)
                    rel = f"{uid}.jsonl"
                    row = session_row(child, is_subagent=True, root_session_uid=root.session_uid,
                                      parent_session_uid=root.session_uid, agent_type=prefix + name)
                write(db, [row], rel=rel, agent=agent)
                kind = "reviewer" if name == "reviewer" else "lane"
                if name == "networker" or (name == "explorer" and (agent != "codex" or prefix)):
                    kind = "subagent"
                if agent == "codex" and root == roots[1] and not prefix and name in ("worker", "explorer"):
                    kind = "subagent"
                expected[(child.session_uid, child.agent_id)] = kind
    load.post_passes(db, None)
    got = {(uid, aid): kind for uid, aid, kind in db.execute(
        "SELECT session_uid, agent_id, orchestration_kind FROM ah.session WHERE is_subagent")}
    assert got == expected


def test_codex_explicit_v3_role_survives_independent_builtin_metadata(db):
    root = SessionKey("codex", "plain-root")
    write(db, [session_row(root)], rel="plain-root.jsonl", agent="codex")
    cases = [
        ("senior", "worker", None, "lane"),
        ("scout", "explorer", None, "lane"),
        ("agent-workflows:worker", "worker", None, "lane"),
        (None, "worker", "senior-escalation", "lane"),
        (None, "explorer", "lane-scout", "lane"),
        ("worker", "explorer", None, "subagent"),
        (None, "worker", None, "subagent"),
    ]
    expected = {}
    for i, (agent_type, agent_role, requested_type, kind) in enumerate(cases):
        child = SessionKey("codex", f"mixed-{i}")
        write(db, [session_row(child, is_subagent=True, root_session_uid=root.session_uid,
                               parent_session_uid=root.session_uid, agent_type=agent_type, agent_role=agent_role)],
              rel=f"mixed-{i}.jsonl", agent="codex")
        if requested_type:
            write(db, [SubagentSpawnRow(agent="codex", spawn_uid=f"spawn-{i}", session=root, byte_offset=i,
                                       child_session_uid=child.session_uid, requested_type=requested_type)],
                  rel="plain-root.jsonl", agent="codex")
        expected[child.session_uid] = kind
    load.post_passes(db, None)
    got = dict(db.execute("SELECT session_uid, orchestration_kind FROM ah.session WHERE is_subagent"))
    assert got == expected


def test_change_log_refresh_and_notify(db):
    k = SessionKey("claude", "s3")
    write(db, [session_row(k), msg(k, "m1", 1)])
    listener = load.connect(DSN)
    listener.autocommit = True
    listener.execute("LISTEN ah_refresh")
    with db.transaction():
        rid = load.new_refresh(db, "refresh")
    load.post_passes(db, rid)
    with db.transaction():
        load.finish_refresh(db, rid, True)
    feed = db.execute("SELECT refresh_id, session_uid, kind FROM ah.change_log ORDER BY seq").fetchall()
    assert feed == [(rid, "s3", "content")]
    assert db.execute("SELECT content_changed_at IS NOT NULL FROM ah.session WHERE session_uid = 's3'").fetchone()[0]
    notes = list(listener.notifies(timeout=5, stop_after=1))
    listener.close()
    assert notes and notes[0].channel == "ah_refresh" and notes[0].payload == str(rid)
    assert db.execute("SELECT ok, sessions_changed FROM ah.refresh_log WHERE refresh_id = %s", (rid,)).fetchone() == (True, 1)


def test_continuations(db):
    parent, child = SessionKey("claude", "old"), SessionKey("claude", "new")
    fork_parent, fork = SessionKey("codex", "t-parent"), SessionKey("codex", "t-fork")
    write(db, [session_row(parent), session_row(child),
               ContinuationRow(agent="claude", child_uid="new", parent_uid="old", kind="compaction_continuation",
                               session=parent, ts=T0, byte_offset=1, evidence="continued-in")])
    write(db, [session_row(fork_parent), session_row(fork, forked_from_uid="t-parent")],
          rel="sessions/x.jsonl", agent="codex")
    load.post_passes(db, None)
    db.commit()
    got = dict(db.execute("SELECT session_uid, continued_from_session_uid || '/' || continuation_kind "
                          "FROM ah.v_session_state WHERE continuation_kind IS NOT NULL").fetchall())
    assert got == {"new": "old/compaction_continuation", "t-fork": "t-parent/fork"}


def test_search_hybrid_and_similar_sessions(db):
    a, b, c = SessionKey("claude", "sa"), SessionKey("claude", "sb"), SessionKey("claude", "sc")
    write(db, [session_row(a), msg(a, "ma", 1, text="the frobnicator quux failed again")])
    write(db, [session_row(b), msg(b, "mb", 1, text="unrelated words"),
               ToolIoRow(agent="claude", io_uid="tb", session=b, ts=T0, byte_offset=2, kind="call", call_uid="tb",
                         tool_name="Bash", input_text="run frobnicator", stderr_text="frobnicator: exit 3")],
          rel="projects/p/b.jsonl")
    write(db, [session_row(c), msg(c, "mc", 1, text="other")], rel="projects/p/c.jsonl")
    load.post_passes(db, None)
    db.commit()
    load.create_post_load_indexes(db)
    hits = db.execute("SELECT hit_kind, session_uid, COALESCE(event_uid, io_uid) FROM ah.search_hybrid('frobnicator') "
                      "ORDER BY 1").fetchall()
    db.rollback()
    assert hits == [("message", "sa", "ma"), ("tool_io", "sb", "tb")]
    # session vectors: mean of chunk vectors; a and b point the same way, c elsewhere
    db.execute("INSERT INTO ah.meta VALUES ('embedding_model', 'm') ON CONFLICT (key) DO UPDATE SET value = 'm'")
    for uid, vec in (("ma", [1.0, 0.1]), ("mb", [0.9, 0.2]), ("mc", [-1.0, 0.0])):
        full = "[" + ",".join(str(x) for x in vec + [0.0] * 1022) + "]"
        db.execute("INSERT INTO ah.embedding (model, input_sha256, embedding) VALUES ('m', %s, l2_normalize(%s::vector)::halfvec)",
                   (uid, full))
        db.execute("INSERT INTO ah.chunk (message_id, session_id, namespace, ts, chunk_no, char_start, char_end, "
                   "source_sha256, model, input_sha256) SELECT id, session_id, namespace, ts, 0, 0, 5, content_sha256, "
                   "'m', %s FROM ah.message WHERE event_uid = %s", (uid, uid))
    assert structure.session_embeddings(db)["session_embeddings"] == 3
    db.commit()
    near = db.execute("SELECT session_uid FROM ah.similar_sessions('claude', 'sa', '', 2)").fetchall()
    assert [r[0] for r in near] == ["sb", "sc"]


def test_heuristic_loop_member_stays_a_descendant_whichever_side_is_dirty(db):
    root, member = SessionKey("claude", "lr"), SessionKey("codex", "exec-1")
    write(db, [session_row(root), msg(root, "lm", 1, "human_prompt", "You are the root for loop 7", "launch_message", "user")])
    write(db, [session_row(member, entrypoint="codex_exec")], rel="sessions/exec.jsonl", agent="codex")
    load.post_passes(db, None)
    db.execute("INSERT INTO ah.loop_run (launch_uid, root_session_id, repo_slug, loop_number, naming) "
               "SELECT 'x', id, 'r', 7, 'loop' FROM ah.session WHERE session_uid = 'lr'")
    db.execute("UPDATE ah.session SET loop_run_id = (SELECT id FROM ah.loop_run), loop_link_method = 'heuristic' "
               "WHERE session_uid = 'exec-1'")
    expected = ("other", True, "lr")
    for dirty in ("exec-1", "lr", "exec-1"):   # member alone, root alone, member alone again
        db.execute("INSERT INTO ah.dirty_session SELECT id FROM ah.session WHERE session_uid = %s", (dirty,))
        db.commit()
        load.post_passes(db, None)
        db.commit()
        got = db.execute("SELECT orchestration_kind, is_orchestration_descendant, root_session_uid "
                         "FROM ah.v_session_orchestration WHERE session_uid = 'exec-1'").fetchone()
        assert got == expected, dirty


def count_classifier_calls(db):
    """Instrument the real SQL function in a rollback-only test transaction."""
    db.execute("CREATE TEMP TABLE classifier_calls (id integer)")
    db.execute("ALTER FUNCTION ah.error_class_of(text,boolean,boolean,integer,boolean,text,text) "
               "RENAME TO error_class_of_original")
    db.execute("""
        CREATE FUNCTION ah.error_class_of(text,boolean,boolean,integer,boolean,text,text)
        RETURNS text LANGUAGE plpgsql VOLATILE AS $$
        BEGIN
            INSERT INTO classifier_calls VALUES (1);
            RETURN ah.error_class_of_original($1,$2,$3,$4,$5,$6,$7);
        END $$
    """)


def test_error_classification_does_not_revisit_unchanged_session_history(db):
    key = SessionKey("claude", "error-history")
    write(db, [session_row(key), *[
        ToolCallRow(agent="claude", call_uid=f"old-{i}", session=key, tool_name="Read", byte_offset=i,
                    outcome="ok") for i in range(500)]])
    load.post_passes(db, None)
    db.commit()
    write(db, [ToolCallRow(agent="claude", call_uid="new-error", session=key, tool_name="Bash",
                          byte_offset=501, outcome="error", exit_code=1)])
    count_classifier_calls(db)
    load.post_passes(db, None)
    assert db.execute("SELECT error_class FROM ah.tool_call WHERE call_uid='new-error'").fetchone()[0] == "nonzero_exit"
    assert db.execute("SELECT count(*) FROM classifier_calls").fetchone()[0] <= 3
    db.rollback()  # also restores the immutable classifier


def test_error_classification_late_io_status_and_cell_ownership(db):
    key = SessionKey("codex", "error-invalidation")
    write(db, [session_row(key), *codex_wait(key, "missing-cell", 10, "cell-a"),
               ToolOpRow(agent="codex", item_uid="late-op", session=key, item_type="command_execution",
                         byte_offset=20, status="failed", exit_code=1)], agent="codex")
    load.post_passes(db, None)
    db.commit()
    assert db.execute("SELECT error_class FROM ah.tool_call WHERE call_uid='missing-cell'").fetchone()[0] == "fake_cell_wait"
    # A late output on an already indexed op must enqueue it even without new source positions.
    write(db, [ToolIoRow(agent="codex", io_uid="item:late-op", session=key, ts=T0, byte_offset=20,
                         kind="op", item_uid="late-op", stderr_text="permission denied")], agent="codex")
    load.post_passes(db, None)
    db.commit()
    assert db.execute("SELECT error_class FROM ah.tool_op WHERE item_uid='late-op'").fetchone()[0] == "permission"
    # Owner arrives after the failed wait; both insertion orders (I/O then call) are supported.
    write(db, [ToolIoRow(agent="codex", io_uid="owner", session=key, ts=T0, byte_offset=1, kind="call",
                         call_uid="owner", output_text="Script running with cell ID cell-a\n")], agent="codex")
    load.post_passes(db, None)
    db.commit()
    write(db, [ToolCallRow(agent="codex", call_uid="owner", session=key, tool_name="exec",
                           byte_offset=1, outcome="ok")], agent="codex")
    load.post_passes(db, None)
    db.commit()
    assert db.execute("SELECT error_class FROM ah.tool_call WHERE call_uid='missing-cell'").fetchone()[0] == "not_found"
    # Content-only and status-only mutations must work without caller-maintained dirty markers.
    db.execute("UPDATE ah.tool_io SET output_text='finished' WHERE io_uid='owner'")
    db.execute("UPDATE ah.tool_op SET status='completed',exit_code=0,is_error=false WHERE item_uid='late-op'")
    load.post_passes(db, None)
    assert db.execute("SELECT error_class FROM ah.tool_call WHERE call_uid='missing-cell'").fetchone()[0] == "fake_cell_wait"
    assert db.execute("SELECT error_class,error_excerpt FROM ah.tool_op WHERE item_uid='late-op'").fetchone() == (None, None)
    db.rollback()
    load.post_passes(db, None)
    db.commit()
    assert db.execute("SELECT error_class FROM ah.tool_call WHERE call_uid='missing-cell'").fetchone()[0] == "not_found"


def test_known_repo_roots_skip_slug_resolution_and_late_commits_still_enrich(db):
    key = SessionKey("claude", "repo-lookup")
    write(db, [session_row(key, cwd="/home/test/repos/example")])
    load.post_passes(db, None)
    db.commit()
    # Existing roots must not call the slug resolver again on an unrelated append.
    write(db, [msg(key, "later", 1)])
    db.execute("CREATE TEMP TABLE repo_lookups (id integer)")
    db.execute("ALTER FUNCTION ah.repo_name(text) RENAME TO repo_name_original")
    db.execute("""CREATE FUNCTION ah.repo_name(text) RETURNS text LANGUAGE plpgsql VOLATILE AS $$
        BEGIN
            INSERT INTO repo_lookups VALUES(1);
            RETURN ah.repo_name_original($1);
        END $$""")
    # Only exercise discovery: late enrichment is deliberately a separate pass.
    db.execute("CREATE TEMP TABLE IF NOT EXISTS dirty_now(session_id bigint PRIMARY KEY) ON COMMIT DELETE ROWS")
    db.execute("INSERT INTO dirty_now SELECT id FROM ah.session")
    db.execute(structure.REPO_SQL[0])
    assert db.execute("SELECT count(*) FROM repo_lookups").fetchone()[0] == 0
    db.rollback()
    db.execute("INSERT INTO ah.git_commit(repo_slug,sha,context,committed_at) VALUES ('example/example',%s,'personal',now())", ('a' * 40,))
    load.post_passes(db, None)
    assert db.execute("SELECT slug FROM ah.repo WHERE local_root='/home/test/repos/example'").fetchone()[0] == "example/example"
    db.rollback()


def test_error_queue_keeps_late_publications_and_rolls_back_consumption(db):
    key = SessionKey("claude", "error-generation")
    write(db, [session_row(key), ToolCallRow(agent="claude",call_uid="changing",session=key,
                                            tool_name="Bash",byte_offset=1,outcome="error",exit_code=1)])
    db.execute("CREATE TEMP TABLE IF NOT EXISTS dirty_now(session_id bigint PRIMARY KEY) ON COMMIT DELETE ROWS")
    structure.prepare_errors(db)

    class LateSource:
        def execute(self, query, *args):
            result = db.execute(query, *args)
            if query == "ANALYZE error_now":
                # Another writer commits after the classifier takes its queue snapshot.
                with psycopg.connect(DSN) as writer:
                    writer.execute("SET LOCAL statement_timeout='3s'")
                    writer.execute("UPDATE ah.tool_call SET timed_out=true WHERE call_uid='changing'")
            return result

    structure.classify_errors(LateSource())
    assert db.execute("SELECT count(*) FROM ah.error_classification_pending").fetchone()[0] == 1
    db.commit()
    # Queue membership is sufficient to wake the pass; no source append is required.
    db.execute("DELETE FROM ah.dirty_session")
    db.commit()
    db.execute("SELECT 1")
    load.post_passes(db, None)
    assert db.execute("SELECT count(*) FROM ah.error_classification_pending").fetchone()[0] == 0
    db.rollback()
    assert db.execute("SELECT count(*) FROM ah.error_classification_pending").fetchone()[0] == 1
    load.post_passes(db, None)
    db.commit()
    assert db.execute("SELECT error_class FROM ah.tool_call WHERE call_uid='changing'").fetchone()[0] == "timeout"
    assert db.execute("SELECT count(*) FROM ah.error_classification_pending").fetchone()[0] == 0


def test_error_classifier_definition_change_revisits_existing_rows(db):
    key = SessionKey("claude", "error-definition")
    write(db, [session_row(key), ToolCallRow(agent="claude",call_uid="existing",session=key,
                                            tool_name="Bash",byte_offset=1,outcome="error",exit_code=1)])
    load.post_passes(db, None)
    db.commit()
    # Simulate retained classifications from an older classifier, without marking source dirty.
    db.execute("UPDATE ah.tool_call SET error_class='old-class' WHERE call_uid='existing'")
    db.execute("UPDATE ah.meta SET value='previous-definition' WHERE key='error_classification_hash'")
    load.post_passes(db, None)
    assert db.execute("SELECT error_class FROM ah.tool_call WHERE call_uid='existing'").fetchone()[0] == "nonzero_exit"
    db.rollback()


def test_error_cell_owner_deletion_and_changed_payload(db):
    key = SessionKey("codex", "cell-deletion")
    write(db, [session_row(key), *codex_wait(key,"waiting",10,"a"),
               ToolCallRow(agent="codex",call_uid="owner",session=key,tool_name="exec",byte_offset=1,outcome="ok"),
               ToolIoRow(agent="codex",io_uid="owner",session=key,ts=T0,byte_offset=1,kind="call",
                         output_text="Script running with cell ID a\n")], agent="codex")
    load.post_passes(db, None)
    db.commit()
    db.execute("DELETE FROM ah.tool_io WHERE io_uid='owner'")
    load.post_passes(db, None)
    assert db.execute("SELECT error_class FROM ah.tool_call WHERE call_uid='waiting'").fetchone()[0] == "fake_cell_wait"
    db.execute("UPDATE ah.tool_io SET stderr_text='permission denied' WHERE io_uid='waiting'")
    load.post_passes(db, None)
    assert db.execute("SELECT error_class FROM ah.tool_call WHERE call_uid='waiting'").fetchone()[0] == "permission"
    db.rollback()
    db.execute("DELETE FROM ah.tool_call WHERE call_uid='owner'")
    load.post_passes(db, None)
    assert db.execute("SELECT error_class FROM ah.tool_call WHERE call_uid='waiting'").fetchone()[0] == "fake_cell_wait"
    db.rollback()


def test_error_queue_adoption_seeds_dirty_history_only(db):
    clean, dirty = SessionKey("claude", "adopt-clean"), SessionKey("claude", "adopt-dirty")
    write(db, [session_row(clean), ToolCallRow(agent="claude",call_uid="clean",session=clean,
                                              tool_name="Read",byte_offset=1,outcome="ok")])
    load.post_passes(db, None)
    db.commit()
    write(db, [session_row(dirty), ToolCallRow(agent="claude",call_uid="dirty",session=dirty,
                                              tool_name="Bash",byte_offset=1,outcome="error",exit_code=1)])
    # Recreate the exact migration over a retained, already classified catalogue.
    for table, trigger in [('tool_call','tool_call_error_changed'), ('tool_op','tool_op_error_changed'),
                           ('tool_io','tool_io_error_changed')]:
        db.execute(f"DROP TRIGGER {trigger} ON ah.{table}")
    for signature in ('error_tool_changed()', 'error_io_changed()', 'queue_error_io(text,text,boolean)',
                      'queue_error_waits(bigint)', 'queue_error_classification(smallint,bigint,bigint)',
                      'error_cell_id(text)'):
        db.execute(f"DROP FUNCTION ah.{signature}")
    db.execute("DROP TABLE ah.error_classification_pending")
    db.execute("DROP SEQUENCE ah.error_classification_generation")
    db.execute("DELETE FROM ah.meta WHERE key='error_classification_hash'")
    db.execute((load.SQL_DIR / 'migrations' / '051_error_classification_queue.sql').read_text())
    pending = db.execute("SELECT c.call_uid FROM ah.error_classification_pending q "
                         "JOIN ah.tool_call c ON q.row_type=1 AND c.id=q.row_id").fetchall()
    assert pending == [('dirty',)]
    count_classifier_calls(db)
    load.post_passes(db, None)
    assert db.execute("SELECT count(*) FROM classifier_calls").fetchone()[0] <= 3
    assert db.execute("SELECT error_class FROM ah.tool_call WHERE call_uid='dirty'").fetchone()[0] == 'nonzero_exit'
    db.rollback()

"""Parser-v4 structure post-passes (structure.py / structure.sql / search.sql) on a scratch database.

Rows are written through the loader's Writer with synthetic content, so these tests pin the SQL
contract independently of the parsers. Skipped unless AGENT_HISTORY_TEST_DSN names a *_test database.
"""

from __future__ import annotations

import os
import random
from datetime import datetime, timedelta, timezone

import pytest

DSN = os.environ.get("AGENT_HISTORY_TEST_DSN", "")
pytestmark = pytest.mark.skipif(not DSN or "agent_history_test" not in DSN,
                                reason="AGENT_HISTORY_TEST_DSN (a *_test database) not set")

psycopg = pytest.importorskip("psycopg")

from agent_history import load, structure  # noqa: E402
from agent_history.model import (ContinuationRow, FileContext, MessageRow, SessionKey,  # noqa: E402
                                 SessionRow, SubagentSpawnRow, ToolCallRow, ToolIoRow, ToolOpRow)

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

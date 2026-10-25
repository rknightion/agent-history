"""Task reference derivation (load.task_refs) against a disposable database. Run: just ci.

task_ref is every tracker-id mention that names a known, unremoved task. A task created or removed
on a board must re-derive task_ref from the stored mentions without re-reading message text; only
a prefix change rescans messages. Whatever the path, the result equals a from-scratch derivation.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest

DSN = os.environ.get("AGENT_HISTORY_TEST_DSN", "")
T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


@pytest.fixture
def conn():
    if not DSN or "agent_history_test" not in DSN:
        pytest.skip("AGENT_HISTORY_TEST_DSN (a *_test database) not set")
    pytest.importorskip("psycopg")
    from agent_history import load

    c = load.connect(DSN)
    load.apply_schema(c, force=True)
    c.execute(
        "TRUNCATE "
        + ", ".join(f"ah.{t}" for t in load.DATA_TABLES + ("task_ref", "task_prefix", "backlog_task"))
        + " RESTART IDENTITY CASCADE"
    )
    c.execute("DELETE FROM ah.meta WHERE key IN ('task_ref_hash', 'task_mention_hash')")
    c.execute(
        "INSERT INTO ah.task_prefix (prefix, repo_slug, context, zero_pad) VALUES ('ABC', 'example/abc', 'local', 4)"
    )
    c.execute(
        "INSERT INTO ah.backlog_task (task_key, repo_slug, context) VALUES "
        "('ABC-0001', 'example/abc', 'local'), ('ABC-0002', 'example/abc', 'local')"
    )
    c.execute(
        "INSERT INTO ah.source_file (rel_path, namespace, agent, profile, file_role, tier) "
        "VALUES ('claude-test/s.jsonl', 'claude-test', 'claude', 'test', 'main', 'hot')"
    )
    c.commit()
    yield c
    c.rollback()
    c.execute("TRUNCATE ah.session, ah.source_file, ah.task_prefix, ah.backlog_task RESTART IDENTITY CASCADE")
    c.execute("DELETE FROM ah.meta WHERE key IN ('task_ref_hash', 'task_mention_hash')")
    c.commit()
    c.close()


def session(c, uid: str) -> int:
    return c.execute(
        "INSERT INTO ah.session (agent, session_uid, namespace, is_stub) VALUES ('claude', %s, 'claude-test', false) "
        "RETURNING id",
        (uid,),
    ).fetchone()[0]


def message(c, session_id: int, n: int, text: str, cls: str = "assistant_text") -> None:
    c.execute(
        "INSERT INTO ah.message (agent, event_uid, session_id, namespace, profile, ts, role, message_class, "
        "text, content_sha256, source_id, byte_offset, byte_length) "
        "VALUES ('claude', %s, %s, 'claude-test', 'test', %s, 'user', %s, %s, md5(%s), 1, %s, 1)",
        (f"{session_id}-{n}", session_id, T0 + timedelta(minutes=n), cls, text, text, n),
    )


def run(c, dirty: tuple[int, ...] = ()) -> str:
    from agent_history import load

    with c.transaction():
        c.execute("CREATE TEMP TABLE IF NOT EXISTS dirty_now (session_id bigint PRIMARY KEY) ON COMMIT DELETE ROWS")
        for s in dirty:
            c.execute("INSERT INTO dirty_now VALUES (%s)", (s,))
        return load.task_refs(c)


def refs(c) -> set[tuple]:
    return set(c.execute("SELECT session_id, task_key, mentions, in_human, in_brief FROM ah.task_ref").fetchall())


def from_scratch(c) -> set[tuple]:
    """task_ref as a fresh catalogue would derive it, leaving the incremental state untouched."""
    import psycopg

    with c.transaction():
        c.execute("DELETE FROM ah.meta WHERE key IN ('task_ref_hash', 'task_mention_hash')")
        c.execute("DELETE FROM ah.task_ref")
        assert run(c) == "full"
        fresh = refs(c)
        raise psycopg.Rollback()
    return fresh


def test_task_set_changes_rederive_refs_without_rescanning_messages(conn):
    c = conn
    s1, s2 = session(c, "s1"), session(c, "s2")
    message(c, s1, 1, "Please pick up ABC-1 and ABC-0002 today", "human_prompt")
    message(c, s1, 2, "Opened abc-0003 - Title.md and checked ABC-0002 again")
    message(c, s2, 3, "Lane: ABC-0002 (synthetic)", "subagent_brief")
    c.commit()

    assert run(c, (s1, s2)) == "full"  # no stored prefix hash: one rescan fills the mentions
    assert refs(c) == {
        (s1, "ABC-0001", 1, True, False),
        (s1, "ABC-0002", 2, True, False),
        (s2, "ABC-0002", 1, False, True),
    }
    assert c.execute("SELECT count(*) FROM ah.task_mention WHERE task_key = 'ABC-0003'").fetchone()[0] == 1

    # Message text changed behind the catalogue's back: a task-set change must not reread it.
    c.execute("UPDATE ah.message SET text = text || ' ABC-0002 ABC-0002' WHERE session_id = %s", (s1,))
    c.execute("INSERT INTO ah.backlog_task (task_key, repo_slug, context) VALUES ('ABC-0003', 'example/abc', 'local')")
    c.execute("UPDATE ah.backlog_task SET status = 'removed' WHERE task_key = 'ABC-0001'")
    c.commit()
    assert run(c) == "incremental"
    assert refs(c) == {
        (s1, "ABC-0002", 2, True, False),
        (s1, "ABC-0003", 1, False, False),
        (s2, "ABC-0002", 1, False, True),
    }

    # A dirty session rereads only its own messages.
    message(c, s2, 4, "ABC-0003 is next", "human_prompt")
    c.commit()
    assert run(c, (s2,)) == "incremental"
    assert (s2, "ABC-0003", 1, True, False) in refs(c)
    assert (s1, "ABC-0003", 1, False, False) in refs(c)  # s1's stale text still unread

    # A prefix change rescans every message, picking up s1's changed text.
    c.execute(
        "INSERT INTO ah.task_prefix (prefix, repo_slug, context, zero_pad) VALUES ('XYZ', 'example/xyz', 'local', 4)"
    )
    c.execute("UPDATE ah.backlog_task SET status = NULL WHERE task_key = 'ABC-0001'")
    c.commit()
    assert run(c) == "full"
    assert (s1, "ABC-0002", 6, True, False) in refs(c)
    assert refs(c) == from_scratch(c)


def test_incremental_paths_match_a_fresh_derivation(conn):
    c = conn
    s1, s2 = session(c, "s1"), session(c, "s2")
    message(c, s1, 1, "ABC-0001 then ABC-0002", "human_prompt")
    c.commit()
    run(c, (s1,))
    for step in range(3):
        message(c, s2, 10 + step, f"working on ABC-000{step + 1}")
        c.execute(
            "INSERT INTO ah.backlog_task (task_key, repo_slug, context) VALUES (%s, 'example/abc', 'local') "
            "ON CONFLICT DO NOTHING",
            (f"ABC-000{step + 3}",),
        )
        if step == 1:
            c.execute("UPDATE ah.backlog_task SET status = 'removed' WHERE task_key = 'ABC-0002'")
        c.commit()
        assert run(c, (s2,)) == "incremental"
        assert refs(c) == from_scratch(c)

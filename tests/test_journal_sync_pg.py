"""Read-only journal export integration against a disposable ParadeDB and synthetic SQLite."""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest

DSN = os.environ.get("AGENT_HISTORY_TEST_DSN", "")
pytestmark = pytest.mark.skipif(
    not DSN or "agent_history_test" not in DSN, reason="AGENT_HISTORY_TEST_DSN (a *_test database) not set"
)

psycopg = pytest.importorskip("psycopg")

from agent_history import journal_sync, load  # noqa: E402


@pytest.fixture(scope="module")
def conn():
    connection = load.connect(DSN)
    load.apply_schema(connection, force=True)
    connection.commit()
    yield connection
    connection.close()


@pytest.fixture
def clean(conn):
    conn.execute("DELETE FROM ah.session_topic")
    conn.execute("DELETE FROM ah.session_summary")
    conn.execute("DELETE FROM ah.session")
    conn.execute(
        "DELETE FROM ah.meta WHERE key IN ('journal_sync_at', 'journal_full_sync_at', 'journal_app_instance_id')"
    )
    conn.commit()
    return conn


def make_row(**over) -> dict:
    base = dict(
        journal_conversation_id="conv-1",
        agent="claude",
        session_uid="sess-1",
        agent_id="",
        namespace="claude-personal",
        revision_id="rev-1",
        analysed_at="2026-09-20T10:00:00Z",
        model="claude-sonnet-5",
        title="Title",
        objective="Objective",
        narrative="Narrative",
        outcomes='["did a"]',
        unfinished='["todo b"]',
        classification="Personal",
        project="camden",
        topics_json='[{"slug":"grafana","provenance":"auto","confidence":0.8}]',
        app_instance_id="inst-1",
    )
    base.update(over)
    return base


def build_app_db(
    path: Path, rows: list[dict], *, view_columns: tuple[str, ...] | None = None, create_view: bool = True
) -> None:
    """A tiny SQLite db: a backing table with every journal_sync.COLUMNS, and (usually) the view."""
    if view_columns is None:
        view_columns = journal_sync.COLUMNS
    for suffix in ("", "-wal", "-shm", "-journal"):
        Path(str(path) + suffix).unlink(missing_ok=True)
    db = sqlite3.connect(path)
    try:
        cols = journal_sync.COLUMNS
        db.execute(f"CREATE TABLE session_analyses ({', '.join(c + ' TEXT' for c in cols)})")
        for row in rows:
            placeholders = ", ".join("?" for _ in cols)
            db.execute(
                f"INSERT INTO session_analyses ({', '.join(cols)}) VALUES ({placeholders})", [row.get(c) for c in cols]
            )
        if create_view:
            db.execute(f"CREATE VIEW {journal_sync.VIEW} AS SELECT {', '.join(view_columns)} FROM session_analyses")
        db.commit()
    finally:
        db.close()


def insert_session(
    conn, *, session_uid: str, agent: str = "claude", agent_id: str = "", namespace: str | None = "claude-personal"
) -> int:
    return conn.execute(
        "INSERT INTO ah.session (agent, session_uid, agent_id, namespace, is_stub) VALUES (%s,%s,%s,%s,false) "
        "RETURNING id",
        (agent, session_uid, agent_id, namespace),
    ).fetchone()[0]


def fetch_summary(conn, conv: str) -> dict | None:
    cur = conn.execute(
        "SELECT session_id, namespace, agent, session_uid, agent_id, app_instance_id, title, "
        "outcomes, unfinished, classification, project, model, analysed_at "
        "FROM ah.session_summary WHERE journal_conversation_id = %s",
        (conv,),
    )
    row = cur.fetchone()
    if row is None:
        return None
    return dict(zip([d.name for d in cur.description], row))


def fetch_topics(conn, conv: str) -> set[str]:
    return {
        r[0] for r in conn.execute("SELECT topic FROM ah.session_topic WHERE journal_conversation_id = %s", (conv,))
    }


def force_full_pass(conn) -> None:
    conn.execute("DELETE FROM ah.meta WHERE key = 'journal_full_sync_at'")
    conn.commit()


def test_missing_file_skips(clean, tmp_path):
    result = journal_sync.sync(clean, tmp_path / "does-not-exist.db")
    assert result["journal_skipped_reason"]
    assert result["journal_rows"] == 0
    assert clean.execute("SELECT count(*) FROM ah.session_summary").fetchone()[0] == 0


def test_missing_view_skips(clean, tmp_path):
    db_path = tmp_path / "app.db"
    build_app_db(db_path, [make_row()], create_view=False)
    result = journal_sync.sync(clean, db_path)
    assert "ah_export_session_summary" in result["journal_skipped_reason"]
    assert result["journal_rows"] == 0


def test_missing_column_skips(clean, tmp_path):
    db_path = tmp_path / "app.db"
    partial = tuple(c for c in journal_sync.COLUMNS if c != "project")
    build_app_db(db_path, [make_row()], view_columns=partial)
    result = journal_sync.sync(clean, db_path)
    assert result["journal_skipped_reason"]
    assert result["journal_rows"] == 0


def test_natural_key_mapping_subagent_and_unmatched(clean, tmp_path):
    main_id = insert_session(clean, session_uid="sess-1", agent_id="", namespace="claude-personal")
    sub_id = insert_session(
        clean, session_uid="sess-1", agent_id="agent-a0000000000000001", namespace="claude-personal"
    )
    clean.commit()
    db_path = tmp_path / "app.db"
    build_app_db(
        db_path,
        [
            make_row(journal_conversation_id="conv-main", agent_id="", namespace="wrong-ns"),
            make_row(journal_conversation_id="conv-sub", agent_id="agent-a0000000000000001", session_uid="sess-1"),
            make_row(
                journal_conversation_id="conv-unmatched",
                session_uid="sess-none",
                agent_id="",
                namespace="codex-personal",
            ),
        ],
    )
    result = journal_sync.sync(clean, db_path)
    assert result["journal_rows"] == 3
    assert result["journal_matched"] == 2
    assert result["journal_unmatched"] == 1

    main_row = fetch_summary(clean, "conv-main")
    assert main_row["session_id"] == main_id
    assert main_row["namespace"] == "claude-personal"  # matched session's namespace wins over the view's
    sub_row = fetch_summary(clean, "conv-sub")
    assert sub_row["session_id"] == sub_id
    unmatched_row = fetch_summary(clean, "conv-unmatched")
    assert unmatched_row["session_id"] is None
    assert unmatched_row["namespace"] == "codex-personal"  # falls back to the view's namespace


def test_incremental_watermark(clean, tmp_path):
    insert_session(clean, session_uid="sess-1")
    clean.commit()
    db_path = tmp_path / "app.db"
    build_app_db(db_path, [make_row(journal_conversation_id="conv-1", analysed_at="2026-09-20T10:00:00Z")])
    first = journal_sync.sync(clean, db_path)
    assert first["journal_rows"] == 1

    # No new rows: a same-day rerun is incremental and finds nothing.
    second = journal_sync.sync(clean, db_path)
    assert second["journal_rows"] == 0
    assert clean.execute("SELECT count(*) FROM ah.session_summary").fetchone()[0] == 1

    # A newer row is picked up without disturbing the first.
    build_app_db(
        db_path,
        [
            make_row(journal_conversation_id="conv-1", analysed_at="2026-09-20T10:00:00Z"),
            make_row(journal_conversation_id="conv-2", analysed_at="2026-09-20T11:00:00Z"),
        ],
    )
    third = journal_sync.sync(clean, db_path)
    assert third["journal_rows"] == 1
    assert clean.execute("SELECT count(*) FROM ah.session_summary").fetchone()[0] == 2


def test_full_pass_deletes_vanished_rows(clean, tmp_path):
    insert_session(clean, session_uid="sess-1")
    clean.commit()
    db_path = tmp_path / "app.db"
    build_app_db(
        db_path,
        [
            make_row(journal_conversation_id="conv-a", analysed_at="2026-09-20T10:00:00Z"),
            make_row(journal_conversation_id="conv-b", analysed_at="2026-09-20T10:05:00Z"),
        ],
    )
    journal_sync.sync(clean, db_path)
    assert clean.execute("SELECT count(*) FROM ah.session_summary").fetchone()[0] == 2
    assert fetch_topics(clean, "conv-b")

    build_app_db(db_path, [make_row(journal_conversation_id="conv-a", analysed_at="2026-09-20T10:00:00Z")])
    force_full_pass(clean)
    result = journal_sync.sync(clean, db_path)
    assert result["journal_deleted"] == 1
    assert clean.execute("SELECT journal_conversation_id FROM ah.session_summary").fetchall() == [("conv-a",)]
    assert fetch_topics(clean, "conv-b") == set()


def test_app_instance_id_change_wipes_and_resyncs(clean, tmp_path):
    insert_session(clean, session_uid="sess-1")
    clean.commit()
    db_path = tmp_path / "app.db"
    build_app_db(db_path, [make_row(journal_conversation_id="conv-old", app_instance_id="inst-1")])
    first = journal_sync.sync(clean, db_path)
    assert first["journal_rows"] == 1
    assert clean.execute("SELECT count(*) FROM ah.session_summary").fetchone()[0] == 1

    build_app_db(db_path, [make_row(journal_conversation_id="conv-new", app_instance_id="inst-2")])
    result = journal_sync.sync(clean, db_path)
    assert result.get("journal_reset") is True
    assert result["journal_deleted"] >= 1
    rows = clean.execute("SELECT journal_conversation_id FROM ah.session_summary").fetchall()
    assert rows == [("conv-new",)]
    stored = clean.execute("SELECT value FROM ah.meta WHERE key = 'journal_app_instance_id'").fetchone()
    assert stored[0] == "inst-2"


def test_failed_reset_after_a_completed_batch_preserves_catalogue(clean, tmp_path):
    db_path = tmp_path / "app.db"
    build_app_db(db_path, [make_row(journal_conversation_id="conv-old", app_instance_id="inst-old")])
    journal_sync.sync(clean, db_path)
    before_meta = clean.execute("SELECT key, value FROM ah.meta WHERE key LIKE 'journal_%' ORDER BY key").fetchall()
    clean.execute(
        "ALTER TABLE ah.session_summary ADD CONSTRAINT synthetic_journal_failure "
        "CHECK (journal_conversation_id <> 'conv-fail')"
    )
    clean.commit()
    rows = [
        make_row(journal_conversation_id=f"conv-new-{i}", app_instance_id="inst-new") for i in range(journal_sync.BATCH)
    ]
    rows.append(make_row(journal_conversation_id="conv-fail", app_instance_id="inst-new"))
    build_app_db(db_path, rows)
    try:
        with pytest.raises(psycopg.errors.CheckViolation):
            journal_sync.sync(clean, db_path)
        clean.rollback()
        assert clean.execute("SELECT journal_conversation_id FROM ah.session_summary").fetchall() == [("conv-old",)]
        assert fetch_topics(clean, "conv-old") == {"grafana"}
        assert (
            clean.execute("SELECT key, value FROM ah.meta WHERE key LIKE 'journal_%' ORDER BY key").fetchall()
            == before_meta
        )
    finally:
        clean.rollback()
        clean.execute("ALTER TABLE ah.session_summary DROP CONSTRAINT synthetic_journal_failure")
        clean.commit()


@pytest.mark.parametrize("reset", [False, True])
@pytest.mark.parametrize("invalid_timestamp", ["not-a-timestamp", None, "2026-09-20T12:00:00"])
def test_invalid_source_timestamp_preserves_entire_catalogue(clean, tmp_path, reset, invalid_timestamp):
    db_path = tmp_path / "app.db"
    build_app_db(db_path, [make_row(journal_conversation_id="conv-old", app_instance_id="inst-old")])
    journal_sync.sync(clean, db_path)
    # A daily reconciliation must not mistake a retained invalid record for a deleted one.
    clean.execute("UPDATE ah.meta SET value = '2000-01-01T00:00:00Z' WHERE key = 'journal_full_sync_at'")
    clean.commit()
    before_summary = clean.execute("SELECT * FROM ah.session_summary ORDER BY journal_conversation_id").fetchall()
    before_topics = clean.execute("SELECT * FROM ah.session_topic ORDER BY journal_conversation_id, topic").fetchall()
    before_meta = clean.execute("SELECT key, value FROM ah.meta WHERE key LIKE 'journal_%' ORDER BY key").fetchall()
    clean.commit()
    instance = "inst-new" if reset else "inst-old"
    # Sorting NULLs first otherwise hides the completed-batch case. Non-NULL invalid
    # timestamps sort after these valid records and cross the real batch boundary.
    rows = [
        make_row(journal_conversation_id=f"conv-new-{i}", app_instance_id=instance) for i in range(journal_sync.BATCH)
    ]
    rows.append(make_row(journal_conversation_id="conv-old", app_instance_id=instance, analysed_at=invalid_timestamp))
    build_app_db(db_path, rows)
    before_source = db_path.read_bytes()
    result = journal_sync.sync(clean, db_path)
    assert "analysed_at" in result.get("journal_skipped_reason", "")
    assert result["journal_rows"] == 0 and result["journal_deleted"] == 0
    assert "journal_reset" not in result
    with psycopg.connect(DSN) as independent:
        assert (
            independent.execute("SELECT * FROM ah.session_summary ORDER BY journal_conversation_id").fetchall()
            == before_summary
        )
        assert (
            independent.execute("SELECT * FROM ah.session_topic ORDER BY journal_conversation_id, topic").fetchall()
            == before_topics
        )
        assert (
            independent.execute("SELECT key, value FROM ah.meta WHERE key LIKE 'journal_%' ORDER BY key").fetchall()
            == before_meta
        )
    assert db_path.read_bytes() == before_source


def test_multiple_distinct_instance_ids_skips(clean, tmp_path):
    db_path = tmp_path / "app.db"
    build_app_db(
        db_path,
        [
            make_row(journal_conversation_id="conv-1", app_instance_id="inst-1"),
            make_row(journal_conversation_id="conv-2", app_instance_id="inst-2"),
        ],
    )
    result = journal_sync.sync(clean, db_path)
    assert result["journal_skipped_reason"]
    assert result["journal_rows"] == 0
    assert clean.execute("SELECT count(*) FROM ah.session_summary").fetchone()[0] == 0


def test_topics_replaced(clean, tmp_path):
    insert_session(clean, session_uid="sess-1")
    clean.commit()
    db_path = tmp_path / "app.db"
    build_app_db(
        db_path,
        [
            make_row(
                journal_conversation_id="conv-1",
                analysed_at="2026-09-20T10:00:00Z",
                topics_json=json.dumps([{"slug": "grafana", "provenance": "auto", "confidence": 0.8}]),
            )
        ],
    )
    journal_sync.sync(clean, db_path)
    assert fetch_topics(clean, "conv-1") == {"grafana"}

    build_app_db(
        db_path,
        [
            make_row(
                journal_conversation_id="conv-1",
                analysed_at="2026-09-20T12:00:00Z",
                topics_json=json.dumps([{"slug": "n8n", "provenance": "human", "confidence": None}]),
            )
        ],
    )
    journal_sync.sync(clean, db_path)
    assert fetch_topics(clean, "conv-1") == {"n8n"}


# ---------------------------------------------------------------------------------------------
# Write-amplification guards: an unchanged conversation must not be rewritten.
# ---------------------------------------------------------------------------------------------

_TRIGGER_DDL = """
CREATE TABLE ah.test_write_log (tbl text NOT NULL, op text NOT NULL);
CREATE FUNCTION ah.test_write_log_fn() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    INSERT INTO ah.test_write_log VALUES (TG_TABLE_NAME, TG_OP);
    RETURN NULL;
END $$;
CREATE TRIGGER test_write_log_summary AFTER INSERT OR UPDATE OR DELETE ON ah.session_summary
    FOR EACH ROW EXECUTE FUNCTION ah.test_write_log_fn();
CREATE TRIGGER test_write_log_topic AFTER INSERT OR UPDATE OR DELETE ON ah.session_topic
    FOR EACH ROW EXECUTE FUNCTION ah.test_write_log_fn();
"""

_TRIGGER_DROP = """
DROP TRIGGER IF EXISTS test_write_log_summary ON ah.session_summary;
DROP TRIGGER IF EXISTS test_write_log_topic ON ah.session_topic;
DROP FUNCTION IF EXISTS ah.test_write_log_fn();
DROP TABLE IF EXISTS ah.test_write_log;
"""


@pytest.fixture
def write_log(clean):
    """Counts every row written to session_summary and session_topic by a test-only row trigger."""
    clean.execute(_TRIGGER_DROP)
    clean.execute(_TRIGGER_DDL)
    clean.commit()

    def writes() -> list[tuple[str, str, int]]:
        return clean.execute(
            "SELECT tbl, op, count(*) FROM ah.test_write_log GROUP BY tbl, op ORDER BY tbl, op"
        ).fetchall()

    def reset() -> None:
        clean.execute("DELETE FROM ah.test_write_log")

    writes.reset = reset  # type: ignore[attr-defined]
    try:
        yield writes
    finally:
        clean.rollback()
        clean.execute(_TRIGGER_DROP)
        clean.commit()


def fetch_topic_rows(conn, conv: str) -> dict[str, tuple]:
    return {
        r[0]: (r[1], r[2], None if r[3] is None else float(r[3]))
        for r in conn.execute(
            "SELECT topic, session_id, provenance, confidence FROM ah.session_topic WHERE journal_conversation_id = %s",
            (conv,),
        )
    }


def _topics(*items) -> str:
    return json.dumps([{"slug": s, "provenance": p, "confidence": c} for s, p, c in items])


def test_unchanged_conversation_is_not_rewritten_on_resync_or_full_pass(clean, write_log, tmp_path):
    sid = insert_session(clean, session_uid="sess-1")
    clean.commit()
    db_path = tmp_path / "app.db"
    # Non-integer confidence (numeric vs float must not look like a change) and a duplicate slug.
    topics = _topics(("grafana", "auto", 0.8), ("grafana", "human", 0.2), ("n8n", "human", None), ("loki", "auto", 1))
    build_app_db(db_path, [make_row(topics_json=topics), make_row(journal_conversation_id="conv-2", topics_json="[]")])
    journal_sync.sync(clean, db_path)
    clean.commit()
    # First occurrence of a duplicate slug wins.
    assert fetch_topic_rows(clean, "conv-1") == {
        "grafana": (sid, "auto", 0.8),
        "n8n": (sid, "human", None),
        "loki": (sid, "auto", 1.0),
    }
    assert write_log() != []

    write_log.reset()
    clean.commit()
    force_full_pass(clean)
    result = journal_sync.sync(clean, db_path)
    assert result["journal_rows"] == 2  # the full pass really did revisit every conversation
    assert write_log() == []
    assert fetch_topic_rows(clean, "conv-1")["grafana"] == (sid, "auto", 0.8)

    # And again, as a plain incremental pass that re-presents the rows (watermark cleared).
    clean.execute("DELETE FROM ah.meta WHERE key = 'journal_sync_at'")
    clean.commit()
    journal_sync.sync(clean, db_path)
    assert write_log() == []


def test_changed_topic_sets_converge_exactly(clean, write_log, tmp_path):
    sid = insert_session(clean, session_uid="sess-1")
    clean.commit()
    db_path = tmp_path / "app.db"

    def run(topics_json: str, analysed_at: str) -> None:
        build_app_db(db_path, [make_row(analysed_at=analysed_at, topics_json=topics_json)])
        force_full_pass(clean)
        journal_sync.sync(clean, db_path)

    run(_topics(("a", "auto", 0.8), ("b", "auto", 0.5), ("c", "human", None)), "2026-09-20T10:00:00Z")
    assert fetch_topic_rows(clean, "conv-1") == {
        "a": (sid, "auto", 0.8),
        "b": (sid, "auto", 0.5),
        "c": (sid, "human", None),
    }

    # a: confidence changed; b: provenance changed; c: removed; d: added; e: duplicate, first wins.
    run(
        _topics(("a", "auto", 0.9), ("b", "human", 0.5), ("d", "auto", 0.25), ("e", "auto", 0.1), ("e", "human", 0.9)),
        "2026-09-20T11:00:00Z",
    )
    assert fetch_topic_rows(clean, "conv-1") == {
        "a": (sid, "auto", 0.9),
        "b": (sid, "human", 0.5),
        "d": (sid, "auto", 0.25),
        "e": (sid, "auto", 0.1),
    }

    # Only the changed rows are written: a, b updated; c deleted; d, e inserted.
    write_log.reset()
    clean.commit()
    run(
        _topics(("a", "auto", 0.9), ("b", "human", 0.5), ("d", "auto", 0.25), ("e", "auto", 0.1), ("f", "auto", 0.3)),
        "2026-09-20T12:00:00Z",
    )
    topic_ops = {op: n for tbl, op, n in write_log() if tbl == "session_topic"}
    assert topic_ops == {"INSERT": 1}  # f added; nothing else touched
    assert fetch_topic_rows(clean, "conv-1").keys() == {"a", "b", "d", "e", "f"}

    # Dropping everything deletes every topic.
    run("[]", "2026-09-20T13:00:00Z")
    assert fetch_topic_rows(clean, "conv-1") == {}
    # Invalid topics JSON behaves as no topics (unchanged semantics).
    run(_topics(("a", "auto", 0.9)), "2026-09-20T14:00:00Z")
    run("not json", "2026-09-20T15:00:00Z")
    assert fetch_topic_rows(clean, "conv-1") == {}


def test_topic_session_link_follows_the_matched_session(clean, tmp_path):
    db_path = tmp_path / "app.db"
    build_app_db(db_path, [make_row(topics_json=_topics(("a", "auto", 0.8)))])
    journal_sync.sync(clean, db_path)
    assert fetch_topic_rows(clean, "conv-1") == {"a": (None, "auto", 0.8)}

    sid = insert_session(clean, session_uid="sess-1")
    clean.commit()
    force_full_pass(clean)
    journal_sync.sync(clean, db_path)
    assert fetch_topic_rows(clean, "conv-1") == {"a": (sid, "auto", 0.8)}


def test_topic_update_in_place_keeps_rebuild_stash_unless_the_session_moves(clean, tmp_path):
    sid = insert_session(clean, session_uid="sess-1")
    clean.commit()
    db_path = tmp_path / "app.db"
    build_app_db(db_path, [make_row(topics_json=_topics(("a", "auto", 0.8)))])
    journal_sync.sync(clean, db_path)
    clean.execute("UPDATE ah.session_topic SET session_agent = 'claude', session_uid = 'sess-1', session_agent_id = ''")
    clean.commit()

    # A confidence change updates the row in place: the stash stays, because the session did not move.
    build_app_db(db_path, [make_row(topics_json=_topics(("a", "auto", 0.9)))])
    force_full_pass(clean)
    journal_sync.sync(clean, db_path)
    row = clean.execute(
        "SELECT session_id, confidence, session_agent, session_uid, session_agent_id FROM ah.session_topic"
    ).fetchone()
    assert (row[0], float(row[1]), row[2], row[3], row[4]) == (sid, 0.9, "claude", "sess-1", "")

    # The session disappearing (a rebuild truncated ah.session) moves session_id to NULL: stash cleared,
    # exactly as a fresh insert would have left it.
    clean.execute("UPDATE ah.session_summary SET session_id = NULL")
    clean.execute("DELETE FROM ah.session")
    clean.commit()
    force_full_pass(clean)
    journal_sync.sync(clean, db_path)
    row = clean.execute(
        "SELECT session_id, session_agent, session_uid, session_agent_id FROM ah.session_topic"
    ).fetchone()
    assert row == (None, None, None, None)


def test_summary_upsert_keeps_analysed_at_ordering(clean, write_log, tmp_path):
    insert_session(clean, session_uid="sess-1")
    clean.commit()
    db_path = tmp_path / "app.db"

    def run(**over) -> None:
        build_app_db(db_path, [make_row(**over)])
        force_full_pass(clean)
        journal_sync.sync(clean, db_path)

    run(analysed_at="2026-09-20T10:00:00Z", title="first")
    assert fetch_summary(clean, "conv-1")["title"] == "first"

    # An older analysis never overwrites a newer one.
    run(analysed_at="2026-09-20T09:00:00Z", title="older")
    assert fetch_summary(clean, "conv-1")["title"] == "first"

    # A genuinely newer analysis does.
    run(analysed_at="2026-09-20T11:00:00Z", title="newer")
    assert fetch_summary(clean, "conv-1")["title"] == "newer"

    # Same timestamp, changed content, is still applied (a re-analysis of the same instant).
    run(analysed_at="2026-09-20T11:00:00Z", title="same-instant edit", outcomes='["did a", "did b"]')
    row = fetch_summary(clean, "conv-1")
    assert row["title"] == "same-instant edit" and row["outcomes"] == ["did a", "did b"]

    # Identical content again: no write at all.
    write_log.reset()
    clean.commit()
    run(analysed_at="2026-09-20T11:00:00Z", title="same-instant edit", outcomes='["did a", "did b"]')
    assert [w for w in write_log() if w[0] == "session_summary"] == []


def test_reset_replaces_topics_and_empty_view_full_pass_prunes_them(clean, tmp_path):
    insert_session(clean, session_uid="sess-1")
    clean.commit()
    db_path = tmp_path / "app.db"
    build_app_db(
        db_path,
        [
            make_row(
                journal_conversation_id="conv-old", app_instance_id="inst-1", topics_json=_topics(("a", "auto", 0.8))
            )
        ],
    )
    journal_sync.sync(clean, db_path)
    assert fetch_topics(clean, "conv-old") == {"a"}

    build_app_db(
        db_path,
        [
            make_row(
                journal_conversation_id="conv-new", app_instance_id="inst-2", topics_json=_topics(("b", "auto", 0.6))
            )
        ],
    )
    result = journal_sync.sync(clean, db_path)
    assert result.get("journal_reset") is True
    assert fetch_topics(clean, "conv-old") == set()
    assert fetch_topic_rows(clean, "conv-new").keys() == {"b"}

    # Everything leaves the view: a full pass prunes every summary and topic.
    build_app_db(db_path, [])
    force_full_pass(clean)
    journal_sync.sync(clean, db_path)
    assert clean.execute("SELECT count(*) FROM ah.session_summary").fetchone()[0] == 0
    assert clean.execute("SELECT count(*) FROM ah.session_topic").fetchone()[0] == 0

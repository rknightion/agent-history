"""Migration 046 indexes ah.subagent_spawn(child_session_id) on fresh and upgraded catalogues."""

import os
import shutil
from pathlib import Path

import pytest

psycopg = pytest.importorskip("psycopg")
from agent_history import load  # noqa: E402

DSN = os.environ.get("AGENT_HISTORY_TEST_DSN", "")
ADMIN_DSN = os.environ.get("AGENT_HISTORY_TEST_ADMIN_DSN", "")
pytestmark = pytest.mark.skipif("agent_history_test" not in DSN, reason="disposable catalogue DSN not set")
MIGRATION = "046_subagent_spawn_child_session_idx.sql"
INDEX = "subagent_spawn_child_session_idx"


@pytest.fixture
def db():
    reset_schema()
    try:
        with psycopg.connect(DSN) as conn:
            try:
                yield conn
            finally:
                conn.rollback()
    finally:
        reset_schema()
        with psycopg.connect(DSN) as conn:
            load.apply_schema(conn, force=True)


def reset_schema():
    assert "agent_history_test" in ADMIN_DSN, "disposable administrative DSN required"
    with psycopg.connect(DSN) as writer:
        target = writer.execute("SELECT current_database()").fetchone()[0]
    with psycopg.connect(ADMIN_DSN) as admin:
        # The DSN text alone proves nothing: both connections must land on the same disposable database.
        actual = admin.execute("SELECT current_database()").fetchone()[0]
        assert "agent_history_test" in actual and actual == target, "disposable catalogue required"
        admin.execute("DROP SCHEMA IF EXISTS ah CASCADE")
        admin.execute("CREATE SCHEMA ah AUTHORIZATION ah_writer")
        admin.execute("GRANT USAGE ON SCHEMA ah TO ah_reader")
        admin.execute("ALTER DEFAULT PRIVILEGES FOR ROLE ah_writer IN SCHEMA ah GRANT SELECT ON TABLES TO ah_reader")


def sql_dir():
    return Path(getattr(load, "SQL_DIR", load.PACKAGE_DIR))


def index_columns(conn):
    """Key columns of the child-session index, or None when it is absent."""
    row = conn.execute(
        "SELECT i.indisvalid, i.indnkeyatts, array_agg(a.attname ORDER BY k.ord) "
        "FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid "
        "JOIN LATERAL unnest(i.indkey::int2[]) WITH ORDINALITY AS k(attnum, ord) ON true "
        "JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = k.attnum "
        "WHERE c.relname = %s AND c.relnamespace = 'ah'::regnamespace "
        "AND i.indrelid = 'ah.subagent_spawn'::regclass "
        "GROUP BY i.indisvalid, i.indnkeyatts",
        (INDEX,),
    ).fetchone()
    return row


def test_fresh_init_has_child_session_index(db):
    load.apply_schema(db, force=True)
    assert index_columns(db) == (True, 1, ["child_session_id"])


def test_upgrade_from_ledger_before_046_adds_index(db, tmp_path, monkeypatch):
    source = sql_dir()
    old = tmp_path / "sql045"
    shutil.copytree(source, old)
    pending = old / "migrations" / MIGRATION
    if pending.exists():
        pending.unlink()  # only the temporary copy, not migration history
    attr = "SQL_DIR" if hasattr(load, "SQL_DIR") else "PACKAGE_DIR"
    with monkeypatch.context() as patch:
        patch.setattr(load, attr, old)
        load.apply_schema(db, force=True)
    # A catalogue whose ledger predates 046 has no such index (the baseline must not carry it).
    assert index_columns(db) is None
    assert db.execute("SELECT count(*) FROM ah.meta WHERE key = %s", ("migration:" + MIGRATION,)).fetchone() == (0,)
    db.commit()
    assert load.apply_schema(db, force=True)
    assert index_columns(db) == (True, 1, ["child_session_id"])
    assert db.execute("SELECT count(*) FROM ah.meta WHERE key = %s", ("migration:" + MIGRATION,)).fetchone() == (1,)
    # Replaying the DDL is harmless.
    db.execute((source / "migrations" / MIGRATION).read_text())
    assert index_columns(db) == (True, 1, ["child_session_id"])

"""Migration 049 adds pi cost provenance to ah.llm_call on fresh and upgraded catalogues."""

import os
import shutil
from pathlib import Path

import pytest

psycopg = pytest.importorskip("psycopg")
from agent_history import load  # noqa: E402

DSN = os.environ.get("AGENT_HISTORY_TEST_DSN", "")
ADMIN_DSN = os.environ.get("AGENT_HISTORY_TEST_ADMIN_DSN", "")
pytestmark = pytest.mark.skipif("agent_history_test" not in DSN, reason="disposable catalogue DSN not set")
MIGRATION = "049_pi_cost_source.sql"
EXPECTED = [("cost_estimate_usd", "numeric", "YES", None), ("cost_from_provider", "boolean", "YES", None)]


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
        # Both connections must land on the same disposable database, not merely carry the name.
        actual = admin.execute("SELECT current_database()").fetchone()[0]
        assert "agent_history_test" in actual and actual == target, "disposable catalogue required"
        admin.execute("DROP SCHEMA IF EXISTS ah CASCADE")
        admin.execute("CREATE SCHEMA ah AUTHORIZATION ah_writer")
        admin.execute("GRANT USAGE ON SCHEMA ah TO ah_reader")
        admin.execute("ALTER DEFAULT PRIVILEGES FOR ROLE ah_writer IN SCHEMA ah GRANT SELECT ON TABLES TO ah_reader")


def sql_dir():
    return Path(getattr(load, "SQL_DIR", load.PACKAGE_DIR))


def columns(conn):
    return conn.execute(
        "SELECT column_name, data_type, is_nullable, column_default FROM information_schema.columns "
        "WHERE table_schema = 'ah' AND table_name = 'llm_call' "
        "AND column_name IN ('cost_from_provider', 'cost_estimate_usd') ORDER BY column_name"
    ).fetchall()


def test_fresh_init_has_nullable_cost_source_columns(db):
    load.apply_schema(db, force=True)
    assert columns(db) == EXPECTED


def test_upgrade_from_ledger_before_049_adds_columns_without_defaults(db, tmp_path, monkeypatch):
    source = sql_dir()
    old = tmp_path / "sql048"
    shutil.copytree(source, old)
    (old / "migrations" / MIGRATION).unlink()  # only the temporary copy, not migration history
    attr = "SQL_DIR" if hasattr(load, "SQL_DIR") else "PACKAGE_DIR"
    with monkeypatch.context() as patch:
        patch.setattr(load, attr, old)
        load.apply_schema(db, force=True)
    # A catalogue whose ledger predates 049 has neither column (the baseline must not carry them).
    assert columns(db) == []
    assert db.execute("SELECT count(*) FROM ah.meta WHERE key = %s", ("migration:" + MIGRATION,)).fetchone() == (0,)
    db.commit()
    assert load.apply_schema(db, force=True)
    # Nullable with no default: existing rows stay NULL, never a false or zero backfill.
    assert columns(db) == EXPECTED
    assert db.execute("SELECT count(*) FROM ah.meta WHERE key = %s", ("migration:" + MIGRATION,)).fetchone() == (1,)
    # Replaying the DDL is harmless.
    db.execute((source / "migrations" / MIGRATION).read_text())
    assert columns(db) == EXPECTED

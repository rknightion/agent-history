"""Migration 021 preserves owner flags and the commit-quality reader contract."""

import os
import shutil
from pathlib import Path

import pytest

psycopg = pytest.importorskip("psycopg")
from agent_history import load  # noqa: E402

DSN = os.environ.get("AGENT_HISTORY_TEST_DSN", "")
ADMIN_DSN = os.environ.get("AGENT_HISTORY_TEST_ADMIN_DSN", "")
pytestmark = pytest.mark.skipif("agent_history_test" not in DSN, reason="disposable catalogue DSN not set")
MIGRATION = "021_drop_git_owner_alias.sql"
VIEWS = ("v_agent_commit_quality", "v_agent_commit_quality_weekly", "v_spawn_outcome")


@pytest.fixture
def db():
    reset_schema()
    try:
        with psycopg.connect(DSN) as conn:
            assert conn.execute("SELECT current_user").fetchone() == ("ah_writer",)
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
    # Match roles.sql: only the administrator creates the schema; the writer owns it
    # and creates objects whose default ACL grants the reader SELECT.
    with psycopg.connect(ADMIN_DSN) as admin:
        admin.execute("DROP SCHEMA IF EXISTS ah CASCADE")
        admin.execute("CREATE SCHEMA ah AUTHORIZATION ah_writer")
        admin.execute("GRANT USAGE ON SCHEMA ah TO ah_reader")
        admin.execute("ALTER DEFAULT PRIVILEGES FOR ROLE ah_writer IN SCHEMA ah GRANT SELECT ON TABLES TO ah_reader")


def sql_dir():
    return Path(getattr(load, "SQL_DIR", load.PACKAGE_DIR))


def assert_shape(conn):
    columns = {
        r[0]
        for r in conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_schema='ah' AND table_name='git_commit'"
        )
    }
    assert "author_is_rob" not in columns
    assert "author_is_owner" in columns
    assert conn.execute(
        "SELECT count(*) FROM pg_trigger WHERE tgrelid='ah.git_commit'::regclass AND tgname='git_commit_owner_sync'"
    ).fetchone() == (0,)
    assert conn.execute("SELECT to_regprocedure('ah.sync_git_commit_owner()')").fetchone() == (None,)
    for view in VIEWS:
        assert conn.execute("SELECT to_regclass(%s)", ("ah." + view,)).fetchone()[0] is not None
        assert conn.execute("SELECT has_table_privilege('ah_reader', %s, 'SELECT')", ("ah." + view,)).fetchone() == (
            True,
        )
        conn.execute(f"SELECT * FROM ah.{view} LIMIT 1")
    assert conn.execute("SELECT count(*) FROM ah.meta WHERE key=%s", ("migration:" + MIGRATION,)).fetchone() == (1,)


def test_migrate_020_preserves_owner_flags_and_reader_views(db, tmp_path, monkeypatch):
    source = sql_dir()
    old = tmp_path / "sql020"
    shutil.copytree(source, old)
    pending = old / "migrations" / MIGRATION
    if pending.exists():
        pending.unlink()  # only the temporary copy, not migration history
    # The public baseline is squashed at the latest version. Reconstitute its 020 compatibility
    # column with the immutable 018 migration; the private schema already reaches 020 normally.
    baseline = old / "baseline.sql"
    if baseline.exists() and "author_is_rob boolean" not in baseline.read_text():
        baseline.write_text(
            baseline.read_text()
            + "\nALTER TABLE ah.git_commit ADD COLUMN author_is_rob boolean;\n"
            + (source / "migrations" / "018_git_owner_alias.sql").read_text()
        )
    attr = "SQL_DIR" if hasattr(load, "SQL_DIR") else "PACKAGE_DIR"
    with monkeypatch.context() as patch:
        patch.setattr(load, attr, old)
        load.apply_schema(db, force=True)
    db.execute(
        "INSERT INTO ah.git_commit (repo_slug,sha,context,committed_at,author_is_owner,author_is_rob) "
        "VALUES ('example/widget',repeat('a',40),'personal',now(),true,true), "
        "('example/widget',repeat('b',40),'personal',now(),false,false), "
        "('example/widget',repeat('c',40),'personal',now(),NULL,NULL)"
    )
    before = db.execute("SELECT sha,author_is_owner FROM ah.git_commit ORDER BY sha").fetchall()
    db.commit()
    assert load.apply_schema(db, force=True)
    assert_shape(db)
    assert db.execute("SELECT sha,author_is_owner FROM ah.git_commit ORDER BY sha").fetchall() == before
    # Replay the DDL, not just the migration-key shortcut; apply_schema recreates the dropped views.
    db.execute((source / "migrations" / MIGRATION).read_text())
    db.execute((source / "migrations" / MIGRATION).read_text())
    load.apply_schema(db, force=True)
    assert_shape(db)
    assert db.execute("SELECT sha,author_is_owner FROM ah.git_commit ORDER BY sha").fetchall() == before


def test_fresh_init_has_only_owner_flag_and_reader_views(db):
    load.apply_schema(db, force=True)
    assert_shape(db)

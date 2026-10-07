"""Table-local autovacuum settings persist through the public init command."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import uuid

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from agent_history import cli, load

DSN = os.environ.get("AGENT_HISTORY_TEST_DSN", "")
ADMIN_DSN = os.environ.get("AGENT_HISTORY_TEST_ADMIN_DSN", "")
pytestmark = pytest.mark.skipif(
    "agent_history_test" not in conninfo_to_dict(DSN).get("dbname", ""),
    reason="disposable catalogue DSN required",
)
MIGRATION = "034_backlog_autovacuum.sql"
PREVIOUS = "033_nullable_telemetry.sql"
TARGETS = ("backlog_task", "backlog_done_event")
EXPECTED = {"autovacuum_vacuum_threshold": "25", "autovacuum_vacuum_scale_factor": "0.05"}


def writer_dsn(name):
    return make_conninfo(DSN, dbname=name, options="-c statement_timeout=60000 -c lock_timeout=10000")


@pytest.fixture
def catalogue():
    """Isolated database, with the same extensions and schema owner as roles.sql."""
    assert "agent_history_test" in conninfo_to_dict(ADMIN_DSN).get("dbname", ""), (
        "disposable administrative DSN required"
    )
    name = f"agent_history_test_autovacuum_{uuid.uuid4().hex}"
    with psycopg.connect(ADMIN_DSN, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    try:
        with psycopg.connect(make_conninfo(ADMIN_DSN, dbname=name), autocommit=True) as admin:
            admin.execute("CREATE EXTENSION IF NOT EXISTS vector")
            admin.execute("CREATE EXTENSION IF NOT EXISTS pg_search")
            admin.execute("CREATE SCHEMA ah AUTHORIZATION ah_writer")
        yield name
    finally:
        with psycopg.connect(ADMIN_DSN, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


def run_init(name):
    result = subprocess.run(
        [sys.executable, "-m", "agent_history.cli", "--dsn", writer_dsn(name), "init"],
        env={**os.environ, "AGENT_HISTORY_CONFIG": "/dev/null/absent"},
        capture_output=True,
        text=True,
        timeout=90,
    )
    # A connection error can contain the DSN, so do not echo subprocess diagnostics.
    assert result.returncode == 0, "init CLI failed"
    assert result.stdout.strip() == "applied"


def reloptions(name):
    # Inspect persisted pg_class state over a new connection after the CLI has exited.
    with psycopg.connect(writer_dsn(name)) as conn:
        return {
            table: dict(option.split("=", 1) for option in (options or []))
            for table, options in conn.execute(
                "SELECT c.relname, c.reloptions FROM pg_class c "
                "JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'ah'"
            )
        }


def assert_tuned(name, extra=None):
    options = reloptions(name)
    for table in TARGETS:
        assert options[table] == EXPECTED | (extra or {})
    with psycopg.connect(writer_dsn(name)) as conn:
        assert conn.execute("SELECT count(*) FROM ah.meta WHERE key = %s", ("migration:" + MIGRATION,)).fetchone() == (
            1,
        )
        assert conn.execute("SELECT value FROM ah.meta WHERE key = 'schema_version'").fetchone() == ("1",)
    return options


def test_fresh_public_init_persists_exact_autovacuum_options(catalogue):
    run_init(catalogue)
    first = assert_tuned(catalogue)
    run_init(catalogue)
    assert assert_tuned(catalogue) == first


@pytest.mark.parametrize("preserve_unrelated", [False, True])
def test_public_init_upgrades_033_and_preserves_other_reloptions(catalogue, tmp_path, monkeypatch, preserve_unrelated):
    previous = tmp_path / "sql033"
    shutil.copytree(load.SQL_DIR, previous)
    # Only the disposable SQL copy loses the new migration, never the repository history.
    (previous / "migrations" / MIGRATION).unlink(missing_ok=True)
    config = tmp_path / "empty.toml"
    config.write_text("")
    with monkeypatch.context() as patch:
        patch.setattr(load, "SQL_DIR", previous)
        assert cli.main(["--config", str(config), "--dsn", writer_dsn(catalogue), "init"]) == 0
    with psycopg.connect(writer_dsn(catalogue)) as conn:
        assert conn.execute("SELECT count(*) FROM ah.meta WHERE key = %s", ("migration:" + PREVIOUS,)).fetchone() == (
            1,
        )
        assert conn.execute("SELECT count(*) FROM ah.meta WHERE key = %s", ("migration:" + MIGRATION,)).fetchone() == (
            0,
        )
        for table in TARGETS:
            assert conn.execute(
                "SELECT reloptions FROM pg_class WHERE oid = %s::regclass", ("ah." + table,)
            ).fetchone() == (None,)
        if preserve_unrelated:
            for table in (*TARGETS, "backlog_done_scan"):
                conn.execute(sql.SQL("ALTER TABLE ah.{} SET (fillfactor = 80)").format(sql.Identifier(table)))
    before = reloptions(catalogue)
    run_init(catalogue)
    extra = {"fillfactor": "80"} if preserve_unrelated else {}
    after = assert_tuned(catalogue, extra)
    assert {table: options for table, options in after.items() if table not in TARGETS} == {
        table: options for table, options in before.items() if table not in TARGETS
    }
    run_init(catalogue)
    assert assert_tuned(catalogue, extra) == after

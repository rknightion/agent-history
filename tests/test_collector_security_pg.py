"""Hourly ingest boundary against server roles in a disposable catalogue.

The separate writer/indexer collect-git API is covered by test_collect_git_pg.py.
"""

import os
import subprocess
import sys
import time
from pathlib import Path

import psycopg
from psycopg.conninfo import make_conninfo
from psycopg.pq import TransactionStatus
import pytest

from agent_history import collect_git as collector, load

DSN = os.environ.get("AGENT_HISTORY_TEST_DSN", "")
ADMIN_DSN = os.environ.get("AGENT_HISTORY_TEST_ADMIN_DSN", "")
READER_DSN = os.environ.get("AGENT_HISTORY_TEST_READER_DSN", "")
pytestmark = pytest.mark.skipif(
    not all("agent_history_test" in dsn for dsn in (DSN, ADMIN_DSN, READER_DSN)),
    reason="disposable AGENT_HISTORY_TEST_* DSNs not set",
)


@pytest.fixture(scope="module")
def ingest_dsn():
    with load.connect(DSN) as writer:
        load.apply_schema(writer, force=True)
    with psycopg.connect(ADMIN_DSN, autocommit=True) as admin:
        admin.execute("CREATE ROLE ah_ingest LOGIN")
        admin.execute("GRANT USAGE ON SCHEMA ah TO ah_ingest")
        admin.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON ah.git_commit_file TO ah_ingest")
    # Role passwords are ephemeral and derived from the actual just-ci admin fixture,
    # not an invented selector or a checked-in credential-bearing URI.
    password = psycopg.conninfo.conninfo_to_dict(ADMIN_DSN).get("password")
    if password:
        with psycopg.connect(ADMIN_DSN, autocommit=True) as admin:
            from psycopg import sql

            admin.execute(sql.SQL("ALTER ROLE ah_ingest PASSWORD {}").format(sql.Literal(password)))
    yield make_conninfo(ADMIN_DSN, user="ah_ingest")
    with psycopg.connect(ADMIN_DSN, autocommit=True) as admin:
        admin.execute("DROP OWNED BY ah_ingest")
        admin.execute("DROP ROLE ah_ingest")


def test_hourly_connect_rejects_admin_writer_and_reader(ingest_dsn):
    for dsn in (ADMIN_DSN, DSN, READER_DSN):
        with pytest.raises(RuntimeError, match="dedicated ah_ingest") as error:
            collector.connect(dsn)
        assert dsn not in str(error.value)


@pytest.mark.parametrize("privilege", ["SUPERUSER", "BYPASSRLS", "CREATEROLE"])
def test_named_ingest_role_is_not_sufficient(ingest_dsn, privilege):
    with psycopg.connect(ADMIN_DSN, autocommit=True) as admin:
        admin.execute(f"ALTER ROLE ah_ingest {privilege}")
        try:
            with pytest.raises(RuntimeError):
                collector.connect(ingest_dsn)
        finally:
            admin.execute(f"ALTER ROLE ah_ingest NO{privilege}")


def test_owner_membership_and_switched_identity_are_rejected(ingest_dsn):
    with psycopg.connect(ADMIN_DSN, autocommit=True) as admin:
        admin.execute("GRANT ah_writer TO ah_ingest")
        try:
            for options in ("", "-c role=ah_writer"):
                with pytest.raises(RuntimeError):
                    collector.connect(make_conninfo(ingest_dsn, options=options))
        finally:
            admin.execute("REVOKE ah_writer FROM ah_ingest")
    with pytest.raises(RuntimeError):
        collector.connect(make_conninfo(ADMIN_DSN, options="-c role=ah_ingest"))


def test_guard_leaves_ingest_idle_and_hourly_writes_are_durable(ingest_dsn):
    slug = "synthetic/ingest-durability"
    rows = [
        {
            "repo_slug": slug,
            "sha": "a" * 40,
            "path": "toy.txt",
            "change": "A",
            "old_path": None,
            "insertions": 1,
            "deletions": 0,
        }
    ]
    with psycopg.connect(ADMIN_DSN, autocommit=True) as admin:
        admin.execute("DELETE FROM ah.git_commit_file WHERE repo_slug = %s", (slug,))
    connection = collector.connect(ingest_dsn)
    try:
        assert connection.info.transaction_status == TransactionStatus.IDLE
        worker = collector.Collector(connection, False, time.monotonic() + 30)
        worker.write("git_commit_file", collector.GIT_FILE_COLS, ["repo_slug", "sha", "path"], rows)
        # Verify before close: a guard SELECT must not turn write() into a savepoint
        # inside an uncommitted outer transaction.
        with psycopg.connect(DSN) as independent:
            assert independent.execute(
                "SELECT path FROM ah.git_commit_file WHERE repo_slug = %s", (slug,)
            ).fetchall() == [("toy.txt",)]
    finally:
        connection.close()
        with psycopg.connect(ADMIN_DSN, autocommit=True) as admin:
            admin.execute("DELETE FROM ah.git_commit_file WHERE repo_slug = %s", (slug,))


@pytest.mark.parametrize("entrypoint", [["agent-history", "collect"], ["agent-history-collect"]])
def test_hourly_entrypoints_refuse_administrative_dsn(ingest_dsn, tmp_path, entrypoint):
    config = tmp_path / "config.toml"
    config.write_text(f'[collector]\nlock_file="{tmp_path / "collect.lock"}"\n')
    env = {**os.environ, "AGENT_HISTORY_INGEST_DSN": ADMIN_DSN}
    # Invoke the installed public commands, not a mock of main or psycopg.
    command = [str(Path(sys.executable).parent / entrypoint[0]), "--config", str(config), *entrypoint[1:]]
    result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 1
    assert '"error":"UnsafeIngestRole"' in result.stdout
    assert ADMIN_DSN not in result.stdout + result.stderr

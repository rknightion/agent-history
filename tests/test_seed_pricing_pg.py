"""Explicit optional pricing through the CLI and real catalogue cost views."""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
import uuid
from decimal import Decimal

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

from agent_history import load

DSN = os.environ.get("AGENT_HISTORY_TEST_DSN", "")
ADMIN_DSN = os.environ.get("AGENT_HISTORY_TEST_ADMIN_DSN", "")
pytestmark = pytest.mark.skipif(
    not DSN or "agent_history_test" not in DSN,
    reason="AGENT_HISTORY_TEST_DSN (a *_test database) not set",
)
MODEL = "gpt-6-sol"
# The approved gpt-6.1-sol list price, USD per million tokens: input, cached input, cache write,
# 1-hour cache write (none published), output. It is the contract, not a copy of the seed file.
MODEL_61 = "gpt-6.1-sol"
RATE_61 = (Decimal("2.00"), Decimal("0.10"), Decimal("2.50"), None, Decimal("10.00"))


def insert_session(conn, model):
    """One synthetic session with 1M uncached input, 1M cached input and 1M output tokens."""
    session = conn.execute(
        "INSERT INTO ah.session (agent, session_uid, agent_id, is_stub) VALUES ('pi', %s, '', false) RETURNING id",
        (f"synthetic-pricing-{model}",),
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO ah.llm_call (agent, response_id, session_id, ts, model, "
        "input_uncached, cache_read, cache_write_5m, cache_write_1h, output, source_id, byte_offset) "
        "VALUES ('pi', %s, %s, '2026-09-26', %s, "
        "1000000, 1000000, 0, 0, 1000000, 0, 0)",
        (f"synthetic-pricing-call-{model}", session, model),
    )
    return session


@contextlib.contextmanager
def priced_session(model):
    conn = load.connect(DSN)
    load.apply_schema(conn, force=True)
    conn.commit()
    original = conn.execute("SELECT * FROM ah.model_pricing").fetchall()
    columns = [d.name for d in conn.execute("SELECT * FROM ah.model_pricing LIMIT 0").description]
    conn.execute("DELETE FROM ah.model_pricing")
    conn.commit()
    session = None
    try:
        # Reinitialising a schema must not opt an operator into list prices.
        load.apply_schema(conn, force=True)
        session = insert_session(conn, model)
        conn.commit()
        yield conn, session
    finally:
        conn.rollback()
        if session is not None:
            conn.execute("DELETE FROM ah.llm_call WHERE session_id = %s", (session,))
            conn.execute("DELETE FROM ah.session WHERE id = %s", (session,))
        conn.execute("DELETE FROM ah.model_pricing")
        if original:
            with conn.cursor() as cur:
                cur.executemany(
                    f"INSERT INTO ah.model_pricing ({', '.join(columns)}) VALUES ({', '.join(['%s'] * len(columns))})",
                    original,
                )
        conn.commit()
        conn.close()


@pytest.fixture
def catalogue():
    with priced_session(MODEL) as state:
        yield state


@pytest.fixture
def catalogue_61():
    with priced_session(MODEL_61) as state:
        yield state


def writer_dsn(dbname=None):
    return make_conninfo(DSN, dbname=dbname) if dbname else DSN


def run_cli(command, expected, dbname=None):
    # Takes a database name, never a DSN: pytest prints the arguments of a failing frame.
    result = subprocess.run(
        [sys.executable, "-m", "agent_history.cli", "--dsn", writer_dsn(dbname), command],
        env={**os.environ, "AGENT_HISTORY_CONFIG": "/dev/null/absent"},
        capture_output=True,
        text=True,
        timeout=60,
    )
    # Do not echo process diagnostics: a connection failure may contain the DSN.
    assert result.returncode == 0, f"{command} CLI failed"
    assert result.stdout.strip() == expected


def seed_pricing(dbname=None):
    run_cli("seed-pricing", "seeded", dbname)


@pytest.fixture
def fresh_database():
    """Name of a database that has roles and an empty schema but has never been initialised."""
    name = f"agent_history_test_fresh_{uuid.uuid4().hex}"
    drop = sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name))
    with psycopg.connect(ADMIN_DSN, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    try:
        # What roles.sql does for a new database; the roles themselves are cluster-wide.
        with psycopg.connect(make_conninfo(ADMIN_DSN, dbname=name), autocommit=True) as admin:
            admin.execute("CREATE EXTENSION IF NOT EXISTS vector")
            admin.execute("CREATE EXTENSION IF NOT EXISTS pg_search")
            admin.execute("CREATE SCHEMA ah AUTHORIZATION ah_writer")
        yield name
    finally:
        with psycopg.connect(ADMIN_DSN, autocommit=True) as admin:
            admin.execute(drop)


def cost(conn, session):
    return conn.execute(
        "SELECT llm_calls, unpriced_calls, priced_cost_usd, ah.session_cost(session_id) "
        "FROM ah.v_session_cost WHERE session_id = %s",
        (session,),
    ).fetchone()


def test_explicit_sol_seed_cost_and_operator_override(catalogue):
    conn, session = catalogue
    seed_pricing()
    assert cost(conn, session) == (1, None, Decimal("12.20"), Decimal("12.20"))
    before = conn.execute("SELECT * FROM ah.model_pricing ORDER BY model, effective_from").fetchall()
    conn.commit()
    seed_pricing()
    assert conn.execute("SELECT * FROM ah.model_pricing ORDER BY model, effective_from").fetchall() == before
    conn.execute(
        "UPDATE ah.model_pricing SET input_per_mtok = 7, source = 'operator override' "
        "WHERE model = %s AND effective_from = '2000-01-01'",
        (MODEL,),
    )
    conn.commit()
    seed_pricing()
    assert conn.execute(
        "SELECT input_per_mtok, source FROM ah.model_pricing WHERE model = %s AND effective_from = '2000-01-01'",
        (MODEL,),
    ).fetchone() == (Decimal("7"), "operator override")
    assert cost(conn, session) == (1, None, Decimal("17.20"), Decimal("17.20"))


def test_schema_without_seed_is_visibly_unpriced(catalogue):
    conn, session = catalogue
    assert conn.execute("SELECT count(*) FROM ah.model_pricing WHERE model = %s", (MODEL,)).fetchone() == (0,)
    assert cost(conn, session) == (1, 1, None, None)


def rate_61(conn):
    return conn.execute(
        "SELECT input_per_mtok, cached_input_per_mtok, cache_write_per_mtok, cache_write_1h_per_mtok, "
        "output_per_mtok, effective_from::text FROM ah.model_pricing WHERE model = %s",
        (MODEL_61,),
    ).fetchall()


def test_explicit_seed_prices_gpt_6_1_sol_and_keeps_operator_row(catalogue_61):
    conn, session = catalogue_61
    seed_pricing()
    assert rate_61(conn) == [(*RATE_61, "2000-01-01")]
    # 1M uncached input, 1M cached input and 1M output: 2.00 + 0.10 + 10.00.
    assert cost(conn, session) == (1, None, Decimal("12.10"), Decimal("12.10"))
    before = conn.execute("SELECT * FROM ah.model_pricing ORDER BY model, effective_from").fetchall()
    conn.commit()
    seed_pricing()
    assert conn.execute("SELECT * FROM ah.model_pricing ORDER BY model, effective_from").fetchall() == before
    conn.execute(
        "UPDATE ah.model_pricing SET cached_input_per_mtok = 0.50, source = 'operator override' "
        "WHERE model = %s AND effective_from = '2000-01-01'",
        (MODEL_61,),
    )
    conn.commit()
    seed_pricing()
    assert conn.execute(
        "SELECT cached_input_per_mtok, source FROM ah.model_pricing WHERE model = %s",
        (MODEL_61,),
    ).fetchall() == [(Decimal("0.50"), "operator override")]
    assert cost(conn, session) == (1, None, Decimal("12.50"), Decimal("12.50"))


def test_unseeded_gpt_6_1_sol_session_is_unpriced_not_zero(catalogue_61):
    conn, session = catalogue_61
    # The fixture emptied the price table and reapplied the schema: nothing but the seed refills it.
    assert rate_61(conn) == []
    assert cost(conn, session) == (1, 1, None, None)


@pytest.mark.skipif("agent_history_test" not in ADMIN_DSN, reason="AGENT_HISTORY_TEST_ADMIN_DSN not set")
def test_init_alone_on_a_fresh_database_leaves_gpt_6_1_sol_unpriced(fresh_database):
    run_cli("init", "applied", fresh_database)
    with psycopg.connect(writer_dsn(fresh_database)) as conn:
        session = insert_session(conn, MODEL_61)
        conn.commit()
        # Initialisation alone prices nothing: no row, and a NULL cost rather than 0 or the list price.
        assert rate_61(conn) == []
        assert cost(conn, session) == (1, 1, None, None)
        conn.commit()
        seed_pricing(fresh_database)
        assert rate_61(conn) == [(*RATE_61, "2000-01-01")]
        assert cost(conn, session) == (1, None, Decimal("12.10"), Decimal("12.10"))


def test_existing_catalogue_init_does_not_replay_recorded_migrations(catalogue_61):
    conn, session = catalogue_61
    before = conn.execute("SELECT key, value FROM ah.meta WHERE key LIKE 'migration:%%' ORDER BY key").fetchall()
    assert [key for key, _ in before] == [
        f"migration:{path.name}" for path in sorted((load.SQL_DIR / "migrations").glob("*.sql"))
    ]
    assert "migration:022_live_loops.sql" in dict(before)
    conn.commit()
    # Force a schema pass as well as ordinary init. Replaying 020 would refill the empty price table.
    load.apply_schema(conn, force=True)
    conn.commit()
    run_cli("init", "applied")
    assert (
        conn.execute("SELECT key, value FROM ah.meta WHERE key LIKE 'migration:%%' ORDER BY key").fetchall() == before
    )
    assert rate_61(conn) == []
    assert cost(conn, session) == (1, 1, None, None)

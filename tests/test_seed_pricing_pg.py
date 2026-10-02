"""Explicit optional pricing through the CLI and real catalogue cost views."""

from __future__ import annotations

import os
import subprocess
import sys
from decimal import Decimal

import pytest

from agent_history import load

DSN = os.environ.get("AGENT_HISTORY_TEST_DSN", "")
pytestmark = pytest.mark.skipif(
    not DSN or "agent_history_test" not in DSN,
    reason="AGENT_HISTORY_TEST_DSN (a *_test database) not set",
)
MODEL = "gpt-6-sol"


@pytest.fixture
def catalogue():
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
        session = conn.execute(
            "INSERT INTO ah.session (agent, session_uid, agent_id, is_stub) "
            "VALUES ('pi', 'synthetic-sol-pricing', '', false) RETURNING id"
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO ah.llm_call (agent, response_id, session_id, ts, model, "
            "input_uncached, cache_read, cache_write_5m, cache_write_1h, output, source_id, byte_offset) "
            "VALUES ('pi', 'synthetic-sol-pricing-call', %s, '2026-09-26', %s, "
            "1000000, 1000000, 0, 0, 1000000, 0, 0)",
            (session, MODEL),
        )
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


def seed_pricing():
    result = subprocess.run(
        [sys.executable, "-m", "agent_history.cli", "--dsn", DSN, "seed-pricing"],
        env={**os.environ, "AGENT_HISTORY_CONFIG": "/dev/null/absent"},
        capture_output=True,
        text=True,
        timeout=30,
    )
    # Do not echo process diagnostics: a connection failure may contain the DSN.
    assert result.returncode == 0, "seed-pricing CLI failed"
    assert result.stdout.strip() == "seeded"


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

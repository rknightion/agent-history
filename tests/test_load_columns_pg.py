"""Nullable telemetry through the real Writer and disposable PostgreSQL, not SQL proxies.

Run with just ci. Fixtures and measurements here are wholly synthetic.
"""

from __future__ import annotations

import os
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from agent_history import load
from agent_history.model import (
    CostStateRow,
    FileContext,
    LlmCallRow,
    MessageRow,
    SessionKey,
    SubagentSpawnRow,
    ToolCallRow,
    ToolOpRow,
    TurnRow,
)

DSN = os.environ.get("AGENT_HISTORY_TEST_DSN", "")
pytestmark = pytest.mark.skipif(
    not DSN or "agent_history_test" not in DSN,
    reason="AGENT_HISTORY_TEST_DSN (a *_test database) not set",
)
TS = datetime(2026, 9, 25, 12, tzinfo=timezone.utc)
KEY = SessionKey("pi", "synthetic-telemetry")
CTX = FileContext("/synthetic/session.jsonl", "pi-lab/session.jsonl", "pi-lab", "pi", "lab", None, "main")

# Independent expected database types and values for every column in the frozen seam.
CASES = [
    (
        LlmCallRow("pi", "synthetic-response", KEY, TS, 0),
        {
            "duration_ms": ("integer", 120, 0),
            "latency_basis": ("text", "pi_request_to_entry", "codex_prev_boundary_to_usage"),
            "cost_usd": ("numeric", Decimal("0.1234567890123456789"), Decimal("0")),
            "thinking_ms": ("integer", 60, 0),
            "raw_stop_reason": ("text", "toolUse", "stop"),
            "api": ("text", "synthetic-api", "synthetic-api-v2"),
            "provider": ("text", "synthetic-provider", "synthetic-provider-v2"),
            "cache_miss_type": ("text", "synthetic-miss", "synthetic-refresh"),
            "cache_missed_tokens": ("bigint", 3_000_000_000, 0),
            "input_transform_types": ("ARRAY", ["synthetic-a", "synthetic-b"], []),
            "advisor_model": ("text", "synthetic-advisor", "synthetic-advisor-v2"),
            "inference_geo": ("text", "synthetic-region", "synthetic-region-v2"),
            "iterations": ("integer", 3, 0),
            "ttft_ms": ("integer", 20, 0),
            "attempts": ("integer", 2, 0),
            "processing_ms": ("integer", 80, 0),
        },
    ),
    (
        CostStateRow("pi", "synthetic-cost", KEY, TS, 0, total_cost_usd=1.25),
        {
            "has_unknown_model_cost": ("boolean", True, False),
        },
    ),
    (
        MessageRow("pi", "synthetic-message", KEY, TS, "assistant", "assistant_text", "original text", 0, 10),
        {
            "phase": ("text", "synthetic-working", "synthetic-final"),
        },
    ),
    (
        TurnRow(KEY, "synthetic-turn", 0),
        {
            "reasoning_summary": ("text", "synthetic reasoning\nfull text", "replacement reasoning"),
            "trace_id": ("text", "synthetic-trace", "synthetic-trace-v2"),
            "root_turn_key": ("text", "synthetic-root", "synthetic-root-v2"),
            "origin_hint": ("text", "synthetic-origin", "synthetic-origin-v2"),
            "prompt_index": ("integer", 4, 0),
            "turn_index": ("integer", 5, 0),
            "pending_bg_agents": ("integer", 2, 0),
            "pending_workflows": ("integer", 1, 0),
        },
    ),
    (
        ToolCallRow("pi", "synthetic-call", KEY, "synthetic-tool", 0, exit_code=7),
        {
            "deadline_hit": ("boolean", True, False),
        },
    ),
    (
        SubagentSpawnRow("pi", "synthetic-spawn", KEY, 0),
        {
            "timeout_ms": ("bigint", 3_000_000_000, 0),
            "deadline_at": ("timestamp with time zone", TS + timedelta(hours=1), TS),
            "run_fanout_budget": ("integer", 10, 0),
            "spawn_budget": ("integer", 4, 0),
            "active_async_capacity": ("integer", 2, 0),
            "lifecycle_status": ("text", "synthetic-running", "synthetic-finished"),
        },
    ),
    (
        ToolOpRow("pi", "synthetic-item", KEY, "synthetic-op", 0),
        {
            "mcp_plugin_id": ("text", "synthetic-plugin", "synthetic-plugin-v2"),
            "mcp_read_only": ("boolean", True, False),
        },
    ),
]


@pytest.fixture(scope="module")
def conn():
    connection = load.connect(DSN)
    load.apply_schema(connection, force=True)
    connection.commit()
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture
def writer(conn):
    # No committed fixture rows and no truncation of other tests' evidence.
    try:
        yield load.Writer(conn)
    finally:
        conn.rollback()


@pytest.mark.parametrize("row,spec", CASES, ids=[row.TABLE for row, _ in CASES])
def test_nullable_telemetry_roundtrip_and_merge(conn, writer, row, spec):
    columns = tuple(spec)
    assert all(row.columns()[name] is None for name in columns)
    writer.write([row], 0, CTX)
    key_columns = writer.key_columns(type(row))
    key_values = tuple(writer.session_id(row.session) if name == "session" else getattr(row, name) for name in row.KEY)
    where = " AND ".join(f"{name}=%s" for name in key_columns)
    schema = conn.execute(
        "SELECT column_name, data_type, is_nullable, column_default, udt_name "
        "FROM information_schema.columns WHERE table_schema='ah' AND table_name=%s "
        "AND column_name = ANY(%s)",
        (row.TABLE, list(columns)),
    ).fetchall()
    assert {name: (dtype, nullable, default) for name, dtype, nullable, default, _ in schema} == {
        name: (dtype, "YES", None) for name, (dtype, _, _) in spec.items()
    }
    if "input_transform_types" in columns:
        assert next(udt for name, _, _, _, udt in schema if name == "input_transform_types") == "_text"

    def stored():
        return conn.execute(f"SELECT {','.join(columns)} FROM ah.{row.TABLE} WHERE {where}", key_values).fetchall()

    assert stored() == [tuple(None for _ in columns)]
    writer.write([replace(row, **{name: first for name, (_, first, _) in spec.items()})], 0, CTX)
    assert stored() == [tuple(first for _, first, _ in spec.values())]
    # A later non-NULL zero, false or empty array is observed data, not absence.
    update = replace(row, **{name: latest for name, (_, _, latest) in spec.items()})
    if isinstance(update, MessageRow):
        update = replace(update, text="must not overwrite content", ts=TS + timedelta(seconds=1))
    elif isinstance(update, CostStateRow):
        update = replace(update, total_cost_usd=99, ts=TS + timedelta(seconds=1))
    writer.write([update], 0, CTX)
    expected = tuple(first if name == "thinking_ms" else latest for name, (_, first, latest) in spec.items())
    assert stored() == [expected]
    writer.write([row], 0, CTX)
    assert stored() == [expected]  # NULL never erases known data.
    if isinstance(row, LlmCallRow):
        writer.write([replace(row, thinking_ms=90, latency_basis="claude_parent_to_last_line")], 0, CTX)
        assert conn.execute(
            f"SELECT thinking_ms, latency_basis FROM ah.llm_call WHERE {where}", key_values
        ).fetchall() == [
            (90, "claude_parent_to_last_line"),
        ]
    elif isinstance(row, MessageRow):
        assert conn.execute(f"SELECT text, ts FROM ah.message WHERE {where}", key_values).fetchall() == [(row.text, TS)]
    elif isinstance(row, CostStateRow):
        assert conn.execute(f"SELECT total_cost_usd, ts FROM ah.cost_state WHERE {where}", key_values).fetchall() == [
            (Decimal("1.25"), TS),
        ]
    elif isinstance(row, ToolCallRow):
        assert conn.execute(f"SELECT exit_code FROM ah.tool_call WHERE {where}", key_values).fetchall() == [(7,)]

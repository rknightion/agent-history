"""The read-only MCP server (agent_history.mcp_server) against a disposable ParadeDB.

Needs, as `just ci` sets them up (sql/roles.sql in a *_test database):
  AGENT_HISTORY_TEST_DSN         ah_writer: owns schema ah (the server must refuse it)
  AGENT_HISTORY_TEST_READER_DSN  ah_reader: SELECT only, default_transaction_read_only = on
  AGENT_HISTORY_TEST_ADMIN_DSN   a superuser (the server must refuse it)
The statement-timeout test takes about 30 seconds by design.
"""

from __future__ import annotations

import json
import os
import secrets
from datetime import datetime, timezone

import psycopg
from psycopg import sql

import pytest

DSN = os.environ.get("AGENT_HISTORY_TEST_DSN", "")
READER = os.environ.get("AGENT_HISTORY_TEST_READER_DSN", "")
ADMIN = os.environ.get("AGENT_HISTORY_TEST_ADMIN_DSN", "")
pytestmark = pytest.mark.skipif(
    not (DSN and READER and "agent_history_test" in DSN),
    reason="AGENT_HISTORY_TEST_DSN and AGENT_HISTORY_TEST_READER_DSN not set",
)

pytest.importorskip("mcp")
from agent_history import load, mcp_server  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def schema():
    conn = load.connect(DSN)
    load.apply_schema(conn, force=True)
    conn.commit()
    conn.close()


@pytest.fixture
def reader(monkeypatch, tmp_path):
    config = tmp_path / "config.toml"
    config.write_text('[contexts]\nlocal = ["claude-local", "codex-local", "pi-local"]\n')
    monkeypatch.setenv("AGENT_HISTORY_CONFIG", str(config))
    monkeypatch.setenv("AGENT_HISTORY_READER_DSN", READER)
    monkeypatch.delenv("AGENT_HISTORY_CONTEXT", raising=False)
    return mcp_server


def test_reader_role_is_accepted_and_header_names_the_context(reader):
    out = reader.sql_impl("SELECT count(*) AS n FROM ah.session")
    header, _, body = out.partition("\n")
    assert header == "[context=local]"
    assert json.loads(body)[0]["n"] >= 0


def test_writer_role_is_refused(reader, monkeypatch):
    monkeypatch.setenv("AGENT_HISTORY_READER_DSN", DSN)
    with pytest.raises(mcp_server.UnsafeRole, match="owns schema ah"):
        reader.sql_impl("SELECT 1")


@pytest.mark.skipif(not ADMIN, reason="AGENT_HISTORY_TEST_ADMIN_DSN not set")
def test_superuser_is_refused(reader, monkeypatch):
    monkeypatch.setenv("AGENT_HISTORY_READER_DSN", ADMIN)
    with pytest.raises(mcp_server.UnsafeRole, match="superuser"):
        reader.sql_impl("SELECT 1")


@pytest.mark.skipif(not ADMIN, reason="AGENT_HISTORY_TEST_ADMIN_DSN not set")
def test_server_file_role_member_is_refused(reader, monkeypatch):
    role = "mcp_probe_" + secrets.token_hex(8)
    password = secrets.token_urlsafe(24)
    with psycopg.connect(ADMIN, autocommit=True) as admin:
        try:
            admin.execute(
                sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(sql.Identifier(role), sql.Literal(password))
            )
            admin.execute(sql.SQL("GRANT pg_read_server_files TO {}").format(sql.Identifier(role)))
            admin.execute(sql.SQL("ALTER ROLE {} SET default_transaction_read_only = on").format(sql.Identifier(role)))
            from psycopg.conninfo import make_conninfo

            monkeypatch.setenv("AGENT_HISTORY_READER_DSN", make_conninfo(ADMIN, user=role, password=password))
            with pytest.raises(mcp_server.UnsafeRole, match="pg_read_server_files"):
                reader.sql_impl("SELECT 1")
        finally:
            admin.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role)))


def test_second_statement_is_rejected_by_the_server(reader):
    with pytest.raises(mcp_server.ToolError, match="multiple commands"):
        reader.sql_impl("SELECT 1; SELECT 2")


@pytest.mark.parametrize(
    "statement",
    [
        "INSERT INTO ah.meta(key, value) VALUES ('mcp_probe', 'x')",
        "SELECT * INTO t_probe_mcp FROM ah.meta",
        "SELECT lo_from_bytea(0, 'x')",
        "CREATE TABLE t_probe_mcp (x int)",
    ],
)
def test_writes_are_rejected(reader, statement):
    with pytest.raises(mcp_server.ToolError, match="read-only"):
        reader.sql_impl(statement)


def test_percent_in_like_patterns_is_literal(reader):
    out = reader.sql_impl("SELECT 'abc' LIKE '%bc' AS hit")
    assert json.loads(out.partition("\n")[2]) == [{"hit": True}]


def test_row_cap_and_cell_truncation(reader, monkeypatch):
    original_stream = psycopg.Cursor.stream
    streamed_rows = 0

    def count_stream(cursor, *args, **kwargs):
        nonlocal streamed_rows
        for row in original_stream(cursor, *args, **kwargs):
            streamed_rows += 1
            yield row

    monkeypatch.setattr(psycopg.Cursor, "stream", count_stream)
    out = reader.sql_impl("SELECT generate_series(1, 250) AS n")
    body, _, note = out.partition("\n[truncated")
    assert len(json.loads(body.partition("\n")[2])) == 200 and "showing first 200 rows" in note
    assert streamed_rows == 201
    row = json.loads(reader.sql_impl("SELECT repeat('a', 3000) AS s").partition("\n")[2])[0]
    assert len(row["s"]) < 3000 and row["s"].endswith("[truncated]")


def test_unknown_context_is_an_error(reader):
    with pytest.raises(mcp_server.ToolError, match="unknown context"):
        reader.search_impl("anything", "bm25", None, 5, "elsewhere")


def test_statement_timeout_is_per_call(reader):
    """A session-level override in one call cannot reach the next: every call gets a fresh
    connection and its own SET LOCAL statement_timeout."""
    reader.sql_impl("SELECT set_config('statement_timeout', '0', false)")
    start = datetime.now(timezone.utc)
    with pytest.raises(mcp_server.ToolError, match="timeout"):
        reader.sql_impl("SELECT pg_sleep(35)")
    elapsed = (datetime.now(timezone.utc) - start).total_seconds()
    assert 25 <= elapsed <= 40


@pytest.fixture
def loaded(reader, tmp_path):
    import shutil
    from pathlib import Path

    fixtures = Path(__file__).parent / "fixtures"
    shutil.copytree(fixtures / "claude" / "projects", tmp_path / "claude-home" / "projects")
    shutil.copytree(fixtures / "pi", tmp_path / "pi-home")
    conn = load.connect(DSN)
    conn.execute("TRUNCATE " + ", ".join(f"ah.{t}" for t in load.DATA_TABLES) + " RESTART IDENTITY CASCADE")
    conn.commit()
    sources = {"claude-local": tmp_path / "claude-home", "pi-local": tmp_path / "pi-home"}
    assert load.refresh(conn, None, None, sources=sources, textfile=None, log=lambda *_: None).errors == 0
    load.create_post_load_indexes(conn)
    conn.close()
    return reader


def test_search_and_session_stay_inside_the_context(loaded, tmp_path, monkeypatch):
    config = tmp_path / "scoped.toml"
    config.write_text('[contexts]\nclaude = ["claude-local"]\npi = ["pi-local"]\n')
    monkeypatch.setenv("AGENT_HISTORY_CONFIG", str(config))
    hits = json.loads(loaded.search_impl("repository", "bm25", None, 20, "pi").partition("\n")[2])
    assert hits and {h["namespace"] for h in hits} == {"pi-local"}
    claude_hits = json.loads(loaded.search_impl("repository", "bm25", None, 20, "claude").partition("\n")[2])
    assert all(h["namespace"] == "claude-local" for h in claude_hits)
    root = "01900000-0000-7000-8000-00000000a001"
    assert "resp_root" not in loaded.session_impl(root, "", "pi")  # a timeline, not raw calls
    with pytest.raises(mcp_server.ToolError, match="no session"):
        loaded.session_impl(root, "", "claude")
    rows = json.loads(loaded.efficiency_impl(root, "", True, "pi").partition("\n")[2])
    assert [r["trigger"] for r in rows][:2] == ["user", "status"]

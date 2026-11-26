"""An opencode-* namespace loads into the catalogue labelled 'opencode' (disposable database only)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from test_opencode_namespace import SID, spool

DSN = os.environ.get("AGENT_HISTORY_TEST_DSN", "")
pytestmark = pytest.mark.skipif(
    not DSN or "agent_history_test" not in DSN, reason="AGENT_HISTORY_TEST_DSN (a *_test database) not set"
)

psycopg = pytest.importorskip("psycopg")
from agent_history import load  # noqa: E402


@pytest.fixture
def conn():
    connection = load.connect(DSN)
    load.apply_schema(connection, force=True)
    connection.commit()
    yield connection
    connection.close()


def test_opencode_namespace_loads_with_agent_label(conn, tmp_path: Path):
    hot = tmp_path / "hot"
    spool(hot)
    result = load.refresh(conn, hot, None, log=lambda *_: None)
    assert result.errors == 0 and result.files_parsed == 1
    assert conn.execute("SELECT agent, namespace, file_role FROM ah.source_file").fetchall() == [
        ("opencode", "opencode-work", "main")
    ]
    assert conn.execute("SELECT agent, namespace, session_uid FROM ah.session").fetchall() == [
        ("opencode", "opencode-work", SID)
    ]
    assert conn.execute("SELECT DISTINCT agent FROM ah.message").fetchall() == [("opencode",)]
    assert conn.execute("SELECT count(*) FROM ah.llm_call WHERE agent = 'opencode'").fetchone() == (1,)
    assert conn.execute("SELECT count(*) FROM ah.tool_call WHERE agent = 'opencode'").fetchone() == (1,)
    assert conn.execute("SELECT count(*) FROM ah.parse_issue").fetchone() == (0,)
    again = load.refresh(conn, hot, None, log=lambda *_: None)
    assert again.files_parsed == 0 and again.errors == 0

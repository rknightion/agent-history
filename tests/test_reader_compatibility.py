"""Public-boundary compatibility for the moved reader and MCP tools."""

import asyncio
import json
import os
import subprocess
import sys

from agent_history import cli, mcp_server


def test_reader_search_keeps_arguments_and_psql_output(monkeypatch, capsys, tmp_path):
    for name in ("AGENT_HISTORY_READER_DSN", "AGENT_HISTORY_CONTEXT", "AGENT_HISTORY_EMBED_ENV"):
        monkeypatch.delenv(name, raising=False)
    config = tmp_path / "config.toml"
    config.write_text('reader_dsn="dbname=synthetic"\n[contexts]\nlab=["claude-lab"]\n')
    observed = []

    def process(argv, **kwargs):
        observed.append((argv, kwargs))
        print("message_id,score,snippet\n1,0.25,synthetic")
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(subprocess, "run", process)
    status = cli.main(
        [
            "--config",
            str(config),
            "--format",
            "csv",
            "search",
            "parser",
            "--mode",
            "bm25",
            "--since",
            "7d",
            "--limit",
            "3",
            "--class",
            "human_prompt",
            "--ns",
            "claude-lab",
        ]
    )
    assert status == 0
    assert capsys.readouterr().out == "message_id,score,snippet\n1,0.25,synthetic\n"
    argv, kwargs = observed[0]
    assert "--csv" in argv and "q=parser" in argv
    assert "ah.hybrid_search" in kwargs["input"]
    assert "human_prompt" in kwargs["input"] and "claude-lab" in kwargs["input"]
    assert kwargs["env"]["PGDATABASE"] == "synthetic"


def test_reader_dsn_uses_libpq_environment_not_process_arguments(monkeypatch):
    from agent_history import reader

    from psycopg.conninfo import make_conninfo

    password = "synthetic-" + "password"
    monkeypatch.setenv(
        "AGENT_HISTORY_READER_DSN",
        make_conninfo(
            host="localhost", port="55601", dbname="catalogue", user="reader", password=password, sslmode="disable"
        ),
    )
    observed = []

    def process(argv, **kwargs):
        observed.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, stdout="title\nSynthetic\n")

    monkeypatch.setattr(subprocess, "run", process)
    assert reader.run("SELECT 1", {}) == 0
    assert reader.query("SELECT 'Synthetic' AS title", {}) == [{"title": "Synthetic"}]
    for argv, kwargs in observed:
        assert password not in " ".join(argv)
        env = kwargs["env"]
        assert env["PGHOST"] == "localhost"
        assert env["PGPORT"] == "55601"
        assert env["PGDATABASE"] == "catalogue"
        assert env["PGUSER"] == "reader"
        assert env["PGPASSWORD"] == password
        assert env["PGSSLMODE"] == "disable"
        assert "default_transaction_read_only=on" in env["PGOPTIONS"]


def test_legacy_reader_configuration_forces_real_read_only_connection(monkeypatch, tmp_path):
    import os
    import pytest
    from psycopg import pq
    from agent_history import reader

    dsn = os.environ.get("AGENT_HISTORY_TEST_DSN", "")
    if "agent_history_test" not in dsn:
        pytest.skip("AGENT_HISTORY_TEST_DSN (a *_test database) not set")
    config = tmp_path / "config.toml"
    config.write_text("")
    monkeypatch.setenv("AGENT_HISTORY_CONFIG", str(config))
    monkeypatch.delenv("AGENT_HISTORY_READER_DSN", raising=False)
    env_file = tmp_path / "reader.env"
    lines = [
        f"{option.envvar.decode()}={option.val.decode()}"
        for option in pq.Conninfo.parse(dsn.encode())
        if option.val is not None and option.envvar is not None
    ]
    lines.append("PGOPTIONS=-c default_transaction_read_only=off -c statement_timeout=0")
    env_file.write_text("\n".join(lines) + "\n")
    monkeypatch.setattr(reader, "ENV_FILE", env_file)
    assert reader.query("SHOW default_transaction_read_only", {}) == [{"default_transaction_read_only": "on"}]
    assert reader.query("SHOW statement_timeout", {}) == [{"statement_timeout": "1min"}]


def test_reader_explicit_dsn_overrides_service_defaults_in_real_psql(monkeypatch, tmp_path):
    import os
    import pytest
    from psycopg.conninfo import make_conninfo
    from agent_history import reader

    dsn = os.environ.get("AGENT_HISTORY_TEST_DSN", "")
    if "agent_history_test" not in dsn:
        pytest.skip("AGENT_HISTORY_TEST_DSN (a *_test database) not set")
    config = tmp_path / "config.toml"
    config.write_text("")
    monkeypatch.setenv("AGENT_HISTORY_CONFIG", str(config))
    service_file = tmp_path / "service.conf"
    service_file.write_text(
        "[synthetic-service]\nhost=localhost\nport=1\ndbname=unused\nuser=unused\n"
        "password=unused\noptions=-c default_transaction_read_only=off\n"
    )
    monkeypatch.setenv("PGSERVICEFILE", str(service_file))
    monkeypatch.setenv("PGSERVICE", "synthetic-service")
    for connection, work_mem in (
        (dsn, None),
        (make_conninfo(dsn, service="synthetic-service"), None),
        (
            make_conninfo(
                dsn, service="synthetic-service", options="-c work_mem=17MB -c default_transaction_read_only=off"
            ),
            "17MB",
        ),
    ):
        monkeypatch.setenv("AGENT_HISTORY_READER_DSN", connection)
        assert reader.query("SELECT current_database() AS database, current_user AS role", {}) == [
            {"database": "agent_history_test", "role": "ah_writer"}
        ]
        assert reader.query("SHOW default_transaction_read_only", {}) == [{"default_transaction_read_only": "on"}]
        if work_mem is not None:
            assert reader.query("SHOW work_mem", {}) == [{"work_mem": work_mem}]


def test_reader_service_keeps_explicit_default_port_over_inherited_port(monkeypatch):
    from contextlib import nullcontext
    from types import SimpleNamespace
    import psycopg
    from psycopg import pq
    from agent_history import reader

    monkeypatch.setenv("PGSERVICE", "synthetic-service")
    monkeypatch.setenv("PGPORT", "1")
    monkeypatch.setenv("AGENT_HISTORY_READER_DSN", "service=synthetic-service port=5432 dbname=synthetic user=reader")
    # Model the database connection edge: get_parameters omits the compiled default
    # port, whereas pgconn.info retains the effective value chosen by libpq.
    connection = SimpleNamespace(
        info=SimpleNamespace(get_parameters=lambda: {"dbname": "synthetic", "user": "reader"}, password="synthetic"),
        pgconn=SimpleNamespace(info=pq.Conninfo.parse(b"port=5432 dbname=synthetic user=reader password=synthetic")),
    )
    monkeypatch.setattr(psycopg, "connect", lambda *args, **kwargs: nullcontext(connection))
    captured = []

    def process(argv, **kwargs):
        captured.append(kwargs["env"])
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(subprocess, "run", process)
    assert reader.run("SELECT 1", {}) == 0
    assert captured[0]["PGPORT"] == "5432"
    assert "PGSERVICE" not in captured[0]


def test_reader_has_no_implicit_home_environment_file(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text("")
    hidden = tmp_path / ".config" / "agent-history"
    hidden.mkdir(parents=True)
    (hidden / "reader.env").write_text("PGDATABASE=hidden-synthetic-catalogue\n")
    env = {key: value for key, value in os.environ.items() if not key.startswith(("PG", "AGENT_HISTORY_"))}
    env.update(HOME=str(tmp_path), AGENT_HISTORY_CONFIG=str(config))
    result = subprocess.run(
        [sys.executable, "-c", "from agent_history.reader import load_env; load_env()"],
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert result.returncode != 0
    assert "configure AGENT_HISTORY_ENV or a reader DSN" in result.stderr
    assert str(tmp_path) not in result.stderr
    assert "hidden-synthetic-catalogue" not in result.stdout + result.stderr


def test_tools_list_contains_every_private_tool_and_public_efficiency():
    tools = asyncio.run(mcp_server.mcp.list_tools())
    names = {tool.name for tool in tools}
    assert names >= {
        "active_sessions",
        "find_sessions",
        "infra_actions",
        "loops",
        "schema",
        "search",
        "search_summaries",
        "session",
        "sql",
        "task",
        "touched",
        "why",
        "efficiency",
    }
    schemas = {t.name: t.input_schema for t in tools}
    for name in ("search", "search_summaries", "find_sessions", "active_sessions", "infra_actions"):
        assert "namespaces" in schemas[name]["properties"]


def test_legacy_provenance_tools_keep_unscoped_default_output(monkeypatch):
    monkeypatch.setattr(mcp_server, "_ns", lambda *a: (["claude-lab"], "lab"))
    row = {"namespace": "codex-other", "session_uid": "synthetic"}
    monkeypatch.setattr(mcp_server, "_run", lambda *a: [row])
    for result in (
        mcp_server.session("synthetic"),
        mcp_server.why("abcdef1"),
        mcp_server.touched("%/example.py"),
        mcp_server.loops(),
    ):
        assert json.loads(result.split("\n", 1)[1]) == [row]
        assert "namespaces=" not in result.split("\n", 1)[0]


def test_summary_and_task_tools_bind_arguments_and_preserve_columns(monkeypatch):
    observed = []
    monkeypatch.setattr(mcp_server, "_ns", lambda *a: (["claude-lab"], "lab"))
    monkeypatch.setattr(mcp_server, "_query_vector", lambda *a: None)

    def run(statement, params=()):
        observed.append((statement, params))
        return [{"session_uid": "synthetic", "tokens": 12}]

    monkeypatch.setattr(mcp_server, "_run", run)
    summary = mcp_server.search_summaries("parser", namespaces=["claude-lab"])
    task = mcp_server.task("LAB-1")
    assert json.loads(summary.split("\n", 1)[1]) == [{"session_uid": "synthetic", "tokens": 12}]
    assert json.loads(task.split("\n", 1)[1]) == [{"session_uid": "synthetic", "tokens": 12}]
    assert observed[0][1][:3] == ("parser", None, ["claude-lab"])
    assert "ah.hybrid_search_summaries" in observed[0][0]
    assert observed[1][1] == ("LAB-1",)
    assert "ah.task_effort" in observed[1][0]

"""Connection policy at the reader's real libpq/process boundary."""

import os
import socket
import subprocess
import sys
import time

import pytest
from psycopg.conninfo import make_conninfo

from agent_history import reader


def service_environment(tmp_path):
    env = {key: value for key, value in os.environ.items() if not key.startswith(("PG", "AGENT_HISTORY_"))}
    config = tmp_path / "config.toml"
    config.write_text("")
    env.update(AGENT_HISTORY_CONFIG=str(config), PGSERVICEFILE=str(tmp_path / "service.conf"))
    return env


def test_unresponsive_service_resolution_is_bounded_and_does_not_disclose_password(tmp_path):
    # A TCP endpoint which never replies is deterministic, unlike an unroutable address.
    # The listening socket keeps the connection queued without completing the PG handshake.
    with socket.socket() as endpoint:
        endpoint.bind(("127.0.0.1", 0))
        endpoint.listen()
        env = service_environment(tmp_path)
        password = "synthetic-" + "password"
        (tmp_path / "service.conf").write_text(
            f"[synthetic]\nhost=127.0.0.1\nport={endpoint.getsockname()[1]}\n"
            f"dbname=synthetic\nuser=reader\npassword={password}\nsslmode=disable\n"
        )
        env["AGENT_HISTORY_READER_DSN"] = "service=synthetic"
        start = time.monotonic()
        result = subprocess.run(
            [sys.executable, "-c", "from agent_history.reader import load_env; load_env()"],
            env=env,
            capture_output=True,
            text=True,
            timeout=8,
        )
        assert time.monotonic() - start < 8
        assert result.returncode != 0
        assert result.stderr.strip() == "agent-history: cannot resolve reader service connection"
        assert password not in result.stdout + result.stderr


@pytest.mark.parametrize("source", ["dsn", "service", "environment"])
@pytest.mark.parametrize("value", ["37", "0"])
def test_reader_service_timeout_keeps_explicit_precedence(monkeypatch, tmp_path, source, value):
    dsn = os.environ.get("AGENT_HISTORY_TEST_DSN", "")
    if "agent_history_test" not in dsn:
        pytest.skip("AGENT_HISTORY_TEST_DSN (a *_test database) not set")
    env = service_environment(tmp_path)
    # Populate lower-priority sources with conflicting policies.
    service_timeout = value if source == "service" else "23" if source == "dsn" else None
    (tmp_path / "service.conf").write_text(
        "[synthetic]\n" + (f"connect_timeout={service_timeout}\n" if service_timeout is not None else "")
    )
    for key in list(os.environ):
        if key.startswith(("PG", "AGENT_HISTORY_")):
            monkeypatch.delenv(key)
    for key, setting in env.items():
        monkeypatch.setenv(key, setting)
    monkeypatch.setenv("PGCONNECT_TIMEOUT", value if source == "environment" else "11")
    overrides = {"connect_timeout": value} if source == "dsn" else {}
    monkeypatch.setenv("AGENT_HISTORY_READER_DSN", make_conninfo(dsn, service="synthetic", **overrides))
    assert reader.load_env()["PGCONNECT_TIMEOUT"] == value


def test_reader_service_default_timeout_is_transferred_to_psql(monkeypatch, tmp_path):
    dsn = os.environ.get("AGENT_HISTORY_TEST_DSN", "")
    if "agent_history_test" not in dsn:
        pytest.skip("AGENT_HISTORY_TEST_DSN (a *_test database) not set")
    env = service_environment(tmp_path)
    (tmp_path / "service.conf").write_text("[synthetic]\n")
    for key in list(os.environ):
        if key.startswith(("PG", "AGENT_HISTORY_")):
            monkeypatch.delenv(key)
    for key, setting in env.items():
        monkeypatch.setenv(key, setting)
    monkeypatch.setenv("AGENT_HISTORY_READER_DSN", make_conninfo(dsn, service="synthetic"))
    assert reader.load_env()["PGCONNECT_TIMEOUT"] == "5"
    assert "PGCONNECT_TIMEOUT" not in os.environ

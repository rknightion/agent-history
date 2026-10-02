"""Connection policy at the reader's real libpq/process boundary."""

import os
import socket
import subprocess
import sys
import threading
import time

import pytest
from psycopg.conninfo import conninfo_to_dict, make_conninfo

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


@pytest.mark.parametrize("service_timeout,environment_timeout", [("2", "37"), ("37", "2"), ("0", "2")])
def test_service_file_timeout_controls_delayed_handshake(tmp_path, service_timeout, environment_timeout):
    with socket.socket() as endpoint:
        endpoint.bind(("127.0.0.1", 0))
        endpoint.listen()
        endpoint.settimeout(6)
        env = service_environment(tmp_path)
        (tmp_path / "service.conf").write_text(
            f"[synthetic]\nhost=127.0.0.1\nport={endpoint.getsockname()[1]}\n"
            f"dbname=synthetic\nuser=reader\nsslmode=disable\nconnect_timeout={service_timeout}\n"
        )
        env.update(AGENT_HISTORY_READER_DSN="service=synthetic", PGCONNECT_TIMEOUT=environment_timeout)

        def delay_response():
            with endpoint.accept()[0] as client:
                time.sleep(3.5)
                # Reject the handshake after the environment timeout, but before
                # the deliberately long (or unlimited) service-file deadline.
                client.sendall(b"invalid")

        server = threading.Thread(target=delay_response)
        server.start()
        try:
            start = time.monotonic()
            result = subprocess.run(
                [sys.executable, "-c", "from agent_history.reader import load_env; load_env()"],
                env=env,
                capture_output=True,
                text=True,
                timeout=6,
            )
            elapsed = time.monotonic() - start
        finally:
            server.join(timeout=7)
        assert not server.is_alive()
        assert result.returncode != 0
        assert result.stderr.strip() == "agent-history: cannot resolve reader service connection"
        if service_timeout == "2":
            assert elapsed < 3.5
        else:
            assert elapsed >= 3.5


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


@pytest.mark.parametrize(
    "source, through_service",
    [("dsn", False), ("dsn", True), ("service", True), ("environment", False), ("environment", True)],
)
def test_rejected_password_from_any_source_is_in_no_error_text(tmp_path, source, through_service):
    dsn = os.environ.get("AGENT_HISTORY_TEST_READER_DSN", "")
    if "agent_history_test" not in dsn:
        pytest.skip("AGENT_HISTORY_TEST_READER_DSN (a *_test database) not set")
    target = conninfo_to_dict(dsn)
    target.pop("password", None)
    # The server rejects this password, so the failure is a real authentication error
    # from the real connection path, not a simulated one.
    password = "synthetic-" + "rejected-" + source
    env = service_environment(tmp_path)
    (tmp_path / "service.conf").write_text("[synthetic]\n" + (f"password={password}\n" if source == "service" else ""))
    if source == "environment":
        env["PGPASSWORD"] = password
    if through_service:
        target["service"] = "synthetic"
    if source == "dsn":
        target["password"] = password
    env["AGENT_HISTORY_READER_DSN"] = make_conninfo("", **target)
    result = subprocess.run(
        [sys.executable, "-c", "from agent_history.reader import query; query('SELECT 1', {})"],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode != 0
    assert result.stderr.startswith("agent-history: ")
    assert password not in result.stdout + result.stderr
    if not through_service:
        # psql's own report: where it connected and why that failed.
        assert str(target["port"]) in result.stderr
        assert "password authentication failed" in result.stderr


def test_service_credentials_use_private_channel_and_parent_connects(monkeypatch, tmp_path):
    import psycopg

    dsn = os.environ.get("AGENT_HISTORY_TEST_READER_DSN", "")
    if "agent_history_test" not in dsn:
        pytest.skip("AGENT_HISTORY_TEST_READER_DSN (a *_test database) not set")
    password = conninfo_to_dict(dsn)["password"]
    env = service_environment(tmp_path)
    (tmp_path / "service.conf").write_text("[synthetic]\n")
    for key in list(os.environ):
        if key.startswith(("PG", "AGENT_HISTORY_")):
            monkeypatch.delenv(key)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("AGENT_HISTORY_READER_DSN", make_conninfo(dsn, service="synthetic"))
    original_run = subprocess.run
    captured = []

    def capture_child(*args, **kwargs):
        result = original_run(*args, **kwargs)
        captured.append(result.stdout + result.stderr)
        return result

    monkeypatch.setattr(reader.subprocess, "run", capture_child)
    effective = reader.load_env()
    assert captured and all(password not in output for output in captured)
    assert effective["PGPASSWORD"] == password
    with psycopg.connect(
        make_conninfo(
            "",
            **{
                key: effective[name]
                for key, name in (
                    ("host", "PGHOST"),
                    ("port", "PGPORT"),
                    ("dbname", "PGDATABASE"),
                    ("user", "PGUSER"),
                    ("password", "PGPASSWORD"),
                )
            },
        )
    ) as connection:
        assert connection.execute("SELECT 1").fetchone() == (1,)


@pytest.mark.parametrize("message", [b"", b'{"password":', b"not json", b"[]", b'{"password": 42}'])
def test_invalid_service_handoff_is_bounded_and_generic(monkeypatch, message):
    original_run = subprocess.run

    def broken_child(args, **kwargs):
        # A real process exercises EOF, descriptor inheritance and captured errors.
        args = [
            sys.executable,
            "-c",
            "import os, sys; "
            "os.write(int(sys.argv[1]), " + repr(message) + ") if len(sys.argv) > 1 "
            "else print(" + repr(message.decode()) + ")",
            *args[3:],
        ]
        return original_run(args, **kwargs, timeout=3)

    monkeypatch.setattr(reader.subprocess, "run", broken_child)
    start = time.monotonic()
    with pytest.raises(SystemExit, match="^agent-history: cannot resolve reader service connection$"):
        reader._service_connection_settings("service=synthetic")
    assert time.monotonic() - start < 3

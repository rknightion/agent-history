"""Connection policy at the reader's real libpq/process boundary."""

import json
import os
import signal
import socket
import subprocess
import sys
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


def service_handshake_probe(tmp_path, service_timeout=None, environment_timeout=None, startup_delay=0):
    # Trace the real resolver child, without replacing its command or libpq call.
    probe = """
import json, subprocess, time
from agent_history import reader

def event(name, **fields):
    print(json.dumps(dict(event=name, at=time.monotonic(), **fields)), flush=True)

class TracedProcess(subprocess.Popen):
    def __init__(self, *args, **kwargs):
        event('child-launch')
        super().__init__(*args, **kwargs)
        event('child-started', pid=self.pid)

    def __exit__(self, *args):
        result = super().__exit__(*args)
        event('child-reaped', pid=self.pid, returncode=self.returncode)
        return result

reader.subprocess.Popen = TracedProcess
event('reader-ready')
time.sleep(STARTUP_DELAY)
reader.load_env()
""".replace("STARTUP_DELAY", repr(startup_delay))
    password = "synthetic-" + "password"
    trace = []
    with socket.socket() as endpoint:
        endpoint.bind(("127.0.0.1", 0))
        endpoint.listen()
        endpoint.settimeout(8)
        env = service_environment(tmp_path)
        policy = f"connect_timeout={service_timeout}\n" if service_timeout is not None else ""
        (tmp_path / "service.conf").write_text(
            f"[synthetic]\nhost=127.0.0.1\nport={endpoint.getsockname()[1]}\n"
            f"dbname=synthetic\nuser=reader\npassword={password}\n"
            f"sslmode=disable\ngssencmode=disable\n{policy}"
        )
        env["AGENT_HISTORY_READER_DSN"] = "service=synthetic"
        if environment_timeout is not None:
            env["PGCONNECT_TIMEOUT"] = environment_timeout
        trace.append({"event": "outer-launch", "at": time.monotonic()})
        with subprocess.Popen(
            [sys.executable, "-c", probe],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        ) as process:
            try:
                with endpoint.accept()[0] as client:
                    client.settimeout(8)

                    def read_exact(size):
                        data = bytearray()
                        while len(data) < size:
                            chunk = client.recv(size - len(data))
                            assert chunk, "resolver closed before sending its startup packet"
                            data.extend(chunk)
                        return data

                    size = int.from_bytes(read_exact(4), "big")
                    assert 8 <= size <= 65536
                    read_exact(size - 4)
                    trace.append({"event": "handshake-received", "at": time.monotonic()})
                    if service_timeout in (None, "2"):
                        # No server response can cause this exit. EOF proves libpq
                        # abandoned the connection, independently of parent scheduling.
                        client.settimeout(3.5 if service_timeout == "2" else 8)
                        assert client.recv(1) == b""
                        trace.append({"event": "timeout-eof", "at": time.monotonic()})
                    else:
                        # Start at the actual handshake, not before two Python imports.
                        # A service-file long/unlimited policy must outlive PGCONNECT_TIMEOUT=2.
                        client.settimeout(3.5)
                        with pytest.raises(TimeoutError):
                            client.recv(1)
                        trace.append({"event": "server-reject", "at": time.monotonic()})
                        client.sendall(b"invalid")
                        client.settimeout(8)
                        assert client.recv(1) == b""
                stdout, stderr = process.communicate(timeout=8)
                trace.append({"event": "outer-reaped", "at": time.monotonic()})
            except BaseException:
                # Bound failure cleanup too, even if the outer process exited
                # unexpectedly and left its unlimited resolver child alive.
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.communicate(timeout=8)
                raise
        trace.extend(json.loads(line) for line in stdout.splitlines())
    trace.sort(key=lambda item: item["at"])
    print(json.dumps({"service_timeout": service_timeout, "startup_delay": startup_delay, "trace": trace}))
    assert process.returncode != 0
    assert stderr.strip() == "agent-history: cannot resolve reader service connection"
    assert password not in stdout + stderr
    events = [item["event"] for item in trace]
    assert events.index("child-launch") < events.index("handshake-received")
    assert events.index("child-reaped") < events.index("outer-reaped")
    child = next(item for item in trace if item["event"] == "child-reaped")
    assert child["returncode"] != 0


def test_unresponsive_service_resolution_is_bounded_and_does_not_disclose_password(tmp_path):
    service_handshake_probe(tmp_path)


@pytest.mark.parametrize(
    "service_timeout,environment_timeout,startup_delay",
    [("2", "37", 0), ("2", "37", 2), ("37", "2", 0), ("0", "2", 0)],
)
def test_service_file_timeout_controls_delayed_handshake(tmp_path, service_timeout, environment_timeout, startup_delay):
    service_handshake_probe(tmp_path, service_timeout, environment_timeout, startup_delay)


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

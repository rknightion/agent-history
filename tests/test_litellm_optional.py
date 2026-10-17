"""LiteLLM is optional: nothing but the usage fill reads its DSN, and its failure is attributable but harmless.

The `[litellm]` source serves one proxied context and may be down, unreachable or removed. Config loading
must not depend on its DSN file, the index pass must leave proxied usage unknown rather than fail, and a
periodic worker's failure line and event must name the failing config key without a path or secret.
"""

from __future__ import annotations

import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import psycopg
import pytest

from agent_history import cli, litellm_usage, telemetry
from agent_history.config import ConfigError, LiteLLM, load_config, parse_config


def marker(kind: str) -> str:
    return f"synthetic{kind}" + secrets.token_hex(8)


@pytest.fixture(autouse=True)
def no_litellm_env(monkeypatch):
    monkeypatch.delenv("AGENT_HISTORY_LITELLM_DSN", raising=False)


def unreadable_litellm(tmp_path, private: str) -> dict:
    return {"dsn_file": str(tmp_path / private / "litellm.dsn"), "namespaces": ["claude-proxied"]}


def test_an_unreadable_litellm_dsn_file_does_not_fail_config_loading(tmp_path):
    private = marker("path")
    config = parse_config({"litellm": unreadable_litellm(tmp_path, private)})
    assert config.litellm.enabled
    with pytest.raises(ConfigError) as caught:
        config.litellm.resolve_dsn()
    assert caught.value.key == "litellm.dsn_file"
    assert private not in str(caught.value)


def test_the_litellm_dsn_is_read_when_used_not_when_loaded(tmp_path):
    file = tmp_path / "reader.dsn"
    config = parse_config({"litellm": {"dsn_file": str(file), "namespaces": ["claude-proxied"]}}).litellm
    file.write_text("postgresql://example.invalid/late\n")
    assert config.resolve_dsn().endswith("/late")


def test_a_config_file_with_an_unreadable_litellm_dsn_loads_for_every_command(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(f'[litellm]\ndsn_file = "{tmp_path / "absent.dsn"}"\nnamespaces = ["claude-proxied"]\n')
    assert load_config(path).litellm.enabled


def test_secret_files_outside_litellm_still_fail_closed_with_their_key(tmp_path):
    private = marker("path")
    with pytest.raises(ConfigError) as caught:
        parse_config({"dsn_file": str(tmp_path / private)})
    assert caught.value.key == "dsn_file"
    assert private not in str(caught.value)


def test_invalid_toml_is_a_config_error_with_a_key(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("dsn = [\n")
    with pytest.raises(ConfigError) as caught:
        load_config(path)
    assert caught.value.key == "config_file"


def test_an_unlisted_key_is_never_carried():
    assert ConfigError("synthetic", key=marker("key")).key is None


@pytest.mark.parametrize(
    ("config", "stage"),
    [
        ({"dsn_file": "{missing}"}, "config"),
        ({"dsn": "host=127.0.0.1 port=1 connect_timeout=2 password={secret}"}, "connect"),
    ],
)
def test_the_fill_reports_which_litellm_stage_failed_without_content(tmp_path, config, stage):
    secret, private = marker("cred"), marker("path")
    values = {key: value.format(missing=str(tmp_path / private), secret=secret) for key, value in config.items()}
    run = litellm_usage.filler(LiteLLM(namespaces=("claude-proxied",), **values))
    assert run is not None
    with pytest.raises(litellm_usage.SourceUnavailable) as caught:
        run(None)
    assert caught.value.source == "litellm" and caught.value.stage == stage
    assert caught.value.label == f"litellm.{stage}"
    assert caught.value.__cause__ is None and caught.value.__context__ is None
    assert secret not in str(caught.value) and private not in str(caught.value)


class _Source:
    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def close(self):
        pass


def test_a_spend_log_query_failure_is_the_query_stage(monkeypatch):
    secret = marker("cred")

    class Broken(_Source):
        def transaction(self):
            raise psycopg.ProgrammingError(f"relation missing {secret}")

    class Catalogue:
        def execute(self, *_args):
            class Rows:
                def fetchall(self):
                    return [(1, "msg_synthetic", 7)]

            return Rows()

    monkeypatch.setattr(litellm_usage, "connect_source", lambda dsn: Broken())
    run = litellm_usage.filler(LiteLLM(dsn="postgresql://example.invalid/x", namespaces=("claude-proxied",)))
    with pytest.raises(litellm_usage.SourceUnavailable) as caught:
        run(Catalogue())
    assert caught.value.stage == "query"
    assert secret not in str(caught.value)


def test_a_catalogue_failure_is_not_blamed_on_litellm(monkeypatch):
    class Catalogue:
        def execute(self, *_args):
            raise psycopg.errors.LockNotAvailable("synthetic catalogue lock")

    monkeypatch.setattr(litellm_usage, "connect_source", lambda dsn: _Source())
    run = litellm_usage.filler(LiteLLM(dsn="postgresql://example.invalid/x", namespaces=("claude-proxied",)))
    # It propagates to the index pass, which records it as usage_fill.error, not litellm.query.
    with pytest.raises(psycopg.errors.LockNotAvailable):
        run(Catalogue())


def periodic(monkeypatch, capsys, argv: list[str]) -> list[str]:
    """Run one iteration of a periodic worker; return its stderr lines."""

    def stop(_):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli.time, "sleep", stop)
    with pytest.raises(KeyboardInterrupt):
        cli.main([*argv, "--every", "60"])
    return [line for line in capsys.readouterr().err.splitlines() if line.startswith("agent-history:")]


@pytest.mark.parametrize("command", ["embed", "journal-sync", "index"])
def test_a_periodic_config_failure_names_the_key(monkeypatch, capsys, tmp_path, command):
    private = marker("path")
    config = tmp_path / "config.toml"
    config.write_text(f'dsn_file = "{tmp_path / private / "writer.dsn"}"\n')
    lines = periodic(monkeypatch, capsys, ["--config", str(config), command])
    assert lines == [f"agent-history: {command} failed (config: dsn_file); retrying"]
    assert private not in "\n".join(lines)


@pytest.mark.parametrize("command", ["embed", "journal-sync"])
def test_a_periodic_worker_does_not_fail_on_an_unreadable_litellm_dsn(monkeypatch, capsys, tmp_path, command):
    config = tmp_path / "config.toml"
    config.write_text(
        'dsn = "host=127.0.0.1 port=1 connect_timeout=2"\n'
        f'[litellm]\ndsn_file = "{tmp_path / "absent.dsn"}"\nnamespaces = ["claude-proxied"]\n'
    )
    extra = ["--source-db", str(tmp_path / "absent.db")] if command == "journal-sync" else []
    lines = periodic(monkeypatch, capsys, ["--config", str(config), command, *extra])
    # The unreachable catalogue fails the pass, and the line says so; configuration no longer does.
    assert lines == [f"agent-history: {command} failed (database); retrying"]


@pytest.fixture
def receiver(monkeypatch):
    pytest.importorskip("opentelemetry.sdk")
    telemetry.shutdown()
    requests = []

    class Receiver(BaseHTTPRequestHandler):
        def do_POST(self):
            requests.append((self.path, self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("localhost", 0), Receiver)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", f"http://localhost:{server.server_port}")
    try:
        yield requests
    finally:
        telemetry.shutdown()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def records(requests):
    from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import ExportLogsServiceRequest

    logs = [ExportLogsServiceRequest.FromString(body) for path, body in requests if path == "/v1/logs"]
    found = [
        record
        for request in logs
        for resource in request.resource_logs
        for scope in resource.scope_logs
        for record in scope.log_records
    ]
    return found, "".join(str(request) for request in logs)


def attributes(record) -> dict[str, str]:
    return {item.key: item.value.string_value for item in record.attributes if item.value.string_value}


def test_the_failure_event_names_the_config_key(receiver, tmp_path):
    private = marker("path")
    config = tmp_path / "config.toml"
    config.write_text(f'dsn_file = "{tmp_path / private / "writer.dsn"}"\n')
    assert cli.main(["--config", str(config), "journal-sync"]) == 2
    telemetry.shutdown()
    found, decoded = records(receiver)
    failed = [record for record in found if record.body.string_value == "worker.pass.failed"]
    assert len(failed) == 1
    assert attributes(failed[0]) == {
        "agent_history.outcome": "error",
        "error.type": "config",
        "agent_history.config_key": "dsn_file",
    }
    assert private not in decoded


def test_the_fill_failure_event_names_the_litellm_source(receiver, tmp_path):
    secret = marker("cred")
    telemetry.setup("agent-history-index")
    run = litellm_usage.filler(
        LiteLLM(dsn=f"host=127.0.0.1 port=1 connect_timeout=2 password={secret}", namespaces=("claude-proxied",))
    )
    with pytest.raises(litellm_usage.SourceUnavailable):
        run(None)
    telemetry.shutdown()
    found, decoded = records(receiver)
    failed = [record for record in found if record.body.string_value == "outbound.call.failed"]
    assert len(failed) == 1
    assert attributes(failed[0]) == {
        "agent_history.usage_source": "litellm",
        "db.system.name": "postgresql",
        "db.operation.name": "connect",
        "error.type": "io",
    }
    assert secret not in decoded

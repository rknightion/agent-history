"""Finite lifecycle and signal-only endpoint activation."""

import os
import subprocess
import sys
import time

from agent_history import telemetry


def test_nonresponsive_receiver_preserves_failure_and_bounds_cleanup():
    import socket

    # A listening socket accepts TCP but supplies no HTTP response.
    with socket.socket() as listener:
        listener.bind(("localhost", 0))
        listener.listen(8)
        env = {key: value for key, value in os.environ.items() if not key.startswith("OTEL_")}
        env["OTEL_EXPORTER_OTLP_ENDPOINT"] = f"http://localhost:{listener.getsockname()[1]}"
        env["OTEL_EXPORTER_OTLP_TIMEOUT"] = "0.05"
        code = """
from agent_history import telemetry
try:
    with telemetry.lifecycle("agent-history-index"):
        with telemetry.pass_span("index.pass"):
            raise ValueError("synthetic-original-result")
except ValueError:
    print("original-result-preserved")
"""
        started = time.monotonic()
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, timeout=15)
        assert result.returncode == 0
        assert result.stdout == "original-result-preserved\n"
        assert result.stderr == ""
        assert time.monotonic() - started < 15


def test_only_explicit_signal_enabled(monkeypatch):
    telemetry.shutdown()
    for key in list(os.environ):
        if key.startswith("OTEL_"):
            monkeypatch.delenv(key)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "http://localhost:1/v1/traces")
    instance = telemetry.setup("agent-history-index")
    try:
        assert instance.enabled
        assert len(instance.providers) == 1
        assert instance.logger is None
        assert isinstance(instance.metric, telemetry._Noop)
    finally:
        telemetry.shutdown()


def test_every_log_record_carries_severity():
    from opentelemetry._logs import SeverityNumber
    from opentelemetry.sdk._logs import LoggerProvider
    from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter, SimpleLogRecordProcessor

    telemetry.shutdown()
    exporter = InMemoryLogRecordExporter()
    provider = LoggerProvider(shutdown_on_exit=False)
    provider.add_log_record_processor(SimpleLogRecordProcessor(exporter))
    telemetry._active = telemetry.Telemetry("agent-history-index", [provider], logger=provider.get_logger("test"))
    try:
        with telemetry.pass_span("index.pass"):
            pass
        with telemetry.pass_span("embed.pass") as result:
            result.skipped("lock_held")
        try:
            with telemetry.pass_span("postpass.pass"):
                raise ValueError("synthetic")
        except ValueError:
            pass
        telemetry.emit("outbound.call.failed", {"error.type": "io"})
        telemetry.emit("telemetry.configuration.invalid", {})
    finally:
        telemetry.shutdown()
    severities = {
        record.log_record.body: (record.log_record.severity_number, record.log_record.severity_text)
        for record in exporter.get_finished_logs()
    }
    assert severities == {
        "worker.pass.completed": (SeverityNumber.INFO, "INFO"),
        "worker.pass.skipped": (SeverityNumber.INFO, "INFO"),
        "worker.pass.failed": (SeverityNumber.ERROR, "ERROR"),
        "outbound.call.failed": (SeverityNumber.ERROR, "ERROR"),
        "telemetry.configuration.invalid": (SeverityNumber.WARN, "WARN"),
    }

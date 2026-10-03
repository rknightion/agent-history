"""Optional lifecycle and real HTTP/protobuf content boundary."""

import importlib
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from agent_history import telemetry


@pytest.fixture(autouse=True)
def reset(monkeypatch):
    telemetry.shutdown()
    for key in list(__import__("os").environ):
        if key.startswith("OTEL_"):
            monkeypatch.delenv(key)
    yield
    telemetry.shutdown()


def test_no_endpoint_no_import_or_callbacks(monkeypatch):
    original = importlib.import_module

    def guarded(name, *args, **kwargs):
        assert not name.startswith("opentelemetry")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(importlib, "import_module", guarded)
    instance = telemetry.setup("agent-history-index")
    assert not instance.enabled
    with telemetry.tracer().start_as_current_span("index.pass"):
        telemetry.meter().create_observable_gauge("unused", callbacks=[lambda _: pytest.fail("callback")])
    assert instance.force_flush()
    instance.shutdown()
    instance.shutdown()
    with pytest.raises(ValueError, match="propagated"):
        with telemetry.pass_span("index.pass"):
            raise ValueError("propagated")


def test_missing_extra_is_noop(monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:1")
    original = importlib.import_module

    def absent(name, *args, **kwargs):
        if name.startswith("opentelemetry"):
            raise ImportError("absent")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(importlib, "import_module", absent)
    assert not telemetry.setup("agent-history-index").enabled


def test_real_otlp_correlated_safe_success_and_failure(monkeypatch):
    pytest.importorskip("opentelemetry.sdk")
    from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import ExportLogsServiceRequest
    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

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
    marker = "synthetic-private-" + "payload"
    secret = "gh" + "p_" + "X" * 36
    try:
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", f"http://localhost:{server.server_port}")
        monkeypatch.setenv("OTEL_RESOURCE_ATTRIBUTES", f"unsafe={marker},credential={secret}")
        instance = telemetry.setup("agent-history-index")
        assert instance.enabled
        assert telemetry.setup("agent-history-index") is instance
        # Intentionally unsafe instrumentation proves this check sees real HTTP bodies.
        with telemetry.tracer().start_as_current_span(marker, record_exception=False, set_status_on_exception=False):
            pass
        assert instance.force_flush()
        unsafe_capture = [ExportTraceServiceRequest.FromString(body) for path, body in requests if path == "/v1/traces"]
        with pytest.raises(AssertionError):
            assert marker not in "".join(str(request) for request in unsafe_capture)
        requests.clear()
        with telemetry.pass_span("index.pass") as result:
            result.counts({"rows": 4, "unsafe": marker, "credential": secret})
            logging.getLogger("application").warning(marker)
        with pytest.raises(ValueError):
            with telemetry.pass_span("postpass.pass"):
                raise ValueError(marker + secret)
        assert instance.force_flush()
        instance.shutdown()
        traces = [ExportTraceServiceRequest.FromString(body) for path, body in requests if path == "/v1/traces"]
        logs = [ExportLogsServiceRequest.FromString(body) for path, body in requests if path == "/v1/logs"]
        assert traces and logs
        spans = [
            s for r in traces for resource in r.resource_spans for scope in resource.scope_spans for s in scope.spans
        ]
        records = [
            r
            for request in logs
            for resource in request.resource_logs
            for scope in resource.scope_logs
            for r in scope.log_records
        ]
        assert {s.name for s in spans} == {"index.pass", "postpass.pass"}
        assert len(records) == 2
        assert {r.span_id for r in records} == {s.span_id for s in spans}
        assert {r.trace_id for r in records} == {s.trace_id for s in spans}
        assert all(not s.events and not s.status.message for s in spans)
        decoded = "".join(str(r) for r in traces + logs)
        assert marker not in decoded and secret not in decoded
    finally:
        telemetry.shutdown()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

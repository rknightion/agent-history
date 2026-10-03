"""Embeddings request telemetry: released GenAI conventions, content-free, optional.

Every test drives Provider.embed with a fake urlopen (no network) and, where telemetry is enabled,
decodes the real OTLP/HTTP bodies received by a loopback receiver.
"""

import importlib
import io
import json
import threading
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from agent_history import embed, telemetry

MODEL = "text-embedding-3-small"
OPERATION_HISTOGRAM = "gen_ai.client.operation.duration"
TOKEN_HISTOGRAM = "gen_ai.client.token.usage"


@pytest.fixture(autouse=True)
def reset(monkeypatch):
    telemetry.shutdown()
    for key in list(__import__("os").environ):
        if key.startswith("OTEL_"):
            monkeypatch.delenv(key)
    monkeypatch.setattr(embed.time, "sleep", lambda _delay: None)
    yield
    telemetry.shutdown()


class FakeResponse:
    def __init__(self, body, status=200):
        self.body = json.dumps(body).encode()
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self, *_):
        return self.body


def http_error(status, body="{}"):
    return urllib.error.HTTPError("http://provider.invalid/embeddings", status, "x", {}, io.BytesIO(body.encode()))


def good(texts, model=MODEL, tokens=7, vector=(0.25, 0.5)):
    out = {"data": [{"index": i, "embedding": list(vector)} for i in range(len(texts))], "model": model}
    if tokens is not None:
        out["usage"] = {"prompt_tokens": tokens, "total_tokens": tokens}
    return out


def script(monkeypatch, *outcomes):
    """urlopen yields each scripted outcome in turn: a response body dict, or an exception."""
    queue = list(outcomes)
    calls = []

    def fake(request, timeout=None):
        calls.append(request)
        outcome = queue.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return FakeResponse(outcome)

    monkeypatch.setattr(embed.urllib.request, "urlopen", fake)
    return calls


def provider(token=""):
    return embed.Provider("fake-http", MODEL, token, "http://provider.invalid/v1", dimensions=2)


def value(any_value):
    kind = any_value.WhichOneof("value")
    return None if kind is None else getattr(any_value, kind)


def attrs(key_values):
    return {kv.key: value(kv.value) for kv in key_values}


class Capture:
    def __init__(self):
        self.requests = []

    def decode(self):
        from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import ExportLogsServiceRequest
        from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import ExportMetricsServiceRequest
        from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

        kinds = {
            "/v1/traces": ExportTraceServiceRequest,
            "/v1/metrics": ExportMetricsServiceRequest,
            "/v1/logs": ExportLogsServiceRequest,
        }
        return [(path, kinds[path].FromString(body)) for path, body in self.requests]

    def spans(self):
        return [
            s
            for path, r in self.decode()
            if path == "/v1/traces"
            for resource in r.resource_spans
            for scope in resource.scope_spans
            for s in scope.spans
        ]

    def logs(self):
        return [
            record
            for path, r in self.decode()
            if path == "/v1/logs"
            for resource in r.resource_logs
            for scope in resource.scope_logs
            for record in scope.log_records
        ]

    def metrics(self):
        """The newest cumulative export of each metric, by name."""
        latest = {}
        for path, r in self.decode():
            if path == "/v1/metrics":
                for resource in r.resource_metrics:
                    for scope in resource.scope_metrics:
                        for metric in scope.metrics:
                            latest[metric.name] = metric
        return latest

    def every_text(self):
        """Every decoded request as text plus the raw bodies: nothing may carry content."""
        return "".join(str(r) for _, r in self.decode()) + "".join(repr(body) for _, body in self.requests)


@pytest.fixture
def otlp(monkeypatch):
    pytest.importorskip("opentelemetry.sdk")
    capture = Capture()

    class Receiver(BaseHTTPRequestHandler):
        def do_POST(self):
            capture.requests.append((self.path, self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Receiver)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", f"http://127.0.0.1:{server.server_port}")
    instance = telemetry.setup("agent-history-embed")
    assert instance.enabled

    def finish():
        assert instance.force_flush()
        return capture

    capture.finish = finish
    yield capture
    telemetry.shutdown()
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def logical(capture):
    spans = [s for s in capture.spans() if s.name.startswith("embeddings")]
    assert len(spans) == 1
    return spans[0]


def attempts(capture):
    return [s for s in capture.spans() if s.name == "embedding.http_attempt"]


def test_success_span_follows_released_genai_conventions(monkeypatch, otlp):
    script(monkeypatch, good(["alpha", "beta"]))
    assert len(provider().embed(["alpha", "beta"])) == 2
    capture = otlp.finish()
    span = logical(capture)
    assert span.name == f"embeddings {MODEL}"
    assert span.kind == span.SPAN_KIND_CLIENT
    assert attrs(span.attributes) == {
        "gen_ai.operation.name": "embeddings",
        "gen_ai.provider.name": "openai-compatible",
        "gen_ai.request.model": MODEL,
        "gen_ai.response.model": MODEL,
        "gen_ai.embeddings.dimension.count": 2,
        "gen_ai.usage.input_tokens": 7,
        "agent_history.retry_count": 0,
        "agent_history.outcome": "success",
    }
    assert span.status.code != span.status.STATUS_CODE_ERROR
    assert not span.events
    [attempt] = attempts(capture)
    assert attempt.kind == attempt.SPAN_KIND_CLIENT
    assert attempt.parent_span_id == span.span_id and attempt.trace_id == span.trace_id
    assert attrs(attempt.attributes)["http.response.status_code"] == 200


def test_retry_count_and_attempt_spans_exclude_backoff(monkeypatch, otlp):
    script(monkeypatch, http_error(503), http_error(429), good(["alpha"]))
    provider().embed(["alpha"])
    capture = otlp.finish()
    span = logical(capture)
    assert attrs(span.attributes)["agent_history.retry_count"] == 2
    assert attrs(span.attributes)["agent_history.outcome"] == "success"
    spans = sorted(attempts(capture), key=lambda s: s.start_time_unix_nano)
    assert [attrs(s.attributes).get("http.response.status_code") for s in spans] == [503, 429, 200]
    assert [attrs(s.attributes).get("error.type") for s in spans] == ["503", "429", None]
    assert all(s.parent_span_id == span.span_id for s in spans)
    # Backoff sleeps between attempts: each attempt ends before the next begins.
    assert all(a.end_time_unix_nano <= b.start_time_unix_nano for a, b in zip(spans, spans[1:]))


def test_failure_sets_error_type_status_and_no_message(monkeypatch, otlp):
    script(monkeypatch, http_error(401, '{"error": "bad key"}'))
    with pytest.raises(embed.ProviderError):
        provider().embed(["alpha"])
    capture = otlp.finish()
    span = logical(capture)
    values = attrs(span.attributes)
    assert values["error.type"] == "auth"
    assert values["agent_history.outcome"] == "error"
    assert values["agent_history.retry_count"] == 0
    assert "gen_ai.usage.input_tokens" not in values
    assert span.status.code == span.status.STATUS_CODE_ERROR and span.status.message == ""
    assert not span.events
    [record] = capture.logs()
    assert record.body.string_value == "outbound.call.failed"
    assert attrs(record.attributes) == {
        "error.type": "status",
        "http.response.status_code": 401,
        "agent_history.retry_count": 0,
    }
    assert record.span_id == span.span_id


@pytest.mark.parametrize(
    "status, body, reason",
    [
        (429, '{"error": {"code": "insufficient_quota"}}', "billing_quota"),
        (404, "{}", "route"),
    ],
)
def test_error_type_is_the_bounded_provider_failure_reason(monkeypatch, otlp, status, body, reason):
    script(monkeypatch, *[http_error(status, body) for _ in range(6 if status == 429 else 1)])
    with pytest.raises(embed.ProviderError):
        provider().embed(["alpha"])
    span = logical(otlp.finish())
    assert attrs(span.attributes)["error.type"] == reason


def test_unexpected_exception_is_other(monkeypatch, otlp):
    script(monkeypatch, {"data": [{"embedding": [1.0]}]})  # no index: KeyError in existing parsing
    with pytest.raises(KeyError):
        provider().embed(["alpha"])
    values = attrs(logical(otlp.finish()).attributes)
    assert values["error.type"] == "_OTHER" and values["agent_history.outcome"] == "error"


def test_duration_and_token_histograms_follow_released_metrics(monkeypatch, otlp):
    script(monkeypatch, good(["alpha"]), http_error(403), good(["alpha"], tokens=None))
    p = provider()
    p.embed(["alpha"])
    with pytest.raises(embed.ProviderError):
        p.embed(["alpha"])
    p.embed(["alpha"])
    metrics = otlp.finish().metrics()

    duration = metrics[OPERATION_HISTOGRAM]
    assert duration.unit == "s"
    points = {attrs(d.attributes).get("error.type"): d for d in duration.histogram.data_points}
    assert set(points) == {None, "auth"}
    assert points[None].count == 2 and points["auth"].count == 1
    assert list(points[None].explicit_bounds) == [
        0.01,
        0.02,
        0.04,
        0.08,
        0.16,
        0.32,
        0.64,
        1.28,
        2.56,
        5.12,
        10.24,
        20.48,
        40.96,
        81.92,
    ]
    assert attrs(points["auth"].attributes) == {
        "gen_ai.operation.name": "embeddings",
        "gen_ai.provider.name": "openai-compatible",
        "gen_ai.request.model": MODEL,
        "error.type": "auth",
    }
    assert attrs(points[None].attributes) == {
        "gen_ai.operation.name": "embeddings",
        "gen_ai.provider.name": "openai-compatible",
        "gen_ai.request.model": MODEL,
        "gen_ai.response.model": MODEL,
    }

    usage = metrics[TOKEN_HISTOGRAM]
    assert usage.unit == "{token}"
    [point] = usage.histogram.data_points
    # One observation for the one request with known usage; none for failure or unknown usage.
    assert point.count == 1 and point.sum == 7
    assert attrs(point.attributes)["gen_ai.token.type"] == "input"
    assert "error.type" not in attrs(point.attributes)
    assert list(point.explicit_bounds) == [
        1,
        4,
        16,
        64,
        256,
        1024,
        4096,
        16384,
        65536,
        262144,
        1048576,
        4194304,
        16777216,
        67108864,
    ]


def test_unapproved_model_names_are_omitted_not_exported(monkeypatch, otlp):
    odd = "a model name with spaces and a long tail " * 3
    script(monkeypatch, good(["alpha"], model=odd))
    embed.Provider("fake-http", odd, "", "http://provider.invalid/v1").embed(["alpha"])
    capture = otlp.finish()
    span = logical(capture)
    assert span.name == "embeddings"
    values = attrs(span.attributes)
    assert not {"gen_ai.request.model", "gen_ai.response.model"} & set(values)
    for metric in capture.metrics().values():
        for point in metric.histogram.data_points:
            assert not {"gen_ai.request.model", "gen_ai.response.model"} & set(attrs(point.attributes))
    assert odd not in capture.every_text()


def test_no_input_text_vector_or_body_reaches_any_signal(monkeypatch, otlp):
    marker = "synthetic-private-" + "payload"
    secret = "gh" + "p_" + "Q" * 36
    vector = (0.7391, 0.2468)
    prompt = f"please remember the {marker} and {secret}"

    # Prove the check sees real OTLP bodies: intentionally unsafe telemetry is caught.
    with telemetry.tracer().start_as_current_span(prompt) as unsafe:
        unsafe.set_attribute("gen_ai.input.messages", prompt)
    unsafe_capture = otlp.finish()
    with pytest.raises(AssertionError):
        assert marker not in unsafe_capture.every_text()
    unsafe_capture.requests.clear()

    # Success where the provider echoes content into every response field, then every failure shape.
    echoed = good([prompt], model=prompt, vector=vector)
    echoed["note"] = prompt
    script(
        monkeypatch,
        echoed,
        http_error(400, json.dumps({"error": {"message": prompt, "code": secret}})),
        http_error(401, prompt),
        urllib.error.URLError(prompt),
        urllib.error.URLError(prompt),
        urllib.error.URLError(prompt),
        urllib.error.URLError(prompt),
        urllib.error.URLError(prompt),
        urllib.error.URLError(prompt),
        {"data": [{"index": 0, "embedding": [1.0]}, {"index": 1, "embedding": [1.0]}], "usage": {"prompt_tokens": 3}},
    )
    p = embed.Provider("fake-http", MODEL, secret, "http://provider.invalid/v1", dimensions=2)
    p.embed([prompt])
    for _ in range(3):
        with pytest.raises(embed.ProviderError):
            p.embed([prompt])
    with pytest.raises(embed.ProviderError):  # vector count mismatch after a usage-bearing response
        p.embed([prompt])
    capture = otlp.finish()

    text = capture.every_text()
    for forbidden in (marker, secret, prompt, "0.7391", "0.2468", "provider.invalid", "Bearer"):
        assert forbidden not in text, forbidden
    assert any(s.name.startswith("embeddings") for s in capture.spans())
    assert capture.metrics() and capture.logs()
    for span in capture.spans():
        assert not span.events and span.status.message == ""
    for record in capture.logs():
        assert prompt not in str(record) and secret not in str(record)


@pytest.mark.parametrize("scenario", ["no-endpoint", "extra-absent"])
def test_noop_when_extra_or_endpoint_is_absent(monkeypatch, scenario):
    original = importlib.import_module

    def guarded(name, *args, **kwargs):
        if name.startswith("opentelemetry"):
            if scenario == "no-endpoint":
                raise AssertionError("telemetry imported without an endpoint")
            raise ImportError("absent")
        return original(name, *args, **kwargs)

    if scenario == "extra-absent":
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:1")
    monkeypatch.setattr(importlib, "import_module", guarded)
    assert not telemetry.setup("agent-history-embed").enabled
    calls = script(monkeypatch, http_error(503), good(["alpha"]), http_error(401))
    before = threading.active_count()
    vectors = provider().embed(["alpha"])
    assert len(vectors) == 1 and len(calls) == 2
    with pytest.raises(embed.ProviderError) as caught:
        provider().embed(["alpha"])
    assert caught.value.reason == "auth"
    assert threading.active_count() == before

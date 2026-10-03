"""Content-free GenAI client telemetry for embeddings requests.

Follows the released OpenTelemetry GenAI semantic conventions, v1.41.0 (gen-ai-spans.md#embeddings and
gen-ai-metrics.md). Every value comes from a fixed schema: constants, a validated model identifier, counts,
durations and bounded enums. Input text, vectors, headers, URLs and provider error bodies never reach this
module, and server.address / server.port are deliberately not recorded (the endpoint is never exported).
"""

from __future__ import annotations

import contextvars
import importlib
import time
import http.client
import urllib.error
from contextlib import contextmanager

from . import telemetry
from .metrics.collection import public_model

OPERATION = "embeddings"
# An OpenAI-compatible endpoint says nothing certain about the real provider, so the identifier is fixed.
PROVIDER = "openai-compatible"
DURATION_BOUNDARIES = [0.01, 0.02, 0.04, 0.08, 0.16, 0.32, 0.64, 1.28, 2.56, 5.12, 10.24, 20.48, 40.96, 81.92]
TOKEN_BOUNDARIES = [1, 4, 16, 64, 256, 1024, 4096, 16384, 65536, 262144, 1048576, 4194304, 16777216, 67108864]
# embed.FAILURE_REASONS plus the semantic-conventions fallback.
_REASONS = frozenset({"auth", "billing_quota", "rate_limit", "route", "provider_error", "network", "other", "_OTHER"})
_current = contextvars.ContextVar("agent_history_embedding_request", default=None)


def _model(value, trusted=frozenset()):
    """The public model allowlist the metrics exposition uses (never a copy), else None."""
    public = public_model(value, trusted) if isinstance(value, str) else "other"
    return None if public in ("other", "unknown") else public


def _error_type(exc):
    reason = getattr(exc, "reason", None)
    return reason if isinstance(reason, str) and reason in _REASONS else "_OTHER"


def _attempt_error_type(exc):
    if isinstance(exc, urllib.error.HTTPError):
        return str(exc.code)
    if isinstance(exc, TimeoutError):
        return "timeout"
    if isinstance(exc, (OSError, http.client.HTTPException)):
        return "network"
    return "_OTHER"


def _error_status(api):
    return api.Status(api.StatusCode.ERROR)


class _Null:
    attempts = 0

    def response(self, out):
        pass

    def status(self, code):
        pass


class _Request:
    def __init__(self, base, trusted):
        self.base = base
        self.trusted = trusted
        self.span = None
        self.attempts = 0
        self.response_model = None
        self.input_tokens = None

    def response(self, out):
        """Record the model and input usage a provider response states, when they are well formed."""
        if not isinstance(out, dict):
            return
        values = {}
        model = _model(out.get("model"), self.trusted)
        if model:
            self.response_model = model
            values["gen_ai.response.model"] = model
        usage = out.get("usage")
        tokens = usage.get("prompt_tokens") if isinstance(usage, dict) else None
        if type(tokens) is int and tokens >= 0:
            self.input_tokens = tokens
            values["gen_ai.usage.input_tokens"] = tokens
        self.span.set_attributes(values)


class _Attempt:
    def __init__(self, span):
        self.span = span

    def status(self, code):
        if type(code) is int:
            self.span.set_attribute("http.response.status_code", code)


def _record(request, elapsed, error):
    try:
        meter = telemetry.meter()
        attributes = dict(request.base)
        if request.response_model:
            attributes["gen_ai.response.model"] = request.response_model
        duration = dict(attributes, **({"error.type": error} if error else {}))
        meter.create_histogram(
            "gen_ai.client.operation.duration",
            unit="s",
            description="GenAI operation duration.",
            explicit_bucket_boundaries_advisory=DURATION_BOUNDARIES,
        ).record(elapsed, duration)
        if request.input_tokens is not None:
            meter.create_histogram(
                "gen_ai.client.token.usage",
                unit="{token}",
                description="Number of input and output tokens used.",
                explicit_bucket_boundaries_advisory=TOKEN_BOUNDARIES,
            ).record(request.input_tokens, dict(attributes, **{"gen_ai.token.type": "input"}))
    except Exception:
        pass


@contextmanager
def request(model, dimensions=0, trusted=frozenset()):
    """One logical embeddings request: a CLIENT span plus the duration and usage histograms."""
    if not telemetry.enabled():
        yield _Null()
        return
    api = importlib.import_module("opentelemetry.trace")
    request_model = _model(model, trusted) or "other"
    base = {"gen_ai.operation.name": OPERATION, "gen_ai.provider.name": PROVIDER}
    base["gen_ai.request.model"] = request_model
    attributes = dict(base)
    if type(dimensions) is int and dimensions > 0:
        attributes["gen_ai.embeddings.dimension.count"] = dimensions
    state = _Request(base, trusted)
    token = _current.set(state)
    started = time.monotonic()
    error = None
    try:
        with telemetry.tracer().start_as_current_span(
            f"{OPERATION} {request_model}",
            kind=api.SpanKind.CLIENT,
            attributes=attributes,
            record_exception=False,
            set_status_on_exception=False,
        ) as span:
            state.span = span
            try:
                yield state
            except BaseException as exc:
                error = _error_type(exc)
                span.set_attributes({"agent_history.outcome": "error", "error.type": error})
                span.set_status(_error_status(api))
                status = getattr(exc, "status", None)
                category = "error"
                if hasattr(exc, "reason") and type(status) is int:
                    category = "status" if status > 0 else "io"
                log = {"error.type": category, "agent_history.retry_count": max(0, state.attempts - 1)}
                if type(status) is int and status > 0:
                    log["http.response.status_code"] = status
                telemetry.emit("outbound.call.failed", log)
                raise
            else:
                span.set_attribute("agent_history.outcome", "success")
            finally:
                span.set_attribute("agent_history.retry_count", max(0, state.attempts - 1))
    finally:
        _current.reset(token)
        _record(state, time.monotonic() - started, error)


@contextmanager
def attempt():
    """One urlopen attempt, a CLIENT child span. Backoff between attempts is outside it."""
    if not telemetry.enabled():
        yield _Null()
        return
    api = importlib.import_module("opentelemetry.trace")
    state = _current.get()
    attributes = {"gen_ai.operation.name": OPERATION, "gen_ai.provider.name": PROVIDER}
    if state is not None:
        state.attempts += 1
        attributes = dict(state.base)
    with telemetry.tracer().start_as_current_span(
        "embedding.http_attempt",
        kind=api.SpanKind.CLIENT,
        attributes=attributes,
        record_exception=False,
        set_status_on_exception=False,
    ) as span:
        try:
            yield _Attempt(span)
        except BaseException as exc:
            values = {"agent_history.outcome": "error", "error.type": _attempt_error_type(exc)}
            code = exc.code if isinstance(exc, urllib.error.HTTPError) else None
            if type(code) is int:
                values["http.response.status_code"] = code
            span.set_attributes(values)
            span.set_status(_error_status(api))
            raise
        else:
            span.set_attribute("agent_history.outcome", "success")

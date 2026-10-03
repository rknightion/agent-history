"""Explicit, optional, content-free telemetry. No global OTel providers or auto-instrumentation."""

from __future__ import annotations

import contextvars
import importlib
import logging
import math
import os
import signal
import threading
import time
from contextlib import contextmanager
from functools import wraps
from importlib.metadata import version


class _Noop:
    def __getattr__(self, name):
        return lambda *args, **kwargs: self

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def is_recording(self):
        return False


_NOOP = _Noop()
_active = None
_lock = threading.RLock()
_passes = contextvars.ContextVar("agent_history_passes", default={})
_EVENTS = frozenset({"worker.pass.completed", "worker.pass.skipped", "worker.pass.failed", "outbound.call.failed"})
_COUNTS = frozenset(
    {
        "files",
        "files_seen",
        "files_loaded",
        "files_skipped",
        "lines",
        "rows",
        "errors",
        "items",
        "chunks",
        "api_inputs",
        "cached_inputs",
        "tokens",
        "repositories",
        "commits",
        "skipped",
        "journal_rows",
        "matched",
        "unmatched",
        "deleted",
        "bad_json",
        "task_refs",
        "sessions",
        "refresh_id",
        "files_parsed",
        "files_rewritten",
        "files_tier_only",
        "cached",
        "failed_inputs",
        "repos",
        "journal_matched",
        "journal_unmatched",
        "journal_deleted",
        "journal_bad_json",
    }
)
_ENUMS = {
    "agent_history.outcome": {"success", "skipped", "error"},
    "agent_history.skip_reason": {
        "lock_held",
        "disabled",
        "empty",
        "gate",
        "cap",
        "embed_running",
        "refresh_running",
        "rebuild_in_progress",
        "daily_cap",
    },
    "error.type": {"config", "io", "lookup", "error", "status"},
    "db.system.name": {"postgresql", "sqlite"},
    "db.operation.name": {"connect", "query", "copy", "commit", "rollback"},
}


def _safe(attributes):
    result = {}
    for key, value in (attributes or {}).items():
        if key in _ENUMS and isinstance(value, str) and value in _ENUMS[key]:
            result[key] = value
        elif key in _COUNTS and type(value) is int and value >= 0:
            result[key] = value
        elif (
            key in {"process.exit.code", "http.response.status_code", "agent_history.retry_count"}
            and type(value) is int
        ):
            result[key] = value
        elif key == "duration" and type(value) in (int, float) and math.isfinite(value) and value >= 0:
            result[key] = value
    return result


def _category(exc):
    if isinstance(exc, OSError):
        return "io"
    if isinstance(exc, LookupError):
        return "lookup"
    return "error"


class Telemetry:
    def __init__(self, service, providers=(), trace=None, metric=None, logger=None):
        self.service = service
        self.providers = list(providers)
        self.trace = trace or _NOOP
        self.metric = metric or _NOOP
        self.logger = logger
        self.enabled = bool(providers)
        self.closed = False

    def force_flush(self, timeout_millis=10000):
        deadline = time.monotonic() + max(0, timeout_millis) / 1000
        success = True
        for provider in self.providers:
            remaining = max(0, int((deadline - time.monotonic()) * 1000))
            try:
                success = bool(provider.force_flush(timeout_millis=remaining)) and success
            except Exception:
                success = False
        return success

    def shutdown(self):
        with _lock:
            if self.closed:
                return
            self.closed = True

        # APIs have different shutdown signatures. Bound the caller independently of
        # SDK retry/processor waits; finite HTTP timeouts bound eventual cleanup too.
        def close():
            for provider in self.providers:
                try:
                    provider.shutdown()
                except Exception:
                    pass

        if self.providers:
            worker = threading.Thread(target=close, name="agent-history-telemetry-shutdown", daemon=True)
            worker.start()
            worker.join(10)


def setup(service_name: str) -> Telemetry:
    global _active
    with _lock:
        if _active is not None and not _active.closed:
            return _active
        service = os.environ.get("OTEL_SERVICE_NAME") or service_name
        signals = [
            s
            for s in ("TRACES", "METRICS", "LOGS")
            if os.environ.get(f"OTEL_EXPORTER_OTLP_{s}_ENDPOINT") or os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
        ]
        _active = Telemetry(service)
        if not signals:
            return _active
        providers = []
        try:
            for signal in signals:
                protocol = os.environ.get(f"OTEL_EXPORTER_OTLP_{signal}_PROTOCOL") or os.environ.get(
                    "OTEL_EXPORTER_OTLP_PROTOCOL", "http/protobuf"
                )
                if protocol != "http/protobuf":
                    return _active
            resource_module = importlib.import_module("opentelemetry.sdk.resources")
            resource = resource_module.Resource(
                {
                    "service.name": service,
                    "service.version": version("agent-history"),
                    "telemetry.sdk.name": "opentelemetry",
                    "telemetry.sdk.language": "python",
                    "telemetry.sdk.version": version("opentelemetry-sdk"),
                }
            )
            # SDK diagnostics may contain transport bodies/configuration. Silence only
            # SDK-owned loggers, without touching the application's console route.
            diagnostic = logging.getLogger("opentelemetry")
            diagnostic.addHandler(logging.NullHandler())
            diagnostic.propagate = False
            trace = metric = logger = None
            for signal in signals:
                prefix = f"OTEL_EXPORTER_OTLP_{signal}_TIMEOUT"
                timeout = min(
                    10.0, max(0.01, float(os.environ.get(prefix) or os.environ.get("OTEL_EXPORTER_OTLP_TIMEOUT", "10")))
                )
                if signal == "TRACES":
                    sdk = importlib.import_module("opentelemetry.sdk.trace")
                    export = importlib.import_module("opentelemetry.sdk.trace.export")
                    http = importlib.import_module("opentelemetry.exporter.otlp.proto.http.trace_exporter")
                    provider = sdk.TracerProvider(resource=resource, shutdown_on_exit=False)
                    providers.append(provider)
                    provider.add_span_processor(export.BatchSpanProcessor(http.OTLPSpanExporter(timeout=timeout)))
                    trace = provider.get_tracer("agent_history", version("agent-history"))
                elif signal == "METRICS":
                    sdk = importlib.import_module("opentelemetry.sdk.metrics")
                    export = importlib.import_module("opentelemetry.sdk.metrics.export")
                    http = importlib.import_module("opentelemetry.exporter.otlp.proto.http.metric_exporter")
                    reader = export.PeriodicExportingMetricReader(http.OTLPMetricExporter(timeout=timeout))
                    provider = sdk.MeterProvider(resource=resource, metric_readers=[reader], shutdown_on_exit=False)
                    providers.append(provider)
                    metric = provider.get_meter("agent_history", version("agent-history"))
                else:
                    sdk = importlib.import_module("opentelemetry.sdk._logs")
                    export = importlib.import_module("opentelemetry.sdk._logs.export")
                    http = importlib.import_module("opentelemetry.exporter.otlp.proto.http._log_exporter")
                    provider = sdk.LoggerProvider(resource=resource, shutdown_on_exit=False)
                    providers.append(provider)
                    provider.add_log_record_processor(
                        export.BatchLogRecordProcessor(http.OTLPLogExporter(timeout=timeout))
                    )
                    logger = provider.get_logger("agent_history", version("agent-history"))
            _active = Telemetry(service, providers, trace, metric, logger)
        except Exception:
            Telemetry(service, providers).shutdown()
        return _active


@contextmanager
def lifecycle(service_name):
    owner = _active is None or _active.closed
    instance = setup(service_name)
    previous_signal = None
    if owner and instance.enabled and threading.current_thread() is threading.main_thread():
        previous_signal = signal.getsignal(signal.SIGTERM)

        def terminate(signum, frame):
            raise SystemExit(128 + signum)

        signal.signal(signal.SIGTERM, terminate)
    try:
        yield instance
    finally:
        if previous_signal is not None:
            signal.signal(signal.SIGTERM, previous_signal)
        instance.force_flush()
        if owner:
            shutdown()


def force_flush(timeout_millis=10000):
    return _active.force_flush(timeout_millis) if _active is not None else True


def shutdown():
    global _active
    if _active is not None:
        _active.shutdown()
    _active = None


def tracer():
    return _active.trace if _active is not None and not _active.closed else _NOOP


def meter():
    return _active.metric if _active is not None and not _active.closed else _NOOP


def emit(event_name, attributes=None):
    if event_name not in _EVENTS or _active is None or _active.logger is None or _active.closed:
        return
    try:
        _active.logger.emit(body=event_name, event_name=event_name, attributes=_safe(attributes))
    except Exception:
        pass


class PassResult:
    def __init__(self, span):
        self.span = span
        self.attributes = {"agent_history.outcome": "success"}

    def counts(self, values):
        self.attributes.update(_safe(values))
        if self.attributes.get("errors", 0):
            self.attributes["agent_history.outcome"] = "error"

    def skipped(self, reason):
        self.attributes.update(_safe({"agent_history.outcome": "skipped", "agent_history.skip_reason": reason}))


@contextmanager
def operation(name, attributes=None, client=False):
    kwargs = {"record_exception": False, "set_status_on_exception": False, "attributes": _safe(attributes)}
    if client and _active is not None and _active.enabled:
        kwargs["kind"] = importlib.import_module("opentelemetry.trace").SpanKind.CLIENT
    with tracer().start_as_current_span(name, **kwargs) as span:
        try:
            yield span
        except BaseException as exc:
            span.set_attributes({"agent_history.outcome": "error", "error.type": _category(exc)})
            if _active is not None and _active.enabled:
                api = importlib.import_module("opentelemetry.trace")
                span.set_status(api.Status(api.StatusCode.ERROR))
            if client:
                emit("outbound.call.failed", {"error.type": _category(exc)})
            raise


@contextmanager
def pass_span(name):
    if name in _passes.get():
        yield _passes.get()[name]
        return
    result = PassResult(_NOOP)
    token = _passes.set({**_passes.get(), name: result})
    try:
        with operation(name) as span:
            result.span = span
            try:
                yield result
            except BaseException:
                emit("worker.pass.failed", {"agent_history.outcome": "error"})
                raise
            else:
                span.set_attributes(result.attributes)
                outcome = result.attributes["agent_history.outcome"]
                if outcome == "error" and _active is not None and _active.enabled:
                    api = importlib.import_module("opentelemetry.trace")
                    span.set_status(api.Status(api.StatusCode.ERROR))
                emit(
                    {
                        "success": "worker.pass.completed",
                        "skipped": "worker.pass.skipped",
                        "error": "worker.pass.failed",
                    }[outcome],
                    result.attributes,
                )
    finally:
        _passes.reset(token)


def instrument_pass(name):
    def decorate(function):
        @wraps(function)
        def wrapped(*args, **kwargs):
            with pass_span(name) as span:
                result = function(*args, **kwargs)
                values = result if isinstance(result, dict) else getattr(result, "__dict__", {})
                span.counts(values)
                if values.get("failed_inputs", 0):
                    span.counts({"errors": values["failed_inputs"]})
                if values.get("lock_held"):
                    span.skipped("lock_held")
                if isinstance(values.get("skipped"), str) and values["skipped"]:
                    span.skipped(
                        values["skipped"] if values["skipped"] in _ENUMS["agent_history.skip_reason"] else "gate"
                    )
                if "journal_skipped_reason" in values:
                    span.skipped("gate")
                if isinstance(values.get("skipped"), list):
                    span.counts({"skipped": len(values["skipped"])})
                return result

        return wrapped

    return decorate


def db_connect(*args, **kwargs):
    """Driver-compatible factory; disabled calls are delegated with identical arguments."""
    import psycopg

    if _active is None or not _active.enabled:
        return psycopg.connect(*args, **kwargs)

    class CursorMixin:
        def execute(self, *args, **kwargs):
            with operation("db.query", {"db.system.name": "postgresql", "db.operation.name": "query"}, client=True):
                return super().execute(*args, **kwargs)

        def executemany(self, *args, **kwargs):
            with operation("db.query", {"db.system.name": "postgresql", "db.operation.name": "query"}, client=True):
                return super().executemany(*args, **kwargs)

        @contextmanager
        def copy(self, *args, **kwargs):
            with operation("db.copy", {"db.system.name": "postgresql", "db.operation.name": "copy"}, client=True):
                with super().copy(*args, **kwargs) as copy:
                    yield copy

    class Connection(psycopg.Connection):
        def commit(self):
            with operation("db.commit", {"db.system.name": "postgresql", "db.operation.name": "commit"}, client=True):
                return super().commit()

        def rollback(self):
            with operation(
                "db.rollback", {"db.system.name": "postgresql", "db.operation.name": "rollback"}, client=True
            ):
                return super().rollback()

    factory = kwargs.get("cursor_factory", psycopg.Cursor)
    kwargs["cursor_factory"] = type("TelemetryCursor", (CursorMixin, factory), {})
    with operation("db.connect", {"db.system.name": "postgresql", "db.operation.name": "connect"}, client=True):
        connection = Connection.connect(*args, **kwargs)
    # Named/server cursors use a separate factory in psycopg.
    connection.server_cursor_factory = type(
        "TelemetryServerCursor", (CursorMixin, connection.server_cursor_factory), {}
    )
    return connection

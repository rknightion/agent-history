"""Prometheus text 0.0.4 endpoint with durable monotonic counters."""

from __future__ import annotations

import json
import math
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import Collector, Family, Sample

NAME = re.compile(r"^[a-zA-Z_:][a-zA-Z0-9_:]*$")
LABEL = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")

# Explicit public identifiers from the pricing seed and supported model references.
# Never accept prefixes: a vendor-looking prefix can still contain private text.
PUBLIC_MODELS = frozenset(
    {
        "gpt-6.1-sol",
        "gpt-6-sol",
        "gpt-6-luna",
        "gpt-6-astra",
        "gpt-5.6-sol",
        "gpt-daybreak-blue-latest",
        "gpt-5.6-terra",
        "gpt-5.6-luna",
        "gpt-5.5",
        "claude-opus-5-5",
        "claude-opus-5",
        "claude-opus-4-8",
        "claude-opus-4-7",
        "claude-opus-4-6",
        "claude-opus-4-5",
        "claude-opus-4-5-20251101",
        "claude-opus-4-1",
        "claude-opus-4-1-20250805",
        "claude-opus-4-20250514",
        "claude-sonnet-5",
        "claude-sonnet-4-6",
        "claude-sonnet-4-5",
        "claude-sonnet-4-5-20250929",
        "claude-sonnet-4-20250514",
        "claude-3-7-sonnet-20250219",
        "claude-fable-5-1",
        "claude-fable-5",
        "claude-haiku-4-5",
        "claude-haiku-4-5-20251001",
        "claude-3-5-haiku-20241022",
        "claude-3-haiku-20240307",
    }
)


def _public_labels(labels: tuple[tuple[str, str], ...]) -> tuple[tuple[str, str], ...]:
    return tuple(
        (key, (value if value in PUBLIC_MODELS else "unknown" if not value or value == "unknown" else "other"))
        if key == "model"
        else (key, value)
        for key, value in labels
    )


def _source_samples(family: Family) -> tuple[Sample, ...]:
    values: dict[tuple[str, tuple[tuple[str, str], ...]], float] = {}
    for sample in family.samples:
        if tuple(sorted(sample.labels)) != sample.labels or len(dict(sample.labels)) != len(sample.labels):
            raise ValueError("labels must be sorted and unique")
        if not all(LABEL.fullmatch(k) for k, _ in sample.labels):
            raise ValueError("invalid label name")
        raw = float(sample.value)
        if not math.isfinite(raw) or (family.type != "gauge" and raw < 0):
            raise ValueError("invalid sample value")
        name = sample.name or family.name
        if family.type == "histogram":
            if name not in (family.name + "_bucket", family.name + "_sum", family.name + "_count"):
                raise ValueError("invalid histogram sample name")
        elif name != family.name:
            raise ValueError("non-histogram sample name differs from family")
        key = (name, sample.labels)
        values[key] = values.get(key, 0.0) + raw
    return tuple(Sample(labels, value, name) for (name, labels), value in values.items())


def _public_samples(samples: tuple[Sample, ...]) -> tuple[Sample, ...]:
    values: dict[tuple[str, tuple[tuple[str, str], ...]], float] = {}
    for sample in samples:
        key = (sample.name, _public_labels(sample.labels))
        values[key] = values.get(key, 0.0) + sample.value
    # Each histogram component and bucket bound is aggregated independently.
    return tuple(Sample(labels, value, name) for (name, labels), value in values.items())


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


class State:
    """Persist offsets by original source identity; expose only privacy-mapped totals.

    Local state is sensitive. Original keys also preserve pre-privacy counter offsets
    without resetting or summing their raw values before independent reset adjustment.
    """

    def __init__(self, directory: Path):
        self.directory = directory
        self.file = directory / "counters.json"
        self.lock = threading.RLock()
        try:
            self.counters = json.loads(self.file.read_text())
        except FileNotFoundError:
            self.counters = {}
        if not isinstance(self.counters, dict):
            raise ValueError("invalid counter state")

    @staticmethod
    def key(name: str, labels: tuple[tuple[str, str], ...]) -> str:
        return json.dumps([name, labels], separators=(",", ":"))

    def public_samples(self, names: set[str]) -> tuple[Sample, ...]:
        with self.lock:
            samples = []
            for key, counter in self.counters.items():
                name, labels = json.loads(key)
                if name in names:
                    samples.append(Sample(tuple(tuple(pair) for pair in labels), counter["value"], name))
            # Retain already-counted sources even when absent from the current scrape.
            return _public_samples(tuple(samples))

    def value(self, name: str, labels: tuple[tuple[str, str], ...]) -> float:
        public = _public_labels(labels)
        for sample in self.public_samples({name}):
            if sample.labels == public:
                return sample.value
        raise KeyError(self.key(name, labels))

    def observe(self, name: str, labels: tuple[tuple[str, str], ...], raw: float) -> float:
        key = self.key(name, labels)
        with self.lock:
            prev = self.counters.get(key)
            if prev is None:
                value = raw
            else:
                value = prev["value"] + (raw if raw < prev["raw"] else raw - prev["raw"])
            if prev is None or prev["raw"] != raw:
                self.counters[key] = {"raw": raw, "value": value}
                self.directory.mkdir(parents=True, exist_ok=True)
                temporary = self.file.with_suffix(".tmp")
                with temporary.open("w") as out:
                    json.dump(self.counters, out, sort_keys=True)
                    out.flush()
                    os.fsync(out.fileno())
                os.replace(temporary, self.file)
            return value


def exposition(families: list[Family], state: State) -> str:
    lines: list[str] = []
    seen: set[str] = set()
    for family in families:
        if not NAME.fullmatch(family.name) or family.type not in ("counter", "gauge", "histogram"):
            raise ValueError("invalid metric name or type")
        if family.name in seen:
            raise ValueError("duplicate metric family")
        seen.add(family.name)
        lines.extend((f"# HELP {family.name} {_escape(family.help)}", f"# TYPE {family.name} {family.type}"))

        def sample_order(sample):
            if family.type != "histogram":
                return (sample.labels, 0, 0.0)
            labels = tuple((key, value) for key, value in sample.labels if key != "le")
            name = sample.name or ""
            suffix = next((suffix for suffix in ("_bucket", "_sum", "_count") if name.endswith(suffix)), "")
            bound = dict(sample.labels).get("le", "0")
            return (
                labels,
                {"_bucket": 0, "_sum": 1, "_count": 2}.get(suffix, 3),
                float("inf") if bound == "+Inf" else float(bound),
            )

        sources = _source_samples(family)
        if family.type == "gauge":
            public = _public_samples(sources)
        else:
            # Adjust each ORIGINAL source independently before public-label summation.
            for sample in sources:
                state.observe(sample.name, sample.labels, sample.value)
            names = (
                {family.name + suffix for suffix in ("_bucket", "_sum", "_count")}
                if family.type == "histogram"
                else {family.name}
            )
            public = state.public_samples(names)
        for sample in sorted(public, key=sample_order):
            name = sample.name or family.name
            if family.type == "histogram":
                if name not in (family.name + "_bucket", family.name + "_sum", family.name + "_count"):
                    raise ValueError("invalid histogram sample name")
            elif name != family.name:
                raise ValueError("non-histogram sample name differs from family")
            raw = float(sample.value)
            if not math.isfinite(raw) or (family.type != "gauge" and raw < 0):
                raise ValueError("invalid sample value")
            value = raw
            labels = "{" + ",".join(f'{key}="{_escape(v)}"' for key, v in sample.labels) + "}" if sample.labels else ""
            lines.append(f"{name}{labels} {value:g}")
    return "\n".join(lines) + "\n"


class MetricServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], collectors: list[Collector], state: State, refresh: float = 15):
        self.collectors = collectors
        self.state = state
        self.refresh = refresh
        self.snapshot = ""
        self.updated = 0.0
        self.snapshot_lock = threading.Lock()
        super().__init__(address, _Handler)

    def metrics(self) -> str:
        with self.snapshot_lock:
            if time.monotonic() - self.updated >= self.refresh:
                from .self import SelfCollector

                self_collector = next((c for c in self.collectors if isinstance(c, SelfCollector)), None)
                families = []
                run_started = time.monotonic()
                failed = False
                for collector in self.collectors:
                    if collector is self_collector:
                        continue
                    start = time.monotonic()
                    try:
                        # Materialize before adding: a generator may fail after yielding.
                        collected = list(collector.collect())
                        families.extend(collected)
                        if self_collector:
                            self_collector.record(collector.name, time.monotonic() - start, False)
                    except Exception:
                        failed = True
                        if self_collector:
                            self_collector.record(collector.name, time.monotonic() - start, True)
                        # Isolate unavailable sources while retaining healthy families.
                if self_collector:
                    self_collector.complete(time.monotonic() - run_started, failed)
                    families.extend(self_collector.collect())
                rendered = exposition(families, self.state)
                self.snapshot = rendered
                self.updated = time.monotonic()
            return self.snapshot


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/healthz":
            body, status = b"ok\n", 200
        elif self.path == "/metrics":
            try:
                body, status = self.server.metrics().encode(), 200
            except Exception:
                body, status = b"collection failed\n", 503
        else:
            body, status = b"not found\n", 404
        self.send_response(status)
        self.send_header(
            "Content-Type",
            "text/plain; version=0.0.4; charset=utf-8" if self.path == "/metrics" else "text/plain; charset=utf-8",
        )
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass

"""The legacy metric bridge: one collection, two outputs, every mapped family compared.

`just otlp-parity` runs this file. It is the parity command a maintainer reruns before a cutover.
"""

import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

pytest.importorskip("opentelemetry.sdk")

import design_spec
from synthetic_metrics import PARSE_KINDS, Stub

from agent_history import telemetry
from agent_history.metrics import Family, Sample, otlp, otlp_parity
from agent_history.metrics.collection import Collection, State


class Receiver:
    def __init__(self):
        self.requests = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                outer.requests.append((self.path, self.rfile.read(int(self.headers["Content-Length"]))))
                self.send_response(200)
                self.end_headers()

            def log_message(self, *_):
                pass

        self.server = ThreadingHTTPServer(("localhost", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()

    def flush(self):
        """Export now, and return the decoded metrics of the newest export request."""
        self.requests.clear()
        assert telemetry.force_flush()
        bodies = [body for path, body in self.requests if path == "/v1/metrics"]
        assert bodies, "no OTLP metrics request reached the local receiver"
        return otlp_parity.decode(bodies[-1])

    def spans(self):
        from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

        requests = [ExportTraceServiceRequest.FromString(body) for path, body in self.requests if path == "/v1/traces"]
        return [s for r in requests for res in r.resource_spans for scope in res.scope_spans for s in scope.spans]

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


@pytest.fixture
def receiver(monkeypatch):
    telemetry.shutdown()
    for key in list(__import__("os").environ):
        if key.startswith("OTEL_"):
            monkeypatch.delenv(key)
    capture = Receiver()
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", f"http://localhost:{capture.server.server_port}")
    # Only an explicit flush exports, so each assertion sees exactly one completed collection.
    monkeypatch.setenv("OTEL_METRIC_EXPORT_INTERVAL", "3600000")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE", "CUMULATIVE")
    try:
        yield capture
    finally:
        telemetry.shutdown()
        capture.close()


def make_collection(tmp_path, collectors, bridge, refresh=0):
    return Collection(collectors, State(tmp_path), refresh, bridge=bridge)


def test_mapping_matches_the_design_tables():
    specs, histograms = design_spec.load()
    assert {n: (s.kind, s.unit, s.attributes) for n, s in specs.items()} == {
        n: (s.kind, s.unit, tuple(sorted(s.attributes))) for n, s in otlp.SPECS.items()
    }
    assert {n: (h.attributes, h.bounds) for n, h in histograms.items()} == {
        n: (tuple(sorted(h.attributes)), h.bounds) for n, h in otlp.HISTOGRAMS.items()
    }


def test_parse_issue_vocabulary_matches_the_design():
    assert otlp.PARSE_ISSUE_KINDS == set(PARSE_KINDS)


def test_every_family_a_producer_can_emit_is_mapped(tmp_path):
    from agent_history.efficiency import parser as rules
    from agent_history.metrics.archive import ArchiveCollector
    from agent_history.metrics.catalogue import RUN_METRICS
    from agent_history.metrics.self import SelfCollector

    hot, cold = tmp_path / "hot", tmp_path / "cold"
    (hot / "pi-personal").mkdir(parents=True)
    (cold / ".archive-receipts").mkdir(parents=True)
    emitted = {f.name for f in ArchiveCollector(hot, cold, None, None).collect()}
    collector = SelfCollector()
    collector.record("archive", 0.5, False)
    collector.complete(1.0, False)
    emitted |= {f.name for f in collector.collect()}
    emitted |= set(RUN_METRICS) | set(rules.EFFICIENCY_COUNTERS) | set(rules.EFFICIENCY_HISTOGRAMS)
    mapped = set(otlp.SPECS) | set(otlp.HISTOGRAMS)
    assert emitted <= mapped, sorted(emitted - mapped)
    for name, (labels, _) in rules.EFFICIENCY_COUNTERS.items():
        assert tuple(sorted(labels)) == tuple(sorted(otlp.SPECS[name].attributes)), name
    for name, (labels, bounds, _) in rules.EFFICIENCY_HISTOGRAMS.items():
        assert tuple(sorted(labels)) == tuple(sorted(otlp.HISTOGRAMS[name].attributes)), name
        assert tuple(map(str, bounds)) + ("+Inf",) == otlp.HISTOGRAMS[name].bounds, name


def test_one_collection_feeds_both_outputs_for_every_mapped_family(tmp_path, receiver):
    telemetry.setup("agent-history-index")
    bridge = otlp.Bridge.create()
    assert bridge is not None
    collector = Stub()
    collection = make_collection(tmp_path, [collector], bridge)
    for step in (0, 1, 2):
        collector.step = step
        text = collection.metrics()
        decoded = receiver.flush()
        report = otlp_parity.compare(text, decoded, rejected=bridge.rejected)
        assert report["ok"], {k: v for k, v in report.items() if v}
        # Every design family, with each histogram compared through its three components.
        assert report["families"] == len(otlp.SPECS) + len(otlp.HISTOGRAMS) - (step == 2)
        assert report["unvalidated"] == report["missing"] == report["extra"] == report["rejected"] == []
    # Neither the flushes nor the callbacks collected again.
    assert collector.collections == 3
    by_loop = {dict(p)["loop"] for p in decoded["agent_efficiency_tool_calls_total"]["points"]}
    assert len(by_loop) > 40  # loop labels are deliberately uncapped
    assert "loop2" not in {dict(p)["loop"] for p in decoded["agent_efficiency_llm_calls_total"]["points"]}
    assert "agent_history_lag_bytes" not in decoded  # an absent gauge stays absent


def test_prometheus_bytes_are_unchanged_while_the_bridge_is_publishing(tmp_path, receiver):
    from test_metrics_exposition_golden import GOLDEN, render

    telemetry.setup("agent-history-index")
    assert render(tmp_path, bridge=otlp.Bridge.create()).encode() == GOLDEN.read_bytes()


def test_retained_counter_state_survives_a_restart(tmp_path, receiver):
    telemetry.setup("agent-history-index")
    first = Stub()
    collection = make_collection(tmp_path, [first], otlp.Bridge.create())
    collection.metrics()
    telemetry.shutdown()  # a process restart: new providers, and counters.json retained on disk
    telemetry.setup("agent-history-index")
    second = Stub()
    second.step = 2  # every counter reset against the persisted raw values
    collection = make_collection(tmp_path, [second], otlp.Bridge.create())
    text = collection.metrics()
    report = otlp_parity.compare(text, receiver.flush(), rejected=[])
    assert report["ok"], {k: v for k, v in report.items() if v}


def test_comparator_distinguishes_every_kind_of_difference(tmp_path, receiver):
    telemetry.setup("agent-history-index")
    bridge = otlp.Bridge.create()
    collector = Stub()
    collection = make_collection(tmp_path, [collector], bridge)
    text = collection.metrics()
    decoded = receiver.flush()
    assert otlp_parity.compare(text, decoded, rejected=[])["ok"]

    def mutated(change):
        copy = {name: {**metric, "points": dict(metric["points"])} for name, metric in decoded.items()}
        change(copy)
        return otlp_parity.compare(text, copy, rejected=[])

    def drop(copy):
        del copy["agent_history_sources"]

    def add(copy):
        copy["agent_history_made_up"] = copy["agent_history_sources"]

    def wrong_value(copy):
        key = next(iter(copy["agent_history_sources"]["points"]))
        copy["agent_history_sources"]["points"][key] += 1

    def wrong_kind(copy):
        copy["agent_efficiency_llm_calls_total"]["monotonic"] = False

    def wrong_histogram(copy):
        key = next(iter(copy["agent_efficiency_lane_seconds_bucket"]["points"]))
        copy["agent_efficiency_lane_seconds_bucket"]["points"][key] += 1

    assert mutated(drop)["missing"] == ["agent_history_sources"]
    assert mutated(add)["extra"] == ["agent_history_made_up"]
    assert mutated(wrong_value)["mismatched"]
    assert mutated(wrong_kind)["mismatched"]
    assert mutated(wrong_histogram)["mismatched"]
    assert not otlp_parity.compare(text, decoded, rejected=[("agent_history_sources", "label_value")])["ok"]
    unknown = (
        text + "# HELP agent_unmapped_total Unmapped.\n# TYPE agent_unmapped_total counter\nagent_unmapped_total 1\n"
    )
    assert otlp_parity.compare(unknown, decoded, rejected=[])["unvalidated"] == ["agent_unmapped_total"]


def test_out_of_contract_labels_are_refused_and_visible(tmp_path, receiver):
    telemetry.setup("agent-history-index")
    bridge = otlp.Bridge.create()
    marker = "synthetic_private_" + "payload"
    secret = "gh" + "p_" + "Q" * 36

    class Odd:
        name = "catalogue"
        retired_loops = {}

        def collect(self):
            return [
                # Identifier-shaped, so the catalogue collector accepts it, but not a parser kind.
                Family(
                    "agent_history_parse_issues_total",
                    "gauge",
                    "Parse issues.",
                    (
                        Sample((("kind", "json_error"),), 1),
                        Sample((("kind", marker),), 2),
                    ),
                ),
                # A label the design does not declare for this family.
                Family("agent_history_rows", "gauge", "Rows.", (Sample((("extra", secret), ("table", "session")), 3),)),
                # The wrong Prometheus type for a mapped name.
                Family("agent_history_lag_bytes", "counter", "Lag.", (Sample((), 4),)),
                Family("agent_history_not_in_design", "gauge", "Unmapped.", (Sample((), 5),)),
            ]

    collection = make_collection(tmp_path, [Odd()], bridge)
    text = collection.metrics()
    # The Prometheus exposition is not changed to hide the discrepancy.
    assert f'agent_history_parse_issues_total{{kind="{marker}"}} 2' in text
    receiver.requests.clear()
    assert telemetry.force_flush()
    bodies = b"".join(body for path, body in receiver.requests)
    assert marker.encode() not in bodies and secret.encode() not in bodies
    decoded = otlp_parity.decode([body for path, body in receiver.requests if path == "/v1/metrics"][-1])
    assert set(bridge.rejected) == {
        ("agent_history_parse_issues_total", "label_value"),
        ("agent_history_rows", "label_set"),
        ("agent_history_lag_bytes", "type"),
    }
    assert bridge.unmapped == {"agent_history_not_in_design"}
    report = otlp_parity.compare(text, decoded, rejected=bridge.rejected, unmapped=bridge.unmapped)
    assert not report["ok"]
    assert sorted(f for f, _ in report["rejected"]) == sorted(f for f, _ in bridge.rejected)
    assert report["unvalidated"] == ["agent_history_not_in_design"]
    assert report["missing"] == []  # refusals are reported as such, not as silent absences
    # The in-contract sample of the same family is still exported.
    assert {dict(k)["kind"] for k in decoded["agent_history_parse_issues_total"]["points"]} == {"json_error"}


def test_bridge_is_absent_unless_metrics_export_is_explicitly_enabled(monkeypatch):
    telemetry.shutdown()
    for key in list(__import__("os").environ):
        if key.startswith("OTEL_"):
            monkeypatch.delenv(key)
    telemetry.setup("agent-history-index")
    assert otlp.Bridge.create() is None
    telemetry.shutdown()
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "http://localhost:1/v1/traces")
    telemetry.setup("agent-history-index")
    assert otlp.Bridge.create() is None  # traces alone do not enable the metric bridge
    telemetry.shutdown()


def test_missing_extra_leaves_the_exporter_as_before(monkeypatch, tmp_path):
    import importlib

    telemetry.shutdown()
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:1")
    original = importlib.import_module

    def absent(name, *args, **kwargs):
        if name.startswith("opentelemetry"):
            raise ImportError("absent")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(importlib, "import_module", absent)
    telemetry.setup("agent-history-index")
    assert otlp.Bridge.create() is None
    collection = make_collection(tmp_path, [Stub()], None)
    try:
        assert collection.metrics().startswith("# HELP agent_efficiency_")
    finally:
        telemetry.shutdown()


@pytest.mark.parametrize("preference", ["DELTA", "LOWMEMORY", "bogus"])
def test_non_cumulative_temporality_disables_the_bridge(tmp_path, receiver, monkeypatch, preference):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE", preference)
    telemetry.setup("agent-history-index")
    assert otlp.Bridge.create() is None


def test_scheduler_publishes_without_a_prometheus_scrape_and_stops_cleanly(tmp_path, receiver):
    telemetry.setup("agent-history-index")
    bridge = otlp.Bridge.create()
    collector = Stub()
    collection = make_collection(tmp_path, [collector], bridge, refresh=0.05)
    refresher = otlp.Refresher(collection)
    refresher.start()
    try:
        deadline = time.monotonic() + 10
        while collector.collections < 3 and time.monotonic() < deadline:
            time.sleep(0.02)
        assert collector.collections >= 3  # scheduled collections, no HTTP client involved
    finally:
        refresher.stop()
    assert not refresher.is_alive()
    settled = collector.collections
    decoded = receiver.flush()
    assert decoded["agent_history_sources"]["points"]  # the retained final snapshot is flushed
    assert collector.collections == settled  # flushing and a stopped scheduler never collect


def test_scheduler_survives_a_failing_refresh(tmp_path, receiver):
    telemetry.setup("agent-history-index")

    class Broken:
        name = "efficiency"
        calls = 0

        def collect(self):
            Broken.calls += 1
            return [Family("agent_history_lag_bytes", "gauge", "Lag.", (Sample((), float("nan")),))]

    collection = make_collection(tmp_path, [Broken()], otlp.Bridge.create(), refresh=0.02)
    refresher = otlp.Refresher(collection)
    refresher.start()
    try:
        deadline = time.monotonic() + 10
        while Broken.calls < 3 and time.monotonic() < deadline:
            time.sleep(0.02)
        assert Broken.calls >= 3  # an invalid sample fails the refresh, never the scheduler
    finally:
        refresher.stop()


def test_collection_spans_are_one_per_real_refresh_and_content_free(tmp_path, receiver):
    telemetry.setup("agent-history-index")
    marker = "synthetic-private-" + "payload"

    class Failing:
        name = "catalogue"

        def collect(self):
            raise OSError(marker)

    collection = make_collection(tmp_path, [Stub(), Failing()], otlp.Bridge.create(), refresh=3600)
    for _ in range(3):  # one real refresh and two cache hits
        collection.metrics()
    receiver.requests.clear()
    assert telemetry.force_flush()
    spans = receiver.spans()
    parents = [s for s in spans if s.name == "metrics.collect"]
    children = [s for s in spans if s.name == "metrics.collector"]
    assert len(parents) == 1 and len(children) == 2
    assert all(c.parent_span_id == parents[0].span_id for c in children)

    def attributes(span):
        return {a.key: a.value.string_value or a.value.int_value for a in span.attributes}

    by_name = {attributes(c).get("agent_history.collector"): c for c in children}
    assert set(by_name) == {"efficiency", "catalogue"}
    assert by_name["catalogue"].status.code == 2  # ERROR
    assert attributes(by_name["catalogue"])["error.type"] == "io"
    assert by_name["efficiency"].status.code != 2
    assert marker not in "".join(str(s) for s in spans)


def test_real_collectors_agree_including_a_failed_collector(tmp_path, receiver, monkeypatch):
    """Archive, worker snapshots, self-health and a failed collector, parsed from synthetic inputs."""
    from agent_history.config import parse_config
    from agent_history.metrics.archive import ArchiveCollector
    from agent_history.metrics.catalogue import RunCollector
    from agent_history.metrics.efficiency import EfficiencyCollector
    from agent_history.metrics.self import SelfCollector

    hot, cold, runs = tmp_path / "hot", tmp_path / "cold", tmp_path / "runs"
    (hot / "pi-personal").mkdir(parents=True)
    (hot / "pi-personal" / "one.jsonl").write_bytes(b"abc")
    (cold / ".archive-receipts").mkdir(parents=True)
    (cold / ".archive-receipts" / "20260929T000000.000Z.json").write_text('{"jsonl_files": 1, "jsonl_bytes": 3}')
    runs.mkdir()
    (runs / "agent-history.prom").write_text(
        'agent_history_run_success 1\nagent_history_run_files{result="parsed"} 2\nagent_history_run_rows 41.5\n'
    )
    (runs / "agent-history-embed.prom").write_text(
        'agent_history_embed_run_skipped{reason="none"} 1\nagent_history_embed_gc_deleted 4\n'
    )
    source = tmp_path / "pi" / "sessions" / "synthetic"
    source.mkdir(parents=True)
    (source / "test.jsonl").write_text(
        '{"type": "session", "id": "synthetic", "timestamp": "2026-01-01T00:00:00Z"}\n'
        '{"type": "message", "timestamp": "2026-01-01T00:00:00Z", "message": {"role": "user", "content": "hi"}}\n'
        '{"type": "message", "timestamp": "2026-01-01T00:00:01Z", "message": {"role": "assistant",'
        ' "model": "test-model", "usage": {"input": 10, "output": 3}, "content": []}}\n'
    )
    monkeypatch.setattr("agent_history.metrics.efficiency.time.time", lambda: 1767225605.0)
    config = parse_config({"sources": {"pi-local": str(tmp_path / "pi")}, "efficiency": {"baseline_ts": 0}})

    class Failed:
        name = "catalogue"

        def collect(self):
            raise OSError("unavailable")

    telemetry.setup("agent-history-index")
    bridge = otlp.Bridge.create()
    collectors = [
        ArchiveCollector(hot, cold, None, None),
        RunCollector(runs),
        EfficiencyCollector(config, tmp_path / "state"),
        Failed(),
        SelfCollector(),
    ]
    collection = make_collection(tmp_path / "offsets", collectors, bridge)
    for _ in range(2):  # the second collection adds self-health counter movement
        text = collection.metrics()
        report = otlp_parity.compare(text, receiver.flush(), rejected=bridge.rejected)
        assert report["ok"], {k: v for k, v in report.items() if v}
    assert 'agent_history_exporter_collection_errors_total{collector="catalogue"} 2' in text
    assert report["families"] > 30


def test_periodic_indexer_publishes_metrics_while_its_index_pass_fails_and_exits_on_term(tmp_path, receiver):
    """The CLI path end to end: `index --every` hosts the collection on its own thread.

    The index pass cannot reach its database and fails on every iteration; metrics still publish,
    exactly one collection happens per refresh interval, and SIGTERM flushes then exits.
    """
    import os
    import signal
    import socket
    import subprocess
    import sys

    hot = tmp_path / "hot"
    (hot / "claude-personal").mkdir(parents=True)
    (hot / "claude-personal" / "one.jsonl").write_bytes(b"abc")
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        closed = probe.getsockname()[1]  # nothing listens here once the probe closes
    config = tmp_path / "config.toml"
    config.write_text(
        "[exporter]\n"
        'listen = "127.0.0.1:9464"\n'  # retired key: still accepted, ignored
        "refresh_interval = 3600\n"
        f'state_dir = "{tmp_path / "state"}"\n'
        f'hot = "{hot}"\n'
        'collectors = ["archive", "self"]\n'
    )
    env = {k: v for k, v in os.environ.items() if not k.startswith(("OTEL_", "AGENT_HISTORY_"))}
    env["AGENT_HISTORY_DSN"] = f"postgresql://synthetic@127.0.0.1:{closed}/absent?connect_timeout=1"
    env["OTEL_EXPORTER_OTLP_ENDPOINT"] = f"http://localhost:{receiver.server.server_port}"
    env["OTEL_METRIC_EXPORT_INTERVAL"] = "300"
    env["OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE"] = "CUMULATIVE"
    argv = ["--config", str(config), "index", "--every", "3600"]
    code = f"from agent_history.cli import main; raise SystemExit(main({argv!r}))"
    process = subprocess.Popen([sys.executable, "-c", code], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 30
        decoded = {}
        while time.monotonic() < deadline and "agent_sessions_storage_files" not in decoded:
            assert process.poll() is None, process.stderr.read()
            bodies = [b for p, b in receiver.requests if p == "/v1/metrics"]
            decoded = otlp_parity.decode(bodies[-1]) if bodies else {}
            time.sleep(0.05)
        assert "agent_sessions_storage_files" in decoded, "the collection published nothing"
        runs = decoded["agent_sessions_metrics_collection_runs_total"]["points"]
        assert list(runs.values()) == [1.0], repr(runs)  # exactly one collection in the refresh interval
        process.send_signal(signal.SIGTERM)
        assert process.wait(timeout=30) == 143
        stderr = process.stderr.read().decode()
        assert "index failed" in stderr  # the pass really failed while metrics published
        assert "metric collection disabled" not in stderr
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()
        process.stdout.close()
        process.stderr.close()


def test_periodic_indexer_collects_nothing_without_metric_export(tmp_path, monkeypatch):
    """With OTLP metric export off, collection never starts and its state directory is never touched."""
    from types import SimpleNamespace

    from agent_history import cli

    telemetry.shutdown()
    for key in list(__import__("os").environ):
        if key.startswith("OTEL_"):
            monkeypatch.delenv(key)
    telemetry.setup("agent-history-index")
    try:
        assert cli._start_collection(SimpleNamespace(config=tmp_path / "absent.toml", dsn=None), tmp_path) is None
    finally:
        telemetry.shutdown()
    assert list(tmp_path.iterdir()) == []

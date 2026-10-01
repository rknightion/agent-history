"""Exporter public HTTP contract with synthetic collectors, no transcript content."""

from __future__ import annotations

import json
import threading

import pytest
from types import SimpleNamespace
from urllib.request import urlopen

from agent_history.config import ConfigError, parse_config
from agent_history.metrics import Family, Sample
from agent_history.metrics.archive import ArchiveCollector
from agent_history.metrics.server import MetricServer, State, exposition
from agent_history.metrics.catalogue import RunCollector


class StubEfficiency:
    name = "efficiency"

    def collect(self):
        return [Family("agent_efficiency_llm_calls_total", "counter", "LLM calls.", (Sample((("agent", "pi"),), 7),))]


def test_stub_efficiency_http_restart(tmp_path):
    state = State(tmp_path)
    server = MetricServer(("127.0.0.1", 0), [StubEfficiency()], state)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        address = f"http://127.0.0.1:{server.server_address[1]}"
        assert urlopen(address + "/healthz").read() == b"ok\n"
        output = urlopen(address + "/metrics").read().decode()
        assert "# TYPE agent_efficiency_llm_calls_total counter" in output
        assert 'agent_efficiency_llm_calls_total{agent="pi"} 7' in output
        assert urlopen(address + "/metrics").headers["Content-Type"].startswith("text/plain; version=0.0.4")
        assert state.value("agent_efficiency_llm_calls_total", (("agent", "pi"),)) == 7
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
    assert State(tmp_path).value("agent_efficiency_llm_calls_total", (("agent", "pi"),)) == 7


def test_archive_receipt_and_bounded_tiers(tmp_path):
    hot, cold = tmp_path / "hot", tmp_path / "cold"
    hot.mkdir()
    (hot / "sample.jsonl").write_text("sample")
    (cold / ".archive-receipts").mkdir(parents=True)
    (cold / ".archive-receipts" / "receipt").write_text("ok")
    families = {family.name: family for family in ArchiveCollector(hot, cold, None, None).collect()}
    assert families["agent_sessions_archive_files"].samples[0] == Sample((("tier", "hot"),), 1)
    assert families["agent_history_cold_tier_available"].samples[0].value == 1
    assert all("sample" not in str(sample.labels) for family in families.values() for sample in family.samples)


def test_archive_private_family_contract(tmp_path):
    hot, cold = tmp_path / "hot", tmp_path / "cold"
    for root in (hot, cold):
        (root / "pi-personal").mkdir(parents=True)
    (hot / "pi-personal" / "one.jsonl").write_bytes(b"abc")
    (cold / ".archive-receipts").mkdir()
    (cold / ".archive-receipts" / "20260929T000000.000Z.json").write_text(
        json.dumps({"jsonl_files": 1, "jsonl_bytes": 3})
    )
    (cold / ".versions" / "snapshot").mkdir(parents=True)
    (cold / ".versions" / "snapshot" / "old.jsonl").write_bytes(b"ab")
    families = {f.name: f for f in ArchiveCollector(hot, cold, None, None, retention_days=0).collect()}
    expected = {
        "agent_sessions_storage_root_available": ("tier",),
        "agent_sessions_filesystem_bytes": ("kind", "tier"),
        "agent_sessions_filesystem_inodes": ("kind", "tier"),
        "agent_sessions_storage_files": ("agent", "machine", "namespace", "profile", "tier"),
        "agent_sessions_storage_bytes": ("agent", "machine", "namespace", "profile", "tier"),
        "agent_sessions_storage_oldest_mtime_seconds": ("agent", "machine", "namespace", "profile", "tier"),
        "agent_sessions_storage_newest_mtime_seconds": ("agent", "machine", "namespace", "profile", "tier"),
        "agent_sessions_archive_pending_files": (),
        "agent_sessions_archive_pending_bytes": (),
        "agent_sessions_hot_retention_eligible_files": (),
        "agent_sessions_hot_retention_eligible_bytes": (),
        "agent_sessions_cold_nfs_mounted": (),
        "agent_sessions_archive_receipts": (),
        "agent_sessions_archive_last_success_timestamp_seconds": (),
        "agent_sessions_archive_receipt_jsonl_files": (),
        "agent_sessions_archive_receipt_jsonl_bytes": (),
        "agent_sessions_archive_version_snapshots": (),
        "agent_sessions_archive_version_files": (),
        "agent_sessions_archive_version_bytes": (),
        "agent_sessions_archive_version_newest_mtime_seconds": (),
    }
    assert set(expected.items()) <= {
        (name, tuple(k for k, _ in f.samples[0].labels)) for name, f in families.items() if f.samples
    }
    assert families["agent_sessions_archive_pending_files"].samples[0].value == 1
    assert families["agent_sessions_archive_receipt_jsonl_bytes"].samples[0].value == 3
    assert all("one.jsonl" not in str(f.samples) for f in families.values())


def test_archive_namespace_cardinality_is_bounded(tmp_path):
    hot = tmp_path / "hot"
    hot.mkdir()
    for number in range(70):
        (hot / f"pi-fixture{number}").mkdir()
    families = {f.name: f for f in ArchiveCollector(hot, None, None, None).collect()}
    assert len(families["agent_sessions_storage_files"].samples) <= 128  # two storage tiers


def test_worker_run_and_gc_families_bounded(tmp_path):
    (tmp_path / "agent-history.prom").write_text(
        'agent_history_run_success 1\nagent_history_run_files{result="parsed"} 2\n'
        'agent_history_run_files{result="/secret"} 3\n'
    )
    (tmp_path / "agent-history-embed.prom").write_text(
        'agent_history_embed_run_success 1\nagent_history_embed_run_skipped{reason="none"} 1\n'
        'agent_history_embed_gc_deleted 4\nagent_history_embed_gc_skipped{reason="none"} 1\n'
    )
    families = {f.name: f for f in RunCollector(tmp_path).collect()}
    assert families["agent_history_run_files"].samples == (Sample((("result", "parsed"),), 2),)
    assert families["agent_history_embed_run_success"].samples[0].value == 1
    assert families["agent_history_embed_gc_deleted"].samples[0].value == 4
    assert all("secret" not in str(f.samples) for f in families.values())


def test_periodic_index_writes_worker_snapshot(monkeypatch, tmp_path):
    from agent_history import cli, load

    class StopLoop(Exception):
        pass

    class Connection:
        def close(self):
            pass

    recorded = []

    def refresh(conn, *args, **kwargs):
        recorded.append(args[4])  # textfile destination in the public refresh contract
        return SimpleNamespace(lock_held=True, errors=0)

    monkeypatch.setattr(load, "connect", lambda dsn: Connection())
    monkeypatch.setattr(load, "refresh", refresh)
    monkeypatch.setattr(cli.time, "sleep", lambda seconds: (_ for _ in ()).throw(StopLoop()))
    with __import__("pytest").raises(StopLoop):
        cli.main(["--config", str(tmp_path / "missing.toml"), "index", "--every", "1"])
    assert recorded == [__import__("pathlib").Path("/var/lib/alloy/textfile-agent-history/agent-history.prom")]
    assert load.refresh is refresh


def test_exporter_config_rejects_unknown_collector():
    with __import__("pytest").raises(ConfigError, match="collectors"):
        parse_config({"exporter": {"collectors": ["unspecified"]}})


def test_efficiency_histogram_keeps_family_metadata_and_samples(tmp_path):
    from agent_history.metrics.efficiency import _Series

    series = _Series()
    for bound, value in (("60", 1), ("300", 2), ("+Inf", 2)):
        series.add(
            "agent_efficiency_first_spawn_seconds_bucket",
            value,
            {"agent": "pi", "le": bound},
            help_text="First spawn.",
            metric_type="histogram",
            family="agent_efficiency_first_spawn_seconds",
        )
    series.add(
        "agent_efficiency_first_spawn_seconds_sum",
        250,
        {"agent": "pi"},
        help_text="First spawn.",
        metric_type="histogram",
        family="agent_efficiency_first_spawn_seconds",
    )
    series.add(
        "agent_efficiency_first_spawn_seconds_count",
        2,
        {"agent": "pi"},
        help_text="First spawn.",
        metric_type="histogram",
        family="agent_efficiency_first_spawn_seconds",
    )
    families = series.families()
    assert len(families) == 1
    assert families[0].name == "agent_efficiency_first_spawn_seconds"
    assert families[0].type == "histogram"
    output = exposition(list(families), State(tmp_path))
    assert "# TYPE agent_efficiency_first_spawn_seconds histogram" in output
    assert output.index('le="60"') < output.index('le="300"') < output.index('le="+Inf"')
    assert 'agent_efficiency_first_spawn_seconds_sum{agent="pi"} 250' in output
    assert 'agent_efficiency_first_spawn_seconds_count{agent="pi"} 2' in output
    assert exposition(list(families), State(tmp_path)) == output


def test_rounded_restored_raw_is_not_a_reset(tmp_path):
    # A raw recorded at 12 significant digits can exceed the lossless source by a rounding step;
    # counting that as a reset doubled every such counter at the loop6 cutover.
    key = State.key("agent_example_seconds_total", (("loop", "a"),))
    (tmp_path / "counters.json").write_text(json.dumps({key: {"raw": 6462.96199942, "value": 6462.96199942}}))

    def render(raw):
        family = Family("agent_example_seconds_total", "counter", "Example.", (Sample((("loop", "a"),), raw),))
        return exposition([family], State(tmp_path))

    assert 'agent_example_seconds_total{loop="a"} 6462.96199942' in render(6462.961999416351)
    assert 'agent_example_seconds_total{loop="a"} 6472.96199942' in render(6472.961999416351)
    assert 'agent_example_seconds_total{loop="a"} 6475.96199942' in render(3)  # A real reset still counts.


def test_pruned_loop_leaves_exposition_and_state(tmp_path):
    # The efficiency collector drops a loop's series after its retention window, as the legacy
    # textfile did; retained counter state must not keep carrying that loop forever.
    def family(*samples):
        return Family("agent_example_total", "counter", "Example.", tuple(samples))

    both = family(
        Sample((("loop", "a"), ("model", "m")), 4),
        Sample((("loop", "b"), ("model", "m")), 5),
        Sample((("loop", "b"), ("model", "n")), 6),
    )
    output = exposition([both], State(tmp_path))
    assert 'loop="a"' in output
    pruned = exposition([family(Sample((("loop", "b"), ("model", "m")), 5))], State(tmp_path))
    assert 'loop="a"' in pruned  # Absence alone is not retirement.
    pruned = exposition([], State(tmp_path), retired_loops={"agent_example_total": {"a"}})
    assert 'loop="a"' not in pruned
    assert "a" not in {
        dict(json.loads(key)[1]).get("loop") for key in json.loads((tmp_path / "counters.json").read_text())
    }
    # A source that disappears while its loop is still emitted keeps its counted value.
    assert 'agent_example_total{loop="b",model="other"} 11' in exposition([family()], State(tmp_path))
    # An empty family is not proof that every loop ended.
    assert 'agent_example_total{loop="b",model="other"} 11' in exposition([family()], State(tmp_path))


def test_counter_never_falls_after_source_resets(tmp_path):
    state = State(tmp_path)
    assert "agent_example_total 9" in exposition(
        [Family("agent_example_total", "counter", "Example.", (Sample((), 9),))], state
    )
    restarted = State(tmp_path)
    assert "agent_example_total 11" in exposition(
        [Family("agent_example_total", "counter", "Example.", (Sample((), 2),))], restarted
    )
    assert "agent_example_total 14" in exposition(
        [Family("agent_example_total", "counter", "Example.", (Sample((), 5),))], restarted
    )


def test_omitted_loop_preserves_offset(tmp_path):
    def render(**raws):
        return exposition(
            [
                Family(
                    "agent_example_total",
                    "counter",
                    "Example.",
                    tuple(Sample((("loop", loop),), raw) for loop, raw in raws.items()),
                )
            ],
            State(tmp_path),
        )

    render(a=100, b=5)
    render(b=5)
    assert 'agent_example_total{loop="a"} 102' in render(a=2, b=5)


@pytest.mark.parametrize(
    "raw,lower",
    [(1000000000, 999999999), (1000000000000000, 999999999999999), (6462.961999416351, 6462.961999416)],
)
def test_near_value_resets_and_current_state_compatibility(tmp_path, raw, lower):
    # Existing live state needs no retirement metadata or migration.
    key = State.key("agent_example_total", ())
    (tmp_path / "counters.json").write_text(json.dumps({key: {"raw": raw, "value": raw + 10}}))
    state = State(tmp_path)
    assert state.observe("agent_example_total", (), raw) == raw + 10
    # Each drop is a reset, not a generic relative-tolerance rounding allowance.
    assert state.observe("agent_example_total", (), lower) == raw + 10 + lower


def test_actual_collector_partial_cache_and_retirement(tmp_path, monkeypatch):
    from agent_history.efficiency import parser as rules
    from agent_history.metrics.efficiency import EfficiencyCollector

    clock = [2000000000.0]
    monkeypatch.setattr("agent_history.metrics.efficiency.time.time", lambda: clock[0])
    directory = tmp_path / "collector"
    directory.mkdir()
    path = directory / "efficiency-state.json"
    metric = "agent_efficiency_llm_calls_total"

    def cache(**raws):
        state = rules.read_efficiency_state(path, clock[0])
        state["loops"] = {loop: clock[0] for loop in raws}
        state["totals"] = {metric: {"pi\tpi-local\tsolo\tuser\t" + loop: raw for loop, raw in raws.items()}}
        path.write_text(json.dumps(state))

    collector = EfficiencyCollector(parse_config({}), directory)
    offsets = State(tmp_path / "exporter")
    server = MetricServer(("127.0.0.1", 0), [collector], offsets, refresh=0)
    try:
        cache(a=100, b=5)
        server.metrics()
        clock[0] += 10
        cache(b=5)
        assert 'loop="a"' in server.metrics()
        cache(a=2, b=5)
        assert 'loop="a",namespace="pi-local",role="solo",trigger="user"} 102' in server.metrics()
        # A partial cache may also omit whole families previously emitted for this loop.
        offsets.observe("agent_efficiency_first_spawn_seconds_sum", (("loop", "a"),), 10)
        # The actual collector's retention decision removes persisted exporter offsets too.
        clock[0] += rules.EFFICIENCY_LOOP_RETAIN_SECONDS + 1
        assert 'loop="a"' not in server.metrics()
        assert not offsets.counters
    finally:
        server.server_close()


def test_failed_collector_does_not_apply_stale_retirement(tmp_path):
    class Failed:
        name = "efficiency"
        retired_loops = {"agent_example_total": {"a"}}

        def collect(self):
            raise OSError("unavailable")

    state = State(tmp_path)
    state.observe("agent_example_total", (("loop", "a"),), 100)
    server = MetricServer(("127.0.0.1", 0), [Failed()], state)
    try:
        server.metrics()
        assert state.value("agent_example_total", (("loop", "a"),)) == 100
    finally:
        server.server_close()


def test_retirement_replays_after_collector_restart(tmp_path, monkeypatch):
    from agent_history.efficiency import parser as rules
    from agent_history.metrics.efficiency import EfficiencyCollector

    now = 2000000000.0
    monkeypatch.setattr("agent_history.metrics.efficiency.time.time", lambda: now)
    directory = tmp_path / "collector"
    directory.mkdir()
    path = directory / "efficiency-state.json"
    state = rules.read_efficiency_state(path, now)
    metric = "agent_efficiency_llm_calls_total"
    state["loops"] = {"a": now - rules.EFFICIENCY_LOOP_RETAIN_SECONDS - 1}
    state["totals"] = {metric: {"pi\tpi-local\tsolo\tuser\ta": 100}}
    path.write_text(json.dumps(state))
    offsets = State(tmp_path / "exporter")
    offsets.observe(metric, (("loop", "a"),), 100)
    config = parse_config({})
    # Simulate the process dying after collector persistence, before exporter persistence.
    EfficiencyCollector(config, directory).collect()
    server = MetricServer(("127.0.0.1", 0), [EfficiencyCollector(config, directory)], offsets)
    try:
        server.metrics()
        assert offsets.counters == {}
        assert json.loads(path.read_text())["retired_loops"] == {}
    finally:
        server.server_close()

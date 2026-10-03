"""Public HTTP and worker-log privacy boundaries, with synthetic private inputs."""

import json
import os
import subprocess
import sys

import pytest

from agent_history.config import parse_config
from agent_history.metrics import Family, Sample
from agent_history.metrics.archive import ArchiveCollector
from agent_history.metrics.efficiency import EfficiencyCollector
from agent_history.metrics.collection import Collection, State


def scrape(collectors, directory):
    return Collection(collectors, State(directory), 0).metrics()


def samples(text, name):
    return [line for line in text.splitlines() if line.startswith(name + "{")]


def test_transcript_and_persisted_collector_models_are_private(tmp_path):
    source = tmp_path / "pi" / "sessions" / "synthetic"
    source.mkdir(parents=True)
    models = ["/synthetic/private/model-path", "gpt-6.1-sol-private-suffix", "gpt-6.1-sol", "claude-opus-4-6"]
    records = [{"type": "session", "id": "synthetic", "timestamp": "2026-09-29T20:00:00Z"}]
    for model in models:
        records.append(
            {
                "type": "message",
                "timestamp": "2026-09-29T20:00:01Z",
                "message": {"role": "assistant", "model": model, "usage": {"input": 1, "output": 1}, "content": []},
            }
        )
    transcript = source / "fixture.jsonl"
    transcript.write_text("".join(json.dumps(record) + "\n" for record in records))
    config = parse_config({"sources": {"pi-local": str(tmp_path / "pi")}, "efficiency": {"first_parse_days": 1000}})
    collector = EfficiencyCollector(config, tmp_path / "collector-state")
    text = scrape([collector], tmp_path / "counter-state")
    transcript.unlink()  # Same real collector now reads the historical series from its state file.
    restarted = scrape([EfficiencyCollector(config, collector.state_dir)], tmp_path / "counter-state")
    for output in (text, restarted):
        assert all(model not in output for model in models[:2])
        calls = samples(output, "agent_efficiency_model_calls_total")
        assert len(calls) == 3
        assert any('model="other"' in line and line.endswith(" 2") for line in calls)
        assert any('model="gpt-6.1-sol"' in line and line.endswith(" 1") for line in calls)
        assert any('model="claude-opus-4-6"' in line and line.endswith(" 1") for line in calls)


def test_missing_model_value_is_unknown_at_http_boundary(tmp_path):
    class Collector:
        name = "synthetic"

        def collect(self):
            return [Family("agent_calls_total", "counter", "Calls.", (Sample((("model", ""),), 1),))]

    assert samples(scrape([Collector()], tmp_path), "agent_calls_total") == ['agent_calls_total{model="unknown"} 1']


def test_privacy_collisions_preserve_histograms_and_historical_counters(tmp_path):
    private = ["/synthetic/private/one", "/synthetic/private/two"]
    counter_name = "agent_efficiency_model_calls_total"
    historic = {
        json.dumps([counter_name, [["model", model]]], separators=(",", ":")): {"raw": raw, "value": raw + 10}
        for model, raw in zip(private, [2, 3])
    }
    (tmp_path / "counters.json").write_text(json.dumps(historic))

    class Collector:
        name = "synthetic"

        def collect(self):
            counter = Family(
                counter_name, "counter", "Calls.", tuple(Sample((("model", m),), n) for m, n in zip(private, [2, 3]))
            )
            histogram = Family(
                "agent_latency",
                "histogram",
                "Latency.",
                tuple(
                    Sample(tuple(sorted({"model": model, **labels}.items())), value, "agent_latency" + suffix)
                    for model in private
                    for suffix, labels, value in [
                        ("_bucket", {"le": "1"}, 1),
                        ("_bucket", {"le": "+Inf"}, 2),
                        ("_sum", {}, 3),
                        ("_count", {}, 2),
                    ]
                ),
            )
            return [counter, histogram]

    first = scrape([Collector()], tmp_path)
    second = scrape([Collector()], tmp_path)
    assert first == second
    assert not any(model in first for model in private)
    assert samples(first, counter_name) == [counter_name + '{model="other"} 25']
    assert samples(first, "agent_latency_bucket") == [
        'agent_latency_bucket{le="1",model="other"} 2',
        'agent_latency_bucket{le="+Inf",model="other"} 4',
    ]
    assert samples(first, "agent_latency_sum") == ['agent_latency_sum{model="other"} 6']
    assert samples(first, "agent_latency_count") == ['agent_latency_count{model="other"} 4']


def test_standalone_namespace_collisions_keep_counts_and_time_extrema(tmp_path):
    hot = tmp_path / "hot"
    for suffix, content, mtime in [("fixture-a", "abc", 100), ("fixture-b", "abcde", 200)]:
        folder = hot / ("pi-standalone-" + suffix)
        folder.mkdir(parents=True)
        path = folder / "one.jsonl"
        path.write_text(content)
        os.utime(path, (mtime, mtime))
    text = scrape([ArchiveCollector(hot, None, None, None)], tmp_path / "state")
    assert "fixture-a" not in text and "fixture-b" not in text
    for suffix, expected in [("files", 2), ("bytes", 8), ("oldest_mtime_seconds", 100), ("newest_mtime_seconds", 200)]:
        rows = [line for line in samples(text, "agent_sessions_storage_" + suffix) if 'tier="hot"' in line]
        assert len(rows) == 1
        assert 'namespace="pi-standalone"' in rows[0]
        assert 'machine="other"' in rows[0]
        assert rows[0].endswith(f" {expected}")


@pytest.mark.parametrize("historical", [False, True])
def test_source_resets_restart_and_disappearance_before_public_sum(tmp_path, historical):
    a, b, c = ["/synthetic/private/" + name for name in ("a", "b", "c")]
    current = {a: 9, b: 1}
    components = [
        ("agent_model_calls_total", {}),
        ("agent_latency_bucket", {"le": "1"}),
        ("agent_latency_bucket", {"le": "+Inf"}),
        ("agent_latency_sum", {}),
        ("agent_latency_count", {}),
    ]
    if historical:
        original = {
            json.dumps([name, sorted({"model": model, **labels}.items())], separators=(",", ":")): {
                "raw": raw,
                "value": raw + 10,
            }
            for name, labels in components
            for model, raw in current.items()
        }
        (tmp_path / "counters.json").write_text(json.dumps(original))

    class Collector:
        name = "synthetic"

        def collect(self):
            return [
                Family(
                    "agent_model_calls_total",
                    "counter",
                    "Calls.",
                    tuple(Sample((("model", model),), raw) for model, raw in current.items()),
                ),
                Family(
                    "agent_latency",
                    "histogram",
                    "Latency.",
                    tuple(
                        Sample(tuple(sorted({"model": model, **labels}.items())), raw, name)
                        for name, labels in components[1:]
                        for model, raw in current.items()
                    ),
                ),
            ]

    def check(expected):
        # Each real HTTP scrape constructs State anew, exercising durable restart accounting.
        text = scrape([Collector()], tmp_path)
        assert "/synthetic/private/" not in text
        assert "# TYPE agent_latency histogram" in text
        assert len([line for line in text.splitlines() if not line.startswith("#")]) == 5
        for name, labels in components:
            public = tuple(sorted({"model": "other", **labels}.items()))
            encoded = ",".join(f'{key}="{value}"' for key, value in public)
            assert f"{name}{{{encoded}}} {expected}" in text

    offset = 20 if historical else 0
    check(10 + offset)
    current.update({a: 2, b: 12})
    check(23 + offset)  # Reset A contributes 2; increasing B contributes 11, not 4 collectively.
    check(23 + offset)
    current.update({a: 3, b: 14})
    check(26 + offset)
    del current[b]
    check(26 + offset)
    current[b] = 14
    check(26 + offset)
    current.clear()
    check(26 + offset)
    current.update({a: 3, b: 15})
    check(27 + offset)
    current[c] = 2
    check(29 + offset)


@pytest.mark.skipif(not os.environ.get("AGENT_HISTORY_TEST_DSN"), reason="needs disposable database DSN")
def test_actual_periodic_cli_malformed_source_exits_without_payload(tmp_path):
    private = "/synthetic/private/source-value"
    dsn = os.environ["AGENT_HISTORY_TEST_DSN"]  # Required disposable database, never a live catalogue.
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "agent_history.cli",
            "--config",
            str(tmp_path / "absent.toml"),
            "--dsn",
            dsn,
            "index",
            "--source",
            private,
            "--every",
            "0.1",
        ],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr == ""


@pytest.mark.parametrize("status, expected", [(7, 7), (0, 0), ("/synthetic/private/exit", 1), (None, 1)])
def test_periodic_system_exit_is_numeric_and_does_not_retry(monkeypatch, tmp_path, capsys, status, expected):
    from agent_history import cli, load

    def connect(dsn):
        raise SystemExit(status)

    monkeypatch.setattr(load, "connect", connect)
    monkeypatch.setattr(cli.time, "sleep", lambda seconds: pytest.fail("SystemExit must escape, not retry"))
    with pytest.raises(SystemExit) as caught:
        cli.main(["--config", str(tmp_path / "absent.toml"), "index", "--every", "1"])
    assert caught.value.code == expected
    output = capsys.readouterr()
    assert output.out == output.err == ""


class StopWorker(BaseException):
    pass


@pytest.mark.parametrize("exception", [KeyboardInterrupt, StopWorker])
def test_periodic_other_base_exceptions_escape(monkeypatch, tmp_path, exception):
    from agent_history import cli, load

    def connect(dsn):
        raise exception()

    monkeypatch.setattr(load, "connect", connect)
    monkeypatch.setattr(cli.time, "sleep", lambda seconds: pytest.fail("BaseException must escape, not retry"))
    with pytest.raises(exception):
        cli.main(["--config", str(tmp_path / "absent.toml"), "index", "--every", "1"])


@pytest.mark.parametrize("command", ["index", "embed"])
def test_periodic_outer_exception_does_not_log_paths(monkeypatch, tmp_path, capsys, command):
    from agent_history import cli, load

    missing = tmp_path / "synthetic-private-session.jsonl"
    # A database connection edge that performs a real failing filesystem operation, not an echoed error.
    monkeypatch.setattr(load, "connect", lambda dsn: missing.read_text())
    intervals = []

    def sleep(seconds):
        intervals.append(seconds)
        if len(intervals) == 2:
            raise StopWorker()

    monkeypatch.setattr(cli.time, "sleep", sleep)
    with pytest.raises(StopWorker):
        cli.main(["--config", str(tmp_path / "absent.toml"), command, "--every", "0.1"])
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == f"agent-history: {command} failed (io); retrying\n" * 2
    assert intervals == [0.1, 0.1]


@pytest.mark.parametrize("command", ["index", "embed"])
def test_periodic_nested_config_diagnostic_is_suppressed(monkeypatch, tmp_path, capsys, command):
    from agent_history import cli

    config = tmp_path / "config.toml"
    config.write_text('synthetic_private_diagnostic = "private-value"\n')
    monkeypatch.setattr(cli.time, "sleep", lambda seconds: (_ for _ in ()).throw(StopWorker()))
    with pytest.raises(StopWorker):
        cli.main(["--config", str(config), command, "--every", "1"])
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == f"agent-history: {command} failed (status); retrying\n"
    assert cli.main(["--config", str(config), command]) == 2
    assert "synthetic_private_diagnostic" in capsys.readouterr().err  # Interactive diagnostics stay useful.


def test_periodic_nested_provider_diagnostic_is_suppressed(monkeypatch, tmp_path, capsys):
    from agent_history import cli, load

    class Connection:
        def close(self):
            pass

    monkeypatch.setattr(load, "connect", lambda dsn: Connection())  # Only the database connection edge is replaced.
    monkeypatch.setattr(cli.time, "sleep", lambda seconds: (_ for _ in ()).throw(StopWorker()))
    with pytest.raises(StopWorker):
        cli.main(["--config", str(tmp_path / "absent.toml"), "embed", "--every", "1"])
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == "agent-history: embed failed (status); retrying\n"
    assert cli.main(["--config", str(tmp_path / "absent.toml"), "embed"]) == 2
    assert "embeddings are off" in capsys.readouterr().err

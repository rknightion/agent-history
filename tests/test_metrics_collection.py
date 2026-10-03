"""Public loader and metric collection contracts at their used boundaries."""

import json
import re

import pytest

from agent_history.config import ConfigError, load_config, parse_config
from agent_history.metrics import Family, Sample
from agent_history.metrics.archive import ArchiveCollector
from agent_history.metrics.efficiency import EfficiencyCollector
from agent_history.metrics.self import SelfCollector
from agent_history.metrics.collection import Collection, State, exposition


def test_trusted_labels_through_shared_and_mcp_loader(tmp_path, monkeypatch):
    from agent_history.mcp_server import _config

    path = tmp_path / "config.toml"
    path.write_text('[metrics_labels]\nmachines = ["worker-a"]\nmodels = ["example-model"]\n')
    monkeypatch.setenv("AGENT_HISTORY_CONFIG", str(path))
    for config in (load_config(path), _config()):
        assert config.metrics_labels.machines == frozenset({"worker-a"})
        assert config.metrics_labels.models == frozenset({"example-model"})
        families = [
            Family(
                "example_calls_total",
                "counter",
                "Calls.",
                (
                    Sample((("model", "example-model"),), 3),
                    Sample((("model", "untrusted-a"),), 4),
                    Sample((("model", "untrusted-b"),), 5),
                ),
            )
        ]
        text = exposition(families, State(tmp_path / "state", config.metrics_labels))
        assert 'model="example-model"} 3' in text
        assert 'model="other"} 9' in text
    # The new optional table must not weaken any existing strict validation.
    for invalid in (
        {"surprise": 1},
        {"exporter": {"collectors": ["not-a-collector"]}},
        {"efficiency": {"surprise": 1}},
        {"sources": {"unknown": "example"}},
    ):
        with pytest.raises(ConfigError):
            parse_config({**invalid, "metrics_labels": {"machines": ["worker-a"]}})


@pytest.mark.parametrize(
    "section", [None, {}, [], "bad", {"models": "bad"}, {"models": ["example-model", 1]}, {"unexpected": []}]
)
def test_absent_empty_malformed_labels_keep_current_output(tmp_path, section):
    data = {} if section is None else {"metrics_labels": section}
    config = parse_config(data)
    family = Family("example", "gauge", "Example.", (Sample((("model", "example-model"),), 7),))
    assert exposition([family], State(tmp_path / "state", config.metrics_labels)) == exposition(
        [family], State(tmp_path / "default")
    )
    assert config.metrics_labels.machines == frozenset()
    assert config.metrics_labels.models == frozenset()


def test_allowlisted_archive_machine_and_configured_zero_namespaces(tmp_path):
    config = parse_config(
        {"sources": {"pi-standalone-worker-a": str(tmp_path / "absent")}, "metrics_labels": {"machines": ["worker-a"]}}
    )
    collector = ArchiveCollector(None, None, None, None, namespaces=config.sources, labels=config.metrics_labels)
    text = exposition(list(collector.collect()), State(tmp_path / "state", config.metrics_labels))
    labels = 'agent="pi",machine="worker-a",namespace="pi-standalone-worker-a",profile="standalone"'
    for tier in ("hot", "cold"):
        assert f'agent_sessions_storage_files{{{labels},tier="{tier}"}} 0' in text
        assert f'agent_sessions_storage_bytes{{{labels},tier="{tier}"}} 0' in text
    assert "storage_oldest_mtime_seconds{" not in text
    assert "storage_newest_mtime_seconds{" not in text


def test_loop_map_failure_is_visible_but_optional_over_http(tmp_path, monkeypatch, capsys):
    import psycopg
    import time

    def unavailable(*args, **kwargs):
        raise psycopg.OperationalError("synthetic unavailability")

    monkeypatch.setattr(psycopg, "connect", unavailable)
    rollouts = tmp_path / "codex" / "sessions" / "2026"
    rollouts.mkdir(parents=True)
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(time.time() - 60))
    (rollouts / "rollout-a.jsonl").write_text(
        json.dumps({"timestamp": stamp, "type": "session_meta", "payload": {"id": "thread-a"}})
        + "\n"
        + json.dumps({"timestamp": stamp, "type": "token_usage_record", "payload": {"usage": {"input_tokens": 5}}})
        + "\n"
    )
    dsn = "postgresql://reader-dsn-secret@127.0.0.1:1/synthetic"
    config = parse_config({"sources": {"codex-local": str(tmp_path / "codex")}, "efficiency": {"loop_dsn": dsn}})
    health = SelfCollector()
    health.record("loops", 0.01, False)
    last = health.last_success["loops"]
    collectors = [EfficiencyCollector(config, tmp_path / "efficiency"), health]
    text = Collection(collectors, State(tmp_path / "collection")).metrics()
    # The efficiency section still succeeds and attributes everything to loop="none", with no map age.
    assert 'agent_sessions_metrics_section_success{section="efficiency"} 1' in text
    assert 'agent_efficiency_llm_calls_total{agent="codex",loop="none"' in text
    assert {label for label in re.findall(r'loop="([^"]*)"', text)} == {"none"}
    assert "agent_efficiency_loop_map_age_seconds" not in text
    streams = capsys.readouterr()
    assert all("reader-dsn-secret" not in surface for surface in (text, streams.out, streams.err))
    assert 'agent_sessions_metrics_section_success{section="loops"} 0' in text
    assert 'agent_sessions_metrics_section_duration_seconds{section="loops"}' in text
    assert health.last_success["loops"] == last
    assert 'agent_sessions_metrics_section_last_success_timestamp_seconds{section="loops"}' in text
    assert "agent_sessions_metrics_collection_success 1" in text
    assert "agent_sessions_metrics_collection_failures_total 0" in text


def test_storage_and_archive_are_distinct_kept_health_sections(tmp_path):
    text = Collection([ArchiveCollector(None, None, None, None), SelfCollector()], State(tmp_path)).metrics()
    for name in ("storage", "archive"):
        assert f'agent_sessions_metrics_section_success{{section="{name}"}} 1' in text


def test_exposition_keeps_legacy_precision(tmp_path):
    families = [
        Family("example_timestamp_seconds", "gauge", "Epoch.", (Sample((), 1790785741.01),)),
        Family("example_seconds_total", "counter", "Time.", (Sample((), 1.2000000476837158),)),
        Family("example_total", "counter", "Count.", (Sample((), 3.0),)),
    ]
    text = exposition(families, State(tmp_path / "state"))
    assert "example_timestamp_seconds 1790785741.01\n" in text
    assert "example_seconds_total 1.20000004768\n" in text
    assert "example_total 3\n" in text

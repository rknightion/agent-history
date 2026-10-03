"""Regression proofs for collector isolation and periodic worker resilience."""

from pathlib import Path

import pytest

from agent_history.metrics import Family, Sample
from agent_history.metrics.archive import ArchiveCollector
from agent_history.metrics.self import SelfCollector
from agent_history.metrics.collection import Collection, State


def test_failed_collector_keeps_the_collection_and_throttles(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from agent_history.metrics import collection as collection_module

    # A newly booted host must still populate the first collection before refresh expires.
    monkeypatch.setattr(collection_module, "time", SimpleNamespace(monotonic=lambda: 1.0))

    class Broken:
        name = "broken"
        calls = 0

        def collect(self):
            self.calls += 1
            yield Family("agent_partial", "gauge", "Partial.", (Sample((), 1),))
            raise RuntimeError("collector unavailable")

    class Healthy:
        name = "healthy"

        def collect(self):
            return [Family("agent_healthy", "gauge", "Healthy.", (Sample((), 7),))]

    broken = Broken()
    collection = Collection([broken, Healthy(), SelfCollector()], State(tmp_path), 60)
    text = collection.metrics()
    assert "agent_healthy 7" in text
    assert "agent_partial" not in text
    assert 'collector="broken"' in text
    assert collection.updated > 0
    assert collection.metrics() == text
    assert broken.calls == 1
    assert any("broken" in key and value["value"] == 1 for key, value in collection.state.counters.items())


def test_worker_textfiles_have_dedicated_writable_volume():
    root = Path(__file__).resolve().parents[1]
    compose = (root / "compose.yml").read_text()
    assert "- textfile:/var/lib/alloy/textfile-agent-history" in compose
    assert "\n  textfile:" in compose
    dockerfile = (root / "Dockerfile").read_text()
    assert "/var/lib/alloy/textfile-agent-history" in dockerfile


@pytest.mark.parametrize("option", [["--every=1"], ["--ev", "1"], ["--every", "1"]])
def test_periodic_iteration_exception_retries(monkeypatch, tmp_path, option, capsys):
    from agent_history import cli, load

    calls = []
    sleeps = []

    class StopLoop(Exception):
        pass

    def connect(dsn):
        calls.append(dsn)
        raise RuntimeError("database temporarily unavailable")

    def sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 2:
            raise StopLoop()

    original = load.refresh
    monkeypatch.setattr(load, "connect", connect)
    monkeypatch.setattr(cli.time, "sleep", sleep)
    with pytest.raises(StopLoop):
        cli.main(["--config", str(tmp_path / "missing.toml"), "index", *option])
    assert len(calls) == 2
    assert sleeps == [1, 1]
    assert "retrying" in capsys.readouterr().err
    assert load.refresh is original


@pytest.mark.parametrize("receipt", ["{", "[]", '{"jsonl_files":"invalid"}'])
def test_malformed_receipt_skips_only_receipt_gauges(tmp_path, receipt):
    cold = tmp_path / "cold"
    receipts = cold / ".archive-receipts"
    receipts.mkdir(parents=True)
    (receipts / "20260929T000000.000Z.json").write_text(receipt)
    families = {f.name: f for f in ArchiveCollector(None, cold, None, None).collect()}
    assert "agent_sessions_archive_receipt_jsonl_files" not in families
    assert families["agent_sessions_archive_receipts"].samples[0].value == 1


@pytest.mark.parametrize("interval", ["bad", None, float("nan"), float("inf"), 0])
def test_invalid_exporter_refresh_is_config_error(interval):
    from agent_history.config import ConfigError, parse_config

    with pytest.raises(ConfigError, match="refresh_interval"):
        parse_config({"exporter": {"refresh_interval": interval}})


def test_rotating_receipt_and_incoming_file_do_not_abort_collection(tmp_path, monkeypatch):
    cold = tmp_path / "cold"
    receipts = cold / ".archive-receipts"
    receipts.mkdir(parents=True)
    receipt = receipts / "receipt.json"
    receipt.write_text("{}")
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    transient = incoming / "one.jsonl"
    transient.write_text("abc")
    original = Path.stat
    original_is_file = Path.is_file

    def is_file(path, *args, **kwargs):
        # Both files existed at discovery; disappearance happens at the metadata edge.
        # Do not depend on whether this Python version implements is_file via stat.
        return True if path in (receipt, transient) else original_is_file(path, *args, **kwargs)

    def stat(path, *args, **kwargs):
        # Rotate only after successful discovery, at the explicit metadata read.
        if path in (receipt, transient) and not kwargs:
            raise FileNotFoundError("synthetic rotation")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "is_file", is_file)
    monkeypatch.setattr(Path, "stat", stat)
    families = {f.name: f for f in ArchiveCollector(None, cold, incoming, None).collect()}
    assert families["agent_history_cold_tier_available"].samples[0].value == 0
    assert families["agent_sessions_archive_receipt_timestamp_seconds"].samples[0].value == 0


def test_archive_counts_jsonl_and_reuses_storage_walk(tmp_path, monkeypatch):
    from agent_history.metrics import archive

    hot = tmp_path / "hot"
    (hot / "pi-fixture").mkdir(parents=True)
    (hot / "pi-fixture" / "one.jsonl").write_text("abc")
    (hot / "pi-fixture" / "note.txt").write_text("not transcript")
    original = archive.os.walk
    walks = []

    def walk(root, **kwargs):
        walks.append(root)
        return original(root, **kwargs)

    monkeypatch.setattr(archive.os, "walk", walk)
    families = {f.name: f for f in ArchiveCollector(hot, None, None, None).collect()}
    assert families["agent_sessions_archive_files"].samples[0].value == 1
    assert families["agent_sessions_archive_bytes"].samples[0].value == 3
    assert len(walks) == 1

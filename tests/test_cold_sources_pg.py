"""Configured CLI archive observations preserve hot-only indexing and parser positions."""

import json
import os
import shutil
from types import SimpleNamespace

from test_loader_pg import clean, conn  # noqa: F401
from test_loader_pg import pytestmark as pytestmark
from test_source_tails_pg import source_tree

from agent_history import cli, source_availability


def test_configured_index_reconciles_cold_metadata_in_one_transaction(clean, tmp_path, monkeypatch, capsys):  # noqa: F811
    hot, cold, rel, _ = source_tree(tmp_path, b"")
    for number in range(3):
        shutil.copy2(hot / rel, (hot / rel).with_name(f"copy-{number}.jsonl"))
    config = tmp_path / "config.toml"
    config.write_text(
        f"dsn = {json.dumps(os.environ['AGENT_HISTORY_TEST_DSN'])}\n"
        f"[sources]\nclaude-local = {json.dumps(str(hot / 'claude-local'))}\n"
        f"[cold_sources]\nclaude-local = {json.dumps(str(cold / 'claude-local'))}\n"
    )
    argv = ["--config", str(config), "index"]
    assert cli.main(argv) == 0
    before = clean.execute(
        "SELECT id,indexed_offset,line_count,indexed_at,parser_state FROM ah.source_file ORDER BY id"
    ).fetchall()
    clean.rollback()
    shutil.copytree(hot, cold)
    # Cold-only content must never be admitted by incremental indexing.
    (cold / rel).with_name("cold-only.jsonl").write_text('{"type":"summary"}\n')
    assert cli.main(argv) == 0
    assert clean.execute("SELECT count(*) FROM ah.source_file WHERE available_cold").fetchone() == (4,)
    assert clean.execute("SELECT count(DISTINCT xmin::text) FROM ah.source_file").fetchone() == (1,)
    assert (
        clean.execute(
            "SELECT id,indexed_offset,line_count,indexed_at,parser_state FROM ah.source_file ORDER BY id"
        ).fetchall()
        == before
    )

    clean.rollback()
    probes = []
    original_probe = source_availability.Observer.file

    def probe(observer, root, relative):
        probes.append(relative)
        return original_probe(observer, root, relative)

    monkeypatch.setattr(source_availability.Observer, "file", probe)
    assert cli.main(argv) == 0
    assert probes == []  # Cached indexed files cause no per-file NFS lookup.

    archived = cold / rel
    archived.rename(tmp_path / "retained-archive.jsonl")
    # A deletion is observed at the next census, without any transcript growth.
    clock = source_availability.time.time()
    monkeypatch.setattr(source_availability, "time", SimpleNamespace(time=lambda: clock + 3601))
    assert cli.main(argv) == 0
    assert len(probes) == 4
    assert clean.execute("SELECT available_cold FROM ah.source_file WHERE rel_path=%s", (rel,)).fetchone() == (False,)
    clean.rollback()
    probes.clear()
    # A symlink to even the exact retained bytes is excluded.
    archived.symlink_to(tmp_path / "retained-archive.jsonl")
    monkeypatch.setattr(source_availability, "time", SimpleNamespace(time=lambda: clock + 7202))
    assert cli.main(argv) == 0
    assert clean.execute("SELECT available_cold FROM ah.source_file WHERE rel_path=%s", (rel,)).fetchone() == (False,)
    clean.rollback()
    archived.rename(tmp_path / "retained-symlink")
    shutil.copy2(hot / rel, archived)
    monkeypatch.setattr(source_availability, "time", SimpleNamespace(time=lambda: clock + 10803))
    assert cli.main(argv) == 0
    assert clean.execute("SELECT count(*) FROM ah.source_file WHERE available_cold").fetchone() == (4,)
    clean.rollback()
    # Missing mount/root is unknown: keep last observations, emit one bounded diagnostic.
    cold.rename(tmp_path / "unmounted-archive")
    probes.clear()
    capsys.readouterr()
    assert cli.main(argv) == 0
    assert probes == []
    assert '"cold_availability": "unknown", "observations": 1' in capsys.readouterr().out
    assert clean.execute("SELECT count(*) FROM ah.source_file WHERE available_cold").fetchone() == (4,)
    assert (
        clean.execute(
            "SELECT id,indexed_offset,line_count,indexed_at,parser_state FROM ah.source_file ORDER BY id"
        ).fetchall()
        == before
    )


def test_configured_partial_source_observes_arrival_between_censuses(clean, tmp_path):  # noqa: F811
    hot, cold, rel, prefix = source_tree(tmp_path, b"\0" * 73)
    (cold / "claude-local").mkdir(parents=True)
    config = tmp_path / "config.toml"
    config.write_text(
        f"dsn = {json.dumps(os.environ['AGENT_HISTORY_TEST_DSN'])}\n"
        f"[sources]\nclaude-local = {json.dumps(str(hot / 'claude-local'))}\n"
        f"[cold_sources]\nclaude-local = {json.dumps(str(cold / 'claude-local'))}\n"
    )
    argv = ["--config", str(config), "index"]
    assert cli.main(argv) == 0
    before = clean.execute(
        "SELECT indexed_offset,indexed_at,parser_state,nul_tail_bytes FROM ah.source_file"
    ).fetchone()
    clean.rollback()
    (cold / rel).parent.mkdir(parents=True)
    shutil.copy2(hot / rel, cold / rel)
    assert cli.main(argv) == 0
    assert clean.execute("SELECT available_cold FROM ah.source_file").fetchone() == (True,)
    assert (
        clean.execute("SELECT indexed_offset,indexed_at,parser_state,nul_tail_bytes FROM ah.source_file").fetchone()
        == before
    )
    assert before[0] == len(prefix)
    assert before[3] == 73


def test_removed_mapping_is_not_reused_on_readdition(clean, tmp_path):  # noqa: F811
    hot, cold, rel, _ = source_tree(tmp_path, b"")
    shutil.copytree(hot, cold)
    config = tmp_path / "config.toml"
    base = (
        f"dsn = {json.dumps(os.environ['AGENT_HISTORY_TEST_DSN'])}\n"
        f"[sources]\nclaude-local = {json.dumps(str(hot / 'claude-local'))}\n"
    )
    mapping = f"[cold_sources]\nclaude-local = {json.dumps(str(cold / 'claude-local'))}\n"
    argv = ["--config", str(config), "index"]
    config.write_text(base + mapping)
    assert cli.main(argv) == 0
    config.write_text(base)
    assert cli.main(argv) == 0
    assert clean.execute("SELECT available_cold FROM ah.source_file").fetchone() == (False,)
    clean.rollback()
    config.write_text(base + mapping)
    assert cli.main(argv) == 0
    assert clean.execute("SELECT available_cold FROM ah.source_file").fetchone() == (True,)


def test_root_disappearance_discards_namespace_census(clean, tmp_path, monkeypatch):  # noqa: F811
    hot, cold, rel, _ = source_tree(tmp_path, b"")
    shutil.copytree(hot, cold)
    config = tmp_path / "config.toml"
    config.write_text(
        f"dsn = {json.dumps(os.environ['AGENT_HISTORY_TEST_DSN'])}\n"
        f"[sources]\nclaude-local = {json.dumps(str(hot / 'claude-local'))}\n"
        f"[cold_sources]\nclaude-local = {json.dumps(str(cold / 'claude-local'))}\n"
    )
    argv = ["--config", str(config), "index"]
    assert cli.main(argv) == 0
    previous_census = clean.execute(
        "SELECT value FROM ah.meta WHERE key=%s", (source_availability.CENSUS_KEY,)
    ).fetchone()
    clean.rollback()
    clock = source_availability.time.time()
    monkeypatch.setattr(source_availability, "time", SimpleNamespace(time=lambda: clock + 3601))
    original = source_availability.Observer.file

    def remove_root(observer, root, relative):
        cold.rename(tmp_path / "retained-root")
        return original(observer, root, relative)

    monkeypatch.setattr(source_availability.Observer, "file", remove_root)
    assert cli.main(argv) == 0
    assert clean.execute("SELECT available_cold FROM ah.source_file").fetchone() == (True,)

    assert (
        clean.execute("SELECT value FROM ah.meta WHERE key=%s", (source_availability.CENSUS_KEY,)).fetchone()
        == previous_census
    )


def test_source_filter_preserves_other_configured_census(clean, tmp_path):  # noqa: F811
    hot, cold, rel, _ = source_tree(tmp_path, b"")
    shutil.copytree(hot, cold)
    config = tmp_path / "config.toml"
    config.write_text(
        f"dsn = {json.dumps(os.environ['AGENT_HISTORY_TEST_DSN'])}\n"
        f"[sources]\nclaude-local = {json.dumps(str(hot / 'claude-local'))}\n"
        f"claude-filtered = {json.dumps(str(hot / 'claude-filtered'))}\n"
        f"[cold_sources]\nclaude-local = {json.dumps(str(cold / 'claude-local'))}\n"
        f"claude-filtered = {json.dumps(str(cold / 'claude-filtered'))}\n"
    )
    cached = {"claude-filtered": {"mapping": "retained-observation", "checked_at": 123}}
    clean.execute(
        "INSERT INTO ah.meta VALUES (%s,%s) ON CONFLICT (key) DO UPDATE SET value=excluded.value",
        (source_availability.CENSUS_KEY, json.dumps(cached)),
    )
    clean.commit()
    assert cli.main(["--config", str(config), "index", "--source", f"claude-local={hot / 'claude-local'}"]) == 0
    actual = json.loads(
        clean.execute("SELECT value FROM ah.meta WHERE key=%s", (source_availability.CENSUS_KEY,)).fetchone()[0]
    )
    assert actual["claude-filtered"] == cached["claude-filtered"]

"""Source-map rebuild preserves cold-only transcripts or refuses without mutation."""

import shutil

import pytest
from agent_history import cli, load
from agent_history.config import ConfigError, parse_config

from test_loader_pg import DSN, build_tree, clean, conn  # noqa: F401

pytestmark = pytest.mark.skipif(
    not DSN or "agent_history_test" not in DSN,
    reason="AGENT_HISTORY_TEST_DSN (a *_test database) not set",
)


def layout(tmp_path):
    hot, cold = build_tree(tmp_path)
    namespace = "claude-local"
    source = hot / namespace
    archive = cold / namespace
    shutil.copytree(source, archive)
    victim = next(source.rglob("11111111-1111-4111-8111-111111111111.jsonl"))
    rel = f"{namespace}/{victim.relative_to(source).as_posix()}"
    victim.unlink()
    return {namespace: source}, {namespace: archive}, rel


def test_rebuild_catalogues_cold_only_and_prefers_hot(clean, tmp_path):  # noqa: F811
    sources, cold_sources, rel = layout(tmp_path)
    config = parse_config(
        {
            "sources": {n: str(p) for n, p in sources.items()},
            "cold_sources": {n: str(p) for n, p in cold_sources.items()},
        }
    )
    stats = load.rebuild(
        clean, sources=config.sources, cold_sources=config.cold_sources, textfile=None, log=lambda *_: None
    )
    assert stats.errors == 0
    assert clean.execute(
        "SELECT tier, available_hot, available_cold FROM ah.source_file WHERE rel_path = %s", (rel,)
    ).fetchone() == ("cold", False, True)
    entries, report = load.check_rebuild_sources(clean, sources, cold_sources)
    shared = [e for e in entries.values() if e.hot and e.cold]
    assert shared and all(e.path == e.hot for e in shared)
    assert report["claude-local"]["cold_only"] == 1
    assert not report["claude-local"]["refused"]


@pytest.mark.parametrize("fault", ["missing", "unreadable"])
def test_rebuild_refuses_before_lock_or_truncate(clean, tmp_path, monkeypatch, fault):  # noqa: F811
    sources, cold_sources, rel = layout(tmp_path)
    load.rebuild(clean, sources=cold_sources, textfile=None, log=lambda *_: None)
    before = clean.execute("SELECT rel_path FROM ah.source_file ORDER BY rel_path").fetchall()
    clean.commit()
    if fault == "missing":
        (cold_sources["claude-local"] / rel.split("/", 1)[1]).unlink()
    else:
        cold_sources["claude-local"] = tmp_path / "absent"
    monkeypatch.setattr(load, "try_lock", lambda _: pytest.fail("refusal must precede writer lock"))
    with pytest.raises(SystemExit, match=r"rebuild refused: claude-local count=1"):
        load.rebuild(clean, sources=sources, cold_sources=cold_sources, textfile=None)
    assert clean.execute("SELECT rel_path FROM ah.source_file ORDER BY rel_path").fetchall() == before


def test_unsearchable_cold_tree_refuses_with_complete_hot_catalogue(clean, tmp_path, monkeypatch):  # noqa: F811
    sources, cold_sources, rel = layout(tmp_path)
    archive = cold_sources["claude-local"]
    relative = rel.split("/", 1)[1]
    shutil.copy2(archive / relative, sources["claude-local"] / relative)
    load.rebuild(clean, sources=sources, textfile=None, log=lambda *_: None)
    before = clean.execute("SELECT rel_path FROM ah.source_file ORDER BY rel_path").fetchall()
    clean.commit()
    archive.chmod(0o400)
    try:
        monkeypatch.setattr(load, "try_lock", lambda _: pytest.fail("refusal must precede writer lock"))
        with pytest.raises(SystemExit, match=r"rebuild refused: claude-local count=1"):
            load.rebuild(clean, sources=sources, cold_sources=cold_sources, textfile=None)
        assert clean.execute("SELECT rel_path FROM ah.source_file ORDER BY rel_path").fetchall() == before
    finally:
        archive.chmod(0o700)


def test_without_cold_directories_retains_hot_only_behavior(clean, tmp_path):  # noqa: F811
    sources, cold_sources, rel = layout(tmp_path)
    load.rebuild(clean, sources=cold_sources, textfile=None, log=lambda *_: None)
    config = parse_config({"sources": {n: str(p) for n, p in sources.items()}})
    assert config.cold_sources == {}
    stats = load.rebuild(clean, sources=config.sources, textfile=None, log=lambda *_: None)
    assert stats.errors == 0
    assert clean.execute("SELECT count(*) FROM ah.source_file WHERE rel_path = %s", (rel,)).fetchone() == (0,)


def test_read_only_check_cli(clean, tmp_path, monkeypatch, capsys):  # noqa: F811
    sources, cold_sources, _ = layout(tmp_path)
    config = tmp_path / "config.toml"
    config.write_text(
        '[sources]\nclaude-local = "'
        + str(sources["claude-local"])
        + '"\n[cold_sources]\nclaude-local = "'
        + str(cold_sources["claude-local"])
        + '"\n'
    )
    monkeypatch.setattr(load, "try_lock", lambda _: pytest.fail("check must not lock"))
    assert cli.main(["--dsn", DSN, "--config", str(config), "rebuild-check"]) == 0
    output = capsys.readouterr().out
    assert '"cold_only": 1' in output and '"refused": false' in output
    assert str(tmp_path) not in output


def test_rebuild_rechecks_catalogue_after_writer_lock(clean, tmp_path, monkeypatch):  # noqa: F811
    sources, cold_sources, rel = layout(tmp_path)
    load.rebuild(clean, sources=cold_sources, textfile=None, log=lambda *_: None)
    before_count = clean.execute("SELECT count(*) FROM ah.source_file").fetchone()[0]
    clean.commit()
    original_lock = load.try_lock
    added_rel = "claude-local/projects/synthetic/concurrent.jsonl"

    def concurrent_writer_then_lock(connection):
        # A completed writer changes the catalogue after preflight, before this
        # rebuild obtains its lock. The newly protected path is absent in both tiers.
        connection.execute("UPDATE ah.source_file SET rel_path = %s WHERE rel_path = %s", (added_rel, rel))
        connection.commit()
        return original_lock(connection)

    monkeypatch.setattr(load, "try_lock", concurrent_writer_then_lock)
    with pytest.raises(SystemExit, match=r"rebuild refused: claude-local count=1"):
        load.rebuild(clean, sources=sources, cold_sources=cold_sources, textfile=None)
    assert clean.execute("SELECT count(*) FROM ah.source_file").fetchone()[0] == before_count
    assert clean.execute("SELECT count(*) FROM ah.source_file WHERE rel_path = %s", (added_rel,)).fetchone()[0] == 1
    clean.rollback()
    # The refusal also releases the writer lock.
    with load.connect(DSN) as other:
        assert original_lock(other)
        load.unlock(other)


def test_cold_source_requires_corresponding_hot_namespace():
    with pytest.raises(ConfigError, match="cold_sources"):
        parse_config({"sources": {}, "cold_sources": {"claude-local": "/archive/claude-local"}})

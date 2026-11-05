"""Real-loader proof that partial tails retain content and fresh archive metadata."""

import json
import shutil

import pytest
from test_loader_pg import clean, conn  # noqa: F401 - guarded disposable database fixtures
from test_loader_pg import pytestmark as pytestmark

from agent_history import load


def source_tree(tmp_path, tail):
    hot, cold = tmp_path / "hot", tmp_path / "cold"
    rel = "claude-local/projects/synthetic/11111111-1111-4111-8111-111111111111.jsonl"
    p = hot / rel
    p.parent.mkdir(parents=True)
    record = {
        "type": "user",
        "sessionId": "11111111-1111-4111-8111-111111111111",
        "uuid": "synthetic-message",
        "timestamp": "2026-01-01T00:00:00Z",
        "message": {"role": "user", "content": "Synthetic retained message"},
    }
    prefix = (json.dumps(record) + "\n").encode()
    p.write_bytes(prefix + tail)
    return hot, cold, rel, prefix


def refresh(connection, hot, cold):
    entries = load.inventory(hot, cold)
    return load.refresh(connection, hot, cold, source_entries=entries, log=lambda *_: None)


@pytest.mark.parametrize("tail", [b"\0" * 73, b'{"type":', b""])
def test_archive_availability_updates_without_new_lines(clean, tmp_path, tail):  # noqa: F811
    hot, cold, rel, prefix = source_tree(tmp_path, tail)
    refresh(clean, hot, cold)
    before = clean.execute(
        "SELECT indexed_offset,line_count,indexed_at,parser_state FROM ah.source_file WHERE rel_path=%s", (rel,)
    ).fetchone()
    messages = clean.execute("SELECT id,text FROM ah.message").fetchall()
    clean.rollback()
    (cold / rel).parent.mkdir(parents=True)
    shutil.copy2(hot / rel, cold / rel)
    refresh(clean, hot, cold)
    assert clean.execute(
        "SELECT tier,available_hot,available_cold FROM ah.source_file WHERE rel_path=%s", (rel,)
    ).fetchone() == ("hot", True, True)
    clean.rollback()
    # The file is our synthetic fixture; relocate it to simulate hot retention without deleting bytes.
    (hot / rel).rename(tmp_path / "retained.jsonl")
    refresh(clean, hot, cold)
    assert clean.execute(
        "SELECT tier,available_hot,available_cold FROM ah.source_file WHERE rel_path=%s", (rel,)
    ).fetchone() == ("cold", False, True)
    assert (
        clean.execute(
            "SELECT indexed_offset,line_count,indexed_at,parser_state FROM ah.source_file WHERE rel_path=%s", (rel,)
        ).fetchone()
        == before
    )
    assert clean.execute("SELECT id,text FROM ah.message").fetchall() == messages
    assert (cold / rel).read_bytes() == prefix + tail


def test_nul_classification_clears_when_tail_changes(clean, tmp_path):  # noqa: F811
    hot, cold, rel, prefix = source_tree(tmp_path, b"\0" * 73)
    refresh(clean, hot, cold)
    assert clean.execute(
        "SELECT indexed_offset,nul_tail_bytes,status FROM ah.source_file WHERE rel_path=%s", (rel,)
    ).fetchone() == (len(prefix), 73, "partial")
    assert clean.execute("SELECT kind,detail FROM ah.parse_issue WHERE kind='nul_tail'").fetchall() == [
        ("nul_tail", "bytes=73")
    ]
    clean.rollback()
    # Keep the original damaged fixture alongside the replacement to prove source preservation.
    shutil.copy2(hot / rel, tmp_path / "damaged.jsonl")
    # Same-size mixed/incomplete bytes are no longer a NUL-only observation.
    (hot / rel).write_bytes(prefix + b'{"type":' + b" " * 65)
    refresh(clean, hot, cold)
    assert clean.execute(
        "SELECT indexed_offset,nul_tail_bytes,status FROM ah.source_file WHERE rel_path=%s", (rel,)
    ).fetchone() == (len(prefix), 0, "partial")
    clean.rollback()
    complete = prefix + b'{"type":"summary","summary":"Synthetic recovery","leafUuid":"synthetic-message"}\n'
    (hot / rel).write_bytes(complete)
    refresh(clean, hot, cold)
    assert clean.execute(
        "SELECT indexed_offset,nul_tail_bytes,status FROM ah.source_file WHERE rel_path=%s", (rel,)
    ).fetchone() == (len(complete), 0, "indexed")
    assert (tmp_path / "damaged.jsonl").read_bytes() == prefix + b"\0" * 73


def test_raw_invalid_and_actionable_lag_are_distinct(clean, tmp_path):  # noqa: F811
    import os
    from agent_history.metrics.catalogue import CatalogueCollector

    hot, cold, rel, prefix = source_tree(tmp_path, b"\0" * 73)
    refresh(clean, hot, cold)
    partial = hot / "claude-local/projects/synthetic/22222222-2222-4222-8222-222222222222.jsonl"
    partial.write_bytes(b'{"type":')
    refresh(clean, hot, cold)
    collector = CatalogueCollector(os.environ["AGENT_HISTORY_TEST_DSN"])
    values = {
        f.name: f.samples[0].value
        for f in collector.collect()
        if f.name in {"agent_history_lag_bytes", "agent_history_nul_tail_bytes", "agent_history_actionable_lag_bytes"}
    }
    assert values == {
        "agent_history_lag_bytes": 81,
        "agent_history_nul_tail_bytes": 73,
        "agent_history_actionable_lag_bytes": 8,
    }
    assert partial.read_bytes() == b'{"type":'
    assert (hot / rel).read_bytes() == prefix + b"\0" * 73


def test_committed_batch_clears_old_nul_classification_before_tail_inspection(clean, tmp_path, monkeypatch):  # noqa: F811
    hot, cold, rel, prefix = source_tree(tmp_path, b"\0" * 73)
    refresh(clean, hot, cold)
    assert clean.execute("SELECT nul_tail_bytes FROM ah.source_file WHERE rel_path=%s", (rel,)).fetchone() == (73,)
    clean.rollback()
    record = b'{"type":"summary","summary":"Synthetic recovery","leafUuid":"synthetic-message"}\n'
    incomplete = b'{"type":'
    replacement = prefix + record + incomplete
    (hot / rel).write_bytes(replacement)
    read_batches = load.read_batches

    def interrupted_batches(*args):
        for batch in read_batches(*args):
            yield batch
            # Simulate process interruption after the first real batch commit, before the final
            # tail observation. No rollback can remove a previously committed source update.
            raise KeyboardInterrupt("synthetic interruption")

    monkeypatch.setattr(load, "read_batches", interrupted_batches)
    with pytest.raises(KeyboardInterrupt, match="synthetic interruption"):
        refresh(clean, hot, cold)
    clean.rollback()
    assert clean.execute(
        "SELECT indexed_offset,size_bytes-indexed_offset,nul_tail_bytes,status FROM ah.source_file WHERE rel_path=%s",
        (rel,),
    ).fetchone() == (len(prefix + record), len(incomplete), 0, "partial")
    assert (hot / rel).read_bytes() == replacement

"""Embedder + hybrid search against the disposable test database, with a deterministic fake provider.

Skipped unless AGENT_HISTORY_TEST_DSN names a *_test database (see test_loader_pg.py).
"""

from __future__ import annotations

import hashlib
import os
import re

import pytest

DSN = os.environ.get("AGENT_HISTORY_TEST_DSN", "")
pytestmark = pytest.mark.skipif(not DSN or "agent_history_test" not in DSN,
                                reason="AGENT_HISTORY_TEST_DSN (a *_test database) not set")

psycopg = pytest.importorskip("psycopg")

from agent_history import embed, load  # noqa: E402

from test_loader_pg import build_tree, refresh  # noqa: E402

MODEL = "fake-bow-1024"
TEXTS = {
    "alloy": "Alloy silently dropped the host labels after the config reload on buildhost, fixed with a relabel rule",
    "traefik": "Traefik answered 404 for about twelve seconds after the container reported healthy",
    "photos": "osxphotos treated the string false as true so the backup exported everything once",
}


class FakeProvider(embed.Provider):
    """Bag-of-words hashing into 1024 dims: shared words -> nearby vectors. Counts calls."""

    def __init__(self):
        super().__init__("fake", MODEL, "t", "a", 16)
        self.inputs = 0

    def embed(self, texts, kind="document"):
        self.inputs += len(texts)
        out = []
        for text in texts:
            vec = [0.0] * embed.DIMS
            for word in re.findall(r"[a-z0-9]+", text.lower()):
                vec[int(hashlib.md5(word.encode()).hexdigest(), 16) % embed.DIMS] += 1.0
            out.append(embed.normalise(vec))
        self.usage["tokens"] += sum(embed.estimate_tokens(t) for t in texts)
        return out


@pytest.fixture
def db(tmp_path):
    conn = load.connect(DSN)
    load.apply_schema(conn, force=True)
    conn.execute("TRUNCATE " + ", ".join(f"ah.{t}" for t in load.DATA_TABLES) + " RESTART IDENTITY CASCADE")
    conn.execute("TRUNCATE ah.embedding, ah.embed_failure")
    conn.execute("DELETE FROM ah.meta WHERE key LIKE 'embed%%' OR key = 'rebuild_in_progress'")
    conn.commit()
    hot, cold = build_tree(tmp_path)
    assert refresh(conn, hot, cold).errors == 0
    load.create_post_load_indexes(conn)
    ids = [r[0] for r in conn.execute(
        "SELECT id FROM ah.message WHERE message_class = 'human_prompt' ORDER BY id LIMIT 3")]
    assert len(ids) == 3
    for mid, text in zip(ids, TEXTS.values()):
        conn.execute("UPDATE ah.message SET text = %s, content_sha256 = %s WHERE id = %s",
                     (text, hashlib.sha256(text.encode()).hexdigest(), mid))
    conn.commit()
    yield conn, dict(zip(TEXTS, ids))
    conn.close()


def run(conn, provider):
    return embed.run(conn, provider, cap_tokens=0, daily_cap=0, log=lambda *_: None)


def qvec(provider, text):
    return embed.halfvec_literal(provider.embed([text], kind="query")[0])


def test_embed_is_incremental_and_rebuild_costs_nothing(db):
    conn, ids = db
    provider = FakeProvider()
    first = run(conn, provider)
    assert first.items > 0 and first.api_inputs > 0 and first.failed_inputs == 0
    chunks = conn.execute("SELECT count(*) FROM ah.chunk WHERE model = %s", (MODEL,)).fetchone()[0]
    assert chunks >= 3

    provider.inputs = 0
    assert run(conn, provider).items == 0 and provider.inputs == 0

    # rebuild truncates chunk (derived); the cache answers every input
    conn.execute("TRUNCATE ah.chunk")
    conn.commit()
    again = run(conn, provider)
    assert provider.inputs == 0 and again.api_inputs == 0 and again.cached > 0
    assert conn.execute("SELECT count(*) FROM ah.chunk").fetchone()[0] == chunks

    # a changed message is re-embedded alone
    text = TEXTS["traefik"] + " and it recurred"
    conn.execute("UPDATE ah.message SET text = %s, content_sha256 = %s WHERE id = %s",
                 (text, hashlib.sha256(text.encode()).hexdigest(), ids["traefik"]))
    conn.commit()
    changed = run(conn, provider)
    assert changed.items == 1 and provider.inputs == 1


def test_chunks_hold_offsets_not_text(db):
    conn, ids = db
    run(conn, FakeProvider())
    cols = {r[0] for r in conn.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_schema = 'ah' AND table_name = 'chunk'")}
    assert not cols & {"text", "input", "content"}
    start, end = conn.execute("SELECT char_start, char_end FROM ah.chunk WHERE message_id = %s",
                              (ids["alloy"],)).fetchone()
    assert (start, end) == (0, len(TEXTS["alloy"]))


def test_hybrid_search_finds_paraphrase_by_vector(db):
    conn, ids = db
    provider = FakeProvider()
    run(conn, provider)
    ns = conn.execute("SELECT namespace FROM ah.message WHERE id = %s", (ids["alloy"],)).fetchone()[0]
    q = "why were host labels dropped after reload"
    rows = conn.execute("SELECT message_id, bm25_rank, vec_rank FROM ah.hybrid_search(%s, %s::halfvec, %s)",
                        (q, qvec(provider, q), [ns])).fetchall()
    assert rows and rows[0][0] == ids["alloy"] and rows[0][2] == 1

    bm25_only = conn.execute("SELECT message_id, vec_rank FROM ah.hybrid_search(%s, NULL, %s)",
                             ("traefik", [ns])).fetchall()
    assert bm25_only and bm25_only[0][0] == ids["traefik"] and bm25_only[0][1] is None


def test_embed_skips_while_refresh_holds_lock(db):
    conn, _ids = db
    other = load.connect(DSN)
    try:
        assert load.try_lock(other)
        stats = run(conn, FakeProvider())
        assert stats.skipped == "refresh_running" and stats.items == 0
    finally:
        load.unlock(other)
        other.close()


def test_input_version_change_rechunks_and_only_changed_inputs_cost(db, monkeypatch):
    conn, _ids = db
    provider = FakeProvider()
    run(conn, provider)
    chunks = conn.execute("SELECT count(*) FROM ah.chunk").fetchone()[0]
    provider.inputs = 0
    monkeypatch.setattr(embed, "INPUT_VERSION", "test-next")
    again = run(conn, provider)
    assert provider.inputs == 0 and again.items > 0
    assert conn.execute("SELECT count(*) FROM ah.chunk").fetchone()[0] == chunks
    assert conn.execute("SELECT value FROM ah.meta WHERE key = 'embed_input_version'").fetchone()[0] == "test-next"


# --- embed-gc ------------------------------------------------------

VEC = embed.halfvec_literal(embed.normalise([1.0] * embed.DIMS))


def _orphan(conn, name, age_days):
    conn.execute("INSERT INTO ah.embedding (model, input_sha256, embedding, created_at) "
                 "VALUES (%s, %s, %s::halfvec, now() - make_interval(days => %s))", (MODEL, name, VEC, age_days))


def _meta(conn, key, value_sql):
    conn.execute(f"INSERT INTO ah.meta VALUES (%s, ({value_sql})::text) "
                 "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", (key,))


def _present(conn, name):
    return conn.execute("SELECT 1 FROM ah.embedding WHERE model = %s AND input_sha256 = %s",
                        (MODEL, name)).fetchone() is not None


@pytest.fixture
def gc_db(db):
    """Embedded catalogue, drained backlog, last chunk reset 10 days ago, no GC yet."""
    conn, ids = db
    run(conn, FakeProvider())
    _meta(conn, "chunks_reset_at", "now() - interval '10 days'")
    conn.execute("DELETE FROM ah.meta WHERE key = 'embed_gc_at'")
    # one referenced vector old enough to be a candidate by age alone
    conn.execute("UPDATE ah.embedding SET created_at = now() - interval '40 days' WHERE input_sha256 = "
                 "(SELECT input_sha256 FROM ah.chunk WHERE model = %s AND input_sha256 <> '' LIMIT 1)", (MODEL,))
    _orphan(conn, "old-orphan", 40)
    _orphan(conn, "young-orphan", 20)
    conn.commit()
    yield conn, ids


def test_gc_deletes_only_old_unreferenced_vectors(gc_db):
    conn, _ids = gc_db
    before = conn.execute("SELECT count(*) FROM ah.embedding").fetchone()[0]
    dry = embed.gc(conn, dry_run=True, log=lambda *_: None)
    assert (dry.skipped, dry.eligible, dry.deleted) == ("", 1, 0)
    assert conn.execute("SELECT count(*) FROM ah.embedding").fetchone()[0] == before
    assert conn.execute("SELECT 1 FROM ah.meta WHERE key = 'embed_gc_at'").fetchone() is None

    real = embed.gc(conn, log=lambda *_: None)
    assert (real.skipped, real.eligible, real.deleted) == ("", 1, 1)
    assert not _present(conn, "old-orphan") and _present(conn, "young-orphan")
    assert conn.execute("SELECT count(*) FROM ah.embedding").fetchone()[0] == before - 1
    assert conn.execute("SELECT count(*) FROM ah.embedding e WHERE NOT EXISTS (SELECT 1 FROM ah.chunk c "
                        "WHERE c.model = e.model AND c.input_sha256 = e.input_sha256)").fetchone()[0] == 1
    assert conn.execute("SELECT 1 FROM ah.meta WHERE key = 'embed_gc_at'").fetchone()


def test_gc_caps_rows_per_run(gc_db):
    conn, _ids = gc_db
    for i in range(4):
        _orphan(conn, f"old-{i}", 35)
    conn.commit()
    first = embed.gc(conn, max_rows=2, log=lambda *_: None)
    assert (first.eligible, first.deleted) == (5, 2)
    assert embed.gc(conn, max_rows=2, log=lambda *_: None).deleted == 2
    assert embed.gc(conn, max_rows=2, log=lambda *_: None).deleted == 1


@pytest.mark.parametrize("setup, reason", [
    ("DELETE FROM ah.meta WHERE key = 'chunks_reset_at'", "no_reset_marker"),
    ("UPDATE ah.meta SET value = 'not a time' WHERE key = 'chunks_reset_at'", "bad_reset_marker"),
    ("UPDATE ah.meta SET value = (now() - interval '6 days')::text WHERE key = 'chunks_reset_at'",
     "recent_chunk_reset"),
    # any age: a rebuild killed a day ago still blocks GC, although the embedder ignores it after 3 h
    ("INSERT INTO ah.meta VALUES ('rebuild_in_progress', (now() - interval '1 day')::text)", "rebuild_in_progress"),
    ("DELETE FROM ah.chunk WHERE ctid IN (SELECT ctid FROM ah.chunk WHERE message_id IS NOT NULL LIMIT 1)",
     "backlog_pending"),
])
def test_gc_guards_skip_without_deleting(gc_db, setup, reason):
    conn, _ids = gc_db
    conn.execute(setup)
    conn.commit()
    stats = embed.gc(conn, log=lambda *_: None)
    assert stats.skipped == reason and stats.deleted == 0
    assert _present(conn, "old-orphan")


def test_gc_skips_while_embed_or_refresh_lock_held(gc_db):
    conn, _ids = gc_db
    other = load.connect(DSN)
    try:
        other.execute("SELECT pg_advisory_lock(%s)", (embed.EMBED_LOCK,))
        other.commit()
        assert embed.gc(conn, log=lambda *_: None).skipped == "embed_running"
        other.execute("SELECT pg_advisory_unlock(%s)", (embed.EMBED_LOCK,))
        assert load.try_lock(other)
        assert embed.gc(conn, log=lambda *_: None).skipped == "refresh_running"
    finally:
        load.unlock(other)
        other.close()
    assert _present(conn, "old-orphan")


def test_embed_run_gcs_at_most_once_per_interval(gc_db):
    conn, _ids = gc_db
    first = embed.run(conn, FakeProvider(), cap_tokens=0, daily_cap=0, log=lambda *_: None, gc_interval_hours=24)
    assert first.gc and first.gc["deleted"] == 1 and not _present(conn, "old-orphan")
    _orphan(conn, "later-orphan", 40)
    conn.commit()
    second = embed.run(conn, FakeProvider(), cap_tokens=0, daily_cap=0, log=lambda *_: None, gc_interval_hours=24)
    assert second.gc is None and _present(conn, "later-orphan")
    # the embed lock is released afterwards
    assert conn.execute("SELECT pg_try_advisory_lock(%s)", (embed.EMBED_LOCK,)).fetchone()[0]
    conn.execute("SELECT pg_advisory_unlock(%s)", (embed.EMBED_LOCK,))
    conn.commit()


def _reset_age(conn):
    return conn.execute("SELECT now() - value::timestamptz FROM ah.meta WHERE key = 'chunks_reset_at'").fetchone()[0]


def test_input_version_rechunk_marks_chunk_reset(gc_db, monkeypatch):
    conn, _ids = gc_db
    monkeypatch.setattr(embed, "INPUT_VERSION", "test-gc-next")
    run(conn, FakeProvider())
    assert _reset_age(conn).total_seconds() < 60


def test_rebuild_marks_chunk_reset_at_start_and_end(gc_db, monkeypatch):
    conn, _ids = gc_db
    seen = {}

    def fake_refresh(c, *a, **k):
        seen["reset_age"] = _reset_age(c)
        seen["flag"] = c.execute("SELECT 1 FROM ah.meta WHERE key = 'rebuild_in_progress'").fetchone()
        c.execute("UPDATE ah.meta SET value = (now() - interval '1 hour')::text WHERE key = 'chunks_reset_at'")
        c.commit()
        return load.RunStats()

    monkeypatch.setattr(load, "cold_tier_ok", lambda cold: True)
    monkeypatch.setattr(load, "refresh", fake_refresh)
    load.rebuild(conn, None, None, None, log=lambda *_: None)
    assert seen["flag"] and seen["reset_age"].total_seconds() < 60
    assert _reset_age(conn).total_seconds() < 60          # rewritten when the rebuild finished
    assert conn.execute("SELECT 1 FROM ah.meta WHERE key = 'rebuild_in_progress'").fetchone() is None


def test_embed_run_survives_a_failing_gc_and_retries_next_day(gc_db, monkeypatch):
    conn, _ids = gc_db
    monkeypatch.setattr(embed, "GC_COUNT_SQL", "SELECT count(*) FROM ah.no_such_table")
    stats = embed.run(conn, FakeProvider(), cap_tokens=0, daily_cap=0, log=lambda *_: None, gc_interval_hours=24)
    assert stats.gc and stats.gc["skipped"] == "error" and _present(conn, "old-orphan")
    assert conn.execute("SELECT 1 FROM ah.meta WHERE key = 'embed_gc_at'").fetchone()

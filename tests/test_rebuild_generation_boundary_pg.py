"""Consumer-boundary proof across rebuild generations, on disposable real PostgreSQL.

A consumer reads the public, unchanged reader surface only: the ``ah.v_session_state``
projection (natural key plus ``content_fingerprint``) and paged ``ah.session_events`` with an
``after_seq`` cursor, as the reader role. Nothing here adds a generation argument or column.

Generations covered: the old complete one, the marked one (destructive preparation committed,
sources partly loaded, rollups absent), a failed one (marker retained), and the completed one.
A paged transcript read is admitted in the old generation, crosses the marked window and resumes
from its cursor after completion. Sources are unchanged across the rebuild, so a crossing read
must equal a single-generation read; seq is deterministic from source position (CONTRACT.md).
"""

import os

import psycopg
import pytest

from agent_history import load

from test_loader_pg import DSN, build_tree, clean, conn  # noqa: F401

READER_DSN = os.environ.get("AGENT_HISTORY_TEST_READER_DSN", "")
pytestmark = pytest.mark.skipif(
    not DSN or "agent_history_test" not in DSN or not READER_DSN,
    reason="AGENT_HISTORY_TEST_DSN (a *_test database) and AGENT_HISTORY_TEST_READER_DSN not set",
)

PAGE = 3
FENCED = psycopg.errors.ObjectNotInPrerequisiteState


def quiet(*_args):
    return None


class Consumer:
    """A reader-role consumer limited to the public projection and Events surfaces."""

    def __init__(self):
        self.db = psycopg.connect(READER_DSN, autocommit=True)

    def close(self):
        self.db.close()

    def sync(self) -> dict[tuple[str, str, str], str | None]:
        rows = self.db.execute(
            "SELECT agent, session_uid, agent_id, content_fingerprint FROM ah.v_session_state"
        ).fetchall()
        return {(a, s, i): fp for a, s, i, fp in rows}

    def page(self, key, after):
        return self.db.execute(
            "SELECT seq, kind, uid, message_class, text, input_text, output_text "
            "FROM ah.session_events(%s,%s,%s,%s,%s)",
            (*key, after, PAGE),
        ).fetchall()

    def read_all(self, key):
        events, after = [], 0
        while True:
            rows = self.page(key, after)
            if not rows:
                return events
            events.extend(rows)
            after = rows[-1][0]


@pytest.fixture
def consumer():
    reader = Consumer()
    yield reader
    reader.close()


@pytest.fixture
def catalogue(clean, tmp_path):  # noqa: F811
    clean.execute("DELETE FROM ah.meta WHERE key='rebuild_in_progress'")
    clean.commit()
    hot, cold = build_tree(tmp_path)
    assert load.refresh(clean, hot, cold, textfile=None, log=quiet).errors == 0
    yield clean, hot, cold
    clean.rollback()
    clean.execute("DELETE FROM ah.meta WHERE key='rebuild_in_progress'")
    clean.commit()


def assert_old_generation_complete(consumer):
    state = consumer.sync()
    assert state, "the old generation must be visible"
    assert all(fp for fp in state.values()), "a complete generation never projects a blank fingerprint"
    # The longest transcript, so a paged read genuinely spans several pages.
    transcripts = {key: consumer.read_all(key) for key in state}
    key = max(transcripts, key=lambda k: len(transcripts[k]))
    assert len(transcripts[key]) > 2 * PAGE
    return state, transcripts, key


def assert_fenced(consumer, key, after):
    """A marked generation exposes no parent projection and no transcript page, empty or not."""
    assert consumer.sync() == {}
    for selected, cursor in ((key, 0), (key, after), (("pi", "absent", ""), 0)):
        with pytest.raises(FENCED, match="catalogue rebuild in progress"):
            consumer.page(selected, cursor)


def test_crossing_read_is_fenced_then_resumes_on_identical_completed_generation(catalogue, consumer, monkeypatch):
    db, hot, cold = catalogue
    old_state, old_transcripts, key = assert_old_generation_complete(consumer)

    # Admit a paged read in the old generation and buffer its first page.
    first = consumer.page(key, 0)
    assert len(first) == PAGE
    cursor = first[-1][0]

    observed = []
    original_refresh, original_post = load.refresh, load.post_passes

    def refresh(connection, *args, **kwargs):
        # Destructive preparation has committed; nothing has been loaded yet.
        assert_fenced(consumer, key, cursor)
        observed.append("emptied")
        return original_refresh(connection, *args, **kwargs)

    def post_passes(connection, *args, **kwargs):
        # Every source batch has committed, but rollups and seq are not yet rebuilt.
        partial = connection.execute(
            "SELECT count(*) FROM ah.session s LEFT JOIN ah.session_rollup r ON r.session_id = s.id "
            "WHERE NOT s.is_stub AND r.content_fingerprint IS NULL"
        ).fetchone()[0]
        connection.commit()
        assert partial > 0, "the window must really hold sessions without rollups"
        assert_fenced(consumer, key, cursor)
        observed.append("loaded")
        result = original_post(connection, *args, **kwargs)
        connection.commit()
        assert_fenced(consumer, key, cursor)
        observed.append("post-passed")
        return result

    monkeypatch.setattr(load, "refresh", refresh)
    monkeypatch.setattr(load, "post_passes", post_passes)
    assert load.rebuild(db, hot, cold, textfile=None, log=quiet).errors == 0
    assert observed == ["emptied", "loaded", "post-passed"]

    # Completed generation: the same projection, so no approved fingerprint is demoted.
    assert consumer.sync() == old_state
    # The admitted read resumes from its old cursor and matches a single-generation read.
    resumed = list(first)
    after = cursor
    while rows := consumer.page(key, after):
        resumed.extend(rows)
        after = rows[-1][0]
    assert resumed == old_transcripts[key]
    assert {k: consumer.read_all(k) for k in old_state} == old_transcripts


@pytest.mark.parametrize("fault", ["postpass", "lock_held"])
def test_failed_generation_stays_fenced_until_a_complete_rebuild(catalogue, consumer, monkeypatch, fault):
    db, hot, cold = catalogue
    old_state, old_transcripts, key = assert_old_generation_complete(consumer)
    first = consumer.page(key, 0)
    cursor = first[-1][0]
    original_refresh, original_post = load.refresh, load.post_passes

    def failing_refresh(connection, *args, **kwargs):
        if fault == "lock_held":
            connection.commit()
            return load.RunStats(lock_held=True)
        return original_refresh(connection, *args, **kwargs)

    def failing_post(connection, *args, **kwargs):
        connection.rollback()
        raise RuntimeError("synthetic postpass failure")

    monkeypatch.setattr(load, "refresh", failing_refresh)
    if fault == "postpass":
        monkeypatch.setattr(load, "post_passes", failing_post)
        with pytest.raises(RuntimeError, match="synthetic"):
            load.rebuild(db, hot, cold, textfile=None, log=quiet)
    else:
        with pytest.raises(SystemExit, match="reader fence retained"):
            load.rebuild(db, hot, cold, textfile=None, log=quiet)
    db.rollback()

    # The failed generation stays invisible, partial or empty, with no age expiry.
    assert_fenced(consumer, key, cursor)
    db.execute("UPDATE ah.meta SET value='2000-01-01' WHERE key='rebuild_in_progress'")
    db.commit()
    assert_fenced(consumer, key, cursor)
    # Neither incremental indexing nor its post-pass reopens a failed generation.
    monkeypatch.setattr(load, "refresh", original_refresh)
    monkeypatch.setattr(load, "post_passes", original_post)
    assert load.refresh(db, hot, cold, textfile=None, log=quiet).errors == 0
    assert_fenced(consumer, key, cursor)

    assert load.rebuild(db, hot, cold, textfile=None, log=quiet).errors == 0
    assert consumer.sync() == old_state
    resumed = list(first)
    after = cursor
    while rows := consumer.page(key, after):
        resumed.extend(rows)
        after = rows[-1][0]
    assert resumed == old_transcripts[key]

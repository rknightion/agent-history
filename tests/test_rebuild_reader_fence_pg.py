"""Reader projections and rebuild completion on disposable real PostgreSQL."""

import os

import psycopg
import pytest

from agent_history import load

from test_loader_pg import DSN, build_tree, clean, conn  # noqa: F401

pytestmark = pytest.mark.skipif(
    not DSN or "agent_history_test" not in DSN,
    reason="AGENT_HISTORY_TEST_DSN (a *_test database) not set",
)


@pytest.fixture
def catalogue(clean, tmp_path):  # noqa: F811
    clean.execute("DELETE FROM ah.meta WHERE key='rebuild_in_progress'")
    clean.commit()
    hot, cold = build_tree(tmp_path)
    assert load.refresh(clean, hot, cold, textfile=None, log=lambda *_: None).errors == 0
    yield clean, hot, cold
    clean.rollback()
    clean.execute("DELETE FROM ah.meta WHERE key='rebuild_in_progress'")
    clean.commit()


def marked(connection):
    return connection.execute("SELECT EXISTS(SELECT 1 FROM ah.meta WHERE key='rebuild_in_progress')").fetchone()[0]


def mark(connection):
    connection.execute("INSERT INTO ah.meta VALUES ('rebuild_in_progress','2000-01-01')")
    connection.commit()


def test_marked_projections_and_transcript_pages_fail_closed(catalogue):
    db, _, _ = catalogue
    key = db.execute("SELECT agent,session_uid,agent_id FROM ah.v_session_state LIMIT 1").fetchone()
    assert key
    mark(db)
    # Missing rollups are the destructive-reload window that previously exposed blank fingerprints.
    db.execute("DELETE FROM ah.session_rollup")
    db.commit()
    with psycopg.connect(os.environ["AGENT_HISTORY_TEST_READER_DSN"], autocommit=True) as reader:
        assert reader.execute("SELECT count(*) FROM ah.v_session_state").fetchone() == (0,)
        for selected, after in ((key, 0), (key, 1000000), (("pi", "absent", ""), 0)):
            with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState, match="catalogue rebuild in progress"):
                reader.execute("SELECT * FROM ah.session_events(%s,%s,%s,%s,200)", (*selected, after)).fetchall()
        assert marked(reader)


@pytest.mark.parametrize("fault", ["exception", "errors", "lock_held", "postpass", "relink", "indexes"])
def test_failed_rebuild_retains_committed_marker(catalogue, monkeypatch, fault):
    db, hot, cold = catalogue
    refresh = load.refresh
    indexes = load.create_post_load_indexes

    def fail_refresh(connection, *args, **kwargs):
        assert marked(connection)
        connection.commit()
        if fault == "exception":
            raise RuntimeError("synthetic source failure")
        if fault in {"errors", "lock_held"}:
            return load.RunStats(errors=int(fault == "errors"), lock_held=fault == "lock_held")
        return refresh(connection, *args, **kwargs)

    def fail_post(*_args):
        raise RuntimeError("synthetic postpass failure")

    def fail_indexes(connection):
        assert marked(connection)
        connection.commit()
        raise RuntimeError("synthetic index failure")

    if fault == "relink":
        # A real SQL error occurs only in the post-load relink, after destructive preparation.
        db.execute(
            "CREATE FUNCTION ah.reject_relink() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN "
            "IF OLD.session_id IS NULL AND NEW.session_id IS NOT NULL THEN RAISE EXCEPTION 'synthetic relink failure'; END IF; "
            "RETURN NEW; END $$"
        )
        key = db.execute("SELECT id,agent,session_uid,agent_id FROM ah.session WHERE NOT is_stub LIMIT 1").fetchone()
        db.execute(
            "INSERT INTO ah.session_summary(session_id,agent,session_uid,agent_id,journal_conversation_id,journal_revision_id,namespace,"
            "analysed_at) VALUES(%s,%s,%s,%s,'synthetic','r1','claude-local',now())",
            key,
        )
        db.execute(
            "CREATE TRIGGER reject_relink BEFORE UPDATE ON ah.session_summary FOR EACH ROW "
            "EXECUTE FUNCTION ah.reject_relink()"
        )
        db.commit()
    monkeypatch.setattr(load, "refresh", fail_refresh)
    if fault == "postpass":
        monkeypatch.setattr(load, "post_passes", fail_post)
    if fault == "indexes":
        monkeypatch.setattr(load, "create_post_load_indexes", fail_indexes)
    try:
        if fault == "errors":
            assert load.rebuild(db, hot, cold, textfile=None, log=lambda *_: None).errors == 1
        elif fault == "lock_held":
            with pytest.raises(SystemExit, match="lock"):
                load.rebuild(db, hot, cold, textfile=None, log=lambda *_: None)
        else:
            with pytest.raises((RuntimeError, psycopg.Error), match="synthetic"):
                load.rebuild(db, hot, cold, textfile=None, log=lambda *_: None)
        db.rollback()
        assert marked(db)
        db.commit()
        # Failed attempts release the outer writer lock, without reopening the generation.
        with load.connect(DSN) as other:
            assert load.try_lock(other)
            load.unlock(other)
    finally:
        db.rollback()
        if fault == "relink":
            db.execute("DROP TRIGGER reject_relink ON ah.session_summary; DROP FUNCTION ah.reject_relink()")
            db.execute("DELETE FROM ah.session_summary WHERE journal_conversation_id='synthetic'")
        db.commit()
        monkeypatch.setattr(load, "refresh", refresh)
        monkeypatch.setattr(load, "create_post_load_indexes", indexes)


def test_success_reopens_only_after_all_work_and_holds_writer_lock(catalogue, monkeypatch):
    db, hot, cold = catalogue
    original_refresh = load.refresh
    original_indexes = load.create_post_load_indexes
    observed = []

    def check_locked(stage):
        with load.connect(DSN) as other:
            assert marked(other)
            other.commit()
            assert not load.try_lock(other)
            observed.append(stage)

    def refresh(connection, *args, **kwargs):
        check_locked("load")
        stats = original_refresh(connection, *args, **kwargs)
        check_locked("after-postpass")
        return stats

    def indexes(connection):
        check_locked("indexes")
        original_indexes(connection)
        check_locked("after-indexes")

    monkeypatch.setattr(load, "refresh", refresh)
    monkeypatch.setattr(load, "create_post_load_indexes", indexes)
    assert load.rebuild(db, hot, cold, textfile=None, log=lambda *_: None).errors == 0
    assert observed == ["load", "after-postpass", "indexes", "after-indexes"]
    assert not marked(db)
    assert db.execute("SELECT count(*) FROM ah.v_session_state WHERE content_fingerprint IS NULL").fetchone() == (0,)
    assert db.execute("SELECT count(*) FROM ah.v_session_state").fetchone()[0] > 0
    db.commit()
    with load.connect(DSN) as other:
        assert load.try_lock(other)
        load.unlock(other)


def test_rebuild_installs_reader_guard_before_destructive_commit(catalogue, monkeypatch):
    db, hot, cold = catalogue
    # Model the legacy same-ABI function which accepts empty pages while marked.
    # Catalogue migrations/analytics have not yet been applied by a periodic indexer.
    db.execute(
        "CREATE OR REPLACE FUNCTION ah.session_events(p_agent text,p_session_uid text,p_agent_id text DEFAULT '',after_seq bigint DEFAULT 0,lim integer DEFAULT 1000) "
        "RETURNS TABLE(seq bigint,kind text,uid text,ts timestamptz,message_class text,role text,prompt_origin text,"
        "tool_name text,text text,input_text text,output_text text,stdout_text text,stderr_text text,"
        "error_class text,error_excerpt text) LANGUAGE sql STABLE AS $$ SELECT NULL::bigint,NULL::text,"
        "NULL::text,NULL::timestamptz,NULL::text,NULL::text,NULL::text,NULL::text,NULL::text,NULL::text,"
        "NULL::text,NULL::text,NULL::text,NULL::text,NULL::text WHERE FALSE $$"
    )
    db.execute("UPDATE ah.meta SET value='legacy' WHERE key='schema_hash'")
    db.commit()
    original = load.refresh

    def refresh(connection, *args, **kwargs):
        with psycopg.connect(os.environ["AGENT_HISTORY_TEST_READER_DSN"], autocommit=True) as reader:
            assert marked(reader)
            with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState):
                reader.execute("SELECT * FROM ah.session_events('pi','absent','',0,200)").fetchall()
        return original(connection, *args, **kwargs)

    monkeypatch.setattr(load, "refresh", refresh)
    assert load.rebuild(db, hot, cold, textfile=None, log=lambda *_: None).errors == 0


def test_inventoried_transcript_disappearing_keeps_fence(catalogue, monkeypatch):
    db, hot, cold = catalogue
    original = load.refresh
    victim = next(hot.rglob("*.jsonl"))

    def refresh(connection, *args, **kwargs):
        # Preserve the synthetic input rather than destroying it. A real filesystem race
        # after preflight must count as incomplete even when path.stat quietly skips it.
        assert marked(connection)
        connection.commit()
        victim.rename(victim.with_suffix(".retained"))
        return original(connection, *args, **kwargs)

    monkeypatch.setattr(load, "refresh", refresh)
    stats = load.rebuild(db, hot, cold, textfile=None, log=lambda *_: None)
    assert stats.errors > 0
    assert marked(db)


@pytest.mark.parametrize("layout", ["map", "archive"])
def test_unreadable_source_tree_refuses_without_destroying_old_generation(catalogue, monkeypatch, layout):
    db, hot, cold = catalogue
    before = db.execute("SELECT count(*) FROM ah.session").fetchone()[0]
    db.commit()
    original = os.walk

    def walk(root, *args, **kwargs):
        if str(root).startswith(str(hot)):
            error = PermissionError("synthetic inventory denied")
            if kwargs.get("onerror"):
                kwargs["onerror"](error)
            return iter(())
        return original(root, *args, **kwargs)

    monkeypatch.setattr(os, "walk", walk)
    with pytest.raises((OSError, SystemExit), match="synthetic|rebuild refused"):
        if layout == "map":
            load.rebuild(db, sources={"claude-local": hot / "claude-local"}, textfile=None)
        else:
            load.rebuild(db, hot, cold, textfile=None)
    assert db.execute("SELECT count(*) FROM ah.session").fetchone()[0] == before
    assert not marked(db)


def test_cold_archive_disappearing_before_locked_inventory_refuses(catalogue, monkeypatch):
    db, hot, cold = catalogue
    before = db.execute("SELECT count(*) FROM ah.session").fetchone()[0]
    db.commit()
    original = load.try_lock

    def lock(connection):
        held = original(connection)
        if held:
            cold.rename(cold.with_name("cold-retained"))
        return held

    monkeypatch.setattr(load, "try_lock", lock)
    with pytest.raises((OSError, SystemExit), match="refused|No such"):
        load.rebuild(db, hot, cold, textfile=None)
    assert db.execute("SELECT count(*) FROM ah.session").fetchone()[0] == before
    assert not marked(db)

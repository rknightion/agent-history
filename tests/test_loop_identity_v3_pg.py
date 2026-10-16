"""V3 metadata/fallback through Writer and post-passes, including collector-only refreshes."""

import os
from datetime import timedelta

import pytest

from agent_history import load
from test_loop_identity_pg import conn  # noqa: F401 - real disposable catalogue fixture
from test_loop_identity_v3 import AT, CHARTER, CLOSEOUT, DIGEST, HEADER
from test_loop_launch_v3_pg import transcript

DSN = os.environ.get("AGENT_HISTORY_TEST_DSN", "")
pytestmark = pytest.mark.skipif(not DSN or "agent_history_test" not in DSN, reason="scratch database not set")


@pytest.fixture
def clean(conn):  # noqa: F811
    conn.execute("TRUNCATE " + ", ".join(f"ah.{table}" for table in load.DATA_TABLES) + " RESTART IDENTITY CASCADE")
    conn.execute("DELETE FROM ah.loop_run_metadata")
    conn.execute("DELETE FROM ah.loop_receipt")
    conn.execute("DELETE FROM ah.meta WHERE key='loops_identity_v3_projection_v1'")
    conn.commit()
    yield conn
    conn.rollback()
    conn.execute("DELETE FROM ah.loop_run_metadata")
    conn.execute("DELETE FROM ah.loop_receipt")
    conn.execute("DELETE FROM ah.meta WHERE key='loops_identity_v3_projection_v1'")
    conn.commit()


def observe(db, *, machine="machine-a", origin="example/project", digest=DIGEST, when=None):
    db.execute(
        "INSERT INTO ah.loop_run_metadata(machine,path,kind,sha256,line1,file_mtime,repo_origin) "
        "VALUES (%s,%s,'closeout',%s,%s,%s,%s)",
        (machine, CLOSEOUT, "b" * 64, HEADER.replace(DIGEST, digest), when or AT + timedelta(minutes=1), origin),
    )
    db.commit()


def result(db):
    return db.execute(
        "SELECT l.mode,l.report_path,l.goal_path,r.repo,r.loop,r.goal_sha256,r.status,l.end_evidence "
        "FROM ah.loop_run l JOIN ah.loops r USING(launch_uid) ORDER BY l.launch_ts"
    ).fetchall()


def refresh(db, sources=None):
    if sources is not None:
        assert load.refresh(db, sources=sources, textfile=None, log=lambda *_: None).errors == 0
    else:
        load.post_passes(db)
        db.commit()


def test_metadata_only_pass_finishes_and_conflicting_copy_revokes(clean, tmp_path):
    transcript(tmp_path, "claude")
    refresh(clean, {"claude-test": tmp_path})
    observe(clean)
    refresh(clean)
    assert result(clean) == [("v3", CLOSEOUT, CHARTER, "example/project", "loop14", DIGEST, "finished", "closeout")]
    observe(clean, machine="machine-b", origin="example/other")
    refresh(clean)
    assert result(clean)[0][3:6] == (None, None, None)
    assert result(clean)[0][6:] == ("stale", "root_last_event")
    # Collector metadata is preserved by the derived-data truncation boundary.
    assert "loop_run_metadata" not in load.DATA_TABLES


@pytest.mark.parametrize("variant", ["before_launch", "tied", "late_lane", "wrong_loop"])
def test_ambiguous_or_unattributable_metadata_cannot_finish(clean, tmp_path, variant):
    transcript(tmp_path, "claude")
    refresh(clean, {"claude-test": tmp_path})
    if variant == "tied":
        clean.execute(
            "INSERT INTO ah.loop_run(launch_uid,root_session_id,loop_number,naming,mode,report_path,goal_path,launch_ts) "
            "SELECT launch_uid||':tied',root_session_id,loop_number,naming,mode,report_path,goal_path,launch_ts FROM ah.loop_run"
        )
    elif variant == "late_lane":
        clean.execute(
            "INSERT INTO ah.session(agent,session_uid,root_session_id,first_event_at) "
            "SELECT 'pi','synthetic-late-lane',root_session_id,%s FROM ah.loop_run",
            (AT + timedelta(minutes=2),),
        )
    clean.commit()
    observe(clean, when=AT - timedelta(seconds=1) if variant == "before_launch" else None)
    if variant == "wrong_loop":
        clean.execute("UPDATE ah.loop_run_metadata SET line1=replace(line1,'loop14','loop15')")
        clean.commit()
    refresh(clean)
    assert all(row[6] != "finished" for row in result(clean))


@pytest.mark.parametrize("agent", ["claude", "codex", "pi"])
def test_exact_structured_fallback_requires_success_and_origin(clean, tmp_path, agent):
    # The parser/Writer fixture is built by the no-database pipeline test's same real format.
    from test_loop_identity_v3_pipeline import captured_fixture

    captured_fixture(tmp_path, agent)
    refresh(clean, {agent + "-test": tmp_path})
    root = clean.execute("SELECT root_session_id FROM ah.loop_run").fetchone()[0]
    clean.execute("UPDATE ah.session SET git_remote_url='https://example.org/example/project.git' WHERE id=%s", (root,))
    clean.execute("INSERT INTO ah.dirty_session(session_id) VALUES(%s) ON CONFLICT DO NOTHING", (root,))
    clean.commit()
    refresh(clean)
    assert result(clean)[0][3:] == ("example/project", "loop14", DIGEST, "finished", "closeout")
    clean.execute("UPDATE ah.tool_call SET outcome='error' WHERE call_uid='closeout'")
    clean.execute("INSERT INTO ah.dirty_session(session_id) VALUES(%s) ON CONFLICT DO NOTHING", (root,))
    clean.commit()
    refresh(clean)
    assert result(clean)[0][6] != "finished"


def test_v3_cannot_finish_from_legacy_receipt(clean, tmp_path):
    transcript(tmp_path, "claude")
    refresh(clean, {"claude-test": tmp_path})
    clean.execute(
        "INSERT INTO ah.loop_receipt(machine,path,kind,content,receipt_mtime,target_exists) "
        "VALUES ('machine-a',%s,'notified','request synthetic',%s,true)",
        (CLOSEOUT, AT + timedelta(seconds=10)),
    )
    clean.commit()
    refresh(clean)
    assert result(clean)[0][6] != "finished"


@pytest.mark.parametrize("directive", ["move", "eof"])
def test_retained_postpass_revokes_closeout_after_multi_file_mutation(clean, tmp_path, directive):
    from test_loop_identity_v3_repair import add_patch, mutation_patch, patch_fixture

    initial = add_patch(HEADER + "\nbody\n")
    patch_fixture(tmp_path, [initial])
    refresh(clean, {"codex-test": tmp_path})
    assert result(clean)[0][6:] == ("finished", "closeout")
    patch_fixture(tmp_path, [initial, mutation_patch(directive)])
    refresh(clean, {"codex-test": tmp_path})
    assert result(clean)[0][6] != "finished"


def test_retained_postpass_unicode_first_line_cannot_finish(clean, tmp_path):
    from test_loop_identity_v3_repair import add_patch, patch_fixture

    patch_fixture(tmp_path, [add_patch(HEADER + "\u2028+suffix\n")])
    refresh(clean, {"codex-test": tmp_path})
    assert result(clean)[0][6] != "finished"


def test_postpass_computes_one_projection_for_completion_and_identity(clean, tmp_path, monkeypatch):
    from agent_history import loop_identity_v3 as v3
    from test_loop_identity_v3_pipeline import captured_fixture

    calls = 0
    original = v3.projections

    def observe(db):
        nonlocal calls
        calls += 1
        return original(db)

    monkeypatch.setattr(v3, "projections", observe)
    captured_fixture(tmp_path, "codex")
    refresh(clean, {"codex-test": tmp_path})
    assert calls == 1
    assert result(clean)[0][6:] == ("finished", "closeout")


def test_retained_unicode_body_agrees_with_collected_file_hash(clean, tmp_path):
    import hashlib
    from test_loop_identity_v3_repair import add_patch, patch_fixture

    content = HEADER + "\nbody\u2028+suffix\n"
    patch_fixture(tmp_path, [add_patch(content, "\r\n")])
    refresh(clean, {"codex-test": tmp_path})
    clean.execute(
        "INSERT INTO ah.loop_run_metadata(machine,path,kind,sha256,line1,file_mtime,repo_origin) "
        "VALUES('machine-a',%s,'closeout',%s,%s,%s,'example/project')",
        (CLOSEOUT, hashlib.sha256(content.encode()).hexdigest(), HEADER, AT + timedelta(seconds=20)),
    )
    clean.commit()
    refresh(clean)
    assert result(clean)[0][6:] == ("finished", "closeout")

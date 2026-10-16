"""Loop push counts: one push observed twice is one push."""

from datetime import timedelta

from agent_history import load
from test_loop_trailers_pg import clean, conn, pytestmark, setup_run  # noqa: F401


def push(db, uid, session, at, evidence, sha=None, branch=None):
    db.execute(
        "INSERT INTO ah.git_event (agent,event_uid,session_id,ts,op,evidence,sha_short,branch,source_id,byte_offset) "
        "VALUES ('pi',%s,%s,%s,'push',%s,%s,%s,0,0)",
        (uid, session, at, evidence, sha, branch),
    )


def test_the_same_pushed_sha_reported_twice_counts_once(clean, tmp_path):  # noqa: F811
    root, _, at = setup_run(clean, tmp_path)
    # A real push, then a later call whose output reports the same range for the same branch.
    push(clean, "call-1:push:3333333", root, at + timedelta(seconds=1), "output_regex", "3333333", "main")
    push(clean, "call-2:push:3333333", root, at + timedelta(seconds=2), "output_regex", "3333333", "main")
    # A quiet push is a separate push even without a range.
    push(clean, "call-3:push:cmd0", root, at + timedelta(seconds=3), "command")
    clean.execute("DELETE FROM ah.meta WHERE key = 'loops_progress_projection_v1'")
    clean.execute("INSERT INTO ah.dirty_session (session_id) VALUES (%s) ON CONFLICT DO NOTHING", (root,))
    load.post_passes(clean)
    assert clean.execute("SELECT pushes FROM ah.loops").fetchone() == (2,)
    assert clean.execute("SELECT pushes FROM ah.session_rollup WHERE session_id = %s", (root,)).fetchone() == (2,)

"""V3 live structure from the collected state log the pi extension writes, and v3 closeouts."""

import json

from agent_history import load
from test_loop_outcomes import encoded
from test_loop_outcomes_pg import collect, launch
from test_loop_v3_state import v3_framed
import test_loops_pg as helpers

clean = helpers.clean
conn = helpers.conn
pytestmark = helpers.pytestmark


def ext_state():
    """Extension-written dispatches and returns plus a root judgement and park, as a pi v3 run records."""
    rows = v3_framed(
        {"ev": "dispatch", "task": "EX-1", "lane": "one", "agent": "worker", "run": "run-one", "base": "base"},
        {"ev": "dispatch", "task": "EX-2", "lane": "two", "agent": "senior", "run": "run-two", "base": "base"},
        {"ev": "return", "lane": "one", "run": "run-one", "status": "complete"},
        {"ev": "park", "task": "EX-3", "reason": "decision", "needs": "owner"},
        {"ev": "judgement", "text": "Lane two owns the remaining migration."},
    )
    for row in rows[:4]:
        row["by"] = "ext"
    return rows


def live(db):
    # A stale synthetic run is projected by the bounded historical pass, so restart that cursor.
    db.execute("DELETE FROM ah.meta WHERE key = 'loops_live_projection_v1'")
    load.post_passes(db)
    return db.execute("SELECT live_phase,active_lanes,parks_total,last_judgement,last_park FROM ah.loops").fetchone()


def test_v3_state_log_supplies_live_lanes_judgement_and_parks(clean, tmp_path):
    launch(clean, tmp_path)
    clean.execute("UPDATE ah.loop_run SET mode='v3'")
    clean.commit()
    collect(clean, tmp_path, encoded(ext_state()), copies=2)
    load.post_passes(clean)
    phase, lanes, parks, judgement, park = live(clean)
    assert phase is not None
    assert [(lane["lane"], lane["task"], lane["agent"]) for lane in lanes] == [("two", "EX-2", "senior")]
    assert parks == 1 and park == {"task": "EX-3", "needs": "owner", "reason": "decision"}
    assert judgement == "Lane two owns the remaining migration."


def test_v3_invalid_state_copy_leaves_live_structure_unknown(clean, tmp_path):
    launch(clean, tmp_path)
    clean.execute("UPDATE ah.loop_run SET mode='v3'")
    clean.commit()
    rows = ext_state()
    collect(clean, tmp_path, encoded(rows))
    rows[2]["unknown"] = True
    clean.execute("UPDATE ah.loop_state SET content=%s", (encoded(rows),))
    clean.commit()
    load.post_passes(clean)
    assert live(clean)[:2] == (None, None)


def test_finished_v3_closeout_is_closing(clean, tmp_path):
    launch(clean, tmp_path)
    clean.execute("UPDATE ah.loop_run SET mode='v3'")
    clean.commit()
    collect(clean, tmp_path, encoded(ext_state()))
    load.post_passes(clean)
    assert live(clean)[0] != "closing"
    clean.execute("UPDATE ah.loop_run SET end_evidence='closeout', end_ts=launch_ts + interval '1 hour'")
    clean.execute("UPDATE ah.loops SET status='finished', end_ts=launch_ts + interval '1 hour'")
    from agent_history import loop_live

    loop_live.refresh(clean)
    row = clean.execute("SELECT live_phase,active_lanes FROM ah.loops").fetchone()
    assert row[0] == "closing"
    assert json.dumps(row[1]) is not None

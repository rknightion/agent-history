"""Lane completion and lane counts through the real refresh path: a lane whose spawn record carries no
status takes it from the lane-return block in its own transcript, and a resumed lane (several spawn
rows, one child session) is one lane. Also the closeout's spend-by-role query."""

from __future__ import annotations

import os

import pytest

from agent_history import reader
from test_loop_links_pg import conn, fixture, refresh  # noqa: F401 - real disposable catalogue fixture

DSN = os.environ.get("AGENT_HISTORY_TEST_DSN", "")
pytestmark = pytest.mark.skipif(not DSN or "agent_history_test" not in DSN, reason="scratch database not set")


def resumed(conn, count=1):  # noqa: F811
    """Give the lane's spawn a launch time inside the run, then resume the same child `count` more times."""
    conn.execute("UPDATE ah.loop_run SET end_ts = launch_ts + interval '1 hour'")
    conn.execute("UPDATE ah.subagent_spawn SET spawned_at = (SELECT launch_ts + interval '1 minute' FROM ah.loop_run)")
    for n in range(count):
        conn.execute(
            "INSERT INTO ah.subagent_spawn (agent, spawn_uid, parent_session_id, child_session_id, spawned_at, "
            "requested_type, launch_status, source_id, byte_offset) "
            "SELECT agent, spawn_uid || ':resume' || %s, parent_session_id, child_session_id, "
            "spawned_at + interval '10 minutes', requested_type, launch_status, source_id, byte_offset + 1 "
            "FROM ah.subagent_spawn ORDER BY id LIMIT 1",
            (n,),
        )
    conn.commit()


def test_a_spawn_without_status_takes_completion_from_the_lane_return(conn, tmp_path):  # noqa: F811
    fixture(tmp_path, direct_child=False)
    refresh(conn, tmp_path)
    assert conn.execute("SELECT completion_status FROM ah.subagent_spawn").fetchall() == [(None,)]
    assert conn.execute("SELECT completion_status, lane_return_status FROM ah.v_spawn_outcome").fetchall() == [
        ("completed", "complete")
    ]
    assert conn.execute("SELECT return_status, spawn_completion FROM ah.loop_lanes(1)").fetchall() == [
        ("complete", "completed")
    ]
    resumed(conn, count=0)  # dates the spawn so the report's window holds it
    assert conn.execute("SELECT spawns, completed FROM ah.routing_report(now() - interval '10 years')").fetchall() == [
        (1, 1)
    ]


@pytest.mark.parametrize("returned", ["complete", "partial", "blocked", "failed"])
def test_the_recorded_spawn_status_wins_and_other_return_statuses_pass_through(conn, tmp_path, returned):  # noqa: F811
    fixture(tmp_path, direct_child=False)
    refresh(conn, tmp_path)
    conn.execute("UPDATE ah.lane SET return_status = %s", (returned,))
    conn.commit()
    expected = "completed" if returned == "complete" else returned
    assert conn.execute("SELECT completion_status FROM ah.v_spawn_outcome").fetchone() == (expected,)
    conn.execute("UPDATE ah.subagent_spawn SET completion_status = 'failed'")
    conn.commit()
    assert conn.execute("SELECT completion_status FROM ah.v_spawn_outcome").fetchone() == ("failed",)
    assert conn.execute("SELECT spawn_completion FROM ah.loop_lanes(1)").fetchone() == ("failed",)


def test_a_resumed_lane_is_one_lane_not_one_per_spawn_row(conn, tmp_path):  # noqa: F811
    fixture(tmp_path, direct_child=False)
    refresh(conn, tmp_path)
    resumed(conn, count=2)
    assert conn.execute("SELECT count(*) FROM ah.subagent_spawn").fetchone() == (3,)
    assert conn.execute("SELECT lanes, subagents FROM ah.v_loop_summary").fetchone() == (1, 1)
    assert conn.execute("SELECT count(*) FROM ah.loop_lanes(1)").fetchone() == (1,)
    assert conn.execute("SELECT lanes_total, lanes_returned FROM ah.loops").fetchone() == (1, 1)


def test_spend_by_role_groups_the_root_and_each_lane_role(conn, tmp_path):  # noqa: F811
    fixture(tmp_path, direct_child=False, role="security-reviewer")
    refresh(conn, tmp_path)
    rows = conn.execute(reader.SPEND_BY_ROLE_SQL.format(ids="ARRAY[1]::bigint[]")).fetchall()
    # role, sessions, calls, input_uncached, output, cache_read, cache_write, cost, unpriced
    assert [(r[0], r[1], r[2], r[3], r[4], r[8]) for r in rows] == [
        ("root", 1, 2, 2, 2, 2),
        ("security-reviewer", 1, 1, 1, 1, 1),
    ]
    assert all(r[7] is None for r in rows)  # the synthetic model has no list price

"""Collected v3 JSONL through qualified PostgreSQL state outcome projection."""

from copy import deepcopy
from datetime import timedelta

import pytest

from agent_history import load
from test_loop_outcomes import AT, encoded
from test_loop_outcomes_pg import collect, launch, surface
from test_loop_v3_state import v3_framed
import test_loops_pg as helpers

clean = helpers.clean
conn = helpers.conn
pytestmark = helpers.pytestmark


def full_state():
    return v3_framed(
        {"ev": "admit", "task": "EX-1", "source": "first-wave", "owned": [], "accept": "proof"},
        {"ev": "dispatch", "task": "EX-1", "lane": "one", "agent": "worker", "run": "r", "base": "base"},
        {"ev": "accept", "task": "EX-1", "accepted": True, "reason": "green"},
        {"ev": "land", "task": "EX-1", "sha": "base", "gate": "green", "mode": "after-green"},
        {"ev": "park", "task": "EX-2", "reason": "decision", "needs": "owner"},
        {"ev": "close", "reason": "owner-stop"},
    )


def test_collected_v3_open_and_native_returns_leave_absent_outcomes_null(clean, tmp_path):
    launch(clean, tmp_path)
    rows = v3_framed(
        {"ev": "dispatch", "task": "EX-1", "lane": "one", "agent": "worker", "run": "r", "base": "base"},
        {"ev": "return", "lane": "one", "run": "r", "status": "complete", "landed": True},
    )
    rows[0]["by"] = "ext"
    collect(clean, tmp_path, encoded(rows), copies=2)
    load.post_passes(clean)
    assert surface(clean) == [("stale", None, None, None, None)]
    assert clean.execute("SELECT end_evidence FROM ah.loop_run").fetchone() == ("root_last_event",)


def test_collected_v3_explicit_outcomes_and_invalid_copy_retraction(clean, tmp_path):
    launch(clean, tmp_path)
    collect(clean, tmp_path, encoded(full_state()), copies=2)
    load.post_passes(clean)
    assert surface(clean) == [("finished", 1, 1, 1, 1)]
    clean.commit()
    load.post_passes(clean)
    assert surface(clean) == [("finished", 1, 1, 1, 1)]
    rows = full_state()
    rows[2]["unknown"] = True
    clean.execute("UPDATE ah.loop_state SET content=%s WHERE machine='synthetic-1'", (encoded(rows),))
    clean.commit()
    load.post_passes(clean)
    assert surface(clean) == [("stale", None, None, None, None)]


def test_collected_v3_conflicts_remain_field_independent_and_block_close(clean, tmp_path):
    launch(clean, tmp_path)
    collect(clean, tmp_path, encoded(full_state()), copies=2)
    rows = full_state()
    rows[4]["by"] = "ext"
    clean.execute("UPDATE ah.loop_state SET content=%s WHERE machine='synthetic-1'", (encoded(rows),))
    clean.commit()
    load.post_passes(clean)
    assert surface(clean) == [("stale", 1, None, 1, 1)]


@pytest.mark.parametrize("case", ["origin", "loop", "charter", "ambiguous", "cutoff"])
def test_collected_v3_requires_exact_identity_and_unique_launch_window(clean, tmp_path, case):
    launch(clean, tmp_path, second=case in ("ambiguous", "cutoff"))
    rows = full_state()
    if case == "charter":
        rows[0]["charter_sha256"] = "f" * 64
    if case == "cutoff":
        rows = v3_framed(
            {"ev": "park", "task": "EX-1", "reason": "decision", "needs": "owner"},
            {"ev": "close", "reason": "owner-stop"},
        )
        rows[0]["ts"] = (AT - timedelta(seconds=61)).strftime("%Y-%m-%dT%H:%M:%SZ")
        for row in rows[1:]:
            row["ts"] = (AT + timedelta(seconds=60)).strftime("%Y-%m-%dT%H:%M:%SZ")
    collect(clean, tmp_path, encoded(rows))
    if case in ("origin", "loop"):
        clean.execute(
            "UPDATE ah.loop_state SET repo_origin=%s,loop=%s",
            ("example/other" if case == "origin" else "example/project", "loop2" if case == "loop" else "loop1"),
        )
        clean.commit()
    load.post_passes(clean)
    actual = surface(clean)
    assert all(row[1:] == (None, None, None, None) for row in actual)
    assert [row[0] for row in actual] == (["finished", "stale"] if case in ("ambiguous", "cutoff") else ["stale"])
    assert not clean.execute("SELECT 1 FROM ah.loop_run WHERE end_evidence='state_close'").fetchall()


def test_collected_v3_unprojected_frame_conflicts_still_prevent_terminal_proof(clean, tmp_path):
    launch(clean, tmp_path)
    rows = v3_framed(
        {"ev": "return", "lane": "one", "run": "r", "status": "complete"},
        {"ev": "close", "reason": "owner-stop"},
    )
    collect(clean, tmp_path, encoded(rows), copies=2)
    other = deepcopy(rows)
    other[1]["status"] = "failed"
    clean.execute("UPDATE ah.loop_state SET content=%s WHERE machine='synthetic-1'", (encoded(other),))
    clean.commit()
    load.post_passes(clean)
    assert surface(clean) == [("stale", None, None, None, None)]

"""The second frozen state dialect keeps wire v=1 and nullable absent outcomes."""

from copy import deepcopy

import pytest

from agent_history import loops
from test_loop_outcomes import GOAL, encoded, framed
from test_loop_state_schema import BODIES


def v3_framed(*bodies):
    rows = framed(*bodies)
    rows[0] = {
        **{k: rows[0][k] for k in ("v", "seq", "ts", "by", "ev")},
        "charter_sha256": GOAL,
        "root": "llm",
        "root_model": "synthetic",
        "first_wave": ["EX-0001"],
    }
    return rows


def cohort(rows):
    parsed = loops.state_progress(encoded(rows))
    assert parsed is not None
    assert parsed[0] == GOAL
    return [(parsed[1], parsed[2])]


def test_v3_open_dispatch_and_return_do_not_invent_outcomes():
    rows = v3_framed(
        {"ev": "dispatch", "lane": "one", "task": "EX-0001", "agent": "worker", "run": "r", "base": "base"},
        {"ev": "return", "lane": "one", "run": "r", "status": "complete", "landed": True},
    )
    rows[0]["by"] = "ext"
    rows[1]["by"] = "daemon"
    cohorts = cohort(rows)
    assert loops.state_outcomes(cohorts) == (None, None, None)
    assert loops._state_event_counts(cohorts) == (None, None)
    assert not any(e["ev"] == "close" for _, events in cohorts for e in events)
    # The legacy envelope still supplies its established known zeros.
    assert loops.state_outcomes(cohort(framed())) == (0, 0, 0)


def test_v3_explicit_events_are_independent_and_deduplicated():
    cohorts = cohort(
        v3_framed(
            {"ev": "admit", "task": "EX-0001", "source": "first-wave", "owned": [], "accept": "proof"},
            {"ev": "park", "task": "EX-0001", "reason": "decision", "needs": "owner"},
            {"ev": "accept", "task": "EX-0001", "lane": "one", "accepted": False, "reason": "red"},
        )
    )
    assert loops.state_outcomes(cohorts * 2) == (1, None, 1)
    assert loops._state_event_counts(cohorts)[0] == 0


@pytest.mark.parametrize(
    "case", ["mixed-open", "dispatcher", "envelope-source", "gate", "gate-kind", "unknown", "wire-version"]
)
def test_v3_rejects_legacy_only_or_unknown_members(case):
    rows = v3_framed()
    if case == "mixed-open":
        rows[0]["goal_sha256"] = GOAL
    elif case == "dispatcher":
        rows[0]["by"] = "dispatcher"
    elif case == "envelope-source":
        rows = v3_framed({"ev": "admit", "task": "EX-1", "source": "envelope", "owned": [], "accept": "proof"})
    elif case == "gate":
        rows = v3_framed({"ev": "gate", "scope": "composed", "sha": "base", "cmd": "proof", "exit": 0})
    elif case == "gate-kind":
        rows = v3_framed(
            {
                "ev": "dispatch",
                "lane": "one",
                "task": "EX-1",
                "agent": "worker",
                "run": "r",
                "base": "b",
                "kind": "gate",
            }
        )
    elif case == "unknown":
        rows[0]["unknown"] = True
    else:
        rows[0]["v"] = 3
    assert loops.state_progress(encoded(rows)) is None


@pytest.mark.parametrize("body", [b for b in BODIES if b["ev"] != "gate"], ids=lambda b: b["ev"])
def test_v3_frozen_bodies_validate_all_fields(body):
    body = deepcopy(body)
    if body["ev"] == "admit":
        body["source"] = "first-wave"
    cohorts = cohort(v3_framed(body))
    assert cohorts[0][1].ledger[2][0] == body["ev"]
    assert loops.state_progress(encoded(v3_framed({**body, "unknown": True}))) is None
    if body["ev"] != "heartbeat":
        for key in body.keys() - {"ev"}:
            assert loops.state_progress(encoded(v3_framed({**body, key: [1]}))) is None


def test_v3_conflicting_copies_withhold_only_affected_outcomes():
    left = v3_framed(
        {"ev": "land", "task": "EX-1", "sha": "base", "gate": "green", "mode": "after-green"},
        {"ev": "park", "task": "EX-1", "reason": "decision", "needs": "owner"},
        {"ev": "close", "reason": "owner-stop"},
    )
    right = deepcopy(left)
    right[1] = {
        **{k: right[1][k] for k in ("v", "seq", "ts", "by")},
        "ev": "return",
        "lane": "one",
        "run": "r",
        "status": "complete",
    }
    cohorts = cohort(left) + cohort(right)
    assert loops.state_outcomes(cohorts) == (None, None, 1)
    assert "land" in loops._state_conflicts(cohorts)
    mixed = framed(*[{k: v for k, v in row.items() if k not in ("v", "seq", "ts", "by")} for row in left[1:]])
    assert loops.state_outcomes(cohort(left) + cohort(mixed)) == (None, None, None)

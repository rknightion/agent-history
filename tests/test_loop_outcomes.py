"""Frozen event-count normalization, independently expected synthetic outcomes."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from agent_history import loops

AT = datetime(2026, 10, 4, 12, tzinfo=timezone.utc)
GOAL = "a" * 64


def framed(*events):
    opened = {
        "ev": "open",
        "goal_sha256": GOAL,
        "tier": "guarded",
        "root": "llm",
        "root_model": "synthetic",
        "envelope": ["EX-0001", "OPS"],
    }
    return [
        {"v": 1, "seq": n, "ts": f"2026-10-04T12:00:{n:02d}Z", "by": "root", **event}
        for n, event in enumerate([opened, *events], 1)
    ]


def encoded(rows):
    return "".join(json.dumps(row) + "\n" for row in rows)


def outcome_events():
    return framed(
        {"ev": "admit", "task": "EX-0001", "source": "envelope", "owned": ["src"], "accept": "proof"},
        {"ev": "admit", "task": "ex-0001.02", "source": "envelope", "owned": "src", "accept": "proof"},
        {"ev": "admit", "task": "EX-0001", "source": "envelope", "owned": "src", "accept": "proof"},
        {"ev": "admit", "task": "DEPLOY", "source": "loop-created", "owned": "src", "accept": "proof"},
        {"ev": "dispatch", "task": "EX-0001", "lane": "one", "agent": "worker", "run": "run-one", "base": "base"},
        {"ev": "accept", "task": "EX-0001", "accepted": True, "reason": "green"},
        {"ev": "accept", "task": "EX-0001", "lane": "one", "accepted": False, "reason": "later red"},
        {"ev": "return", "lane": "one", "run": "run-one", "status": "complete", "landed": True},
        {"ev": "land", "task": "EX-0001", "sha": "b" * 40, "gate": "green", "mode": "after-green"},
        {"ev": "land", "task": "EX-0001", "sha": "c" * 40, "gate": "green", "mode": "after-green"},
        {"ev": "land", "task": "DEPLOY", "sha": "d" * 40, "gate": "green", "mode": "after-green"},
        {"ev": "revert", "task": "EX-0001", "sha": "b" * 40, "reverted_by": "e" * 40, "reason": "root-decision"},
        {"ev": "park", "task": "ex-0001.02", "reason": "decision", "needs": "owner"},
        {"ev": "park", "task": "ex-0001.02", "reason": "decision", "needs": "owner"},
        {"ev": "park", "task": "DEPLOY", "reason": "decision", "needs": "owner"},
        {"ev": "close", "reason": "owner-stop"},
    )


def test_independent_event_normalization_and_machine_deduplication():
    parsed = loops.state_progress(encoded(outcome_events()))
    assert parsed is not None
    cohorts = [(parsed[1], parsed[2])] * 2
    # Two tracker admits, one tracker publication, three distinct park events,
    # one accepted lane despite later rejection. Neither alias nor return is a publication.
    assert loops.state_outcomes(cohorts) == (2, 1, 3)
    assert loops._state_event_counts(cohorts)[0] == 1


def test_observed_open_zero_is_not_absence_or_envelope_admission():
    parsed = loops.state_progress(encoded(framed()))
    assert parsed is not None
    assert loops.state_outcomes([(parsed[1], parsed[2])]) == (0, 0, 0)
    assert loops.state_progress("") is None


@pytest.mark.parametrize(
    "event",
    [
        {"ev": "admit", "task": "EX-0001"},
        {"ev": "park", "task": "EX-0001", "reason": "decision", "needs": "invalid"},
        {"ev": "close", "reason": "invalid"},
    ],
)
def test_invalid_outcome_event_invalidates_snapshot(event):
    assert loops.state_progress(encoded(framed(event))) is None


def test_torn_close_is_not_terminal_and_unresolved_accept_is_independently_null():
    rows = framed({"ev": "accept", "task": "EX-0001", "accepted": True, "reason": "green"})
    parsed = loops.state_progress(encoded(rows) + '{"ev":"close"\n')
    assert parsed is not None
    cohorts = [(parsed[1], parsed[2])]
    assert not any(e["ev"] == "close" for e in parsed[2])
    assert loops._state_event_counts(cohorts)[0] is None
    assert loops.state_outcomes(cohorts) == (0, 0, 0)

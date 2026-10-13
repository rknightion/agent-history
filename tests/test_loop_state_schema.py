"""Frozen public v1 event schemas, including bodies that have no outcome projection."""

from __future__ import annotations

from copy import deepcopy

import pytest

from agent_history import loops
from test_loop_outcomes import encoded, framed

# Independent complete synthetic bodies for every frozen v1 event, not generated from
# the implementation schema. The open body is supplied by framed().
BODIES = [
    {
        "ev": "admit",
        "task": "EX-1",
        "source": "envelope",
        "owned": [],
        "accept": "proof",
        "tier": "routine",
        "surfaces": [],
    },
    {
        "ev": "dispatch",
        "lane": "one",
        "task": "EX-1",
        "agent": "worker",
        "run": "one",
        "base": "base",
        "deadline": "later",
        "model": "synthetic",
        "effort": "high",
        "surface": "synthetic",
        "tier": "guarded",
        "tasks": [],
        "kind": "work",
        "recovery_of": "old",
    },
    {
        "ev": "return",
        "lane": "one",
        "run": "one",
        "status": "partial",
        "sha": None,
        "landed": False,
        "check": "proof",
        "exit": None,
        "ci": None,
        "coderabbit": {"ran": False, "major": 0, "unreviewed": 0},
        "base": "base",
        "questions": [],
    },
    {"ev": "gate", "scope": "composed", "sha": "base", "cmd": "proof", "exit": None, "lane": "one", "base": "base"},
    {"ev": "accept", "task": "EX-1", "accepted": True, "reason": "green", "lane": "one"},
    {"ev": "park", "task": "EX-1", "reason": "decision", "needs": "owner", "lane": "one", "until": "later"},
    {"ev": "land", "task": "EX-1", "sha": "base", "gate": "green", "mode": "pre-green", "lane": "one", "ci": "proof"},
    {"ev": "revert", "sha": "base", "reverted_by": "revert", "reason": "root-decision", "task": "EX-1"},
    {"ev": "watch", "op": "start", "what": "worker", "deadline": "later"},
    {"ev": "heartbeat", "at": "2026-10-04T12:00:00.123456Z"},
    {"ev": "judgement", "text": "synthetic", "task": "EX-1", "lane": "one"},
    {"ev": "close", "reason": "budget"},
]


@pytest.mark.parametrize("body", BODIES, ids=lambda e: e["ev"])
def test_every_frozen_event_body_is_validated_and_fingerprinted(body):
    parsed = loops.state_progress(encoded(framed(body)))
    assert parsed is not None
    assert set(parsed[2].ledger) == {1, 2}
    assert parsed[2].ledger[2][0] == body["ev"]
    invalid = {**body, "unknown": "member"}
    assert loops.state_progress(encoded(framed(invalid))) is None


@pytest.mark.parametrize("body", [e for e in BODIES if e["ev"] != "heartbeat"], ids=lambda e: e["ev"])
def test_invalid_members_of_unprojected_and_projected_events_invalidate_snapshot(body):
    # Every supplied required/optional member must have the frozen type. A list
    # with a non-string item is invalid for strlist and owned as well as scalars.
    for key in body.keys() - {"ev"}:
        invalid = deepcopy(body)
        invalid[key] = [1]
        assert loops.state_progress(encoded(framed(invalid))) is None, (body["ev"], key)


@pytest.mark.parametrize("key", ["agent", "run", "base"])
def test_dispatch_requires_complete_body(key):
    body = deepcopy(BODIES[1])
    del body[key]
    assert loops.state_progress(encoded(framed(body))) is None


@pytest.mark.parametrize("key,value", [("ran", 1), ("major", True), ("unreviewed", False)])
def test_return_review_counts_do_not_accept_booleans_as_integers(key, value):
    body = deepcopy(BODIES[2])
    body["coderabbit"][key] = value
    assert loops.state_progress(encoded(framed(body))) is None


def test_judgement_limit_is_utf8_bytes_not_characters():
    assert loops.state_progress(encoded(framed({"ev": "judgement", "text": "é" * 2048}))) is not None
    assert loops.state_progress(encoded(framed({"ev": "judgement", "text": "é" * 2049}))) is None


def test_invalid_heartbeat_activity_retains_ledger_without_activity():
    parsed = loops.state_progress(encoded(framed({"ev": "heartbeat", "at": False})))
    assert parsed is not None and parsed[2] == []
    assert parsed[2].ledger[2][0] == "heartbeat"
    assert loops.state_outcomes([(parsed[1], parsed[2])]) == (0, 0, 0)


def test_return_artifact_is_optional_typed_and_fingerprinted():
    body = {**BODIES[2], "artifact": "/fixture/full return.md"}
    parsed = loops.state_progress(encoded(framed(body)))
    assert parsed is not None
    assert '"artifact": "/fixture/full return.md"' in parsed[2].ledger[2][1]
    assert loops.state_outcomes([(parsed[1], parsed[2])]) == (0, 0, 0)
    for invalid in (None, "", "relative.md", "/nul" + chr(0), 1, True, [], {}):
        assert loops.state_progress(encoded(framed({**body, "artifact": invalid}))) is None
    assert loops.state_progress(encoded(framed({**body, "unknown": "member"}))) is None

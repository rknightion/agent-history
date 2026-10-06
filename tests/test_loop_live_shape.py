"""Producer JSONB shape at the pure projection and real collector boundaries."""

from datetime import datetime, timedelta, timezone

import pytest

from agent_history import loop_live
from test_loop_live import AT, event


def test_projection_nulls_invalid_members_without_coercion():
    state = loop_live.project(
        [
            event("open"),
            event("dispatch", run="one", lane=42, task=False, title={"text": "invalid"}, agent=["reviewer"]),
            event("gate", sha=["synthetic"], scope=42, exit=True),
        ],
        AT,
    )
    assert state["active_lanes"] == [
        {"lane": None, "task": None, "title": None, "agent": None, "started_at": "2026-01-01T00:00:00.000000Z"}
    ]
    assert state["last_gate"] == {"sha": None, "scope": None, "exit": None, "at": "2026-01-01T00:00:00.000000Z"}


@pytest.mark.parametrize("value", ["x" * 1001, "bad\x00text", "\ud800", 42, True, [], {}, None])
def test_projection_invalid_lane_text_is_member_null(value):
    fields = {key: value for key in ("lane", "task", "title", "agent")}
    state = loop_live.project([event("dispatch", run="one", **fields)], AT)
    assert state["active_lanes"] == [{**dict.fromkeys(fields), "started_at": "2026-01-01T00:00:00.000000Z"}]


@pytest.mark.parametrize("value", ["", "界" * 1000])
def test_projection_valid_lane_text_keeps_exact_characters(value):
    fields = {key: value for key in ("lane", "task", "title", "agent")}
    state = loop_live.project([event("dispatch", run="one", **fields)], AT)
    assert state["active_lanes"][0] == {**fields, "started_at": "2026-01-01T00:00:00.000000Z"}


@pytest.mark.parametrize("exit_code", [None, True, False, "0", 0.0, -2147483649, 2147483648, [], {}])
def test_projection_invalid_gate_exit_is_null(exit_code):
    state = loop_live.project([event("gate", exit=exit_code)], AT)
    assert state["last_gate"]["exit"] is None


@pytest.mark.parametrize("exit_code", [-2147483648, 0, 2147483647])
def test_projection_int32_gate_exit_is_retained(exit_code):
    assert loop_live.project([event("gate", exit=exit_code)], AT)["last_gate"]["exit"] == exit_code


@pytest.mark.parametrize("value", [[], {}, 1, False, "bad\x00text", "\udfff"])
def test_projection_invalid_gate_strings_are_null(value):
    gate = loop_live.project([event("gate", sha=value, scope=value)], AT)["last_gate"]
    assert gate["sha"] is gate["scope"] is None


def test_projection_gate_strings_have_no_length_or_hash_format_limit():
    value = "界" * 2000
    gate = loop_live.project([event("gate", sha=value, scope="")], AT)["last_gate"]
    assert gate["sha"] == value and gate["scope"] == ""


def test_projection_absent_members_and_unobserved_containers_are_null():
    unknown = loop_live.project([], AT)
    assert unknown["active_lanes"] is unknown["last_gate"] is None
    opened = loop_live.project([event("open")], AT)
    assert opened["active_lanes"] == [] and opened["last_gate"] is None
    observed = loop_live.project([event("dispatch", run="one"), event("gate")], AT)
    assert observed["active_lanes"] == [
        {"lane": None, "task": None, "title": None, "agent": None, "started_at": "2026-01-01T00:00:00.000000Z"}
    ]
    assert observed["last_gate"] == {"sha": None, "scope": None, "exit": None, "at": "2026-01-01T00:00:00.000000Z"}


@pytest.mark.parametrize("count", [64, 65])
def test_projection_lane_container_limit_is_not_a_truncated_list(count):
    events = [event("dispatch", run=str(n), lane=str(n), agent="complex-worker") for n in range(count)]
    state = loop_live.project(events, AT)
    assert state["live_phase"] == "working"
    if count == 64:
        assert len(state["active_lanes"]) == 64
    else:
        assert state["active_lanes"] is None


def test_projection_unkeyed_dispatch_does_not_claim_known_empty_lanes():
    state = loop_live.project([event("open"), event("dispatch", lane=[], run={})], AT)
    assert state["active_lanes"] is None
    assert state["live_phase"] is None


def test_projection_timestamps_are_utc_microseconds_not_local_spelling():
    at = datetime(2026, 1, 1, 2, 0, 0, 123456, timezone(timedelta(hours=2)))
    state = loop_live.project([{"ev": "dispatch", "run": "one", "at": at}, {"ev": "gate", "at": at}], AT)
    assert state["active_lanes"][0]["started_at"] == state["last_gate"]["at"] == "2026-01-01T00:00:00.123456Z"


@pytest.mark.parametrize("value", [None, "2026-01-01T00:00:00.1234567Z", datetime(2026, 1, 1), 0])
def test_unknown_or_noncanonical_internal_time_is_not_guessed(value):
    assert loop_live._live_timestamp(value) is None

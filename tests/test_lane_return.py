"""parse_lane_return: the old free-form lane-return object and the v2 object, side by side."""

from __future__ import annotations

import json

import pytest

pytest.importorskip("psycopg")

from agent_history.loops import parse_lane_return  # noqa: E402

V2 = {
    "v": 2,
    "lane": "H1",
    "status": "complete",
    "sha": None,
    "landed": False,
    "base": "a" * 40,
    "check": "just check",
    "exit": 0,
    "tail": "line\n```\nline",
    "ci": None,
    "coderabbit": {"ran": True, "major": 0, "unreviewed": 0},
    "questions": [],
}


def block(value) -> str:
    body = value if isinstance(value, str) else json.dumps(value)
    return f"Done.\n\n```lane-return\n{body}\n```\n"


def test_v2_block_is_stored_whole_with_its_status():
    assert parse_lane_return(block(V2)) == (V2, "complete")  # a fence quoted inside the tail does not end it


def test_old_shape_keeps_any_string_status():
    old = {"job_id": "E49A", "status": "blocked", "candidate_sha": "b" * 40}
    assert parse_lane_return(block(json.dumps(old, indent=2))) == (old, "blocked")


@pytest.mark.parametrize("bad", [{**V2, "status": "accepted"}, {**V2, "lane": 7}])
def test_invalid_v2_fields_give_no_status(bad):
    assert parse_lane_return(block(bad)) == (bad, None)


def test_last_block_wins_and_garbage_is_unparsed():
    assert parse_lane_return(block({"status": "partial"}) + block(V2))[1] == "complete"
    assert parse_lane_return(block("not json")) == ({"unparsed": True}, None)
    assert parse_lane_return("no block here") == (None, None)


def test_crlf_line_endings_and_a_closing_fence_on_the_json_line():
    crlf = block(V2).replace("\n", "\r\n")
    assert parse_lane_return(crlf) == (V2, "complete")
    same_line = 'Done.\n```lane-return\n{"status": "blocked", "job": "x"}```\n'
    assert parse_lane_return(same_line) == ({"status": "blocked", "job": "x"}, "blocked")
    compact = f"Done.\n```lane-return\n{json.dumps(V2)}```"
    assert parse_lane_return(compact) == (V2, "complete")  # the fence inside the tail does not end it


@pytest.mark.parametrize("version", ["2", 2.0, True, 3])
def test_only_the_integer_2_is_v2_and_other_versions_give_no_status(version):
    # an object that declares a version is never read as the free-form shape
    value = {**V2, "v": version}
    assert parse_lane_return(block(value)) == (value, None)

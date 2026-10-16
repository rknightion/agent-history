"""Lane-return projection from legacy, v2 and v3 final messages."""

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


@pytest.mark.parametrize("version", ["2", 2.0, True, "3", 3.0, 4])
def test_only_supported_integer_versions_give_status(version):
    # an object that declares a version is never read as the free-form shape
    value = {**V2, "v": version}
    assert parse_lane_return(block(value)) == (value, None)


@pytest.mark.parametrize("status", ["complete", "partial", "blocked", "failed"])
@pytest.mark.parametrize("form", ["patch", "sha"])
def test_v3_final_message_through_pi_parser(tmp_path, status, form):
    from agent_history.memstore import MemStore, run_file
    from agent_history.model import FileContext
    from agent_history.parse_pi import PiParser

    value = {
        "v": 3,
        "lane": "parser",
        "task": "TASK-123",
        "status": status,
        "base": "a" * 40,
        "worktree": "/tmp/example-tree",
        "check": "just check",
        "exit": 0,
        "tail": "result\n```\ncomplete",
        "coderabbit": {"ran": True, "major": 0, "unreviewed": 0},
        "questions": [],
    }
    if form == "patch":
        value.update(patch="/tmp/candidate.patch", patch_sha256="b" * 64)
    else:
        value["sha"] = "c" * 40
    final = f"```lane-return\n{json.dumps(value)}\n```\nCandidate ready."
    records = [
        {"type": "session", "version": 3, "id": "synthetic-lane", "timestamp": "2026-09-01T10:00:00Z"},
        {
            "type": "message",
            "id": "final",
            "timestamp": "2026-09-01T10:01:00Z",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": final}],
                "stopReason": "stop",
            },
        },
    ]
    path = tmp_path / "session.jsonl"
    path.write_text("".join(json.dumps(record) + "\n" for record in records))
    store = MemStore()
    context = FileContext(str(path), "pi-test/session.jsonl", "pi-test", "pi", "test", None, "subagent")
    run_file(PiParser, context, store)
    reports = [row for row in store.rows("message") if row["message_class"] == "subagent_report"]
    assert len(reports) == 1
    assert reports[0]["text"] == final
    parsed, return_status = parse_lane_return(reports[0]["text"])
    assert (parsed, return_status) == (value, status)
    assert (parsed["lane"], parsed["task"]) == ("parser", "TASK-123")
    assert parsed[form] == value[form]


@pytest.mark.parametrize("bad", [{"lane": 7}, {"status": "accepted"}, {"status": None}])
def test_invalid_v3_fields_give_no_status(bad):
    value = {**V2, "v": 3, **bad}
    assert parse_lane_return(block(value)) == (value, None)

"""Retained successful patch input cannot fabricate a header or hide a later mutation."""

import hashlib
import json
from datetime import timedelta

import pytest

from agent_history import load
from agent_history.loop_identity_v3 import project, structured_closeout
from agent_history.memstore import MemStore, run_file
from agent_history.model import FileContext
from test_loop_identity_v3 import AT, CLOSEOUT, CWD, HEADER, RUN
from test_loop_identity_v3_pipeline import captured_fixture


def mutation_patch(directive):
    before = "*** Update File: unrelated.txt\n"
    if directive == "move":
        before += "*** Move to: renamed.txt\n@@\n-old\n+new\n"
        target = "*** Delete File: " + CLOSEOUT + "\n"
    else:
        before += "@@\n-old\n+new\n*** End of File\n"
        target = "*** Update File: " + CLOSEOUT + "\n@@\n-old\n+changed\n"
    return "*** Begin Patch\n" + before + target + "*** End Patch"


def patch_fixture(tmp_path, patches):
    path = captured_fixture(tmp_path, "codex")
    records = [json.loads(line) for line in path.read_text().splitlines()]
    records = records[:2]
    for index, patch in enumerate(patches):
        uid = f"patch-{index}"
        records += [
            {
                "type": "response_item",
                "timestamp": (AT + timedelta(seconds=10 + 20 * index)).isoformat(),
                "payload": {"type": "custom_tool_call", "call_id": uid, "name": "apply_patch", "input": patch},
            },
            {
                "type": "response_item",
                "timestamp": (AT + timedelta(seconds=20 + 20 * index)).isoformat(),
                "payload": {
                    "type": "custom_tool_call_output",
                    "call_id": uid,
                    "output": "Success. Updated the following files:\nA " + CLOSEOUT,
                },
            },
        ]
    path.write_text("\n".join(json.dumps(record) for record in records) + "\n")
    return path


def add_patch(content, delimiter="\n"):
    return delimiter.join(
        [
            "*** Begin Patch",
            "*** Add File: " + CLOSEOUT,
            *("+" + line for line in content.split("\n")[:-1]),
            "*** End Patch",
        ]
    )


def retained_writes(path):
    context = FileContext(str(path), "codex-test/repair", "codex-test", "codex", "test", None, "main")
    store = MemStore()
    parser, _ = load.parser_for("codex")
    run_file(parser, context, store)
    calls = {row["call_uid"]: row for row in store.rows("tool_call")}
    writes = []
    for io in sorted(store.rows("tool_io"), key=lambda row: row["ts"]):
        call = calls[io["call_uid"]]
        assert call["outcome"] == "ok" and call["ended_at"] is not None
        observed = structured_closeout(io["tool_name"], io["input_text"], CLOSEOUT, CWD, io["ts"], call["ended_at"])
        if observed is not None:
            writes.append(observed)
    return writes


@pytest.mark.parametrize("directive", ["move", "eof"])
def test_retained_multi_file_patch_cannot_hide_target_invalidation(tmp_path, directive):
    path = patch_fixture(tmp_path, [add_patch(HEADER + "\nbody\n"), mutation_patch(directive)])
    writes = retained_writes(path)
    assert project(RUN, [RUN], [], writes[:1], origins=["example/project"]).end is not None
    assert project(RUN, [RUN], [], writes, origins=["example/project"]).end is None


@pytest.mark.parametrize("separator", ["\u2028", "\u0085", "\r"])
def test_retained_unicode_content_does_not_manufacture_a_header(tmp_path, separator):
    content = HEADER + separator + "+suffix\n"
    (written,) = retained_writes(patch_fixture(tmp_path, [add_patch(content)]))
    assert written.sha256 == hashlib.sha256(content.encode()).hexdigest()
    assert written.line1 is None
    assert project(RUN, [RUN], [], [written], origins=["example/project"]).end is None


@pytest.mark.parametrize("delimiter", ["\n", "\r\n"])
def test_patch_delimiters_preserve_unicode_body_hash(tmp_path, delimiter):
    content = HEADER + "\nbody\u2028+suffix\u0085+tail\v+end\n"
    (written,) = retained_writes(patch_fixture(tmp_path, [add_patch(content, delimiter)]))
    assert written.sha256 == hashlib.sha256(content.encode()).hexdigest()
    assert written.line1 == HEADER

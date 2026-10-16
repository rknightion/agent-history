"""Synthetic file and transcript fixtures through real collectors/parsers to v3 projection."""

import hashlib
import json
import os
import subprocess

import pytest

from agent_history import collect_loop_metadata, load
from agent_history.loop_identity_v3 import Run, _remote, project, structured_closeout
from agent_history.memstore import MemStore, run_file
from agent_history.model import FileContext
from test_loop_identity_v3 import AT, CLOSEOUT, CWD, DIGEST, HEADER, RUN
from test_loop_launch_v3_pg import transcript


def test_real_metadata_iterator_to_projector(tmp_path):
    root = tmp_path / "not-the-repository-name"
    root.mkdir()
    subprocess.run(["git", "init", str(root)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(root), "remote", "add", "origin", "https://example.org/example/project.git"], check=True
    )
    (root / "codex").mkdir()
    charter = root / "codex" / "charter-synthetic-loop14.md"
    closeout = root / "codex" / "closeout-synthetic-loop14.md"
    charter.write_text("Synthetic charter, not retained.\n")
    digest = hashlib.sha256(charter.read_bytes()).hexdigest()
    closeout.write_text(HEADER.replace(DIGEST, digest) + "\nSynthetic body, not retained.\n")
    os.utime(charter, (AT.timestamp() - 10, AT.timestamp() - 10))
    os.utime(closeout, (AT.timestamp() + 10, AT.timestamp() + 10))
    run = Run("synthetic:launch", 1, 14, AT, str(charter), str(closeout))
    result = project(run, [run], collect_loop_metadata.metadata(root, "test-machine"), [])
    assert result.fields == {"repo": "example/project", "loop": "loop14", "goal_sha256": digest}
    assert result.end.timestamp() == AT.timestamp() + 10


def captured_fixture(tmp_path, agent):
    path = transcript(tmp_path, agent)
    records = [json.loads(line) for line in path.read_text().splitlines()]
    at = "2026-10-04T12:00:10Z"
    ended = "2026-10-04T12:00:20Z"
    if agent == "codex":
        records[0]["payload"]["git"] = {"repository_url": "https://example.org/example/project.git"}
        records[2]["timestamp"] = at
        records[2]["payload"]["input"] = (
            "*** Begin Patch\n*** Add File: " + CLOSEOUT + "\n+" + HEADER + "\n+Synthetic body.\n*** End Patch"
        )
        records[3]["timestamp"] = ended
    elif agent == "claude":
        records += [
            {
                "type": "assistant",
                "uuid": "closeout-write",
                "sessionId": records[0]["sessionId"],
                "timestamp": at,
                "cwd": CWD,
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "closeout",
                            "name": "Write",
                            "input": {"file_path": CLOSEOUT, "content": HEADER + "\nSynthetic body.\n"},
                        }
                    ],
                },
            },
            {
                "type": "user",
                "uuid": "closeout-result",
                "sessionId": records[0]["sessionId"],
                "timestamp": ended,
                "message": {
                    "role": "user",
                    "content": [{"type": "tool_result", "tool_use_id": "closeout", "content": "Written successfully."}],
                },
            },
        ]
    else:
        from test_loop_links_pg import assistant, message

        call = assistant(
            "closeout-write",
            [
                {
                    "type": "toolCall",
                    "id": "closeout",
                    "name": "write",
                    "arguments": {"path": CLOSEOUT, "content": HEADER + "\nSynthetic body.\n"},
                }
            ],
            stopReason="toolUse",
        )
        call["timestamp"] = call["message"]["timestamp"] = at
        result = message(
            "closeout-result",
            "toolResult",
            [{"type": "text", "text": "Written successfully."}],
            toolCallId="closeout",
            toolName="write",
            isError=False,
        )
        result["timestamp"] = result["message"]["timestamp"] = ended
        records += [call, result]
    path.write_text("\n".join(json.dumps(record) for record in records) + "\n")
    return path


@pytest.mark.parametrize("agent", ["claude", "codex", "pi"])
def test_captured_structured_closeout_through_real_parser(tmp_path, agent):
    path = captured_fixture(tmp_path, agent)
    context = FileContext(str(path), agent + "-test/fixture", agent + "-test", agent, "test", None, "main")
    store = MemStore()
    parser, _ = load.parser_for(agent)
    run_file(parser, context, store)
    (call,) = [row for row in store.rows("tool_call") if row["call_uid"] == "closeout"]
    assert call["outcome"] == "ok" and call["ended_at"] is not None
    (prompt,) = [row for row in store.rows("message") if row["message_class"] == "human_prompt"]
    assert call["session"] == prompt["session"]
    (io,) = [row for row in store.rows("tool_io") if row["call_uid"] == "closeout"]
    observed = structured_closeout(io["tool_name"], io["input_text"], CLOSEOUT, CWD, io["ts"], call["ended_at"])
    assert observed is not None and observed.line1 == HEADER
    # Origin is explicit provenance, not a name found in a prompt/header/cwd.
    origin = _remote("git@example.org:example/project.git")
    result = project(RUN, [RUN], [], [observed], origins=[origin])
    assert result.end == call["ended_at"]
    assert result.fields == {"repo": "example/project", "loop": "loop14", "goal_sha256": DIGEST}

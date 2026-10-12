"""Synthetic session-header generator for contradictory and late pi lineage evidence."""

import json
from pathlib import Path

DIRECTORY_ROOT = "10000000-0000-4000-8000-000000000001"
HEADER_PARENT = "10000000-0000-4000-8000-000000000002"
ACTUAL_ROOT = "10000000-0000-4000-8000-000000000003"
CHILD = "10000000-0000-4000-8000-000000000004"
MISSING = "10000000-0000-4000-8000-000000000005"
FIXED = "10000000-0000-4000-8000-000000000006"
NESTED_PARENT = "10000000-0000-4000-8000-000000000007"
NESTED_CHILD = "10000000-0000-4000-8000-000000000008"
RUNS = [f"20000000-0000-4000-8000-{index:012d}" for index in range(1, 7)]
AT = "2026-10-08T00:00:00Z"


def basename(uid):
    return f"2026-10-08T00-00-00-000Z_{uid}"


def write(base: Path, relative: str, uid: str, parent=None):
    """Generate a retained synthetic v3 header, never a copied transcript."""
    path = base / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {"type": "session", "version": 3, "id": uid, "timestamp": AT, "cwd": "/tmp/synthetic"}
    if parent is not None:
        record["parentSession"] = parent
    path.write_text(json.dumps(record) + "\n")
    return path


def initial(base: Path):
    write(base, basename(DIRECTORY_ROOT) + ".jsonl", DIRECTORY_ROOT)
    root = basename(DIRECTORY_ROOT)
    foreign = f"/tmp/synthetic/{basename(HEADER_PARENT)}.jsonl"
    write(base, f"{root}/{RUNS[0]}/run-0/session.jsonl", CHILD, foreign)
    write(base, f"{root}/{RUNS[1]}/session.jsonl", MISSING)
    write(base, f"{root}/{RUNS[2]}/run-0/session.jsonl", FIXED, "/tmp/synthetic/session.jsonl")
    nested = f"{root}/{RUNS[3]}/run-0"
    write(base, f"{nested}/session.jsonl", NESTED_PARENT, foreign)
    write(base, f"{nested}/session/{RUNS[4]}/run-1/session.jsonl", NESTED_CHILD, "/tmp/synthetic/session.jsonl")


def parent_arrives(base: Path):
    write(
        base,
        f"{basename(DIRECTORY_ROOT)}/{RUNS[5]}/run-0/session.jsonl",
        HEADER_PARENT,
        f"/tmp/synthetic/{basename(ACTUAL_ROOT)}.jsonl",
    )


def root_arrives(base: Path):
    write(base, basename(ACTUAL_ROOT) + ".jsonl", ACTUAL_ROOT)


def path_spawn(base: Path):
    path = base / (basename(DIRECTORY_ROOT) + ".jsonl")
    records = [
        {
            "type": "message",
            "id": "dispatch",
            "timestamp": AT,
            "message": {
                "role": "assistant",
                "stopReason": "toolUse",
                "content": [
                    {
                        "type": "toolCall",
                        "id": "lineage-spawn",
                        "name": "subagent",
                        "arguments": {"agent": "mapper", "task": "Synthetic lineage check."},
                    }
                ],
            },
        },
        {
            "type": "message",
            "id": "result",
            "timestamp": AT,
            "message": {
                "role": "toolResult",
                "toolCallId": "lineage-spawn",
                "toolName": "subagent",
                "isError": False,
                "content": [],
                "details": {
                    "mode": "single",
                    "results": [
                        {
                            "agent": "mapper",
                            "exitCode": 0,
                            "sessionFile": f"/tmp/synthetic/{basename(DIRECTORY_ROOT)}/{RUNS[0]}/run-0/session.jsonl",
                        }
                    ],
                },
            },
        },
    ]
    with path.open("a") as handle:
        handle.write("".join(json.dumps(record) + "\n" for record in records))

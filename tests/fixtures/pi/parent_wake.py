"""Synthetic pi 1.0.4 / pi-subagents 0.76.1 idle-parent wake sequence.

No transcript content is copied. The notification, intervening custom state entries and plain
user-role wake mirror the retained record shapes; pi does not persist the extension input source.
"""

from __future__ import annotations


def records() -> list[dict]:
    rows = [
        {
            "type": "session",
            "version": 3,
            "id": "synthetic-parent-wake",
            "timestamp": "2026-10-07T00:00:00Z",
            "cwd": "/tmp/synthetic",
        }
    ]

    def add(kind, **fields):
        index = len(rows)
        rows.append(
            {
                "type": kind,
                "id": fields.pop("id", f"entry-{index}"),
                "timestamp": f"2026-10-07T00:00:{index:02d}Z",
                **fields,
            }
        )

    def user(eid, text):
        add("message", id=eid, message={"role": "user", "content": [{"type": "text", "text": text}]})

    def assistant(eid, stop="stop"):
        add(
            "message",
            id=eid,
            message={
                "role": "assistant",
                "stopReason": stop,
                "content": [{"type": "text", "text": "Synthetic response."}],
            },
        )

    user("launch", "Start the synthetic work.")
    assistant("waiting")
    for index, ctype in enumerate(("subagent-notify", "subagent-incremental-child-notify")):
        add("custom_message", id=f"notice-{index}", customType=ctype, content="Synthetic child update.", display=True)
        for _ in range(3):
            add("custom", customType="loop-wait-state", data={"v": 1})
        user(f"wake-{index}", "Subagent updates above.")
        assistant(f"response-{index}")
    user("genuine", "Please check the final result.")
    assistant("final")
    return rows

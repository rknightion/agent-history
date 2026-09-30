#!/usr/bin/env python3
"""Generate the synthetic pi session fixtures in this directory (deterministic; rerun after editing).

Every record is hand-built in the pi v3 session format (docs/session-format.md in pi-coding-agent)
and the shapes pi-subagents writes. No content comes from a real session. Layout:

  sessions/--example-project--/
    <ts>_<ASYNC>.jsonl          root: an async workflow (mapper + lane-worker), a WAITING turn end,
                                woken by child notifications; status, wait and model-retry calls
    <ts>_<ASYNC>/<dir>/run-0/session.jsonl   its two children
    <ts>_<FAILED>.jsonl         five failed model calls (stopReason error) with context edits
    <ts>_<LISTED>.jsonl         a `subagent {action: list}` call
    <ts>_<LOOP>.jsonl           a loop root: launch prompt, loop-watch, loop-wake, loop-continuation,
                                a thinking-level change and a manual compaction
    subagent-artifacts/         pi-subagents copies: transcripts pair run id and response id
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
SLUG = "--example-project--"
CWD = "/home/tester/example"

ASYNC = "01900000-0000-7000-8000-00000000a001"
FAILED = "01900000-0000-7000-8000-00000000a002"
LISTED = "01900000-0000-7000-8000-00000000a003"
LOOP = "01900000-0000-7000-8000-00000000a004"
MAPPER_UID = "01900000-0000-7000-8000-00000000b001"
WORKER_UID = "01900000-0000-7000-8000-00000000b002"
WORKFLOW = "5f000000-0000-4000-8000-000000000001"
MAPPER_RUN = "5f000000-0000-4000-8000-000000000002"
WORKER_RUN = "5f000000-0000-4000-8000-000000000003"
MAPPER_DIR = "6e000000-0000-4000-8000-000000000001"
WORKER_DIR = "6e000000-0000-4000-8000-000000000002"


class Clock:
    def __init__(self, start: str):
        self.t = datetime.fromisoformat(start).replace(tzinfo=timezone.utc)

    def tick(self, ms: int = 1000) -> str:
        self.t += timedelta(milliseconds=ms)
        return self.t.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def base_name(start: str, uid: str) -> str:
    """pi's session file stem: <ISO timestamp with : and . as -><Z>_<session id>."""
    return f"{start[:19].replace(':', '-')}-000Z_{uid}"


class Session:
    def __init__(self, uid: str, start: str, model: str, effort: str, provider: str = "example"):
        self.clock = Clock(start)
        self.n = 0
        self.model = model
        self.records: list[dict] = []
        at = self.clock.tick(0)
        self.records.append({"type": "session", "version": 3, "id": uid, "timestamp": at, "cwd": CWD})
        self.add({"type": "model_change", "provider": provider, "modelId": model})
        self.add({"type": "thinking_level_change", "thinkingLevel": effort})

    def eid(self) -> str:
        self.n += 1
        return f"e{self.n:04d}"

    def add(self, record: dict, ms: int = 200) -> dict:
        record = {
            "type": record.pop("type"),
            "id": self.eid(),
            "parentId": None,
            "timestamp": self.clock.tick(ms),
            **record,
        }
        self.records.append(record)
        return record

    def user(self, text: str) -> None:
        self.add({"type": "message", "message": {"role": "user", "content": [{"type": "text", "text": text}]}})

    def assistant(
        self,
        content: list,
        stop: str,
        response: str,
        usage: dict | None = None,
        error: str | None = None,
        ms: int = 2000,
    ) -> None:
        message = {
            "role": "assistant",
            "content": content,
            "provider": "example",
            "model": self.model,
            "responseId": response,
            "stopReason": stop,
            "usage": usage or {"input": 900, "output": 30, "cacheRead": 0, "cacheWrite": 0, "totalTokens": 930},
        }
        if error:
            message["errorMessage"] = error
        self.add({"type": "message", "message": message}, ms)

    def result(
        self, call_id: str, tool: str, text: str, details: dict | None = None, error: bool = False, ms: int = 300
    ) -> None:
        message = {
            "role": "toolResult",
            "toolCallId": call_id,
            "toolName": tool,
            "content": [{"type": "text", "text": text}],
            "isError": error,
        }
        if details is not None:
            message["details"] = details
        self.add({"type": "message", "message": message}, ms)

    def custom(self, kind: str, text: str) -> None:
        self.add({"type": "custom_message", "customType": kind, "content": text, "display": True})

    def write(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(r, separators=(",", ":")) + "\n" for r in self.records))


def call(call_id: str, name: str, arguments: dict) -> dict:
    return {"type": "toolCall", "id": call_id, "name": name, "arguments": arguments}


def text(value: str) -> dict:
    return {"type": "text", "text": value}


def usage(inp: int, out: int, cache: int = 0, reasoning: int | None = None) -> dict:
    u = {"input": inp, "output": out, "cacheRead": cache, "cacheWrite": 0, "totalTokens": inp + out + cache}
    if reasoning is not None:
        u["reasoning"] = reasoning
    return u


def async_root(root: Path) -> tuple[str, str]:
    start = "2026-09-01T10:00:00"
    s = Session(ASYNC, start, "model-large", "medium")
    s.user("Map the example repository, then fix the failing test.")
    s.assistant(
        [call("call-status", "bash", {"command": "git status --short"})],
        "toolUse",
        "resp_root_01",
        usage(1500, 56, 0, 31),
    )
    s.result("call-status", "bash", " M src/example.py\n")
    s.assistant(
        [
            call(
                "call-wf",
                "subagent",
                {
                    "workflowScript": "workflow.js",
                    "async": True,
                    "tasks": [
                        {"agent": "mapper", "task": "Map the repository layout."},
                        {"agent": "lane-worker", "task": "Fix the failing test."},
                    ],
                },
            )
        ],
        "toolUse",
        "resp_root_02",
        usage(200, 80, 1500),
    )
    s.result(
        "call-wf",
        "subagent",
        f"Workflow started: {WORKFLOW}",
        {"mode": "async", "runId": WORKFLOW, "asyncId": WORKFLOW, "results": []},
    )
    s.custom(
        "subagent-incremental-child-notify",
        f"Workflow run: {WORKFLOW}\nChild run: {MAPPER_RUN}\nWorkflow child completed: **mapper**",
    )
    s.assistant(
        [call("call-wait", "subagent", {"action": "wait", "timeoutMs": 1000})],
        "toolUse",
        "resp_root_03",
        usage(120, 20, 1700),
    )
    s.result("call-wait", "subagent", "Still running.", {"mode": "management", "results": []}, ms=1200)
    s.assistant(
        [text(f"WAITING: workflow {WORKFLOW} until its children report.")], "stop", "resp_root_04", usage(90, 25, 1850)
    )
    s.custom(
        "subagent-incremental-child-notify",
        f"Workflow run: {WORKFLOW}\nChild run: {WORKER_RUN}\nWorkflow child completed: **lane-worker**",
    )
    s.custom(
        "subagent-notify",
        f"Workflow run: {WORKFLOW}\nChild runs: mapper={MAPPER_RUN} (completed), lane-worker={WORKER_RUN} (completed)",
    )
    s.assistant([call("call-wait2", "subagent", {"action": "wait"})], "toolUse", "resp_root_05", usage(300, 15, 1900))
    s.result(
        "call-wait2",
        "subagent",
        "Children finished.",
        {"mode": "management", "results": [{"runId": MAPPER_RUN, "status": "completed"}]},
    )
    s.assistant(
        [text("Retrying the summary.")],
        "error",
        "resp_root_06",
        usage(0, 0),
        error="upstream_request_timeout: the upstream did not answer",
    )
    s.assistant([text("Both children finished; the test passes.")], "stop", "resp_root_07", usage(80, 40, 2100))
    name = base_name(start, ASYNC)
    s.write(root / f"{name}.jsonl")

    mapper = Session(MAPPER_UID, "2026-09-01T10:00:10", "model-small", "medium")
    mapper.user("Map the repository layout.")
    mapper.assistant([call("m-ls", "bash", {"command": "ls -la"})], "toolUse", "resp_mapper_01", usage(700, 12))
    mapper.result("m-ls", "bash", "total 8\n-rw-r--r-- 1 tester tester 12 README.md\n")
    mapper.assistant(
        [text("`ls -la` shows one README and a src directory.")], "stop", "resp_mapper_02", usage(60, 18, 700)
    )
    mapper.write(root / name / MAPPER_DIR / "run-0" / "session.jsonl")

    worker = Session(WORKER_UID, "2026-09-01T10:00:12", "model-small", "max")
    worker.user("Fix the failing test.")
    worker.assistant(
        [call("w-edit", "edit", {"path": "src/example.py", "edits": [{"oldText": "return 1", "newText": "return 2"}]})],
        "toolUse",
        "resp_worker_01",
        usage(800, 30),
    )
    worker.result("w-edit", "edit", "Edited src/example.py")
    worker.user("Also run the tests.")
    worker.assistant([text("Fixed the test.")], "stop", "resp_worker_02", usage(40, 10, 800))
    worker.write(root / name / WORKER_DIR / "run-0" / "session.jsonl")

    artifacts = root / "subagent-artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    for run, agent, response in (
        (MAPPER_RUN, "mapper", "resp_mapper_01"),
        (WORKER_RUN, "lane-worker", "resp_worker_01"),
    ):
        (artifacts / f"{run}_{agent}_transcript.jsonl").write_text(
            json.dumps(
                {
                    "version": 1,
                    "recordType": "message",
                    "runId": run,
                    "agent": agent,
                    "timestamp": "2026-09-01T10:00:20.000Z",
                    "message": {"role": "assistant", "responseId": response},
                },
                separators=(",", ":"),
            )
            + "\n"
        )
        (artifacts / f"{run}_{agent}_meta.json").write_text(json.dumps({"runId": run, "agent": agent}) + "\n")
        (artifacts / f"{run}_{agent}_output.md").write_text("Synthetic output.\n")
    return name, start


def failed_root(root: Path) -> None:
    start = "2026-09-01T09:00:00"
    s = Session(FAILED, start, "model-large", "medium")
    s.user("Summarise the build log.")
    errors = ["upstream_request_timeout: the upstream did not answer"] * 4 + ["Request failed (400): bad request"]
    for index, error in enumerate(errors):
        s.assistant([], "error", f"resp_failed_{index:02d}", usage(0, 0), error=error)
        if index < 4:
            s.add({"type": "context_edit", "targetId": f"e{s.n:04d}", "replacement": None})
    s.write(root / f"{base_name(start, FAILED)}.jsonl")


def listed_root(root: Path) -> None:
    start = "2026-09-01T09:30:00"
    s = Session(LISTED, start, "model-large", "medium")
    s.user("Which agents are available?")
    s.assistant([call("call-list", "subagent", {"action": "list"})], "toolUse", "resp_list_01", usage(500, 10))
    s.result("call-list", "subagent", "mapper, lane-worker", {"mode": "management"})
    s.assistant([text("Two agents are available.")], "stop", "resp_list_02", usage(60, 12, 500))
    s.write(root / f"{base_name(start, LISTED)}.jsonl")


def loop_root(root: Path) -> None:
    start = "2026-09-01T11:00:00"
    s = Session(LOOP, start, "model-large", "medium")
    s.add({"type": "message", "message": {"role": "system", "content": "You are a coding agent."}})
    s.user("You are the root of the example loop. Write codex/report-example-loop1.md when done.")
    s.assistant([call("l-echo", "bash", {"command": "echo hi"})], "toolUse", "resp_loop_01", usage(1000, 10))
    s.result("l-echo", "bash", "hi\n")
    s.assistant(
        [call("l-watch", "watch_start", {"command": "sleep 1; exit 0", "deadline_s": 30, "label": "w1"})],
        "toolUse",
        "resp_loop_02",
        usage(50, 20, 1000),
    )
    s.result("l-watch", "watch_start", "watch-id=w1")
    s.assistant([text("Watcher started.\nWAITING: w1")], "stop", "resp_loop_03", usage(40, 12, 1050))
    s.custom("loop-watch", "WATCH w1: phase=done exit_code=0 deadline_hit=false")
    s.assistant(
        [call("l-wake", "wake_at", {"at": "2026-09-01T11:05:00.000Z", "reason": "check-later"})],
        "toolUse",
        "resp_loop_04",
        usage(60, 14, 1100),
    )
    s.result("l-wake", "wake_at", "timer-id=t1")
    s.assistant([text("Timer armed.\nWAITING: check-later")], "stop", "resp_loop_05", usage(30, 10, 1160))
    s.custom("loop-wake", "WAKE t1: check-later")
    s.assistant([text("Woke; nothing left to do.")], "stop", "resp_loop_06", usage(30, 9, 1190))
    s.custom("loop-continuation", "A message with no tool call ends your turn; continue the loop or report.")
    s.assistant([text("PAUSED: example complete")], "stop", "resp_loop_07", usage(45, 8, 1220))
    s.add({"type": "thinking_level_change", "thinkingLevel": "high"})
    s.add(
        {
            "type": "compaction",
            "summary": "Example compaction summary: ran echo, a watcher and a wake.",
            "firstKeptEntryId": "e0005",
            "tokensBefore": 2400,
            "fromHook": False,
            "usage": usage(1000, 20),
        }
    )
    s.user("One more turn after compaction.")
    s.assistant([text("PAUSED: after compaction")], "stop", "resp_loop_08", usage(300, 9, 200))
    s.write(root / f"{base_name(start, LOOP)}.jsonl")


def main() -> None:
    sessions = HERE / "sessions"
    if sessions.exists():
        shutil.rmtree(sessions)
    root = sessions / SLUG
    async_root(root)
    failed_root(root)
    listed_root(root)
    loop_root(root)
    print(f"wrote {sum(1 for p in sessions.rglob('*') if p.is_file())} files under {sessions}")


if __name__ == "__main__":
    main()

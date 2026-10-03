"""Synthetic Codex and Claude transcript builders and a harness over the public efficiency collector.

Every record, path, thread id and model name here is invented. The harness drives
`EfficiencyCollector.collect()` with a pinned clock, so a test reads the samples a real
collection would emit, including the persisted state file.
"""

from __future__ import annotations

import json
import re
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from agent_history.config import parse_config
from agent_history.metrics.efficiency import EfficiencyCollector

CODEX = "codex-local"
CLAUDE = "claude-local"

# Efficiency series that carry no loop label: parser health, account-wide quota and the loop map itself.
UNLOOPED = re.compile(
    r"agent_efficiency_(tracked_files|baseline_timestamp_seconds|malformed_records_total|rate_limit_.*|loop_map_.*|loop_labels)$"
)


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def codex(ts: float, kind: str, payload: dict, ordinal: int = 0) -> str:
    return (
        json.dumps({"timestamp": iso(ts), "ordinal": ordinal, "type": kind, "payload": payload}, separators=(",", ":"))
        + "\n"
    )


def claude(ts: float, kind: str, message: dict | None = None, **extra: object) -> str:
    record: dict[str, object] = {"parentUuid": None, "isSidechain": False, "cwd": "/synthetic/cwd", "type": kind}
    if message is not None:
        record["message"] = message
    record.update(extra)
    record["timestamp"] = iso(ts)
    return json.dumps(record, separators=(",", ":")) + "\n"


def usage(total: int, cached: int, output: int) -> dict:
    return {
        "input_tokens": total,
        "cached_input_tokens": cached,
        "cache_write_input_tokens": 0,
        "output_tokens": output,
        "reasoning_output_tokens": 0,
        "total_tokens": total + output,
    }


def codex_meta(ts: float, thread: str, *, parent: str | None = None, cwd: str = "/synthetic/cwd") -> str:
    payload: dict[str, object] = {
        "id": thread,
        "session_id": thread,
        "cwd": cwd,
        "thread_source": "user",
        "source": "cli",
    }
    if parent:
        payload["source"] = {"subagent": {"thread_spawn": {"parent_thread_id": parent, "depth": 1}}}
        payload["thread_source"] = "subagent"
        payload["parent_thread_id"] = parent
    return codex(ts, "session_meta", payload)


def record(ts: float, thread: str | None = None, total: int = 100, cached: int = 0, output: int = 1) -> str:
    """A Codex token_usage_record: one model call."""
    payload: dict[str, object] = {"usage": usage(total, cached, output)}
    if thread:
        payload["thread_id"] = thread
    return codex(ts, "token_usage_record", payload)


def exec_fn(ts: float, call_id: str, cmd: str) -> str:
    """A Codex exec_command function call."""
    arguments = json.dumps({"cmd": cmd, "yield_time_ms": 1000})
    return codex(
        ts,
        "response_item",
        {"type": "function_call", "name": "exec_command", "call_id": call_id, "arguments": arguments},
    )


def stdin_fn(ts: float, call_id: str, session: int) -> str:
    """A Codex empty write_stdin poll."""
    arguments = json.dumps({"session_id": session, "chars": "", "yield_time_ms": 1000})
    return codex(
        ts,
        "response_item",
        {"type": "function_call", "name": "write_stdin", "call_id": call_id, "arguments": arguments},
    )


def fn_out(ts: float, call_id: str, text: str) -> str:
    return codex(ts, "response_item", {"type": "function_call_output", "call_id": call_id, "output": text})


def exited(code: int, body: str = "") -> str:
    return f"Chunk ID: a1b2c3\nWall time: 1.0000 seconds\nProcess exited with code {code}\nOriginal token count: 1\nOutput:\n{body}"


def running(session: int, body: str = "") -> str:
    return f"Chunk ID: a1b2c3\nWall time: 1.0000 seconds\nProcess running with session ID {session}\nOriginal token count: 1\nOutput:\n{body}"


def exec_js(ts: float, call_id: str, js: str) -> str:
    """A Codex code-mode exec cell."""
    return codex(ts, "response_item", {"type": "custom_tool_call", "name": "exec", "call_id": call_id, "input": js})


def js_out(ts: float, call_id: str, result: object, status: str = "Script completed") -> str:
    text = result if isinstance(result, str) else json.dumps(result)
    output = [
        {"type": "input_text", "text": f"{status}\nWall time 1.0 seconds\nOutput:\n"},
        {"type": "input_text", "text": text},
    ]
    return codex(ts, "response_item", {"type": "custom_tool_call_output", "call_id": call_id, "output": output})


def spawn_fn(ts: float, call_id: str, **arguments: object) -> str:
    payload = json.dumps({"task_name": "t", "message": "m", **arguments})
    return codex(
        ts, "response_item", {"type": "function_call", "name": "spawn_agent", "call_id": call_id, "arguments": payload}
    )


def wait_agent(ts: float, call_id: str, timeout_ms: int = 1000) -> str:
    arguments = json.dumps({"timeout_ms": timeout_ms})
    return codex(
        ts, "response_item", {"type": "function_call", "name": "wait_agent", "arguments": arguments, "call_id": call_id}
    )


def waited(ts: float, call_id: str, timed_out: bool) -> str:
    return fn_out(ts, call_id, json.dumps({"message": "m", "timed_out": timed_out}))


def user_item(ts: float, *texts: str, image: bool = False) -> str:
    """A Codex response_item message with role user, one input_text item per text."""
    content: list[dict] = [{"type": "input_text", "text": text} for text in texts]
    if image:
        content.append({"type": "input_image", "image_url": "data:image/png;base64,AAAA"})
    return codex(ts, "response_item", {"type": "message", "id": f"msg_{ts:.3f}", "role": "user", "content": content})


def cr_line(kind: str, **fields: object) -> str:
    return json.dumps({"type": kind, **fields}, separators=(",", ":"))


def claude_tool(
    ts: float, mid: str, tool_id: str, name: str, arguments: dict, model: str = "claude-test", **message: object
) -> str:
    body = {"id": mid, "model": model, "usage": {"input_tokens": 1, "output_tokens": 1}, **message}
    body["content"] = [{"type": "tool_use", "id": tool_id, "name": name, "input": arguments}]
    return claude(ts, "assistant", body)


def claude_result(ts: float, tool_id: str, content: str, *, is_error: bool = False, **extra: object) -> str:
    block: dict[str, object] = {"type": "tool_result", "tool_use_id": tool_id, "content": content}
    if is_error:
        block["is_error"] = True
    return claude(ts, "user", {"role": "user", "content": [block]}, **extra)


def with_loop(name: str, labels: dict[str, str]) -> dict[str, str]:
    """Efficiency lookups default to loop="none" unless a test names a loop."""
    if name.startswith("agent_efficiency_") and not UNLOOPED.match(name) and "loop" not in labels:
        return {**labels, "loop": "none"}
    return labels


def value(samples: dict, name: str, **labels: str) -> float:
    return samples.get((name, frozenset(with_loop(name, labels).items())), 0.0)


def select(samples: dict, name: str, **fixed: str) -> dict[frozenset, float]:
    """Samples of one series matching `fixed` labels, keyed by their remaining labels."""
    fixed = with_loop(name, fixed)
    found: dict[frozenset, float] = {}
    for (sample, labels), number in samples.items():
        plain = dict(labels)
        if sample == name and all(plain.get(key) == val for key, val in fixed.items()):
            found[frozenset((key, val) for key, val in plain.items() if key not in fixed)] = number
    return found


def total(samples: dict, name: str, **labels: str) -> float:
    return sum(
        number for (sample, key), number in samples.items() if sample == name and labels.items() <= dict(key).items()
    )


def calls(samples: dict) -> float:
    return total(samples, "agent_efficiency_llm_calls_total")


class _Connection:
    def __init__(self, roots, members):
        self.results = [roots, members]

    def execute(self, _sql):
        rows = self.results.pop(0)
        return type("Result", (), {"fetchall": lambda self: list(rows)})()


class Harness:
    """Synthetic namespace homes under one hot root, collected by the public EfficiencyCollector."""

    def __init__(self, root: Path, monkeypatch, baseline: float) -> None:
        self.root = root
        self.hot = root / "hot"
        self.state_dir = root / "state"
        self.state = self.state_dir / "efficiency-state.json"
        self.baseline = baseline
        self.monkeypatch = monkeypatch
        self.codex_dir = self.hot / CODEX / "sessions" / "2026" / "09" / "25"
        self.claude_dir = self.hot / CLAUDE / "projects" / "-example-project"
        self.codex_dir.mkdir(parents=True)
        self.claude_dir.mkdir(parents=True)

    def codex_file(self, name: str, lines: list[str]) -> Path:
        path = self.codex_dir / name
        path.write_text("".join(lines), encoding="utf-8")
        return path

    def claude_file(self, name: str, lines: list[str]) -> Path:
        path = self.claude_dir / name
        path.write_text("".join(lines), encoding="utf-8")
        return path

    def config(self, loop_dsn: str | None = None):
        sources = {home.name: str(home) for home in sorted(self.hot.iterdir()) if home.is_dir()}
        efficiency: dict[str, object] = {"baseline_ts": self.baseline}
        if loop_dsn:
            efficiency["loop_dsn"] = loop_dsn
        return parse_config({"sources": sources, "efficiency": efficiency})

    def families(
        self,
        now: float,
        loop_rows: tuple[list, list] | None = None,
        loop_error: Exception | None = None,
        **budget: object,
    ):
        """One collection at `now`. `loop_rows` is a catalogue answer; `loop_error` an unreachable catalogue.

        `budget` passes the collector's parse budget and its clock (`budget=`, `monotonic=`)."""
        self.monkeypatch.setattr("agent_history.metrics.efficiency.time.time", lambda: now)
        dsn = None
        if loop_rows is not None or loop_error is not None:
            dsn = "postgresql://reader@127.0.0.1:1/synthetic"

            @contextmanager
            def connect(*_args, **_kwargs):
                if loop_error is not None:
                    raise loop_error
                yield _Connection(*loop_rows)

            self.monkeypatch.setattr("agent_history.metrics.efficiency.telemetry.db_connect", connect)
        collector = EfficiencyCollector(self.config(dsn), self.state_dir, **budget)
        families = collector.collect()
        self.last_collector = collector
        return families

    def collect(self, now: float, **kwargs) -> dict[tuple[str, frozenset], float]:
        samples: dict[tuple[str, frozenset], float] = {}
        for family in self.families(now, **kwargs):
            for sample in family.samples:
                key = (sample.name or family.name, frozenset(sample.labels))
                assert key not in samples, f"duplicate series {key}"
                samples[key] = sample.value
        return samples

    def saved(self) -> dict:
        return json.loads(self.state.read_text(encoding="utf-8"))

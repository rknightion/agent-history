"""pi session parser for the agent-history catalogue (namespace pi-<profile>).

Pure: turns decoded pi v3 session JSONL records (docs/session-format.md in pi-coding-agent 0.87.1)
into model.py rows and keeps a small JSON-serialisable state. Rules, validated against live gpt-6
sessions and faux-provider sessions (synthetic shapes in tests/fixtures/pi):

Files (load.file_role)
  sessions/<cwd-slug>/<ts>_<session id>.jsonl                   main (a root or a standalone run)
  sessions/<cwd-slug>/<root base>/<run dir>/run-<i>/<x>.jsonl   subagent (pi-subagents child); a
      nested child adds `session/<run dir>/run-<j>/` per level below its parent's run dir.
  sessions/<cwd-slug>/subagent-artifacts/*_transcript.jsonl       structural run evidence only.
      PiArtifactParser reads runId, resolved agent-file name and responseId from the version-1
      copy. It emits no message or LLM rows, so child calls remain counted once.

Session identity
  SessionKey('pi', <header id>, ''). The header (`type: session`) is the first line; records before
  it are ignored with one `missing_session_header` issue. A child learns its lineage from its path
  only: root_session_uid = the id after the last `_` of the root base name (equal to the root's
  header id), parent_session_uid = the root for a first-level child (nested parents resolve in SQL
  through agent_path), agent_path = "<root uid>/<run dir>/run-<i>[/<run dir>/run-<j>...]",
  spawn_kind 'pi_subagent'. A display name is never evidence of agent type; an exactly linked
  parent spawn supplies the child type in the loader post-pass.
  The run dir is NOT the child's pi-subagents runId (5cfcc8f2... ran in 35c8deaf... in the fixture).
  An archived artifact transcript pairs the runId with the child's API responseId. Free-text
  notifications do not establish a run/path pair; neither display names nor timing infer type.

Turns (pi has no turn records)
  A turn is keyed by the entry id that started it. The agent is busy after an assistant message with
  stopReason toolUse, idle after stop/length/error/aborted. A user message while idle opens a turn
  (origin 'human' in a main session; 'subagent_brief' for a child's first, 'peer' later); while busy
  it is a queued_prompt inside the current turn. A custom_message while idle is a pending trigger
  that opens a turn at the next assistant message, with origin by customType: loop-watch ->
  loop_watch, loop-wake -> loop_wake, loop-continuation -> loop_continuation, subagent-notify and
  subagent-incremental-child-notify -> task_notification, else unknown. An assistant message while
  idle with no pending trigger continues the current turn (pi's automatic retry after an error).

Messages (event_uid "<session uid>:<entry id>[:suffix]")
  system messages -> system_prompt (sections joined), user -> human_prompt | queued_prompt (main) or
  agent_message (child), assistant text -> assistant_text, non-empty thinking -> reasoning, a child's
  assistant text on a `stop` -> also subagent_report (":report"), subagent `task`/`tasks[].task`
  arguments -> subagent_brief in the parent, compaction and branch_summary -> compaction_summary,
  custom_message -> hook_output (loop-*), task_notification_summary (subagent-*), else
  context_injection; detail {"source": customType}.

LLM calls
  One row per assistant message: response_id = message.responseId, else "pi:<uid>:<entry id>";
  input_uncached = usage.input (pi reports it without cache reads), cache_read = cacheRead,
  cache_write_5m = cacheWrite (pi has no TTL split; 0 stays 0 and cache_write_1h = 0, so loop
  aggregates and priced cost stay known), output (includes reasoning), reasoning; effort =
  the session's thinking level at that point. stopReason 'error' -> is_api_error, error_kind from
  errorMessage ("upstream_request_timeout: ..." -> upstream_request_timeout; "(400)" -> status 400).
  `usage` entries and compaction/branch_summary `usage` -> rows "pi:<uid>:<entry id>" with
  stop_reason "usage:<kind>" / "compaction" / "branch_summary".

Subagents
  The `subagent` tool call's meta carries its action, async flag and (from the result) mode and
  run_id (the workflow/async run id). A spawn row per child is keyed by the child's runId, taken from
  the completion texts (subagent-notify "Child runs: key=<runId> (status)" and
  subagent-incremental-child-notify "Child run: <runId>"), with workflow_id = the run id, which
  also names the spawning call (ToolCallRow.meta.run_id), and child_task_name = "<run dir>/run-<i>"
  when a child session path appears after that runId in the notify text (it can be truncated).
  Foreground results (details.results[].sessionFile) give spawn rows "<call id>:<index>".
  A single async run completes with "Background task completed|failed: **<agent>**" (opening the
  text) and a trailing "Retention-managed async directory: .../async-subagent-runs/<runId>" line;
  the run id selects the launching call and its spawn row (spawn_uid = call id) gains
  completion_status and completed_at. The "Session file:" line is not used (no run/path pairing).

Runtime entry
  `custom` entry `loop-pi-runtime` {v:1, variant, models:{<id>:{service_tier}}}: llm_call.service_tier
  of calls to that model in the same session; absent means NULL.

Known gaps: a pi `/fork` copy re-keys copied entries under the new session (entry ids are only
unique within a file).
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime
from typing import Any, Iterable

from .common import (BOUNDED_STATE_SECONDS, artifact_kind, as_int, as_str, cmd_verb, git_event_extras,
                     git_from_output, git_ops_from_command, json_size, linked_paths, parse_ts, prompt_origin,
                     ssh_target)
from .model import (ArtifactRow, AttachmentRow, CompactionRow, ContinuationRow, FileContext, FileTouchRow,
                    GitEventRow, LinePos, LlmCallRow, MessageRow, ParseIssueRow, RecordTypeRow, Row,
                    PiRunResponseRow, SessionEventRow, SessionKey, SessionRow, SubagentSpawnRow, ToolCallRow,
                    ToolIoRow, TurnRow)
from .parse_claude import _diff_counts, _nlines

AGENT = "pi"
PARSER_VERSION = "6"
PI_AGENT_FILES = frozenset({"mapper", "mapper-deep", "gate-runner", "lane-worker", "lane-worker-push",
                            "lane-worker-retry", "lane-worker-retry-push", "complex-worker", "complex-worker-push", "reviewer", "reviewer-high",
                            "security-reviewer", "rescue-sol", "rescue-astra"})
KNOWN_TYPES = {"session", "message", "model_change", "thinking_level_change", "usage", "compaction",
               "branch_summary", "custom", "custom_message", "label", "session_info", "context_edit"}
TERMINAL = {"stop", "length", "error", "aborted"}
TURN_STATUS = {"stop": "completed", "length": "completed", "error": "error", "aborted": "aborted"}
ORIGINS = {"loop-watch": "loop_watch", "loop-wake": "loop_wake", "loop-continuation": "loop_continuation",
           "subagent-notify": "task_notification", "subagent-incremental-child-notify": "task_notification"}
BUILTIN = {"read", "bash", "edit", "write", "grep", "find", "ls"}
TOUCH = {"read": "read", "write": "create", "edit": "edit"}
ARTIFACT_ACTION = {"read": "read", "write": "written", "edit": "edited"}
UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
WORKFLOW_RUN_RE = re.compile(rf"^Workflow run: ({UUID})\s*$", re.M)
CHILD_RUNS_RE = re.compile(r"^Child runs: (.+)$", re.M)
CHILD_RUN_ITEM_RE = re.compile(rf"(?:([\w.-]+)=)?({UUID})(?: \(([\w-]+)\))?")
CHILD_RUN_RE = re.compile(rf"^Child run: ({UUID})\s*$", re.M)
CHILD_DONE_RE = re.compile(r"^Workflow child (\w+): \*\*([^*]+)\*\*", re.M)
# a single async run's notify: "Background task completed|failed: **<agent>**", then the run's
# "Retention-managed async directory: .../async-subagent-runs/<runId>" line
ASYNC_DONE_RE = re.compile(r"Background task (completed|failed): \*\*([^*]+)\*\*")
ASYNC_DIR_RE = re.compile(rf"^Retention-managed async directory: .*/async-subagent-runs/({UUID})[ \t]*$", re.M)
# a child session path inside a notify: .../<root base>/<run dir>/run-<i>/<file>.jsonl
SESSION_PATH_RE = re.compile(rf"/({UUID})/(run-\d+)/[^/\"\s]+\.jsonl")
ERROR_KIND_RE = re.compile(r"^([a-z][a-z0-9_]{2,63}):")
ERROR_STATUS_RE = re.compile(r"\((\d{3})\)")


def _epoch(dt: datetime | None) -> float | None:
    return dt.timestamp() if dt is not None else None


def _dumps(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, default=str)


def _text(content: Any) -> str:
    """Text of a pi content value: a string, or text blocks joined."""
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    parts = [b["text"] for b in content if isinstance(b, dict) and b.get("type") == "text"
             and isinstance(b.get("text"), str) and b["text"].strip()]
    return "\n\n".join(p.strip() for p in parts)


def _images(content: Any) -> list[dict[str, Any]]:
    if not isinstance(content, list):
        return []
    out = []
    for index, block in enumerate(content):
        if isinstance(block, dict) and block.get("type") == "image":
            data = block.get("data")
            size = (len(data) * 3) // 4 if isinstance(data, str) else None
            out.append({"index": index, "type": "image", "mime": as_str(block.get("mimeType")), "bytes": size})
    return out


def lineage(rel_path: str) -> dict[str, Any]:
    """Root uid, depth, agent_path and run-dir tail of a session file from its path under sessions/."""
    parts = rel_path.split("/")
    try:
        start = parts.index("sessions")
    except ValueError:
        return {}
    rest = parts[start + 1:]
    if len(rest) < 2:
        return {}
    if len(rest) == 2:   # sessions/<slug>/<file>
        stem = rest[1].removesuffix(".jsonl")
        return {"root": stem.rsplit("_", 1)[-1], "depth": 0}
    base = rest[1]
    segs = [p for p in rest[2:-1] if p != "session"]
    if not segs or len(segs) % 2:
        return {}
    root = base.rsplit("_", 1)[-1]
    return {"root": root, "depth": len(segs) // 2, "path": "/".join([root, *segs]),
            "task": "/".join(segs[-2:])}


class PiParser:
    def __init__(self, ctx: FileContext, state: dict[str, Any]) -> None:
        self.ctx = ctx
        self.s: dict[str, Any] = {
            "sid": None, "cwd": None, "pre": False, "model": None, "provider": None, "effort": None,
            "cur": None, "cs": None, "busy": False, "pend": None, "users": 0, "last_iso": None,
            "calls": {}, "wf": {},
        }
        self.s.update(state or {})
        self.s.setdefault("tiers", {})   # model id -> service tier, from the session's loop-pi-runtime entry
        self.sub = ctx.file_role != "main"
        self.lin = lineage(ctx.rel_path)
        self._types: dict[tuple[str, str], list[Any]] = {}
        self._unknown: dict[str, int] = {}
        self._first: datetime | None = None
        self._last: datetime | None = None
        self._first_human: datetime | None = None
        self._last_human: datetime | None = None
        self._touched = False

    # --- protocol -----------------------------------------------------------------------------

    def state(self) -> dict[str, Any]:
        return self.s

    def line(self, record: dict[str, Any], pos: LinePos) -> Iterable[Row]:
        rows: list[Row] = []
        rtype = record.get("type") if isinstance(record.get("type"), str) else "?"
        message = record.get("message") if isinstance(record.get("message"), dict) else {}
        sub = (as_str(message.get("role")) if rtype == "message" else
               as_str(record.get("customType")) or as_str(record.get("kind")) or "")
        self._count(rtype, sub or "", message if rtype == "message" else record, pos)
        ts = parse_ts(record.get("timestamp"))
        if ts is not None:
            self.s["last_iso"] = ts.isoformat()
        else:
            ts = parse_ts(self.s.get("last_iso"))
        if self.s["sid"] is None:
            if rtype != "session" or not as_str(record.get("id")):
                if not self.s["pre"]:
                    self.s["pre"] = True
                    rows.append(ParseIssueRow(pos.byte_offset, "missing_session_header", pos.line_number,
                                              f"type={rtype}"))
                return rows
            return self._header(record, ts, pos)
        if rtype == "session":
            rows.append(ParseIssueRow(pos.byte_offset, "late_session_header", pos.line_number))
            return rows
        if ts is None:
            return rows
        self._seen(ts)
        eid = as_str(record.get("id")) or f"o{pos.byte_offset}"
        uid = f"{self.s['sid']}:{eid}"
        if rtype == "message":
            role = message.get("role")
            if role == "assistant":
                rows.extend(self._assistant(message, eid, uid, ts, pos))
            elif role == "user":
                rows.extend(self._user(message, eid, uid, ts, pos))
            elif role == "toolResult":
                rows.extend(self._tool_result(message, uid, ts, pos))
            elif role == "system":
                rows.extend(self._system(message, uid, ts, pos))
            elif role in {"custom", "bashExecution"}:
                rows.extend(self._custom_message(record | {"customType": message.get("customType") or role,
                                                           "content": message.get("content")}, eid, uid, ts, pos))
        elif rtype == "custom_message":
            rows.extend(self._custom_message(record, eid, uid, ts, pos))
        elif rtype == "model_change":
            rows.extend(self._model_change(record, uid, ts, pos))
        elif rtype == "thinking_level_change":
            rows.extend(self._thinking(record, uid, ts, pos))
        elif rtype == "usage":
            rows.extend(self._usage_row(record.get("usage"), f"pi:{uid}", as_str(record.get("model")),
                                        f"usage:{as_str(record.get('kind')) or 'unknown'}", ts, pos))
        elif rtype in {"compaction", "branch_summary"}:
            rows.extend(self._compaction(rtype, record, uid, ts, pos))
        elif rtype == "custom" and as_str(record.get("customType")) == "loop-pi-runtime":
            self._runtime(record.get("data"))
        elif rtype == "session_info":
            name = as_str(record.get("name"))
            if name:
                rows.append(SessionRow(session=self._key(), custom_title=name, title=name))
        elif rtype == "context_edit":
            rows.append(SessionEventRow(AGENT, uid, self._key(), ts, "context_edit", pos.byte_offset, self.s["cur"],
                                        "omit" if record.get("replacement") is None else "replace",
                                        {"target": as_str(record.get("targetId"))}))
        elif rtype == "label":
            rows.append(SessionEventRow(AGENT, uid, self._key(), ts, "label", pos.byte_offset, self.s["cur"],
                                        (as_str(record.get("label")) or "")[:120] or None,
                                        {"target": as_str(record.get("targetId"))}))
        return rows

    def flush(self) -> Iterable[Row]:
        rows: list[Row] = []
        fallback = parse_ts(self.s.get("last_iso"))
        for (rtype, sub), (count, keys, ts) in self._types.items():
            if ts or fallback:
                rows.append(RecordTypeRow(AGENT, rtype, sub, ts or fallback, count, keys, None))
        for detail, offset in self._unknown.items():
            rows.append(ParseIssueRow(offset, "unknown_type", None, detail))
        self._types, self._unknown = {}, {}
        key = self._key()
        if key is not None and self._touched:
            rows.append(SessionRow(session=key, first_event_at=self._first, last_event_at=self._last,
                                   first_human_at=self._first_human, last_human_at=self._last_human))
        self._first = self._last = self._first_human = self._last_human = None
        self._touched = False
        rows.extend(self._prune())
        return rows

    # --- helpers ------------------------------------------------------------------------------

    def _key(self) -> SessionKey:
        return SessionKey(AGENT, self.s["sid"], "")

    def _count(self, rtype: str, sub: str, record: dict[str, Any], pos: LinePos) -> None:
        ts = parse_ts(record.get("timestamp")) if isinstance(record.get("timestamp"), str) else None
        entry = self._types.get((rtype, sub))
        if entry is None:
            self._types[(rtype, sub)] = [1, ",".join(sorted(record.keys())), ts]
        else:
            entry[0] += 1
            if ts is not None:
                entry[2] = ts
        if rtype not in KNOWN_TYPES:
            self._unknown.setdefault(rtype, pos.byte_offset)

    def _seen(self, ts: datetime) -> None:
        self._touched = True
        if self._first is None or ts < self._first:
            self._first = ts
        if self._last is None or ts > self._last:
            self._last = ts

    def _human(self, ts: datetime) -> None:
        if self._first_human is None or ts < self._first_human:
            self._first_human = ts
        if self._last_human is None or ts > self._last_human:
            self._last_human = ts

    def _prune(self) -> list[Row]:
        rows: list[Row] = []
        now = _epoch(parse_ts(self.s.get("last_iso")))
        if now is None or self.s["sid"] is None:
            return rows
        horizon = now - BOUNDED_STATE_SECONDS
        for call_id, call in list(self.s["calls"].items()):
            if call["e"] < horizon:
                del self.s["calls"][call_id]
                rows.append(ToolCallRow(AGENT, call_id, self._key(), call["n"], call["bo"], outcome="no_result"))
        for name in ("wf",):
            for k, v in list(self.s[name].items()):
                if v.get("e", now) < horizon:
                    del self.s[name][k]
        return rows

    def _msg(self, uid: str, ts: datetime, role: str, cls: str, text: str, pos: LinePos, turn: str | None,
             model: str | None = None, origin: str | None = None, detail: dict[str, Any] | None = None) -> MessageRow:
        return MessageRow(AGENT, uid, self._key(), ts, role, cls, text, pos.byte_offset, pos.byte_length,
                          pos.line_number, turn, model, self.sub, origin, detail)

    # --- header, settings ---------------------------------------------------------------------

    def _header(self, record: dict[str, Any], ts: datetime | None, pos: LinePos) -> list[Row]:
        s = self.s
        s["sid"] = record["id"]
        s["cwd"] = as_str(record.get("cwd"))
        key = self._key()
        lin = self.lin if self.sub else {}
        depth = lin.get("depth")
        parent_file = as_str(record.get("parentSession"))
        parent_uid = parent_file.rsplit("/", 1)[-1].removesuffix(".jsonl").rsplit("_", 1)[-1] if parent_file else None
        if ts is not None:
            self._seen(ts)
        rows: list[Row] = [SessionRow(
            session=key, byte_offset=pos.byte_offset, is_subagent=self.sub,
            root_session_uid=lin.get("root") if self.sub else None,
            parent_session_uid=lin.get("root") if self.sub and depth == 1 else None,
            spawn_kind="pi_subagent" if self.sub else None, spawn_depth=depth if self.sub else None,
            agent_path=lin.get("path"), forked_from_uid=parent_uid if not self.sub else None,
            cwd=s["cwd"], entrypoint="pi",
            first_event_at=ts, last_event_at=ts)]
        if parent_uid and not self.sub and ts is not None:
            rows.append(ContinuationRow(AGENT, s["sid"], parent_uid, "fork", key, ts, pos.byte_offset,
                                        "parentSession"))
        return rows

    def _runtime(self, data: Any) -> None:
        """loop-pi-runtime {v:1, variant, models:{<id>:{service_tier}}}: tier per model for this session."""
        models = data.get("models") if isinstance(data, dict) and data.get("v") == 1 else None
        if not isinstance(models, dict):
            return
        self.s["tiers"] = {mid: tier[:32] for mid, cfg in models.items() if isinstance(mid, str)
                           and isinstance(cfg, dict) and (tier := as_str(cfg.get("service_tier")))}

    def _model_change(self, record: dict[str, Any], uid: str, ts: datetime, pos: LinePos) -> list[Row]:
        model, provider = as_str(record.get("modelId")), as_str(record.get("provider"))
        rows: list[Row] = []
        if model and self.s["model"] and model != self.s["model"]:
            rows.append(SessionEventRow(AGENT, uid, self._key(), ts, "model_switch", pos.byte_offset, self.s["cur"],
                                        model, {"from": self.s["model"], "provider": provider}))
        self.s["model"] = model or self.s["model"]
        self.s["provider"] = provider or self.s["provider"]
        if provider:
            rows.append(SessionRow(session=self._key(), model_provider=provider))
        return rows

    def _thinking(self, record: dict[str, Any], uid: str, ts: datetime, pos: LinePos) -> list[Row]:
        level = as_str(record.get("thinkingLevel"))
        rows: list[Row] = []
        if level and self.s["effort"] and level != self.s["effort"]:
            rows.append(SessionEventRow(AGENT, uid, self._key(), ts, "effort_change", pos.byte_offset, self.s["cur"],
                                        level, {"from": self.s["effort"]}))
        self.s["effort"] = level or self.s["effort"]
        return rows

    # --- turns --------------------------------------------------------------------------------

    def _open_turn(self, key: str, origin: str, ts: datetime, pos: LinePos, model: str | None = None) -> TurnRow:
        self.s["cur"], self.s["cs"], self.s["busy"] = key, ts.isoformat(), True
        return TurnRow(self._key(), key, pos.byte_offset, origin=origin, started_at=ts, status="open",
                       model=model or self.s["model"], effort=self.s["effort"])

    # --- messages -----------------------------------------------------------------------------

    def _system(self, m: dict[str, Any], uid: str, ts: datetime, pos: LinePos) -> list[Row]:
        sections = m.get("sections") if isinstance(m.get("sections"), dict) else {}
        body = [m["content"]] if isinstance(m.get("content"), str) and m["content"].strip() else []
        body += [v for v in sections.values() if isinstance(v, str) and v.strip()]
        names = lambda k: [t.get("name") for t in m.get(k) or [] if isinstance(t, dict)]  # noqa: E731
        detail = {"source": "pi_system", "sections": sorted(sections), "tools_added": names("toolsAdded"),
                  "tools_removed": names("toolsRemoved")}
        return [self._msg(uid, ts, "system", "system_prompt", "\n\n".join(body), pos, self.s["cur"],
                          detail={k: v for k, v in detail.items() if v})]

    def _user(self, m: dict[str, Any], eid: str, uid: str, ts: datetime, pos: LinePos) -> list[Row]:
        rows: list[Row] = []
        text = _text(m.get("content"))
        self.s["users"] += 1
        idle = self.s["cur"] is None or not self.s["busy"]
        if idle:
            origin = "human" if not self.sub else ("subagent_brief" if self.s["users"] == 1 else "peer")
            self.s["pend"] = None
            rows.append(self._open_turn(eid, origin, ts, pos))
        turn = self.s["cur"]
        if self.sub:
            rows.append(self._msg(uid, ts, "user", "agent_message", text, pos, turn,
                                  detail={"source": "subagent_task" if self.s["users"] == 1 else "parent_message"}))
        else:
            self._human(ts)
            rows.append(self._msg(uid, ts, "user", "human_prompt" if idle else "queued_prompt", text, pos, turn,
                                  origin=prompt_origin(text)))
        for img in _images(m.get("content")):
            rows.append(AttachmentRow(AGENT, f"{uid}:att:{img['index']}", self._key(), ts, pos.byte_offset, "image",
                                      "prompt", event_uid=uid, turn_key=turn, mime=img["mime"],
                                      size_bytes=img["bytes"]))
        return rows

    def _custom_message(self, record: dict[str, Any], eid: str, uid: str, ts: datetime, pos: LinePos) -> list[Row]:
        ctype = as_str(record.get("customType")) or "custom"
        rows: list[Row] = []
        if self.s["cur"] is None or not self.s["busy"]:
            if self.s["pend"] is None:
                self.s["pend"] = {"k": eid, "o": ORIGINS.get(ctype, "unknown"), "ts": ts.isoformat()}
            turn = self.s["pend"]["k"]
        else:
            turn = self.s["cur"]
        text = _text(record.get("content"))
        cls = ("hook_output" if ctype.startswith("loop-") else
               "task_notification_summary" if ctype.startswith("subagent-") else "context_injection")
        detail: dict[str, Any] = {"source": ctype}
        if isinstance(record.get("details"), dict) and len(json.dumps(record["details"], default=str)) < 2000:
            detail["details"] = record["details"]
        if text:
            rows.append(self._msg(uid, ts, "user", cls, text, pos, turn, detail=detail))
        if ctype.startswith("subagent-") and text:
            rows.extend(self._notify(ctype, text, turn, ts, pos))
        return rows

    def _assistant(self, m: dict[str, Any], eid: str, uid: str, ts: datetime, pos: LinePos) -> list[Row]:
        rows: list[Row] = []
        model = as_str(m.get("model")) or self.s["model"]
        pend = self.s["pend"]
        if self.s["cur"] is None or (not self.s["busy"] and pend is not None):
            if pend is not None:
                rows.append(self._open_turn(pend["k"], pend["o"], parse_ts(pend["ts"]) or ts, pos, model))
            else:
                rows.append(self._open_turn(eid, "unknown", ts, pos, model))
            self.s["pend"] = None
        turn = self.s["cur"]
        stop = as_str(m.get("stopReason"))
        self.s["busy"] = stop not in TERMINAL
        response_id = as_str(m.get("responseId")) or f"pi:{uid}"
        rows.extend(self._usage_row(m.get("usage"), response_id, model, stop, ts, pos, turn,
                                    error=as_str(m.get("errorMessage")) if stop == "error" else None))
        if stop == "error":
            rows.append(SessionEventRow(AGENT, uid, self._key(), ts, "api_error", pos.byte_offset, turn,
                                        self._error_kind(as_str(m.get("errorMessage")))[0]))
        elif stop == "aborted":
            rows.append(SessionEventRow(AGENT, uid, self._key(), ts, "interrupt", pos.byte_offset, turn, "aborted"))
        if stop in TERMINAL:
            started = parse_ts(self.s.get("cs"))
            rows.append(TurnRow(self._key(), turn, pos.byte_offset, completed_at=ts, status=TURN_STATUS[stop],
                                duration_ms=int((ts - started).total_seconds() * 1000) if started else None,
                                abort_reason="aborted" if stop == "aborted" else None))
        content = m.get("content") if isinstance(m.get("content"), list) else []
        text = _text(content)
        if text:
            rows.append(self._msg(uid, ts, "assistant", "assistant_text", text, pos, turn, model))
            for path in linked_paths(text):
                rows.append(ArtifactRow(AGENT, uid, self._key(), ts, artifact_kind(path), "linked", path,
                                        "assistant_link", pos.byte_offset, turn,
                                        display_name=path.rsplit("/", 1)[-1] or None))
            if self.sub and stop == "stop":
                rows.append(self._msg(f"{uid}:report", ts, "assistant", "subagent_report", text, pos, turn, model))
        for index, block in enumerate(content):
            if not isinstance(block, dict):
                continue
            if block.get("type") == "thinking" and isinstance(block.get("thinking"), str) and block["thinking"].strip():
                rows.append(self._msg(f"{uid}:think:{index}", ts, "assistant", "reasoning", block["thinking"].strip(),
                                      pos, turn, model))
            elif block.get("type") == "toolCall":
                rows.extend(self._tool_call(block, uid, response_id, ts, pos, turn, model))
        return rows

    @staticmethod
    def _error_kind(message: str | None) -> tuple[str, int | None]:
        if not message:
            return "error", None
        status = ERROR_STATUS_RE.search(message[:200])
        match = ERROR_KIND_RE.match(message)
        kind = match.group(1) if match else ("http_error" if status else "error")
        return kind, int(status.group(1)) if status else None

    def _usage_row(self, usage: Any, response_id: str, model: str | None, stop: str | None, ts: datetime,
                   pos: LinePos, turn: str | None = None, error: str | None = None) -> list[Row]:
        if not isinstance(usage, dict):
            return []
        kind, status = self._error_kind(error) if error is not None or stop == "error" else (None, None)
        model = model or self.s["model"]
        write = as_int(usage.get("cacheWrite"))   # pi has no TTL split: all of it prices as the 5m write
        return [LlmCallRow(AGENT, response_id, self._key(), ts, pos.byte_offset,
                           turn_key=turn if turn is not None else self.s["cur"], model=model,
                           stop_reason=stop, input_uncached=as_int(usage.get("input")),
                           cache_read=as_int(usage.get("cacheRead")),
                           cache_write_5m=write, cache_write_1h=None if write is None else 0,
                           output=as_int(usage.get("output")), service_tier=self.s["tiers"].get(model),
                           reasoning=as_int(usage.get("reasoning")), effort=self.s["effort"],
                           is_api_error=stop == "error", error_kind=kind, api_error_status=status,
                           is_sidechain=self.sub, line_count=1)]

    def _compaction(self, rtype: str, record: dict[str, Any], uid: str, ts: datetime, pos: LinePos) -> list[Row]:
        turn = self.s["cur"]
        rows: list[Row] = []
        if rtype == "compaction":
            rows.append(CompactionRow(AGENT, uid, self._key(), ts, pos.byte_offset, turn,
                                      trigger="extension" if record.get("fromHook") else None,
                                      pre_tokens=as_int(record.get("tokensBefore"))))
        summary = record.get("summary")
        if isinstance(summary, str) and summary.strip():
            rows.append(self._msg(uid, ts, "user", "compaction_summary", summary.strip(), pos, turn,
                                  detail={"source": rtype}))
        rows.extend(self._usage_row(record.get("usage"), f"pi:{uid}", self.s["model"], rtype, ts, pos, turn))
        return rows

    # --- tools --------------------------------------------------------------------------------

    def _tool_call(self, block: dict[str, Any], uid: str, response_id: str, ts: datetime, pos: LinePos,
                   turn: str | None, model: str | None) -> list[Row]:
        call_id = as_str(block.get("id"))
        name = as_str(block.get("name")) or "?"
        if not call_id:
            return [ParseIssueRow(pos.byte_offset, "missing_call_id", pos.line_number)]
        args = block.get("arguments") if isinstance(block.get("arguments"), dict) else {}
        key = self._key()
        meta: dict[str, Any] = {}
        background = None
        if name == "bash":
            verb = cmd_verb(args.get("command"))
            if verb:
                meta["cmd_verb"] = verb
            remote = ssh_target(args.get("command"))
            if remote:
                meta["target_host"] = remote[0]
                if remote[1]:
                    meta["remote_verb"] = remote[1]
        elif name == "subagent":
            meta = {k: args[k] for k in ("action", "agent", "async") if isinstance(args.get(k), (str, bool))}
            if "workflowScript" in args:
                meta["workflow"] = True
            background = args.get("async") is True or ("workflowScript" in args and args.get("async") is not False)
        elif name in {"watch_start", "wake_at"}:
            background = True
        self.s["calls"][call_id] = {"n": name, "e": ts.timestamp(), "bo": pos.byte_offset, "t": turn, "m": meta,
                                    "a": {k: args.get(k) for k in ("path", "command") if isinstance(args.get(k), str)}}
        family = "subagent" if name == "subagent" else ("builtin" if name in BUILTIN else "extension")
        rows: list[Row] = [
            ToolCallRow(AGENT, call_id, key, name, pos.byte_offset, turn_key=turn, response_id=response_id,
                        tool_family=family, started_at=ts, input_bytes=json_size(args), background=background,
                        meta=meta or None),
            ToolIoRow(AGENT, call_id, key, ts, pos.byte_offset, "call", tool_name=name, call_uid=call_id,
                      turn_key=turn, input_text=_dumps(block.get("arguments"))),
        ]
        rows.extend(self._touch(name, args, call_id, ts, pos, turn))
        if name == "subagent":
            briefs = [args["task"]] if isinstance(args.get("task"), str) else []
            briefs += [t["task"] for t in args.get("tasks") or [] if isinstance(t, dict) and isinstance(t.get("task"), str)]
            for n, brief in enumerate(b for b in briefs if b.strip()):
                rows.append(self._msg(f"{uid}:brief:{call_id}:{n}", ts, "assistant", "subagent_brief", brief.strip(),
                                      pos, turn, model))
            requested = as_str(args.get("agent"))
            if requested and args.get("async") is True and isinstance(args.get("task"), str):
                rows.append(SubagentSpawnRow(
                    AGENT, call_id, key, pos.byte_offset, turn_key=turn, spawned_at=ts,
                    requested_type=requested, requested_type_source="explicit",
                    background=background, name=requested))
        return rows

    def _touch(self, name: str, args: dict[str, Any], call_id: str, ts: datetime, pos: LinePos,
               turn: str | None) -> list[Row]:
        op = TOUCH.get(name)
        path = as_str(args.get("path"))
        if op is None or not path:
            return []
        if not path.startswith("/") and self.s["cwd"]:
            path = os.path.normpath(os.path.join(self.s["cwd"], path))
        added = removed = None
        if name == "write":
            added = _nlines(args.get("content"))
        elif name == "edit" and isinstance(args.get("edits"), list):
            added = removed = 0
            for edit in args["edits"]:
                if isinstance(edit, dict):
                    x, y = _diff_counts(edit.get("oldText"), edit.get("newText"))
                    added += x or 0
                    removed += y or 0
        rows: list[Row] = [FileTouchRow(AGENT, f"{call_id}:0", self._key(), ts, pos.byte_offset, path, op, name,
                                        call_uid=call_id, turn_key=turn, lines_added=added, lines_removed=removed)]
        if path.startswith("/"):
            rows.append(ArtifactRow(AGENT, call_id, self._key(), ts, artifact_kind(path), ARTIFACT_ACTION[name], path,
                                    "tool_input", pos.byte_offset, turn, call_id, path.rsplit("/", 1)[-1] or None))
        return rows

    def _tool_result(self, m: dict[str, Any], uid: str, ts: datetime, pos: LinePos) -> list[Row]:
        call_id = as_str(m.get("toolCallId"))
        call = self.s["calls"].pop(call_id, None) if call_id else None
        if call is None:
            return [ParseIssueRow(pos.byte_offset, "orphan_output", pos.line_number, as_str(m.get("toolName")))]
        key = self._key()
        content = m.get("content")
        text = _text(content) if isinstance(content, list) else (content if isinstance(content, str) else None)
        details = m.get("details") if isinstance(m.get("details"), dict) else None
        is_error = bool(m.get("isError"))
        meta = dict(call.get("m") or {})
        rows: list[Row] = []
        if call["n"] == "subagent" and details:
            run_id = as_str(details.get("runId")) or as_str(details.get("asyncId"))
            if as_str(details.get("mode")):
                meta["mode"] = details["mode"]
            if run_id:
                meta["run_id"] = run_id
                self.s["wf"][run_id] = {"c": call_id, "e": call["e"], "t": call.get("t"),
                                        "a": meta.get("agent"), "bo": call["bo"]}
                if meta.get("agent") and (meta.get("async") is True or meta.get("mode") == "async") and not is_error:
                    rows.append(SubagentSpawnRow(
                        AGENT, call_id, key, call["bo"], turn_key=call.get("t"),
                        spawned_at=datetime.fromtimestamp(call["e"], ts.tzinfo),
                        requested_type=meta["agent"], requested_type_source="explicit",
                        background=True, launch_status="launched", workflow_id=run_id))
            rows.extend(self._foreground(details, call_id, call, ts, pos))
        duration = int((ts.timestamp() - call["e"]) * 1000)
        parts = _images(content)
        rows.append(ToolCallRow(AGENT, call_id, key, call["n"], call["bo"], turn_key=call.get("t"), ended_at=ts,
                                duration_ms=duration if duration >= 0 else None, output_bytes=json_size(content),
                                outcome="error" if is_error else "ok", is_error=is_error, meta=meta or None))
        rows.append(ToolIoRow(AGENT, call_id, key, ts, pos.byte_offset, "call", tool_name=call["n"], call_uid=call_id,
                              turn_key=call.get("t"), output_text=text or None, result_json=_dumps(details),
                              output_parts=parts or None, output_at=ts, output_byte_offset=pos.byte_offset))
        for img in parts:
            rows.append(AttachmentRow(AGENT, f"{call_id}:att:{img['index']}", key, ts, pos.byte_offset, "image",
                                      "tool_result", call_uid=call_id, turn_key=call.get("t"), mime=img["mime"],
                                      size_bytes=img["bytes"]))
        if call["n"] == "bash" and text and not is_error:
            commits, pushes = git_from_output(text)
            commit_op = "cherry_pick" if "cherry-pick" in (call.get("a") or {}).get("command", "") else "commit"
            for branch, sha in commits:
                rows.append(GitEventRow(AGENT, f"{call_id}:commit:{sha}", key, ts, commit_op, "output_regex",
                                        pos.byte_offset, call.get("t"), call_id, self.s["cwd"], branch, sha[:12]))
            for _old, new, _src, dst in pushes:
                rows.append(GitEventRow(AGENT, f"{call_id}:push:{new}", key, ts, "push", "output_regex",
                                        pos.byte_offset, call.get("t"), call_id, self.s["cwd"], dst[:200], new[:12]))
        if call["n"] == "bash" and not is_error:
            commits, pushes = git_from_output(text or "")
            ops = git_ops_from_command((call.get("a") or {}).get("command"))
            for op, n in git_event_extras(ops, len(commits), len(pushes)):
                rows.append(GitEventRow(AGENT, f"{call_id}:{'push' if op == 'push' else 'commit'}:cmd{n}", key, ts, op,
                                        "command", pos.byte_offset, call.get("t"), call_id, self.s["cwd"]))
        return rows

    # --- subagents ----------------------------------------------------------------------------

    def _foreground(self, details: dict[str, Any], call_id: str, call: dict[str, Any], ts: datetime,
                    pos: LinePos) -> list[Row]:
        rows: list[Row] = []
        for n, result in enumerate(details.get("results") or []):
            if not isinstance(result, dict) or not as_str(result.get("agent")):
                continue
            index = as_int(result.get("index"))
            path = SESSION_PATH_RE.search(as_str(result.get("sessionFile")) or "")
            usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}
            tokens = sum(as_int(usage.get(k)) or 0 for k in ("input", "output", "cacheRead", "cacheWrite")) or None
            exit_code = as_int(result.get("exitCode"))
            rows.append(SubagentSpawnRow(
                AGENT, f"{call_id}:{index if index is not None else n}", self._key(), call["bo"],
                turn_key=call.get("t"), child_task_name=f"{path.group(1)}/{path.group(2)}" if path else None,
                spawned_at=datetime.fromtimestamp(call["e"], ts.tzinfo), requested_type=result["agent"],
                requested_type_source="explicit",
                requested_model=as_str(result.get("requestedModel")), resolved_model=as_str(result.get("model")),
                reasoning_effort=as_str(result.get("thinking")), background=False,
                name=as_str(result.get("workflowKey")) or result["agent"], launch_status="launched",
                completion_status=None if exit_code is None else ("completed" if exit_code == 0 else "failed"),
                completed_at=ts, reported_tokens=tokens, workflow_id=as_str(details.get("runId"))))
        return rows

    def _notify(self, ctype: str, text: str, turn: str | None, ts: datetime, pos: LinePos) -> list[Row]:
        wf = WORKFLOW_RUN_RE.search(text)
        wf_id = wf.group(1) if wf else None
        children: dict[str, dict[str, Any]] = {}
        if ctype == "subagent-incremental-child-notify":
            run = CHILD_RUN_RE.search(text)
            done = CHILD_DONE_RE.search(text)
            if run:
                children[run.group(1)] = {"agent": done.group(2).strip() if done else None,
                                          "status": done.group(1) if done else None}
        else:
            line = CHILD_RUNS_RE.search(text)
            for item in CHILD_RUN_ITEM_RE.finditer(line.group(1) if line else ""):
                children[item.group(2)] = {"agent": item.group(1), "status": item.group(3)}
        rows: list[Row] = []
        # the header opens the text and the directory line is the trailer; the middle is agent-authored
        done, dirs = ASYNC_DONE_RE.match(text), list(ASYNC_DIR_RE.finditer(text))
        launch = self.s["wf"].get(dirs[-1].group(1)) if done and dirs else None
        if launch:
            # the launching call's spawn row (spawn_uid = call id) gains its completion
            rows.append(SubagentSpawnRow(AGENT, launch["c"], self._key(), launch["bo"],
                                         completion_status=done.group(1), completed_at=ts))
        spawn = self.s["wf"].get(wf_id or "")
        for run_id, child in children.items():
            rows.append(SubagentSpawnRow(
                AGENT, run_id, self._key(), pos.byte_offset, turn_key=spawn.get("t") if spawn else turn,
                child_task_name=None,
                spawned_at=datetime.fromtimestamp(spawn["e"], ts.tzinfo) if spawn else None,
                requested_type=None,
                requested_type_source=None,
                name=child.get("agent"), background=True,
                launch_status="launched", completion_status=child.get("status"), completed_at=ts,
                workflow_id=wf_id))
        return rows


class PiArtifactParser:
    """Index only exact run/response evidence from a pi-subagents transcript copy."""

    def __init__(self, ctx: FileContext, state: dict[str, Any]) -> None:
        self.ctx = ctx

    def state(self) -> dict[str, Any]:
        return {}

    def line(self, record: dict[str, Any], pos: LinePos) -> Iterable[Row]:
        if record.get("version") != 1:
            return []
        run_id = as_str(record.get("runId"))
        agent_name = as_str(record.get("agent"))
        message = record.get("message") if isinstance(record.get("message"), dict) else {}
        response_id = as_str(message.get("responseId"))
        if not run_id or not response_id:
            return []
        return [PiRunResponseRow(run_id, response_id, pos.byte_offset,
                                 agent_name if agent_name in PI_AGENT_FILES else None)]

    def flush(self) -> Iterable[Row]:
        return []

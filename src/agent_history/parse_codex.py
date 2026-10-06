"""Codex rollout parser for the agent-history catalogue (sessions/ and archived_sessions/ trees).

Pure: turns decoded JSONL records into model.py rows and keeps a small JSON-serialisable state.

Session identity
  The FIRST session_meta is the file's own thread: SessionKey('codex', payload.id, ''). Later
  session_meta lines are ancestors copied in by a fork and never re-key the session.

Fork replay (records a forked child copied from its parent; skipped and counted in
`inherited_skipped`)
  - first session_meta has `forked_from_id` and `subagent_history_start_ordinal` = N:
    every record with a top-level `ordinal` < N (other than the first line) is replay.
  - first session_meta has `forked_from_id` and no start ordinal (history_mode legacy and a few
    paginated forks): the replay is written as one burst at fork time. The burst is every record
    from the first line up to the first inter-record timestamp gap > BURST_GAP_S. The child's own
    first turn is the LAST `task_started` inside the burst; everything in the burst before it is
    replay. Replayed parent turns stay open (no task_complete), so "first task_started" is wrong
    for these files; the burst rule matched the parent-file ground truth on 858/858 local legacy
    forks. Records inside the burst after the child's own task_started are dropped except its
    `turn_context`, whose scalars are held in state.
  - Always: an item_completed or token_usage_record whose payload.thread_id is another thread
    is replay.

Text sources (MessageRow)
  human_prompt   event_msg/user_message (<0.147) and item_completed UserMessage (>=0.147), only
                 in non-subagent threads. Never response_item/message role=user (injected context).
  assistant_text response_item/message role=assistant, output_text blocks. It exists in every
                 CLI version; event_msg/agent_message (<=0.148) and AgentMessage items (>=0.147)
                 overlap it and are ignored to avoid duplicates.
  subagent_brief spawn_agent arguments.message, in the PARENT thread, on the function_call line.
  subagent_report task_complete.last_agent_message, in a subagent thread.
  compaction_summary compacted.message when non-empty.
  event_uid = "<thread>:<ordinal>" when the record has a monotonic top-level ordinal, else
  "<thread>:o<byte_offset>".

Tokens (LlmCallRow, normalised: input_uncached excludes cached and cache-write tokens)
  - token_usage_record (>=0.153): one row per record, response_id from the record, else
    "codex-tur:<event_uid>".
  - legacy token_count (file's first cli_version < 0.153 and no token_usage_record seen yet):
    info.last_token_usage counts only when info.total_token_usage differs from the previous
    sample in this file, replayed samples included, so a fork's inherited total is the baseline.
    response_id = "codex-legacy:<event_uid>". A total that falls (0.147-0.151 resets it) still
    counts as changed. 0.145.0-alpha.18 zeroed breakdowns become NULL token columns.

Field conventions
  - tool_call.meta.cmd_verb only for shell function calls (exec_command); the `exec` custom tool
    takes JavaScript, so its input is never mined. Executed commands carry cmd_verb on tool_op.
  - tool_op.exec_source holds CommandExecution.source, Extension.kind or CollabAgentToolCall.tool.
    CollabAgentToolCall item ids equal the function_call call_id (link_method 'item_id').
  - subagent_spawn completion and child_session_uid come from sub_agent_activity /
    SubAgentActivity matched on the last agent_path segment == spawn task_name.
  - compaction.pre_tokens = latest_token_usage_record.usage.input_tokens (last call's prompt).
  - Turn origin: human (typed prompt), sdk (codex_exec prompt), subagent_brief (a subagent's first
    own turn), peer (inbound agent message or parent follow-up), else unknown.

Known gap: a legacy fork whose whole file lies inside one burst (imported 0.145.0-alpha.18
rollouts) never leaves burst mode, so only its SessionRow is emitted until more lines arrive.

Parser v4 additions (every v3 row is emitted unchanged; new rows obey the same replay filters)
  Message classes and event_uids
    reasoning         role assistant, one row per reasoning item, event_uid = the response_item
                      reasoning line's uid. Sources merged per item: item_completed Reasoning
                      (>=0.147: summary_text / raw_content, written just BEFORE the response_item)
                      and event_msg agent_reasoning / agent_reasoning_raw_content (<=0.146, one per
                      summary part, before or after it) are held in state["pr"] and merged into
                      the next response_item reasoning (its own summary/content win when non-empty).
                      A held source is emitted on its own (uid of its first line) when a different
                      response_item or a turn boundary arrives first. A source whose parts all equal
                      the previous emitted item's parts in the same turn is a repeat and dropped.
                      text = summary parts joined "\n\n", then raw parts; detail {source,
                      summary_parts, raw_parts}. Encrypted-only reasoning stores nothing.
    system_prompt     role system, first session_meta.base_instructions.text,
                      event_uid "<thread>:system_prompt", detail {source, provenance{type, model}}.
    context_injection role system (developer role, world_state) or user (user-role injected blocks).
    skill_body / interrupt_marker / hook_output / agent_message: see _classify.
      response_item message role user|developer is split per content block: event_uid
      "<uid>:<block index>", detail {"source": <leading tag> | agents_md | developer | user_text}.
      <image ...> / </image> label blocks are markup around an input_image and are not stored.
      world_state: text = JSON of payload.state, detail {source, full}; a snapshot identical to
      the previous one in this thread is not stored again.
      response_item agent_message: text = its input_text parts, detail {source, author, recipient};
      role assistant when author is this thread's own agent_path, else user.
    Prompt dedupe: the user-role response_item copy of a prompt is written immediately BEFORE the
      user_message / UserMessage record (rarely immediately after). A user-role response_item is
      held in state["pu"] until the next processed record; a block whose stripped text equals the
      prompt's v3 text (or all text blocks joined) is dropped, and its input_image metadata enriches
      the prompt's attachments. A copy right after the prompt (adjacent byte offsets, state["lp"])
      is dropped the same way.
    Subagent threads (intentional change 1): the parent's brief and follow-ups that v3 dropped in
      _prompt become agent_message rows (role user) on the prompt record's uid, detail {source}.
    prompt_origin on human_prompt: common.prompt_origin(text); 'skill' instead of typed/pasted when
      the UserMessage has a `skill` part or the text starts with `$<lowercase-name>`.
  Tool I/O (ToolIoRow)
    kind call, io_uid = call_uid = call_id: function_call (input_text = arguments string as
      recorded), custom_tool_call (input), tool_search_call / local_shell_call (arguments or action as
      JSON), web_search_call (action JSON; io_uid = payload.id, else the id of the web_search_end
      written immediately before it, else "ws:<uid>"), image_generation_call (payload minus result).
      Outputs from *_call_output / tool_search_output: output_text = the string, or text parts
      joined (a "\n" is inserted only where a part does not already end in one); image parts go to
      output_parts {index,type,mime,bytes} and AttachmentRows "<io_uid>:att:<n>".
      output_truncated when the output carries Codex's "…N tokens truncated…" marker.
      Event enrichment of the same call (patch_apply_end, mcp_tool_call_end, web_search_end,
      exec_command_end, image_generation_end) goes to stdout/stderr/result_json; an event whose
      call_id is not a model call of this thread (exec-internal "exec-..." ids, <0.147) becomes an
      op row "item:<call_id>" instead. MCP end-event text content is not copied for a model call
      (the *_call_output carries it); only non-text parts, structuredContent, isError and Err.
    kind op, io_uid "item:<item id>", item_uid = item id, call_uid = item id only when it equals a
      model call id (CollabAgentToolCall, open/recent calls, WebSearch ws_ ids):
      CommandExecution input_text = command (JSON for a list), output_text = aggregated_output,
        stdout_text only when it differs from aggregated_output (it never did locally), stderr_text
        when non-empty; result_json drops formatted_output when it equals aggregated_output.
      McpToolCall input_text = arguments JSON, output_text = text content; structuredContent is
        dropped when it equals the JSON-decoded single text part.
      FileChange input_text = changes JSON (diffs/content), stdout/stderr when non-empty.
      Extension/ImageView/WebSearch/CollabAgentToolCall: the rest of the item in result_json.
    Binary payloads (data: URLs, base64 image data, image_generation results) are replaced by
      {"type","mime","bytes"} everywhere, including inside result_json.
  Attachments: prompt images/local_images/image parts ("<event_uid>:att:<n>", source prompt), unmatched
    user-role response_item input_image parts, tool-output images (source tool_result), ImageView and
    view_image_tool_call paths (source tool_input).
  File touches: FileChange items and patch_apply_end changes (add->create, update->edit, delete,
    move_path->move with move_from), touch_uid "<io_uid>:<n>", lines counted from the unified diff
    (or content lines for add/delete). apply_patch input is parsed into state at the call and
    emitted at the call's output only when no FileChange/patch_apply_end with the call id arrived.
  Continuations: a NON-subagent thread whose first session_meta has forked_from_id -> ContinuationRow
    kind fork, evidence forked_from_id. `codex resume` / Desktop re-open writes a repeated
    session_meta with the SAME id into the same file: SessionEventRow kind 'resume' (no new thread).
"""

from __future__ import annotations

import json
import mimetypes
import re
from collections import Counter
from datetime import datetime, timedelta
from typing import Any, Iterable

from .common import (BOUNDED_STATE_SECONDS, artifact_kind, as_bool, as_int, as_str, cmd_verb, ssh_target,
                     git_event_extras, git_ops_from_command, git_from_output, json_size, linked_paths, mcp_split, parse_ts, prompt_origin,
                     sha256_text, split_prompt_injections, text_blocks)
from .model import (ArtifactRow, AttachmentRow, CompactionRow, ContinuationRow, FileContext, FileTouchRow,
                    GitEventRow, LinePos, LlmCallRow, MessageRow, ParseIssueRow, RateLimitRow, RecordTypeRow,
                    Row, SessionEventRow, SessionKey, SessionRow, SubagentSpawnRow, ToolCallRow, ToolIoRow,
                    ToolOpRow, TurnRow)

AGENT = "codex"
BURST_GAP_S = 1.0
TOKEN_RECORD_VERSION = (0, 153, 0)
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
PR_URL_RE = re.compile(r"https://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/pull/(\d+)")
PR_ACTIONS = {"create", "merge", "edit", "close", "ready", "reopen", "review", "comment", "view"}

KNOWN_TOP = {"session_meta", "turn_context", "response_item", "event_msg", "token_usage_record",
             "compacted", "world_state", "inter_agent_communication_metadata"}
KNOWN_RESPONSE = {"message", "reasoning", "function_call", "function_call_output", "custom_tool_call",
                  "custom_tool_call_output", "agent_message", "tool_search_call", "tool_search_output",
                  "web_search_call", "local_shell_call", "image_generation_call"}
KNOWN_EVENT = {"token_count", "item_completed", "item_started", "task_started", "task_complete",
               "turn_aborted", "user_message", "agent_message", "agent_reasoning", "sub_agent_activity",
               "patch_apply_end", "mcp_tool_call_end", "thread_settings_applied", "context_compacted",
               "web_search_end", "thread_goal_updated", "exec_command_end", "view_image_tool_call",
               "image_generation_end", "entered_review_mode", "exited_review_mode",
               "agent_reasoning_raw_content", "turn_diff", "plan_update", "error", "stream_error",
               "thread_rolled_back"}
KNOWN_ITEMS = {"Reasoning", "CommandExecution", "AgentMessage", "FileChange", "SubAgentActivity",
               "CollabAgentToolCall", "McpToolCall", "ContextCompaction", "UserMessage", "Extension",
               "ImageView", "WebSearch"}
OP_ITEMS = {"CommandExecution", "McpToolCall", "FileChange", "Extension", "ImageView",
            "CollabAgentToolCall", "WebSearch"}


def _version(value: Any) -> tuple[int, ...] | None:
    if not isinstance(value, str):
        return None
    parts = re.findall(r"\d+", value)[:3]
    return tuple(int(p) for p in parts) if parts else None


def _epoch(dt: datetime | None) -> float | None:
    return dt.timestamp() if dt is not None else None


def _turn_of(payload: dict[str, Any]) -> str | None:
    for key in ("internal_chat_message_metadata_passthrough", "metadata"):
        meta = payload.get(key)
        if isinstance(meta, dict) and as_str(meta.get("turn_id")):
            return meta["turn_id"]
    return as_str(payload.get("turn_id"))


def _usage_sig(usage: Any) -> str | None:
    if not isinstance(usage, dict) or not usage:
        return None
    return json.dumps(usage, sort_keys=True)


def _normalise(usage: dict[str, Any]) -> dict[str, int | None]:
    inp = as_int(usage.get("input_tokens"))
    parts = [as_int(usage.get(k)) or 0 for k in ("input_tokens", "cached_input_tokens", "output_tokens")]
    if not any(parts) and (as_int(usage.get("total_tokens")) or 0) > 0:
        # 0.145.0-alpha.18 wrote zeroed breakdowns beside a non-zero total: unknown, not zero.
        return {"input_uncached": None, "cache_read": None, "cache_write_5m": None, "cache_write_1h": None,
                "output": None, "reasoning": None}
    cached = as_int(usage.get("cached_input_tokens")) or 0
    write = as_int(usage.get("cache_write_input_tokens")) or 0
    uncached = None if inp is None else max(inp - cached - write, 0)
    # Codex has no TTL split: all writes price as the 5m write, and 0 stays 0 (known) so loop
    # cache_write and priced cost are not NULL
    return {"input_uncached": uncached, "cache_read": cached, "cache_write_5m": write, "cache_write_1h": 0,
            "output": as_int(usage.get("output_tokens")),
            "reasoning": as_int(usage.get("reasoning_output_tokens"))}


def _duration_ms(value: Any) -> int | None:
    if isinstance(value, dict):
        secs = as_int(value.get("secs"))
        nanos = as_int(value.get("nanos")) or 0
        if secs is not None:
            return secs * 1000 + nanos // 1_000_000
    return as_int(value)


def _first_line(output: Any) -> str:
    if isinstance(output, list):
        for block in output:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                return block["text"].split("\n", 1)[0]
        return ""
    if isinstance(output, str):
        return output.split("\n", 1)[0]
    return ""


def _command_text(command: Any) -> str:
    if isinstance(command, list):
        words = [w for w in command if isinstance(w, str)]
        if len(words) >= 3 and words[1] in {"-c", "-lc"}:
            return words[2]
        return " ".join(words)
    return command if isinstance(command, str) else ""


# --- parser v4 helpers --------------------------------------------------------------------------

STATS: Counter = Counter()   # diagnostics for corpus sweeps only (never rows, never state)
RECENT_CALLS = 64
TRUNC_RE = re.compile(r"…\d+ (?:tokens|chars) truncated…")
DATA_URL_RE = re.compile(r"^data:([\w.+-]+/[\w.+-]+)?((?:;[\w.+-]+=[^;,]*)*);base64,", re.I)
B64_RE = re.compile(r"[A-Za-z0-9+/\r\n]+={0,2}")
MAGIC = (("iVBORw0KGgo", "image/png"), ("/9j/", "image/jpeg"), ("R0lGOD", "image/gif"),
         ("UklGR", "image/webp"), ("JVBERi", "application/pdf"))
TAG_RE = re.compile(r"\s*<([A-Za-z_][\w-]*)")
IMAGE_LABEL_RE = re.compile(r"\s*</?image\b[^>]*>\s*")
AGENTS_RE = re.compile(r"#+ AGENTS\.md\b")
SKILL_INVOKE_RE = re.compile(r"\s*\$[a-z][\w:.-]*(?:\s|$)")
SKILL_NAME_RE = re.compile(r"\s*<skill>\s*<name>([^<\n]{1,200})</name>")
TEXT_PARTS = {"input_text", "output_text", "text"}
IMAGE_PARTS = {"input_image", "image", "output_image"}
TURN_EDGES = {"task_started", "task_complete", "turn_aborted"}
USER_TAG_CLASS = {"skill": "skill_body", "turn_aborted": "interrupt_marker", "hook_prompt": "hook_output",
                  "subagent_notification": "agent_message"}
PATCH_FILE_RE = re.compile(r"^\*\*\* (Add|Update|Delete) File: (.+)$")
PATCH_FAILED_RE = re.compile(r"\s*(?:apply_patch verification failed|error|failed|invalid patch)", re.I)


def _sniff(value: str) -> str | None:
    head = value[:16]
    for prefix, mime in MAGIC:
        if head.startswith(prefix):
            return mime
    return None


def _b64_bytes(value: str, start: int = 0) -> int:
    body = len(value) - start - value.count("\n", start) - value.count("\r", start)
    pad = 2 if value.endswith("==") else (1 if value.endswith("=") else 0)
    return max(body * 3 // 4 - pad, 0)


def _binary(value: Any) -> dict[str, Any] | None:
    """Metadata for a base64 payload (data: URL or raw base64 with a known magic), else None."""
    if not isinstance(value, str):
        return None
    m = DATA_URL_RE.match(value)
    if m:
        return {"type": "binary", "mime": m.group(1), "bytes": _b64_bytes(value, m.end())}
    if len(value) >= 256:
        mime = _sniff(value)
        if mime and B64_RE.fullmatch(value[:4096]):
            return {"type": "binary", "mime": mime, "bytes": _b64_bytes(value)}
    return None


def _scrub(value: Any) -> Any:
    """Copy of a JSON value with every binary payload replaced by its metadata."""
    if isinstance(value, dict):
        return {k: _scrub(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub(v) for v in value]
    meta = _binary(value)
    return meta if meta is not None else value


def _dumps(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(_scrub(value), ensure_ascii=False)


def _text_of(value: Any) -> str | None:
    """A text column value: strings as recorded, anything else as JSON."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return _dumps(value)


def _image_meta(part: dict[str, Any]) -> dict[str, Any]:
    """{type, mime, bytes} (+ file_name) for an image content part; never the payload."""
    ptype = as_str(part.get("type")) or "image"
    meta: dict[str, Any] = {"type": ptype, "mime": None, "bytes": None}
    for key in ("image_url", "url", "data", "result"):
        raw = part.get(key)
        if isinstance(raw, dict):
            raw = raw.get("url")
        found = _binary(raw)
        if found:
            meta["mime"], meta["bytes"] = found["mime"], found["bytes"]
            break
    meta["mime"] = meta["mime"] or as_str(part.get("mimeType")) or as_str(part.get("mime_type"))
    path = as_str(part.get("path"))
    if path:
        meta["file_name"] = path
        meta["mime"] = meta["mime"] or mimetypes.guess_type(path)[0]
    return meta


def _join_parts(parts: list[Any]) -> tuple[str | None, list[dict[str, Any]] | None]:
    """(text parts joined, metadata of non-text parts) for a content-part list."""
    text: str | None = None
    meta: list[dict[str, Any]] = []
    for index, part in enumerate(parts):
        if not isinstance(part, dict):
            continue
        ptype = part.get("type")
        if ptype in TEXT_PARTS and isinstance(part.get("text"), str):
            if text is None:
                text = part["text"]
            else:
                text += ("" if text.endswith("\n") else "\n") + part["text"]
        elif ptype in IMAGE_PARTS or any(_binary(part.get(k)) for k in ("image_url", "data")):
            meta.append({"index": index, **_image_meta(part)})
        else:
            meta.append({"index": index, "type": as_str(ptype) or "?", "mime": None, "bytes": json_size(part)})
    return text, meta or None


def _output_text(output: Any) -> tuple[str | None, list[dict[str, Any]] | None]:
    if isinstance(output, str):
        return output, None
    if isinstance(output, list):
        return _join_parts(output)
    if output is None:
        return None, None
    return _dumps(output), None


def _tag(text: str) -> str | None:
    m = TAG_RE.match(text)
    return m.group(1) if m else None


def _hash(text: str) -> str:
    return sha256_text(text)[:24]


def _diff_counts(diff: str) -> tuple[int, int]:
    added = removed = 0
    started = False
    for line in diff.split("\n"):
        if line.startswith("@@"):
            started = True
            continue
        if not started and line.startswith(("--- ", "+++ ")):
            continue
        if line.startswith("+"):
            added += 1
        elif line.startswith("-"):
            removed += 1
    return added, removed


def _content_lines(content: Any) -> int | None:
    if not isinstance(content, str):
        return None
    if not content:
        return 0
    return content.count("\n") + (0 if content.endswith("\n") else 1)


def _change_touches(changes: Any) -> list[list[Any]]:
    """[[op, path, move_from, added, removed]] from a FileChange / patch_apply_end changes dict."""
    out: list[list[Any]] = []
    if not isinstance(changes, dict):
        return out
    for path, change in changes.items():
        if not isinstance(path, str) or not path:
            continue
        change = change if isinstance(change, dict) else {}
        kind = as_str(change.get("type")) or "update"
        added = removed = None
        if isinstance(change.get("unified_diff"), str):
            added, removed = _diff_counts(change["unified_diff"])
        elif kind == "add":
            added, removed = _content_lines(change.get("content")), 0
        elif kind == "delete":
            added, removed = 0, _content_lines(change.get("content"))
        target, move_from = path, None
        if as_str(change.get("move_path")):
            op, target, move_from = "move", change["move_path"], path
        else:
            op = {"add": "create", "update": "edit", "delete": "delete"}.get(kind, "edit")
        out.append([op, target, move_from, added, removed])
    return out


def _patch_touches(patch: Any) -> list[list[Any]]:
    """[[op, path, move_from, added, removed]] parsed from an apply_patch input."""
    out: list[list[Any]] = []
    if not isinstance(patch, str):
        return out
    current: list[Any] | None = None
    for line in patch.split("\n"):
        m = PATCH_FILE_RE.match(line)
        if m:
            op = {"Add": "create", "Update": "edit", "Delete": "delete"}[m.group(1)]
            current = [op, m.group(2).strip(), None, None, None] if op == "delete" else \
                [op, m.group(2).strip(), None, 0, 0]
            out.append(current)
            continue
        if current is None:
            continue
        if line.startswith("*** Move to: "):
            current[2], current[1], current[0] = current[1], line[13:].strip(), "move"
        elif line.startswith("*** "):
            if line.startswith("*** End Patch"):
                current = None
        elif current[3] is None:
            continue
        elif line.startswith("+"):
            current[3] += 1
        elif line.startswith("-"):
            current[4] += 1
    return out


class CodexParser:
    def __init__(self, ctx: FileContext, state: dict[str, Any]) -> None:
        self.ctx = ctx
        self.s: dict[str, Any] = {
            "thread": None, "ver": None, "sub": False, "exec": False, "hs": None, "mode": "none",
            "burst": None, "inherited": 0, "last_ord": None, "last_iso": None, "own_turns": 0,
            "cur": None, "turns": {}, "model": None, "effort": None, "calls": {}, "spawns": {},
            "prev_total": None, "base": None, "tur": False, "rl": {}, "pre_meta": False,
            "path": None, "service_tier": None,
            # v4: pending user-role response_item, last prompt, pending/last reasoning, recent closed
            # call ids, open io-only model calls, last web_search_end, last world_state hash
            "pu": None, "lp": None, "pr": None, "lr": None, "rc": [], "xc": {}, "lwe": None, "ws": None,
        }
        self.s.update(state or {})
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
        payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}
        ptype = payload.get("type") if isinstance(payload.get("type"), str) else ""
        item = payload.get("item") if ptype in {"item_completed", "item_started"} and isinstance(payload.get("item"), dict) else None
        itype = item.get("type") if item and isinstance(item.get("type"), str) else ""
        ts = parse_ts(record.get("timestamp"))
        self._record_ts = ts   # Telemetry endpoints never use the legacy last_iso fallback.
        self._count(rtype, f"{ptype}/{itype}" if itype else ptype, payload, ts, pos)
        if ts is not None:
            self.s["last_iso"] = ts.isoformat()
        else:
            ts = parse_ts(self.s.get("last_iso"))
        uid = self._uid(record, pos, rows)

        if self.s["thread"] is None:
            if rtype != "session_meta" or not as_str(payload.get("id")):
                if not self.s["pre_meta"]:
                    self.s["pre_meta"] = True
                    rows.append(ParseIssueRow(pos.byte_offset, "missing_session_meta", pos.line_number,
                                              f"type={rtype}"))
                return rows
            rows.extend(self._session_meta(payload, ts, pos))
            return rows

        # --- fork replay filters ----------------------------------------------------------
        mode = self.s["mode"]
        if mode == "ordinal":
            ordinal = record.get("ordinal")
            if isinstance(ordinal, int) and ordinal < self.s["hs"]:
                self._skip(rtype, ptype, payload)
                return rows
        elif mode == "burst":
            done, extra = self._burst(record, rtype, ptype, payload, ts, pos, uid)
            rows.extend(extra)
            if not done:
                return rows
        thread_id = payload.get("thread_id")
        if (rtype == "token_usage_record" or ptype in {"item_completed", "item_started"}) and \
                isinstance(thread_id, str) and thread_id and thread_id != self.s["thread"]:
            self._skip(rtype, ptype, payload)
            return rows
        is_prompt = rtype == "event_msg" and (ptype == "user_message" or
                                              (ptype == "item_completed" and itype == "UserMessage"))
        if self.s["pu"] is not None and not is_prompt:
            rows.extend(self._flush_user())
        if self.s["pr"] is not None and not (rtype == "response_item" and ptype == "reasoning") and \
                (rtype == "response_item" or ptype in TURN_EDGES):
            rows.extend(self._flush_reasoning())
        if rtype == "session_meta":
            rows.append(ParseIssueRow(pos.byte_offset, "late_session_meta", pos.line_number))
            if payload.get("id") == self.s["thread"] and ts is not None and self._key() is not None:
                rows.append(SessionEventRow(AGENT, uid, self._key(), ts, "resume", pos.byte_offset,
                                            self.s.get("cur"), as_str(payload.get("cli_version")),
                                            {"originator": as_str(payload.get("originator"))}))
            return rows

        self._seen(ts)
        self._observe_boundary(rtype, ptype, payload)
        if rtype == "turn_context":
            rows.extend(self._turn_context(payload, ts, pos, uid))
        elif rtype == "response_item":
            rows.extend(self._response_item(ptype, payload, ts, pos, uid))
        elif rtype == "event_msg":
            rows.extend(self._event(ptype, payload, item, itype, ts, pos, uid))
        elif rtype == "token_usage_record":
            rows.extend(self._token_record(payload, ts, pos, uid))
        elif rtype == "compacted":
            rows.extend(self._compacted(payload, ts, pos, uid))
        elif rtype == "world_state":
            rows.extend(self._world_state(payload, ts, pos, uid))
        if is_prompt and self.s["pu"] is not None:   # not consumed by _prompt: store it as it is
            rows.extend(self._flush_user())
        return rows

    def flush(self) -> Iterable[Row]:
        rows: list[Row] = []
        key = self._key()
        fallback = parse_ts(self.s.get("last_iso"))
        for (rtype, sub), (count, keys, ts) in self._types.items():
            if ts or fallback:
                rows.append(RecordTypeRow(AGENT, rtype, sub, ts or fallback, count, keys, self.s.get("ver")))
        for detail, offset in self._unknown.items():
            rows.append(ParseIssueRow(offset, "unknown_type", None, detail))
        self._types, self._unknown = {}, {}
        if key is not None and self._touched:
            rows.append(SessionRow(session=key, first_event_at=self._first, last_event_at=self._last,
                                   first_human_at=self._first_human, last_human_at=self._last_human,
                                   inherited_skipped=self.s["inherited"]))
        self._first = self._last = self._first_human = self._last_human = None
        self._touched = False
        rows.extend(self._prune())
        return rows

    # --- helpers ------------------------------------------------------------------------------

    def _key(self) -> SessionKey | None:
        return SessionKey(AGENT, self.s["thread"], "") if self.s["thread"] else None

    def _count(self, rtype: str, sub: str, payload: dict[str, Any], ts: datetime | None, pos: LinePos) -> None:
        entry = self._types.get((rtype, sub))
        if entry is None:
            keys = ",".join(sorted(payload.keys())) if payload else None
            self._types[(rtype, sub)] = [1, keys, ts]
        else:
            entry[0] += 1
            if ts is not None:
                entry[2] = ts
        ptype, _, itype = sub.partition("/")
        unknown = rtype not in KNOWN_TOP or \
            (rtype == "response_item" and ptype not in KNOWN_RESPONSE) or \
            (rtype == "event_msg" and ptype not in KNOWN_EVENT) or \
            (bool(itype) and itype not in KNOWN_ITEMS)
        if unknown:
            self._unknown.setdefault(f"{rtype}/{sub}", pos.byte_offset)

    def _uid(self, record: dict[str, Any], pos: LinePos, rows: list[Row]) -> str:
        ordinal = record.get("ordinal")
        thread = self.s["thread"] or "?"
        if isinstance(ordinal, int) and not isinstance(ordinal, bool):
            last = self.s["last_ord"]
            if last is None or ordinal > last:
                self.s["last_ord"] = ordinal
                return f"{thread}:{ordinal}"
            rows.append(ParseIssueRow(pos.byte_offset, "ordinal_not_monotonic", pos.line_number))
        return f"{thread}:o{pos.byte_offset}"

    def _seen(self, ts: datetime | None) -> None:
        self._touched = True
        if ts is None:
            return
        if self._first is None or ts < self._first:
            self._first = ts
        if self._last is None or ts > self._last:
            self._last = ts

    def _human(self, ts: datetime | None) -> None:
        if ts is None:
            return
        if self._first_human is None or ts < self._first_human:
            self._first_human = ts
        if self._last_human is None or ts > self._last_human:
            self._last_human = ts

    def _skip(self, rtype: str, ptype: str, payload: dict[str, Any]) -> None:
        """A replayed record: count it and keep the running token total as the fork baseline."""
        self.s["inherited"] += 1
        self._touched = True
        if rtype == "event_msg" and ptype == "token_count":
            info = payload.get("info")
            if isinstance(info, dict):
                sig = _usage_sig(info.get("total_token_usage"))
                if sig is not None:
                    self.s["prev_total"] = sig

    def _turn(self, turn_id: str | None, ts: datetime | None = None) -> dict[str, Any]:
        turns = self.s["turns"]
        entry = turns.get(turn_id or "")
        if entry is None:
            entry = turns[turn_id or ""] = {"o": False, "peer": False, "m": None, "ef": None, "cw": None}
        entry["e"] = _epoch(ts) or entry.get("e")
        return entry

    def _prune(self) -> list[Row]:
        rows: list[Row] = []
        now = _epoch(parse_ts(self.s.get("last_iso")))
        if now is None:
            return rows
        horizon = now - BOUNDED_STATE_SECONDS
        key = self._key()
        for call_id, call in list(self.s["calls"].items()):
            if (call.get("e") or now) < horizon:
                del self.s["calls"][call_id]
                if key is not None:
                    rows.append(ToolCallRow(AGENT, call_id, key, call["n"], call["bo"], outcome="no_result"))
        for call_id, call in list(self.s["xc"].items()):
            if (call.get("e") or now) < horizon:
                del self.s["xc"][call_id]
        for name, spawn in list(self.s["spawns"].items()):
            if (spawn.get("e") or now) < horizon:
                del self.s["spawns"][name]
        for turn_id, turn in list(self.s["turns"].items()):
            if turn_id != self.s.get("cur") and (turn.get("e") or now) < horizon:
                del self.s["turns"][turn_id]
        return rows

    def _observe_boundary(self, rtype: str, ptype: str, p: dict[str, Any]) -> None:
        """Track causal request boundaries after replay filtering, before outputs close calls."""
        if rtype == "event_msg" and ptype == "thread_settings_applied":
            settings = p.get("thread_settings")
            tier = as_str(settings.get("service_tier")) if isinstance(settings, dict) else None
            if tier is not None:
                self.s["service_tier"] = tier
        turn_id = _turn_of(p)
        boundary = self._record_ts.isoformat() if self._record_ts else None
        if rtype == "event_msg" and ptype == "task_started" and turn_id:
            self._turn(turn_id)["boundary"] = boundary
            self._turn(turn_id)["boundary_kind"] = "start"
        elif rtype == "response_item" and ptype in {"function_call_output", "custom_tool_call_output",
                                                   "tool_search_output"}:
            call_id = as_str(p.get("call_id"))
            call = self.s["calls"].get(call_id) or self.s["xc"].get(call_id)
            call_turn = call.get("t") if call else None
            if turn_id and call_turn and turn_id != call_turn:
                self._turn(turn_id)["boundary"] = None
                self._turn(call_turn)["boundary"] = None
                return
            turn_id = turn_id or call_turn
            if turn_id:
                self._turn(turn_id)["boundary"] = boundary
                self._turn(turn_id)["boundary_kind"] = "output"
            elif self.s.get("cur"):
                # An orphan result cannot establish a request boundary for the current turn.
                self._turn(self.s["cur"])["boundary"] = None
        elif rtype == "event_msg" and ptype == "token_count":
            turn_id = turn_id or self._timing_turn()
            if turn_id:
                self._turn(turn_id)["boundary"] = boundary
                self._turn(turn_id)["boundary_kind"] = "tokens"

    def _timing_turn(self) -> str | None:
        opened = [tid for tid, turn in self.s["turns"].items() if turn.get("timing_open")]
        return opened[0] if len(opened) == 1 else None

    def _call_latency(self, p: dict[str, Any]) -> dict[str, Any]:
        turn_id = as_str(p.get("turn_id")) or self._timing_turn()
        turn = self.s["turns"].get(turn_id) if turn_id else None
        if turn is None or not turn.get("timing_open"):
            return {}
        start = parse_ts(turn.pop("boundary", None))
        end = self._record_ts
        # Calls emitted after the boundary belong to this response, not to its input. Only
        # unfinished calls preceding an output boundary make that boundary partial/ambiguous.
        pending = start is not None and turn.get("boundary_kind") == "output" and any(
            call.get("t") == turn_id and (call.get("e") is None or call["e"] <= start.timestamp())
            for calls in (self.s["calls"], self.s["xc"]) for call in calls.values())
        if start is None or end is None or end < start or pending:
            return {}
        duration = (end - start) // timedelta(milliseconds=1)
        if duration > 2_147_483_647:   # The nullable seam is a PostgreSQL int.
            return {}
        return {"duration_ms": duration, "latency_basis": "codex_prev_boundary_to_usage"}

    # --- session_meta and replay --------------------------------------------------------------

    def _session_meta(self, p: dict[str, Any], ts: datetime | None, pos: LinePos) -> list[Row]:
        s = self.s
        s["thread"] = p["id"]
        s["ver"] = as_str(p.get("cli_version"))
        source = p.get("source")
        sub = source.get("subagent") if isinstance(source, dict) else None
        spawn = sub.get("thread_spawn") if isinstance(sub, dict) and isinstance(sub.get("thread_spawn"), dict) else {}
        thread_source = as_str(p.get("thread_source")) or (source if isinstance(source, str) else None)
        s["sub"] = bool(sub) or thread_source == "subagent"
        originator = as_str(p.get("originator"))
        s["exec"] = originator == "codex_exec"
        s["path"] = as_str(p.get("agent_path")) or as_str(spawn.get("agent_path"))
        forked = as_str(p.get("forked_from_id"))
        hs = as_int(p.get("subagent_history_start_ordinal"))
        if forked:
            s["hs"] = hs
            s["mode"] = "ordinal" if hs is not None else "burst"
            if s["mode"] == "burst":
                s["burst"] = {"t": _epoch(ts), "pend": None, "ctx": None, "before": 0}
        spawn_kind = "codex_fork" if forked else ("codex_spawn" if s["sub"] else
                                                 ("codex_exec" if s["exec"] else None))
        git = p.get("git") if isinstance(p.get("git"), dict) else {}
        key = self._key()
        assert key is not None
        self._seen(ts)
        rows: list[Row] = [SessionRow(
            session=key, byte_offset=pos.byte_offset, is_subagent=s["sub"],
            root_session_uid=as_str(p.get("session_id")),
            parent_session_uid=as_str(p.get("parent_thread_id")) or as_str(spawn.get("parent_thread_id")),
            spawn_kind=spawn_kind, spawn_depth=as_int(spawn.get("depth")),
            agent_type=as_str(p.get("agent_type")) or as_str(spawn.get("agent_type")),
            agent_type_source="explicit" if as_str(p.get("agent_type")) or as_str(spawn.get("agent_type")) else None,
            agent_role=as_str(p.get("agent_role")) or as_str(spawn.get("agent_role")),
            agent_path=s["path"],
            agent_nickname=as_str(p.get("agent_nickname")) or as_str(spawn.get("agent_nickname")),
            forked_from_uid=forked, cwd=as_str(p.get("cwd")), git_branch=as_str(git.get("branch")),
            git_commit_start=as_str(git.get("commit_hash")), git_remote_url=as_str(git.get("repository_url")),
            cli_version_first=s["ver"], cli_version_last=s["ver"], entrypoint=originator,
            model_provider=as_str(p.get("model_provider")), thread_source=thread_source,
            history_mode=as_str(p.get("history_mode")), first_event_at=ts, last_event_at=ts,
        )]
        if git and ts is not None and (git.get("commit_hash") or git.get("branch")):
            sha = as_str(git.get("commit_hash"))
            rows.append(GitEventRow(AGENT, f"{s['thread']}:session_start", key, ts, "session_start",
                                    "session_meta", pos.byte_offset, cwd=as_str(p.get("cwd")),
                                    branch=as_str(git.get("branch")), sha_short=sha[:12] if sha else None,
                                    remote_url=as_str(git.get("repository_url"))))
        ts = ts or parse_ts(p.get("timestamp"))
        if ts is None:
            return rows
        base = p.get("base_instructions")
        text = base.get("text") if isinstance(base, dict) else base
        if isinstance(text, str) and text.strip():
            detail: dict[str, Any] = {"source": "base_instructions"}
            prov = base.get("provenance") if isinstance(base, dict) else None
            if isinstance(prov, dict):
                detail["provenance"] = {k: prov[k] for k in ("type", "model") if isinstance(prov.get(k), str)}
            rows.append(MessageRow(AGENT, f"{s['thread']}:system_prompt", key, ts, "system", "system_prompt",
                                   text, pos.byte_offset, pos.byte_length, pos.line_number,
                                   detail=detail))
        if forked and not s["sub"]:
            rows.append(ContinuationRow(AGENT, s["thread"], forked, "fork", key, ts, pos.byte_offset,
                                        "forked_from_id"))
        return rows

    def _burst(self, record: dict[str, Any], rtype: str, ptype: str, payload: dict[str, Any],
               ts: datetime | None, pos: LinePos, uid: str) -> tuple[bool, list[Row]]:
        """Legacy fork replay. Returns (burst_over, rows); rows are the finalised own first turn."""
        b = self.s["burst"]
        now = _epoch(ts)
        if now is not None and b["t"] is not None and now - b["t"] > BURST_GAP_S:
            self.s["mode"] = "done"
            self.s["burst"] = None
            self.s["inherited"] += b["before"]
            self._touched = True
            rows: list[Row] = []
            pend = b["pend"]
            if pend is not None:
                ppos = LinePos(*pend["pos"])
                pts = parse_ts(pend["ts"])
                self._seen(pts)
                rows.extend(self._task_started(pend["p"], pts, ppos, pend["uid"]))
                turn = self._turn(as_str(pend["p"].get("turn_id")))
                turn["boundary"], turn["boundary_kind"] = pend.get("boundary"), "start"
                if b["ctx"] is not None:
                    rows.extend(self._turn_context(b["ctx"]["p"], parse_ts(b["ctx"]["ts"]),
                                                   LinePos(*b["ctx"]["pos"]), b["ctx"]["uid"]))
                rows.extend(self._replay_own(b.get("own") or [], pend["p"].get("turn_id")))
            return True, rows
        if now is not None:
            b["t"] = now
        if rtype == "event_msg" and ptype == "token_count":
            info = payload.get("info")
            if isinstance(info, dict):
                sig = _usage_sig(info.get("total_token_usage"))
                if sig is not None:
                    self.s["prev_total"] = sig
        if rtype == "event_msg" and ptype == "task_started":
            if b["pend"] is not None:
                b["before"] += 1 + b.get("tail", 0)
            keep = {k: payload[k] for k in ("turn_id", "started_at", "model_context_window",
                                            "collaboration_mode_kind", "trace_id", "root_turn_id") if k in payload}
            b["pend"] = {"p": keep, "ts": ts.isoformat() if ts else None,
                         "boundary": self._record_ts.isoformat() if self._record_ts else None,
                         "pos": [pos.byte_offset, pos.byte_length, pos.line_number], "uid": uid}
            b["ctx"], b["tail"], b["own"] = None, 0, []
            return False, []
        if b["pend"] is not None:
            b["tail"] = b.get("tail", 0) + 1
            if rtype == "turn_context":
                keep = {k: payload[k] for k in ("turn_id", "model", "effort", "approval_policy",
                                                "sandbox_policy", "permission_profile",
                                                "collaboration_mode", "summary") if k in payload}
                if isinstance(keep.get("collaboration_mode"), dict):
                    keep["collaboration_mode"] = {"mode": keep["collaboration_mode"].get("mode")}
                b["ctx"] = {"p": keep, "ts": ts.isoformat() if ts else None,
                            "pos": [pos.byte_offset, pos.byte_length, pos.line_number], "uid": uid}
            elif self.s["sub"] and ts is not None and (
                    (rtype == "response_item" and (ptype == "agent_message" or
                                                   (ptype == "message" and payload.get("role") != "assistant")))
                    or (rtype == "event_msg" and (ptype == "user_message" or
                                                  (ptype == "item_completed" and isinstance(payload.get("item"), dict)
                                                   and payload["item"].get("type") == "UserMessage")))
                    or rtype == "world_state"):
                # v4: the subagent's own brief and context inside the fork burst (v3 dropped them too)
                b.setdefault("own", []).append({"r": rtype, "p": payload, "ts": ts.isoformat(), "uid": uid,
                                                "pos": [pos.byte_offset, pos.byte_length, pos.line_number]})
            return False, []
        b["before"] += 1
        return False, []

    def _replay_own(self, held: list[dict[str, Any]], turn_id: str | None) -> list[Row]:
        """v4: the subagent's own first-turn records held inside a legacy fork burst, dispatched in order."""
        rows: list[Row] = []
        for h in held:
            p, ts, pos, uid = h["p"], parse_ts(h["ts"]), LinePos(*h["pos"]), h["uid"]
            if ts is None:
                continue
            ptype = p.get("type") if isinstance(p.get("type"), str) else ""
            item = p.get("item") if isinstance(p.get("item"), dict) else None
            is_prompt = h["r"] == "event_msg"
            if self.s["pu"] is not None and not is_prompt:
                rows.extend(self._flush_user())
            if h["r"] == "world_state":
                rows.extend(self._world_state(p, ts, pos, uid))
            elif h["r"] == "response_item":
                rows.extend(self._response_item(ptype, p, ts, pos, uid))
            elif item is not None:
                rows.extend(self._item(item, "UserMessage", p, ts, pos, uid))
            else:
                rows.extend(self._event(ptype, p, None, "", ts, pos, uid))
            if is_prompt and self.s["pu"] is not None:
                rows.extend(self._flush_user())
        return rows

    # --- turns --------------------------------------------------------------------------------

    def _turn_context(self, p: dict[str, Any], ts: datetime | None, pos: LinePos, uid: str) -> list[Row]:
        key = self._key()
        turn_id = as_str(p.get("turn_id")) or self.s.get("cur")
        if key is None or not turn_id:
            return []
        model = as_str(p.get("model"))
        effort = as_str(p.get("effort"))
        sandbox = p.get("sandbox_policy")
        profile = p.get("permission_profile")
        collab = p.get("collaboration_mode")
        turn = self._turn(turn_id, ts)
        turn["m"] = model or turn["m"]
        turn["ef"] = effort or turn["ef"]
        rows: list[Row] = [TurnRow(
            key, turn_id, pos.byte_offset, model=model, effort=effort,
            approval_policy=as_str(p.get("approval_policy")),
            sandbox_type=as_str(sandbox.get("type")) if isinstance(sandbox, dict) else as_str(sandbox),
            permission_mode=as_str(profile.get("type")) if isinstance(profile, dict) else None,
            collaboration_mode=as_str(collab.get("mode")) if isinstance(collab, dict) else None,
            reasoning_summary=p.get("summary") if p.get("summary") in ("none", "detailed") else None,
        )]
        if ts is not None:
            if model and self.s["model"] and model != self.s["model"]:
                rows.append(SessionEventRow(AGENT, uid, key, ts, "model_switch", pos.byte_offset, turn_id,
                                            model, {"from": self.s["model"]}))
            if effort and self.s["effort"] and effort != self.s["effort"]:
                rows.append(SessionEventRow(AGENT, uid, key, ts, "effort_change", pos.byte_offset, turn_id,
                                            effort, {"from": self.s["effort"]}))
        self.s["model"] = model or self.s["model"]
        self.s["effort"] = effort or self.s["effort"]
        return rows

    def _task_started(self, p: dict[str, Any], ts: datetime | None, pos: LinePos, uid: str) -> list[Row]:
        key = self._key()
        turn_id = as_str(p.get("turn_id"))
        if key is None or not turn_id:
            return []
        self.s["cur"] = turn_id
        self.s["own_turns"] += 1
        turn = self._turn(turn_id, ts)
        turn["timing_open"] = True
        cw = as_int(p.get("model_context_window"))
        turn["cw"] = cw or turn["cw"]
        origin = None
        if self.s["sub"] and self.s["own_turns"] == 1:
            origin, turn["o"] = "subagent_brief", True
        return [TurnRow(key, turn_id, pos.byte_offset, origin=origin,
                        started_at=parse_ts(p.get("started_at")) or ts, status="open", context_window=cw,
                        collaboration_mode=as_str(p.get("collaboration_mode_kind")),
                        trace_id=as_str(p.get("trace_id")), root_turn_key=as_str(p.get("root_turn_id")))]

    def _turn_end(self, p: dict[str, Any], ts: datetime | None, pos: LinePos, uid: str, aborted: bool) -> list[Row]:
        key = self._key()
        turn_id = as_str(p.get("turn_id")) or self.s.get("cur")
        if key is None or not turn_id:
            return []
        turn = self._turn(turn_id, ts)
        turn["timing_open"] = False
        turn["boundary"] = None
        origin = None
        if not turn["o"]:
            origin = "peer" if turn["peer"] else ("sdk" if self.s["exec"] else "unknown")
            turn["o"] = True
        error = p.get("error")
        status = "aborted" if aborted else ("error" if error else "complete")
        rows: list[Row] = [TurnRow(
            key, turn_id, pos.byte_offset, origin=origin, completed_at=parse_ts(p.get("completed_at")) or ts,
            duration_ms=as_int(p.get("duration_ms")), ttft_ms=as_int(p.get("time_to_first_token_ms")),
            status=status, abort_reason=as_str(p.get("reason")) if aborted else None)]
        if ts is None:
            return rows
        if aborted:
            rows.append(SessionEventRow(AGENT, uid, key, ts, "interrupt", pos.byte_offset, turn_id,
                                        as_str(p.get("reason")) or "aborted"))
        if error:
            info = error.get("codex_error_info") if isinstance(error, dict) else None
            value = info if isinstance(info, str) else (
                next(iter(info)) if isinstance(info, dict) and info else "error")
            rows.append(SessionEventRow(AGENT, uid, key, ts, "api_error", pos.byte_offset, turn_id, value))
        report = p.get("last_agent_message")
        if self.s["sub"] and not aborted and isinstance(report, str) and report.strip():
            rows.append(MessageRow(AGENT, uid, key, ts, "assistant", "subagent_report", report.strip(),
                                   pos.byte_offset, pos.byte_length, pos.line_number, turn_id, turn["m"]))
        return rows

    def _prompt(
        self,
        text: str,
        images: int,
        turn_id: str | None,
        ts: datetime | None,
        pos: LinePos,
        uid: str,
        raw: str | None = None,
        parts: list[dict[str, Any]] | None = None,
        skill: bool = False,
        source: str = "user_message",
    ) -> list[Row]:
        key = self._key()
        turn_id = turn_id or self.s.get("cur")
        if key is None or ts is None:
            return []
        rows: list[Row] = []
        ri_images = self._match_user(text, images, rows)
        self.s["lp"] = {"h": _hash(text), "end": pos.byte_offset + pos.byte_length, "t": turn_id} if text else None
        attachments = self._prompt_attachments(parts or [], ri_images, uid, ts, pos, turn_id)
        if self.s["sub"]:
            # Subagent threads never carry typed prompts; these are parent briefs and follow-ups.
            self._turn(turn_id, ts)["peer"] = True
            body = raw if isinstance(raw, str) and raw.strip() else text
            if body:
                rows.append(
                    MessageRow(
                        AGENT,
                        uid,
                        key,
                        ts,
                        "user",
                        "agent_message",
                        body,
                        pos.byte_offset,
                        pos.byte_length,
                        pos.line_number,
                        turn_id,
                        detail={"source": source},
                    )
                )
            return rows + attachments
        human, injections = split_prompt_injections(text)
        for n, (cls, tag, body, start, stop) in enumerate(injections):
            event_uid = uid if not human and n == 0 else f"{uid}:injection:{n}"
            rows.append(
                MessageRow(
                    AGENT,
                    event_uid,
                    key,
                    ts,
                    "user",
                    cls,
                    body,
                    pos.byte_offset,
                    pos.byte_length,
                    pos.line_number,
                    turn_id,
                    detail={"source": tag, "text_start": start, "text_end": stop},
                )
            )
        if human:
            self._human(ts)
            origin = prompt_origin(human)
            if origin != "launch_message" and (skill or SKILL_INVOKE_RE.match(human)):
                origin = "skill"
            rows.append(
                MessageRow(
                    AGENT,
                    uid,
                    key,
                    ts,
                    "user",
                    "human_prompt",
                    human,
                    pos.byte_offset,
                    pos.byte_length,
                    pos.line_number,
                    turn_id,
                    prompt_origin=origin,
                )
            )
            if turn_id:
                turn = self._turn(turn_id, ts)
                if not turn["o"]:
                    turn["o"] = True
                    rows.append(TurnRow(key, turn_id, pos.byte_offset, origin="sdk" if self.s["exec"] else "human"))
        if images:
            rows.append(SessionEventRow(AGENT, uid, key, ts, "image_attach", pos.byte_offset, turn_id, str(images)))
        return rows + attachments

    def _prompt_attachments(self, parts: list[dict[str, Any]], ri_images: list[dict[str, Any]], uid: str,
                            ts: datetime, pos: LinePos, turn_id: str | None) -> list[Row]:
        key = self._key()
        assert key is not None
        rows: list[Row] = []
        for n, part in enumerate(parts):
            meta = _image_meta(part)
            if n < len(ri_images):
                meta["mime"] = meta["mime"] or ri_images[n].get("mime")
                meta["bytes"] = meta["bytes"] if meta["bytes"] is not None else ri_images[n].get("bytes")
            rows.append(AttachmentRow(AGENT, f"{uid}:att:{n}", key, ts, pos.byte_offset, "image", "prompt",
                                      event_uid=uid, turn_key=turn_id, mime=meta["mime"],
                                      file_name=meta.get("file_name"), size_bytes=meta["bytes"],
                                      detail={"part": meta["type"]}))
        return rows

    # --- v4: injected context, prompt copies, agent messages ---------------------------------------

    def _classify(self, role: str, text: str) -> tuple[str, str]:
        tag = _tag(text)
        if tag in USER_TAG_CLASS:
            return USER_TAG_CLASS[tag], tag
        if tag:
            return "context_injection", tag
        if role != "user":
            return "context_injection", "developer"
        if AGENTS_RE.match(text):
            return "context_injection", "agents_md"
        if self.s["sub"]:
            return "agent_message", "response_item"
        return "context_injection", "user_text"

    def _context_message(self, p: dict[str, Any], ts: datetime, pos: LinePos, uid: str,
                         turn_id: str | None) -> list[Row]:
        role = "user" if p.get("role") == "user" else as_str(p.get("role")) or "?"
        content = p.get("content") if isinstance(p.get("content"), list) else []
        blocks: list[list[Any]] = []
        images: list[dict[str, Any]] = []
        for n, block in enumerate(content):
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype in TEXT_PARTS and isinstance(block.get("text"), str):
                if block["text"].strip() and not IMAGE_LABEL_RE.fullmatch(block["text"]):
                    blocks.append([n, block["text"]])
            elif btype in IMAGE_PARTS:
                images.append({"index": n, **_image_meta(block)})
        if not blocks and not images:
            return []
        pending = {"uid": uid, "pos": [pos.byte_offset, pos.byte_length, pos.line_number], "ts": ts.isoformat(),
                   "t": turn_id, "role": role, "b": blocks, "i": images}
        if role != "user":
            self.s["pu"], held = pending, self.s["pu"]
            rows = self._flush_user()
            self.s["pu"] = held
            return rows
        lp = self.s["lp"]
        if lp is not None and lp["end"] == pos.byte_offset:
            kept = [b for b in blocks if _hash(b[1].strip()) != lp["h"]]
            if len(kept) == len(blocks) and \
                    _hash("\n\n".join(b[1].strip() for b in blocks)) == lp["h"]:
                kept = []
            if len(kept) < len(blocks):
                STATS["prompt_copy_after_dropped"] += 1
                pending["b"], pending["i"] = kept, []
        self.s["lp"] = None
        self.s["pu"] = pending
        return []

    def _match_user(self, text: str, images: int, rows: list[Row]) -> list[dict[str, Any]]:
        """Resolve the held user-role response_item against the prompt record that follows it."""
        pu = self.s["pu"]
        if pu is None:
            return []
        blocks = pu["b"]
        kept = blocks
        if text:
            kept = [b for b in blocks if b[1].strip() != text]
            if len(kept) == len(blocks) and "\n\n".join(b[1].strip() for b in blocks) == text:
                kept = []
        matched = len(kept) < len(blocks) or (not text and not blocks and images > 0 and bool(pu["i"]))
        ri_images: list[dict[str, Any]] = []
        if matched:
            STATS["prompt_copy_before_dropped"] += 1
            pu["b"], ri_images, pu["i"] = kept, pu["i"], []
        elif text:
            STATS["prompt_without_copy"] += 1
        rows.extend(self._flush_user())
        return ri_images

    def _flush_user(self) -> list[Row]:
        pu, self.s["pu"] = self.s["pu"], None
        key = self._key()
        if pu is None or key is None:
            return []
        ts = parse_ts(pu["ts"])
        if ts is None:
            return []
        pos = LinePos(*pu["pos"])
        role = "user" if pu["role"] == "user" else "system"
        rows: list[Row] = []
        first: str | None = None
        for n, text in pu["b"]:
            cls, source = self._classify(pu["role"], text)
            detail: dict[str, Any] = {"source": source}
            if cls == "skill_body":
                m = SKILL_NAME_RE.match(text)
                if m:
                    detail["name"] = m.group(1).strip()
            if source == "user_text":
                STATS["user_text_unmatched"] += 1
            event_uid = f"{pu['uid']}:{n}"
            first = first or event_uid
            rows.append(MessageRow(AGENT, event_uid, key, ts, role, cls, text, pos.byte_offset, pos.byte_length,
                                   pos.line_number, pu["t"], detail=detail))
        for meta in pu["i"]:
            rows.append(AttachmentRow(AGENT, f"{pu['uid']}:att:{meta['index']}", key, ts, pos.byte_offset, "image",
                                      "prompt", event_uid=first, turn_key=pu["t"], mime=meta.get("mime"),
                                      file_name=meta.get("file_name"), size_bytes=meta.get("bytes"),
                                      detail={"part": meta.get("type")}))
        return rows

    def _agent_message(self, p: dict[str, Any], ts: datetime, pos: LinePos, uid: str,
                       turn_id: str | None) -> list[Row]:
        key = self._key()
        assert key is not None
        content = p.get("content")
        if isinstance(content, str):
            text = content
        else:
            texts = [b["text"] for b in content or [] if isinstance(b, dict) and b.get("type") in TEXT_PARTS
                     and isinstance(b.get("text"), str)]
            text = "\n\n".join(texts)
        if not text.strip():
            return []
        author, recipient = as_str(p.get("author")), as_str(p.get("recipient"))
        role = "assistant" if author and author == self.s.get("path") else "user"
        detail = {k: v for k, v in (("source", "agent_message"), ("author", author), ("recipient", recipient)) if v}
        return [MessageRow(AGENT, uid, key, ts, role, "agent_message", text, pos.byte_offset, pos.byte_length,
                           pos.line_number, turn_id, detail=detail)]

    def _world_state(self, p: dict[str, Any], ts: datetime | None, pos: LinePos, uid: str) -> list[Row]:
        key = self._key()
        state = p.get("state")
        if key is None or ts is None or state is None:
            return []
        text = _dumps(state) or ""
        digest = _hash(text)
        if digest == self.s["ws"]:
            STATS["world_state_repeat"] += 1
            return []
        self.s["ws"] = digest
        detail: dict[str, Any] = {"source": "world_state"}
        if isinstance(p.get("full"), bool):
            detail["full"] = p["full"]
        return [MessageRow(AGENT, uid, key, ts, "system", "context_injection", text, pos.byte_offset,
                           pos.byte_length, pos.line_number, self.s.get("cur"), detail=detail)]

    # --- v4: reasoning ------------------------------------------------------------------------

    @staticmethod
    def _strings(value: Any) -> list[str]:
        out: list[str] = []
        for part in value if isinstance(value, list) else ([value] if isinstance(value, str) else []):
            text = part if isinstance(part, str) else (part.get("text") if isinstance(part, dict) else None)
            if isinstance(text, str) and text.strip():
                out.append(text)
        return out

    def _reasoning_part(self, summary: list[str], raw: list[str], source: str, ts: datetime, pos: LinePos,
                        uid: str, turn_id: str | None) -> list[Row]:
        parts = summary + raw
        if not parts:
            return []
        lr = self.s["lr"]
        if lr is not None and lr["t"] == turn_id and {_hash(x) for x in parts} <= set(lr["h"]):
            STATS["reasoning_repeat_dropped"] += 1
            return []
        rows: list[Row] = []
        pr = self.s["pr"]
        if pr is not None and pr["t"] != turn_id:
            rows.extend(self._flush_reasoning())
            pr = None
        if pr is None:
            pr = self.s["pr"] = {"uid": uid, "pos": [pos.byte_offset, pos.byte_length, pos.line_number],
                                 "ts": ts.isoformat(), "t": turn_id, "s": [], "r": [], "src": source}
        for text in summary:
            if text not in pr["s"]:
                pr["s"].append(text)
        for text in raw:
            if text not in pr["r"]:
                pr["r"].append(text)
        return rows

    def _reasoning_row(self, summary: list[str], raw: list[str], source: str, ts: datetime, pos: LinePos,
                       uid: str, turn_id: str | None) -> list[Row]:
        key = self._key()
        parts = summary + raw
        if key is None or not parts:
            return []
        hashes = [_hash(x) for x in parts]
        lr = self.s["lr"]
        if lr is not None and lr["t"] == turn_id and set(hashes) <= set(lr["h"]):
            STATS["reasoning_repeat_dropped"] += 1
            return []
        self.s["lr"] = {"t": turn_id, "h": hashes}
        text = "\n\n".join(summary)
        if raw:
            text = (text + "\n\n" if text else "") + "\n\n".join(raw)
        turn = self._turn(turn_id, ts) if turn_id else {"m": None}
        STATS[f"reasoning_from_{source}"] += 1
        return [MessageRow(AGENT, uid, key, ts, "assistant", "reasoning", text, pos.byte_offset, pos.byte_length,
                           pos.line_number, turn_id, turn.get("m") or self.s["model"],
                           detail={"source": source, "summary_parts": len(summary), "raw_parts": len(raw)})]

    def _flush_reasoning(self) -> list[Row]:
        pr, self.s["pr"] = self.s["pr"], None
        ts = parse_ts(pr["ts"]) if pr else None
        if pr is None or ts is None:
            return []
        return self._reasoning_row(pr["s"], pr["r"], pr["src"], ts, LinePos(*pr["pos"]), pr["uid"], pr["t"])

    def _reasoning_item(self, p: dict[str, Any], ts: datetime, pos: LinePos, uid: str,
                        turn_id: str | None) -> list[Row]:
        summary = self._strings(p.get("summary"))
        raw = self._strings(p.get("content"))
        pr, self.s["pr"] = self.s["pr"], None
        rows: list[Row] = []
        own_summary, own_raw = bool(summary), bool(raw)
        if pr is not None and pr["t"] != turn_id:
            self.s["pr"] = pr
            rows.extend(self._flush_reasoning())
            pr = None
        if pr is not None:
            summary = summary or pr["s"]
            raw = raw or pr["r"]
        source = "response_item" if own_summary or (own_raw and not summary) else \
            (pr["src"] if pr is not None else "response_item")
        if not summary and not raw:
            STATS["reasoning_encrypted_only" if p.get("encrypted_content") else "reasoning_empty"] += 1
            return rows
        return rows + self._reasoning_row(summary, raw, source, ts, pos, uid, turn_id)

    # --- response items -----------------------------------------------------------------------

    def _response_item(self, ptype: str, p: dict[str, Any], ts: datetime | None, pos: LinePos,
                       uid: str) -> list[Row]:
        key = self._key()
        if key is None or ts is None:
            return []
        turn_id = _turn_of(p) or self.s.get("cur")
        if ptype == "message":
            if p.get("role") != "assistant":
                return self._context_message(p, ts, pos, uid, turn_id)
            text = text_blocks(p.get("content"), {"output_text"})
            if not text:
                return []
            turn = self._turn(turn_id, ts) if turn_id else {"m": self.s["model"]}
            rows: list[Row] = [MessageRow(AGENT, uid, key, ts, "assistant", "assistant_text", text,
                                          pos.byte_offset, pos.byte_length, pos.line_number, turn_id,
                                          turn.get("m") or self.s["model"], phase=as_str(p.get("phase")))]
            for path in linked_paths(text):
                rows.append(ArtifactRow(AGENT, uid, key, ts, artifact_kind(path), "linked", path,
                                        "assistant_link", pos.byte_offset, turn_id,
                                        display_name=path.rsplit("/", 1)[-1] or None))
            return rows
        if ptype == "agent_message":
            author = as_str(p.get("author"))
            if turn_id and author != self.s.get("path"):
                self._turn(turn_id, ts)["peer"] = True
            return self._agent_message(p, ts, pos, uid, turn_id)
        if ptype in {"function_call", "custom_tool_call"}:
            return self._call(ptype, p, ts, pos, uid, turn_id)
        if ptype in {"function_call_output", "custom_tool_call_output"}:
            call_id = as_str(p.get("call_id"))
            io = self._io_output(call_id, p.get("output"), ts, pos) if call_id else []
            return io + self._output(ptype, p, ts, pos)
        if ptype == "reasoning":
            return self._reasoning_item(p, ts, pos, uid, turn_id)
        if ptype in {"web_search_call", "local_shell_call", "tool_search_call", "image_generation_call"}:
            return self._io_model_call(ptype, p, ts, pos, uid, turn_id)
        if ptype == "tool_search_output":
            call_id = as_str(p.get("call_id"))
            return self._io_output(call_id, _dumps(p.get("tools")), ts, pos) if call_id else []
        return []

    def _call(self, ptype: str, p: dict[str, Any], ts: datetime, pos: LinePos, uid: str,
              turn_id: str | None) -> list[Row]:
        key = self._key()
        call_id = as_str(p.get("call_id"))
        name = as_str(p.get("name")) or "?"
        if key is None or not call_id:
            return [ParseIssueRow(pos.byte_offset, "missing_call_id", pos.line_number)]
        ns = as_str(p.get("namespace"))
        raw_args = p.get("input") if ptype == "custom_tool_call" else p.get("arguments")
        mcp_server = mcp_tool = None
        if ptype == "custom_tool_call":
            family = "custom"
        elif ns and ns.startswith("mcp__"):
            family, mcp_server, mcp_tool = "mcp", ns[5:] or None, name
        elif name.startswith("mcp__"):
            family = "mcp"
            mcp_server, mcp_tool = mcp_split(name)
        elif ns == "collaboration":
            family = "collaboration"
        else:
            family = "function"
        args: dict[str, Any] = {}
        if ptype == "function_call" and isinstance(raw_args, str):
            try:
                decoded = json.loads(raw_args)
                args = decoded if isinstance(decoded, dict) else {}
            except ValueError:
                args = {}
        meta = None
        if name in {"exec_command", "shell", "local_shell"}:
            command = args.get("cmd") if "cmd" in args else args.get("command")
            verb = cmd_verb(command)
            meta = {"cmd_verb": verb} if verb else {}
            remote = ssh_target(command)
            if remote:
                meta["target_host"] = remote[0]
                if remote[1]:
                    meta["remote_verb"] = remote[1]
            meta = meta or None
        self.s["calls"][call_id] = {"n": name, "e": ts.timestamp(), "bo": pos.byte_offset, "t": turn_id}
        rows: list[Row] = [ToolCallRow(
            AGENT, call_id, key, name, pos.byte_offset, turn_key=turn_id, tool_family=family,
            mcp_server=mcp_server, mcp_tool=mcp_tool, codex_namespace=ns, started_at=ts,
            input_bytes=json_size(raw_args), meta=meta)]
        rows.append(ToolIoRow(AGENT, call_id, key, ts, pos.byte_offset, "call", tool_name=name, call_uid=call_id,
                              turn_key=turn_id, input_text=_text_of(raw_args)))
        if name == "apply_patch":
            patch = raw_args if ptype == "custom_tool_call" else (args.get("input") or args.get("patch"))
            touches = _patch_touches(patch)
            if touches:
                self.s["calls"][call_id]["pt"] = touches
        if name == "spawn_agent":
            rows.extend(self._spawn(call_id, args, ts, pos, uid, turn_id))
        elif name == "update_plan":
            plan = args.get("plan") if isinstance(args.get("plan"), list) else []
            statuses: dict[str, int] = {}
            for step in plan:
                if isinstance(step, dict) and isinstance(step.get("status"), str):
                    statuses[step["status"][:32]] = statuses.get(step["status"][:32], 0) + 1
            rows.append(SessionEventRow(AGENT, uid, key, ts, "task_list_op", pos.byte_offset, turn_id,
                                        "update_plan", {"steps": len(plan), **statuses}))
        elif name in {"request_user_input", "request_user_input_async"}:
            questions = args.get("questions")
            rows.append(SessionEventRow(AGENT, uid, key, ts, "user_question", pos.byte_offset, turn_id,
                                        name, {"questions": len(questions) if isinstance(questions, list) else None}))
        elif name == "interrupt_agent":
            rows.append(SessionEventRow(AGENT, uid, key, ts, "interrupt", pos.byte_offset, turn_id,
                                        "interrupt_agent"))
        return rows

    def _spawn(self, call_id: str, args: dict[str, Any], ts: datetime, pos: LinePos, uid: str,
               turn_id: str | None) -> list[Row]:
        key = self._key()
        assert key is not None
        task = as_str(args.get("task_name"))
        if task:
            self.s["spawns"][task] = {"c": call_id, "e": ts.timestamp()}
        fork = args.get("fork_turns")
        if fork is None and "fork_context" in args:
            fork = f"context:{bool(args.get('fork_context'))}".lower()
        rows: list[Row] = [SubagentSpawnRow(
            AGENT, call_id, key, pos.byte_offset, turn_key=turn_id, child_task_name=task, spawned_at=ts,
            requested_type=as_str(args.get("agent_type")) or "default",
            requested_type_source="explicit" if as_str(args.get("agent_type")) else "default",
            requested_model=as_str(args.get("model")),
            reasoning_effort=as_str(args.get("reasoning_effort")),
            fork_scope=str(fork) if fork is not None else None, name=task)]
        brief = args.get("message")
        if isinstance(brief, str) and brief.strip():
            rows.append(MessageRow(AGENT, uid, key, ts, "assistant", "subagent_brief", brief.strip(),
                                   pos.byte_offset, pos.byte_length, pos.line_number, turn_id,
                                   self.s["model"]))
        return rows

    def _output(self, ptype: str, p: dict[str, Any], ts: datetime, pos: LinePos) -> list[Row]:
        key = self._key()
        call_id = as_str(p.get("call_id"))
        call = self.s["calls"].pop(call_id, None) if call_id else None
        if key is None or call is None:
            return [ParseIssueRow(pos.byte_offset, "orphan_output", pos.line_number, ptype)]
        output = p.get("output")
        first = _first_line(output)
        outcome, is_error, interrupted, background = "ok", False, None, None
        if first.startswith("aborted by user"):
            outcome, interrupted, is_error = "interrupted", True, None
        elif first.startswith("Script failed"):
            outcome, is_error = "error", True
        elif first.startswith("Script running with cell ID"):
            background = True
        if call.get("err"):
            outcome, is_error = "error", True
        duration = int((ts.timestamp() - call["e"]) * 1000) if call.get("e") is not None else None
        rows: list[Row] = [ToolCallRow(
            AGENT, call_id, key, call["n"], call["bo"], turn_key=call.get("t"), ended_at=ts,
            duration_ms=duration if duration is None or duration >= 0 else None,
            output_bytes=json_size(output), outcome=outcome, is_error=is_error, interrupted=interrupted,
            background=background)]
        if call["n"] == "spawn_agent":
            launched: dict[str, Any] | None = None
            if isinstance(output, str):
                try:
                    decoded = json.loads(output)
                    launched = decoded if isinstance(decoded, dict) else None
                except ValueError:
                    launched = None
            child = as_str(launched.get("agent_id")) if launched else None
            rows.append(SubagentSpawnRow(
                AGENT, call_id, key, call["bo"],
                child_session_uid=child if child and UUID_RE.match(child) else None,
                launch_status="launched" if launched and (launched.get("task_name") or launched.get("agent_id"))
                else "error"))
        return rows

    # --- v4: tool I/O -------------------------------------------------------------------------

    def _known_call(self, call_id: str | None) -> bool:
        return bool(call_id) and (call_id in self.s["calls"] or call_id in self.s["xc"] or call_id in self.s["rc"])

    def _remember(self, call_id: str) -> None:
        recent = self.s["rc"]
        if call_id not in recent:
            recent.append(call_id)
            del recent[:-RECENT_CALLS]

    def _io_images(self, io_uid: str, parts: list[dict[str, Any]] | None, ts: datetime, pos: LinePos,
                   turn_id: str | None, call_uid: str | None) -> list[Row]:
        key = self._key()
        assert key is not None
        rows: list[Row] = []
        for meta in parts or []:
            if meta.get("type") not in IMAGE_PARTS and meta.get("type") != "binary":
                continue
            rows.append(AttachmentRow(AGENT, f"{io_uid}:att:{meta['index']}", key, ts, pos.byte_offset, "image",
                                      "tool_result", call_uid=call_uid, turn_key=turn_id, mime=meta.get("mime"),
                                      file_name=meta.get("file_name"), size_bytes=meta.get("bytes"),
                                      detail={"part": meta.get("type")}))
        return rows

    def _touches(self, io_uid: str, touches: list[list[Any]], tool: str, ts: datetime, pos: LinePos,
                 turn_id: str | None, call_uid: str | None = None, item_uid: str | None = None) -> list[Row]:
        key = self._key()
        assert key is not None
        return [FileTouchRow(AGENT, f"{io_uid}:{n}", key, ts, pos.byte_offset, path, op, tool, call_uid=call_uid,
                             item_uid=item_uid, turn_key=turn_id, lines_added=added, lines_removed=removed,
                             move_from=move_from)
                for n, (op, path, move_from, added, removed) in enumerate(touches) if path]

    def _consume_patch(self, call_id: str | None) -> None:
        call = self.s["calls"].get(call_id or "")
        if call is not None:
            call.pop("pt", None)

    def _io_output(self, call_id: str, output: Any, ts: datetime, pos: LinePos) -> list[Row]:
        key = self._key()
        if key is None:
            return []
        call = self.s["calls"].get(call_id) or self.s["xc"].pop(call_id, None)
        turn_id = call.get("t") if call else self.s.get("cur")
        text, parts = _output_text(output)
        other = [b for b in output if isinstance(b, dict) and b.get("type") not in TEXT_PARTS | IMAGE_PARTS
                 and not any(_binary(b.get(k)) for k in ("image_url", "data"))] if isinstance(output, list) else None
        rows: list[Row] = [ToolIoRow(
            AGENT, call_id, key, ts, pos.byte_offset, "call", tool_name=call["n"] if call else None, call_uid=call_id,
            turn_key=turn_id, output_text=text, output_truncated=True if text and TRUNC_RE.search(text) else None,
            output_parts=[m for m in parts or [] if m["type"] != "?"] or None,
            result_json=_dumps({"other_parts": other}) if other else None, output_at=ts,
            output_byte_offset=pos.byte_offset)]
        rows.extend(self._io_images(call_id, parts, ts, pos, turn_id, call_id))
        if call is not None and call.get("pt"):
            touches = call.pop("pt")
            if text is not None and PATCH_FAILED_RE.match(text):
                STATS["patch_input_failed_skipped"] += 1   # nothing was changed
            else:
                STATS["touches_from_patch_input"] += 1
                rows.extend(self._touches(call_id, touches, "apply_patch", ts, pos, turn_id, call_uid=call_id))
        self._remember(call_id)
        return rows

    def _io_model_call(self, ptype: str, p: dict[str, Any], ts: datetime, pos: LinePos, uid: str,
                       turn_id: str | None) -> list[Row]:
        key = self._key()
        assert key is not None
        skip = {"type", "id", "call_id", "internal_chat_message_metadata_passthrough", "metadata"}
        result: dict[str, Any] = {k: p[k] for k in ("status", "execution") if k in p}
        parts: list[dict[str, Any]] | None = None
        if ptype == "web_search_call":
            io_uid = as_str(p.get("id"))
            lwe = self.s["lwe"]
            if io_uid is None and lwe is not None and lwe["end"] == pos.byte_offset:
                io_uid = lwe["id"]
            io_uid, name, inp = io_uid or f"ws:{uid}", "web_search", p.get("action")
        elif ptype == "local_shell_call":
            io_uid, name, inp = as_str(p.get("call_id")) or as_str(p.get("id")), "local_shell", p.get("action")
        elif ptype == "tool_search_call":
            io_uid, name, inp = as_str(p.get("call_id")) or as_str(p.get("id")), "tool_search", p.get("arguments")
        else:
            io_uid, name = as_str(p.get("id")) or as_str(p.get("call_id")), "image_generation"
            inp = {k: v for k, v in p.items() if k not in skip | {"result", "status"}} or None
            if p.get("result") is not None:
                parts = [{"index": 0, **_image_meta({"type": "image", "result": p["result"]})}]
        if not io_uid:
            return [ParseIssueRow(pos.byte_offset, "missing_call_id", pos.line_number, ptype)]
        if ptype in {"local_shell_call", "tool_search_call"}:
            self.s["xc"][io_uid] = {"n": name, "e": ts.timestamp(), "t": turn_id}
        rows: list[Row] = [ToolIoRow(AGENT, io_uid, key, ts, pos.byte_offset, "call", tool_name=name, call_uid=io_uid,
                                     turn_key=turn_id, input_text=_text_of(inp), output_parts=parts,
                                     result_json=_dumps(result) if result else None,
                                     output_at=ts if parts else None,
                                     output_byte_offset=pos.byte_offset if parts else None)]
        rows.extend(self._io_images(io_uid, parts, ts, pos, turn_id, io_uid))
        return rows

    def _end_event(self, ptype: str, p: dict[str, Any], ts: datetime, pos: LinePos, uid: str) -> list[Row]:
        """Result events of a call: enrich the model call, or an exec-internal op (<0.147)."""
        key = self._key()
        call_id = as_str(p.get("call_id"))
        if key is None or not call_id:
            return []
        known = self._known_call(call_id) or (ptype == "web_search_end" and call_id.startswith("ws_"))
        call = self.s["calls"].get(call_id)
        turn_id = as_str(p.get("turn_id")) or (call.get("t") if call else None) or self.s.get("cur")
        op_name = {"patch_apply_end": "FileChange", "mcp_tool_call_end": "McpToolCall",
                   "web_search_end": "WebSearch", "exec_command_end": "CommandExecution",
                   "image_generation_end": "ImageGeneration"}[ptype]
        io_uid = call_id if known else f"item:{call_id}"
        row = ToolIoRow(AGENT, io_uid, key, ts, pos.byte_offset, "call" if known else "op",
                        tool_name=(call["n"] if call else None) if known else op_name,
                        call_uid=call_id if known else None, item_uid=None if known else call_id,
                        turn_key=turn_id, output_at=ts, output_byte_offset=pos.byte_offset)
        rest = {k: v for k, v in p.items() if k not in {"type", "call_id", "turn_id"}}
        rows: list[Row] = [row]
        if ptype == "patch_apply_end":
            changes = rest.pop("changes", None)
            if not known:
                row.input_text = _text_of(changes)
            row.stdout_text = rest.pop("stdout", None) or None
            row.stderr_text = rest.pop("stderr", None) or None
            touches = _change_touches(changes)
            self._consume_patch(call_id)
            rows.extend(self._touches(io_uid, touches, "apply_patch", ts, pos, turn_id,
                                      call_uid=call_id if known else None, item_uid=None if known else call_id))
        elif ptype == "mcp_tool_call_end":
            inv = rest.pop("invocation", None)
            inv = dict(inv) if isinstance(inv, dict) else {}
            args = inv.pop("arguments", None)
            if not known:
                row.input_text = _text_of(args)
            rest.update(inv)
            result = rest.pop("result", None)
            if isinstance(result, dict) and isinstance(result.get("Ok"), dict):
                ok = dict(result["Ok"])
                text, parts = self._mcp_content(ok)
                if not known:
                    row.output_text = text
                row.output_parts = parts
                if ok:
                    rest["result"] = {"Ok": ok}
                rows.extend(self._io_images(io_uid, parts, ts, pos, turn_id, row.call_uid))
            elif result is not None:
                rest["result"] = result
        elif ptype == "web_search_end":
            results = rest.pop("results", None)
            row.output_text = _text_of(results)
            if not known:
                row.input_text = _dumps({k: rest.pop(k) for k in ("query", "action") if k in rest})
            self.s["lwe"] = {"id": call_id, "end": pos.byte_offset + pos.byte_length}
        elif ptype == "exec_command_end":
            agg = rest.pop("aggregated_output", None)
            stdout, stderr = rest.pop("stdout", None), rest.pop("stderr", None)
            if not known:
                row.input_text = _text_of(rest.pop("command", None))
            if isinstance(agg, str) and (not known or call is None):
                row.output_text = agg
            elif isinstance(agg, str) and agg != stdout:
                rest["aggregated_output"] = agg
            if not known and row.output_text is None and isinstance(stdout, str):
                row.output_text, stdout = stdout, None
            if isinstance(stdout, str) and stdout and stdout != row.output_text:
                row.stdout_text = stdout
            row.stderr_text = stderr if isinstance(stderr, str) and stderr else None
            if rest.get("formatted_output") in (agg, stdout):
                rest.pop("formatted_output", None)
        elif ptype == "image_generation_end":
            image = rest.pop("result", None)
            if image is not None:
                meta = {"index": 0, **_image_meta({"type": "image", "result": image})}
                if as_str(rest.get("saved_path")):
                    meta["file_name"] = rest["saved_path"]
                row.output_parts = [{k: meta[k] for k in ("index", "type", "mime", "bytes")}]
                rows.extend(self._io_images(io_uid, [meta], ts, pos, turn_id, row.call_uid))
        row.result_json = _dumps(rest) if rest else None
        return rows

    @staticmethod
    def _mcp_content(result: dict[str, Any]) -> tuple[str | None, list[dict[str, Any]] | None]:
        """Pops text/image content off an MCP result dict (in place); returns (text, part metadata)."""
        content = result.pop("content", None)
        if not isinstance(content, list):
            if content is not None:
                result["content"] = content
            return None, None
        text, parts = _join_parts(content)
        other = [b for b in content if isinstance(b, dict) and b.get("type") not in TEXT_PARTS | IMAGE_PARTS]
        if other:
            result["content_other"] = other
        if text is not None and "structuredContent" in result:
            try:
                if json.loads(text) == result["structuredContent"]:
                    del result["structuredContent"]
            except ValueError:
                pass
        return text, [m for m in parts or [] if m["type"] in IMAGE_PARTS or m["type"] == "binary"] or None

    def _io_op(self, item: dict[str, Any], itype: str, item_id: str, turn_id: str | None,
               started: datetime | None, completed: datetime | None, ts: datetime, pos: LinePos) -> list[Row]:
        key = self._key()
        assert key is not None
        known = itype == "CollabAgentToolCall" or self._known_call(item_id) or \
            (itype == "WebSearch" and item_id.startswith("ws_"))
        io_uid = f"item:{item_id}"
        call_uid = item_id if known else None
        row = ToolIoRow(AGENT, io_uid, key, started or ts, pos.byte_offset, "op", tool_name=itype, call_uid=call_uid,
                        item_uid=item_id, turn_key=turn_id, output_at=completed or ts,
                        output_byte_offset=pos.byte_offset)
        rest = {k: v for k, v in item.items() if k not in {"type", "id"}}
        rows: list[Row] = [row]
        if itype == "CommandExecution":
            row.input_text = _text_of(rest.pop("command", None))
            agg = rest.pop("aggregated_output", None)
            row.output_text = _text_of(agg)
            stdout, stderr = rest.pop("stdout", None), rest.pop("stderr", None)
            if isinstance(stdout, str) and stdout and stdout != agg:
                row.stdout_text = stdout
            if isinstance(stderr, str) and stderr:
                row.stderr_text = stderr
            if "formatted_output" in rest and rest["formatted_output"] == agg:
                del rest["formatted_output"]
        elif itype == "McpToolCall":
            row.input_text = _text_of(rest.pop("arguments", None))
            result = rest.pop("result", None)
            if isinstance(result, dict):
                result = dict(result)
                row.output_text, row.output_parts = self._mcp_content(result)
                rows.extend(self._io_images(io_uid, row.output_parts, ts, pos, turn_id, call_uid))
            if result is not None:
                rest["result"] = result
        elif itype == "FileChange":
            changes = rest.pop("changes", None)
            row.input_text = _text_of(changes)
            row.stdout_text = rest.pop("stdout", None) or None
            row.stderr_text = rest.pop("stderr", None) or None
            self._consume_patch(item_id)
            rows.extend(self._touches(io_uid, _change_touches(changes), "FileChange", ts, pos, turn_id,
                                      call_uid=call_uid, item_uid=item_id))
        elif itype == "Extension":
            inp = {k: rest.pop(k) for k in ("action", "query") if k in rest}
            row.input_text = _dumps(inp) if inp else None
            row.output_text = _text_of(rest.pop("results", None))
        elif itype == "ImageView":
            path = rest.pop("path", None)
            row.input_text = _text_of(path)
            if isinstance(path, str) and path:
                rows.append(AttachmentRow(AGENT, f"{io_uid}:att:path", key, ts, pos.byte_offset, "image",
                                          "tool_input", call_uid=call_uid, turn_key=turn_id,
                                          mime=mimetypes.guess_type(path)[0], file_name=path))
        elif itype == "WebSearch":
            inp = {k: rest.pop(k) for k in ("query", "action") if k in rest}
            row.input_text = _dumps(inp) if inp else None
        row.result_json = _dumps(rest) if rest else None
        return rows

    # --- event_msg ----------------------------------------------------------------------------

    def _event(self, ptype: str, p: dict[str, Any], item: dict[str, Any] | None, itype: str,
               ts: datetime | None, pos: LinePos, uid: str) -> list[Row]:
        key = self._key()
        if key is None:
            return []
        if ptype == "task_started":
            return self._task_started(p, ts, pos, uid)
        if ptype == "task_complete":
            return self._turn_end(p, ts, pos, uid, aborted=False)
        if ptype == "turn_aborted":
            return self._turn_end(p, ts, pos, uid, aborted=True)
        if ptype == "token_count":
            return self._token_count(p, ts, pos, uid)
        if ts is None:
            return []
        if ptype == "user_message":
            text = p.get("message").strip() if isinstance(p.get("message"), str) else ""
            images = sum(len(p[k]) for k in ("images", "local_images") if isinstance(p.get(k), list))
            parts: list[dict[str, Any]] = []
            for k, part_type, field in (("images", "image", "image_url"), ("local_images", "local_image", "path")):
                for value in p.get(k) if isinstance(p.get(k), list) else []:
                    parts.append(value if isinstance(value, dict) else {"type": part_type, field: value})
            return self._prompt(text, images, None, ts, pos, uid, as_str(p.get("message")), parts)
        if ptype == "item_completed" and item is not None:
            return self._item(item, itype, p, ts, pos, uid)
        if ptype == "sub_agent_activity":
            return self._activity(p, as_int(p.get("occurred_at_ms")), ts, pos)
        if ptype in {"agent_reasoning", "agent_reasoning_raw_content"}:
            text = self._strings(p.get("text"))
            summary, raw = (text, []) if ptype == "agent_reasoning" else ([], text)
            return self._reasoning_part(summary, raw, ptype, ts, pos, uid, self.s.get("cur"))
        if ptype == "patch_apply_end":
            io = self._end_event(ptype, p, ts, pos, uid)
            return self._changes(p.get("changes"), "patch_apply", as_str(p.get("call_id")),
                                 as_str(p.get("turn_id")) or self.s.get("cur"), ts, pos, uid) + io
        if ptype == "mcp_tool_call_end":
            result = p.get("result")
            call = self.s["calls"].get(as_str(p.get("call_id")) or "")
            if call is not None and isinstance(result, dict) and "Err" in result:
                call["err"] = True
            return self._end_event(ptype, p, ts, pos, uid)
        if ptype in {"web_search_end", "exec_command_end", "image_generation_end"}:
            return self._end_event(ptype, p, ts, pos, uid)
        if ptype == "view_image_tool_call":
            call_id, path = as_str(p.get("call_id")), as_str(p.get("path"))
            if not call_id or not path:
                return []
            return [AttachmentRow(AGENT, f"{call_id}:att:path", key, ts, pos.byte_offset, "image", "tool_input",
                                  call_uid=call_id, turn_key=self.s.get("cur"),
                                  mime=mimetypes.guess_type(path)[0], file_name=path)]
        if ptype == "thread_settings_applied":
            ts_ = p.get("thread_settings") if isinstance(p.get("thread_settings"), dict) else {}
            profile, collab = ts_.get("permission_profile"), ts_.get("collaboration_mode")
            detail = {"model": as_str(ts_.get("model")), "reasoning_effort": as_str(ts_.get("reasoning_effort")),
                      "approval_policy": as_str(ts_.get("approval_policy")),
                      "permission_profile": as_str(profile.get("type")) if isinstance(profile, dict) else None,
                      "collaboration_mode": as_str(collab.get("mode")) if isinstance(collab, dict) else None,
                      "service_tier": as_str(ts_.get("service_tier"))}
            return [SessionEventRow(AGENT, uid, key, ts, "thread_settings", pos.byte_offset, self.s.get("cur"),
                                    detail["model"], {k: v for k, v in detail.items() if v is not None})]
        if ptype == "thread_rolled_back":
            return [SessionEventRow(AGENT, uid, key, ts, "rollback", pos.byte_offset, self.s.get("cur"),
                                    str(as_int(p.get("num_turns"))) if as_int(p.get("num_turns")) is not None else None)]
        if ptype == "thread_goal_updated":
            goal = p.get("goal") if isinstance(p.get("goal"), dict) else {}
            return [SessionEventRow(AGENT, uid, key, ts, "goal_update", pos.byte_offset, self.s.get("cur"),
                                    as_str(goal.get("status")),
                                    {"tokens_used": as_int(goal.get("tokensUsed")),
                                     "time_used_s": as_int(goal.get("timeUsedSeconds"))})]
        return []

    def _item(self, item: dict[str, Any], itype: str, p: dict[str, Any], ts: datetime, pos: LinePos,
              uid: str) -> list[Row]:
        turn_id = as_str(p.get("turn_id")) or self.s.get("cur")
        if itype == "UserMessage":
            content = item.get("content") if isinstance(item.get("content"), list) else []
            text = text_blocks(content, {"text"})
            images = sum(1 for b in content if isinstance(b, dict) and b.get("type") in {"local_image", "image"})
            raw = "\n\n".join(b["text"] for b in content if isinstance(b, dict) and b.get("type") == "text"
                               and isinstance(b.get("text"), str) and b["text"].strip())
            parts = [b for b in content if isinstance(b, dict) and b.get("type") in {"local_image", "image"}]
            skill = any(isinstance(b, dict) and b.get("type") == "skill" for b in content)
            return self._prompt(text, images, turn_id, ts, pos, uid, raw, parts, skill, "UserMessage")
        if itype == "SubAgentActivity":
            return self._activity(item, as_int(p.get("completed_at_ms")), ts, pos)
        if itype == "Reasoning":
            return self._reasoning_part(self._strings(item.get("summary_text")), self._strings(item.get("raw_content")),
                                        "item_completed", ts, pos, uid, turn_id)
        if itype in OP_ITEMS:
            return self._op(item, itype, p, turn_id, ts, pos, uid)
        return []

    def _op(self, item: dict[str, Any], itype: str, p: dict[str, Any], turn_id: str | None,
            ts: datetime, pos: LinePos, uid: str) -> list[Row]:
        key = self._key()
        item_id = as_str(item.get("id"))
        if key is None or not item_id:
            return [ParseIssueRow(pos.byte_offset, "missing_item_id", pos.line_number, itype)]
        started = parse_ts(as_int(p.get("started_at_ms")))
        completed = parse_ts(as_int(p.get("completed_at_ms")))
        duration = _duration_ms(item.get("duration"))
        if duration is None and itype == "Extension":
            duration = as_int(item.get("durationMs"))
        if duration is None and started and completed:
            duration = int((completed - started).total_seconds() * 1000)
        status = as_str(item.get("status"))
        op = ToolOpRow(AGENT, item_id, key, itype, pos.byte_offset, turn_key=turn_id, started_at=started,
                       completed_at=completed, duration_ms=duration, status=status)
        rows: list[Row] = [op]
        if itype == "CommandExecution":
            op.exit_code = as_int(item.get("exit_code"))
            op.exec_source = as_str(item.get("source"))
            op.cmd_verb = cmd_verb(item.get("command"))
            types = sorted({pc["type"][:32] for pc in item.get("parsed_cmd") or []
                            if isinstance(pc, dict) and isinstance(pc.get("type"), str)})
            op.parsed_cmd_types = types or None
            op.output_bytes = json_size(item.get("aggregated_output"))
            op.is_error = status == "failed" or (op.exit_code is not None and op.exit_code != 0)
            if op.cmd_verb in {"git", "gh"} or git_ops_from_command(item.get("command")):
                rows.extend(self._git(item, op, ts, pos))
        elif itype == "McpToolCall":
            op.mcp_server, op.mcp_tool = as_str(item.get("server")), as_str(item.get("tool"))
            op.mcp_plugin_id = as_str(item.get("pluginId"))
            op.mcp_read_only = item.get("readOnlyHint") if isinstance(item.get("readOnlyHint"), bool) else None
            result = item.get("result")
            op.is_error = bool(status == "failed" or item.get("error") is not None or
                               (isinstance(result, dict) and as_bool(result.get("isError"))))
            op.output_bytes = json_size(result)
        elif itype == "FileChange":
            changes = item.get("changes")
            op.file_count = len(changes) if isinstance(changes, dict) else None
            op.is_error = status not in {None, "completed"}
            rows.extend(self._changes(changes, "file_change", None, turn_id, ts, pos, uid))
        elif itype == "Extension":
            op.exec_source = as_str(item.get("kind"))
        elif itype == "CollabAgentToolCall":
            op.exec_source = as_str(item.get("tool"))
            op.is_error = status not in {None, "completed"}
            op.call_uid, op.link_method = item_id, "item_id"
        rows.extend(self._io_op(item, itype, item_id, turn_id, started, completed, ts, pos))
        return rows

    def _git(self, item: dict[str, Any], op: ToolOpRow, ts: datetime, pos: LinePos) -> list[Row]:
        key = self._key()
        assert key is not None
        output = item.get("aggregated_output")
        output = output if isinstance(output, str) else ""
        command = _command_text(item.get("command"))
        cwd = as_str(item.get("cwd"))
        rows: list[Row] = []
        commits, pushes = git_from_output(output)
        if not op.is_error:
            for kind, n in git_event_extras(git_ops_from_command(item.get("command")), len(commits), len(pushes)):
                rows.append(GitEventRow(AGENT, f"{op.item_uid}:{'push' if kind == 'push' else 'commit'}:cmd{n}", key,
                                        ts, kind, "command", pos.byte_offset, op.turn_key, cwd=cwd))
        commit_op = "cherry_pick" if "cherry-pick" in command else "commit"
        for branch, sha in commits:
            rows.append(GitEventRow(AGENT, f"{op.item_uid}:commit:{sha}", key, ts, commit_op, "output_regex",
                                    pos.byte_offset, op.turn_key, cwd=cwd, branch=branch, sha_short=sha[:12]))
        for _old, new, _src, dst in pushes:
            rows.append(GitEventRow(AGENT, f"{op.item_uid}:push:{new}", key, ts, "push", "output_regex",
                                    pos.byte_offset, op.turn_key, cwd=cwd, branch=dst[:200], sha_short=new[:12]))
        if op.cmd_verb == "gh" and output:
            match = re.search(r"\bgh\s+pr\s+([a-z-]+)", command)
            action = match.group(1) if match and match.group(1) in PR_ACTIONS else None
            seen: set[str] = set()
            for m in PR_URL_RE.finditer(output):
                if m.group(0) in seen:
                    continue
                seen.add(m.group(0))
                rows.append(GitEventRow(AGENT, f"{op.item_uid}:pr:{m.group(1)}#{m.group(2)}", key, ts, "pr",
                                        "output_regex", pos.byte_offset, op.turn_key, cwd=cwd,
                                        pr_number=int(m.group(2)), pr_action=action, pr_url=m.group(0),
                                        pr_repo=m.group(1)))
        return rows

    def _changes(self, changes: Any, evidence: str, call_id: str | None, turn_id: str | None,
                 ts: datetime, pos: LinePos, uid: str) -> list[Row]:
        key = self._key()
        if key is None or not isinstance(changes, dict):
            return []
        rows: list[Row] = []
        for path, change in changes.items():
            if not isinstance(path, str) or not path:
                continue
            change = change if isinstance(change, dict) else {}
            action = as_str(change.get("type")) or "update"
            target = path
            if as_str(change.get("move_path")):
                action, target = "move", change["move_path"]
            rows.append(ArtifactRow(AGENT, uid, key, ts, artifact_kind(target), action, target, evidence,
                                    pos.byte_offset, turn_id, call_id, target.rsplit("/", 1)[-1] or None))
        return rows

    def _activity(self, a: dict[str, Any], at_ms: int | None, ts: datetime, pos: LinePos) -> list[Row]:
        key = self._key()
        path = as_str(a.get("agent_path"))
        if key is None or not path:
            return []
        spawn = self.s["spawns"].get(path.rsplit("/", 1)[-1])
        if spawn is None:
            return []
        kind = as_str(a.get("kind"))
        child = as_str(a.get("agent_thread_id"))
        done = kind in {"completed", "interrupted"}
        return [SubagentSpawnRow(AGENT, spawn["c"], key, pos.byte_offset, child_session_uid=child,
                                 completion_status=kind if done else None,
                                 completed_at=(parse_ts(at_ms) or ts) if done else None)]

    # --- tokens, rate limits, compaction ------------------------------------------------------

    def _legacy_enabled(self) -> bool:
        if self.s["tur"]:
            return False
        version = _version(self.s.get("ver"))
        return version is None or version < TOKEN_RECORD_VERSION

    def _token_count(self, p: dict[str, Any], ts: datetime | None, pos: LinePos, uid: str) -> list[Row]:
        key = self._key()
        if key is None or ts is None:
            return []
        rows: list[Row] = self._rate_limits(p.get("rate_limits"), ts, pos, uid)
        info = p.get("info")
        if not isinstance(info, dict):
            return rows
        sig = _usage_sig(info.get("total_token_usage"))
        if sig is None:
            return rows
        changed = sig != self.s["prev_total"]
        if self.s["base"] is None:
            prev = json.loads(self.s["prev_total"]) if self.s["prev_total"] else {}
            self.s["base"] = as_int(prev.get("total_tokens")) or 0
        self.s["prev_total"] = sig
        last = info.get("last_token_usage")
        if not changed or not self._legacy_enabled() or not isinstance(last, dict) or not last:
            return rows
        turn_id = self.s.get("cur")
        turn = self._turn(turn_id, ts) if turn_id else {}
        rows.append(LlmCallRow(AGENT, f"codex-legacy:{uid}", key, ts, pos.byte_offset, turn_key=turn_id,
                               model=turn.get("m") or self.s["model"], effort=turn.get("ef") or self.s["effort"],
                               context_window=as_int(info.get("model_context_window")) or turn.get("cw"),
                               line_count=1, service_tier=self.s["service_tier"], **_normalise(last)))
        return rows

    def _token_record(self, p: dict[str, Any], ts: datetime | None, pos: LinePos, uid: str) -> list[Row]:
        key = self._key()
        self.s["tur"] = True
        usage = p.get("usage")
        if key is None or ts is None or not isinstance(usage, dict):
            return []
        turn_id = as_str(p.get("turn_id")) or self.s.get("cur")
        turn = self._turn(turn_id, ts) if turn_id else {}
        response_id = as_str(p.get("response_id")) or f"codex-tur:{uid}"
        return [LlmCallRow(AGENT, response_id, key, ts, pos.byte_offset, turn_key=turn_id,
                           model=turn.get("m") or self.s["model"], effort=turn.get("ef") or self.s["effort"],
                           context_window=turn.get("cw"), line_count=1, service_tier=self.s["service_tier"],
                           **self._call_latency(p), **_normalise(usage))]

    def _rate_limits(self, rl: Any, ts: datetime, pos: LinePos, uid: str) -> list[Row]:
        key = self._key()
        if key is None or not isinstance(rl, dict):
            return []
        reached = as_str(rl.get("rate_limit_reached_type")) or ("spend_control" if rl.get("spend_control_reached") else None)
        rows: list[Row] = []
        windows: list[tuple[str, dict[str, Any]]] = [(k, rl[k]) for k in ("primary", "secondary")
                                                     if isinstance(rl.get(k), dict)]
        credits = rl.get("credits")
        if isinstance(credits, dict):
            status = "unlimited" if credits.get("unlimited") else ("has_credits" if credits.get("has_credits") else "none")
            windows.append(("credits", {"limit_name": status}))
        for kind, w in windows:
            used = w.get("used_percent")
            fields = (float(used) if isinstance(used, (int, float)) and not isinstance(used, bool) else None,
                      as_int(w.get("window_minutes")), as_int(w.get("resets_at")),
                      as_str(rl.get("limit_id")), as_str(rl.get("plan_type")), reached, w.get("limit_name"))
            sig = json.dumps(fields)
            if self.s["rl"].get(kind) == sig:
                continue
            self.s["rl"][kind] = sig
            rows.append(RateLimitRow(AGENT, uid, key, ts, kind, pos.byte_offset, limit_id=fields[3],
                                     limit_name=as_str(w.get("limit_name")) or as_str(rl.get("limit_name")),
                                     plan_type=fields[4], used_percent=fields[0], window_minutes=fields[1],
                                     resets_at=parse_ts(w.get("resets_at")), reached_type=reached))
        return rows

    def _compacted(self, p: dict[str, Any], ts: datetime | None, pos: LinePos, uid: str) -> list[Row]:
        key = self._key()
        if key is None or ts is None:
            return []
        latest = p.get("latest_token_usage_record")
        usage = latest.get("usage") if isinstance(latest, dict) and isinstance(latest.get("usage"), dict) else {}
        turn_id = self.s.get("cur")
        rows: list[Row] = [CompactionRow(
            AGENT, uid, key, ts, pos.byte_offset, turn_id, pre_tokens=as_int(usage.get("input_tokens")),
            window_number=as_int(p.get("window_number")), window_id=as_str(p.get("window_id")),
            previous_window_id=as_str(p.get("previous_window_id")))]
        summary = p.get("message")
        if isinstance(summary, str) and summary.strip():
            rows.append(MessageRow(AGENT, uid, key, ts, "user", "compaction_summary", summary.strip(),
                                   pos.byte_offset, pos.byte_length, pos.line_number, turn_id))
        return rows

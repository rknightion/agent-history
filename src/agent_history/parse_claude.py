"""Claude Code transcript parser for the agent-history catalogue.

Pure: turns decoded JSONL records into `model` rows. The loader reads lines, batches, upserts and
persists `state()`. Contract: model.py and CONTRACT.md.

Identity. Every key is derived from content ids so the same record seen in another file (a
fork replaying a sibling's prefix, an archived copy) produces the same key:
- message / hook / compaction / session_event / rate_limit rows: line `uuid`, with a suffix when a
  line yields several rows (`<uuid>:<n>`, `<call_id>:brief`, `<call_id>:report`).
- llm_call: `message.id` (one row per API response; every content-block line re-emits it and the
  MAX policies keep the final usage). Lines without an id fall back to the line uuid.
- tool_call / subagent_spawn: the `tool_use` id. Workflow journal agents: `wf:<wf_id>:<agentId>`.
- git_event: `<call_uid>:commit:<sha[:7]>`, `<call_uid>:push:<branch>`, `<call_uid>:pr:<n>`, so
  `toolUseResult.gitOperation` and the output regex fallback produce the same key.
- task-notification facts: `tn:<task-id>:<sha16 of the notification>`; the same notification is
  written as a user line and as a queued_command attachment, and both collapse.
- Records with no uuid (permission-mode, cost-state, queue-operation, pr-link, ...) use
  `<sessionId>/<agentId>@<byte_offset>` (session-prefixed offset, last resort) or a content id.

Turns. `turn_key` = the user line's `promptId`, else `u:<uuid>`. Assistant lines inherit the
current turn. A turn's `origin` is decided by its first *strong* user line (human, queued, sdk,
task_notification, peer, coordinator, slash_command, compaction_summary, bash, subagent_brief).
Weak lines (isMeta, interrupt markers) are held in state and only emitted when the turn closes
(first assistant line, next turn, or turn_duration) without a strong line, so the result does not
depend on batch boundaries.

Workflow journals emit no session or messages: each `started`/`result` record becomes a
SubagentSpawnRow keyed `wf:<wf_id>:<agentId>` in the parent session (launch/completion facts only;
the `result` payload is never read).

Parser v4 (content contract lifted; every v3 row is still emitted unchanged, new rows only add):

Reasoning. Each assistant `thinking` block -> MessageRow class reasoning, role assistant,
  event_uid "<line uuid>:think:<index in message.content>", text = the thinking text as recorded.
  Empty thinking with a signature -> text '' + detail {"signature_only": true}; `redacted_thinking`
  -> text '' + detail {"redacted": true}. Empty thinking without a signature is skipped.
Harness-injected content (MessageRow, detail {"source": <attachment type | tag | record field>}):
  event_uid = the line uuid when a line yields one row, "<uuid>:<suffix>" when it yields several
  (prompt_snapshot ":tools"/":ctx", instructions/invoked_skills ":<n>", hook fields ":<field>").
  User lines (the class v3 left empty; v3 classes are never re-decided):
    <command-name>/<command-message> in a main-file, non-isMeta line -> human_prompt, prompt_origin
      'skill' when the line starts with <command-message> (the layout Claude Code writes for skill
      invocations; 177 of 195 such lines were followed by a skill body in the local corpus, while
      <command-name>-first lines never were) or the command name is plugin-namespaced (contains
      ':'), else 'slash_command'. <bash-input> in a main file -> human_prompt, 'local_command'.
      (Session first/last_human_at are NOT moved by these new rows, so v3 session columns hold.)
    <local-command-stdout|stderr>, <bash-stdout|stderr> -> local_command_output.
    <system-reminder>, <local-command-caveat> -> system_reminder. "[Request interrupted" ->
      interrupt_marker. Task notifications (user line or queued_command mirror) -> agent_message
      "tn:<task-id>:<sha16>:text" (both copies collapse, as v3's :summary/:result rows do).
    isMeta: sourceToolUseID or "Base directory for this skill" -> skill_body; the first isMeta line
      after a slash command -> command_expansion (skill_body when that command was a skill;
      state["after_cmd"]); origin.kind peer/coordinator/human/... -> agent_message (source = kind);
      "Stop hook feedback:" -> hook_output; "[Image" annotations -> context_injection; any other
      isMeta text -> system_reminder (source "isMeta").
    Non-meta: origin.kind other than human, promptSource sdk/other -> agent_message (source =
      kind or promptSource); subagent/workflow user lines after the first (SendMessage follow-ups)
      -> agent_message (source "followup"). The first user line of a subagent file stays unstored
      (it duplicates the parent's "<call_id>:brief").
  Attachments: prompt_snapshot.systemPrompt -> system_prompt (role system; a list of blocks is
    joined with "\n\n", detail blocks=N); .tools -> context_injection ":tools" (JSON); the other
    non-boolean keys -> context_injection ":ctx". Reminder types (total_tokens_reminder,
    output_style, silent_turn_reminder, batching_reminder_sent, task_reminder, date, date_change,
    auto_mode*, plan_mode*, ultra*_effort*, budget_usd, max_turns_reached, read_truncation_notice,
    ...) -> system_reminder. Hook attachments -> hook_output per non-empty content/stdout/stderr/
    response field (HookEventRow as v3). invoked_skills -> skill_body per skill. queued_command in a
    non-main file -> agent_message. Everything else the harness injects (nested_memory,
    instructions (one row per file), session_context, environment, skill_listing, *_delta,
    deferred_tools_record, output_style_instructions, command_permissions, diagnostics,
    edited_text_file, compact_file_reference, file, directory, remote_session_change,
    credential_org, model, unknown future types) -> context_injection. Text = the type's natural
    text field, else compact JSON of the attachment minus `type`. Not stored as messages:
    structured_output (the StructuredOutput tool input carries it), bash_output_audience_note (ids
    only), thinking_drop (client telemetry). system/local_command stdout/stderr ->
    local_command_output (role system); system/stop_hook_summary hookErrors /
    hookAdditionalContext / stopReason -> hook_output ":errors" / ":context" / ":stop_reason".
  Roles: system_prompt and rows from `system` records are 'system'; everything else 'user'.
prompt_origin: set on every human_prompt / queued_prompt. v3 rows: common.prompt_origin(text),
  except a queued_command whose origin.kind is not human or that is isMeta (peer/coordinator
  messages v3 stores as queued_prompt) -> 'agent_message'.
Tool I/O (ToolIoRow kind 'call', io_uid = call_uid = tool_use id). Call half: tool_name,
  input_text = json.dumps(block["input"], ensure_ascii=False) (default separators, key order as
  recorded; base64 source blocks replaced by metadata first, a no-op in practice), ts = call ts.
  Result half: output_text = content string as-is, or the text parts joined like
  common.text_blocks (stripped, "\n\n"); output_parts = [{"index","type","mime","bytes"}] for
  image/document parts ({"index","type","tool_name"} for tool_reference); stdout_text / stderr_text
  = toolUseResult.stdout/.stderr when present; result_json = json.dumps of toolUseResult minus
  stdout/stderr (any JSON type), every base64 payload (source.data, file.base64) replaced by
  {"type":"base64","mime","bytes"}; output_truncated when persistedOutputPath / truncated=true;
  output_at = ts = result ts. Only the half in hand is emitted (early results merge). A
  read_truncation_notice emits ToolIoRow(io_uid=toolUseID, output_truncated=True).
Attachments (AttachmentRow): prompt/queued/meta image and document blocks "<uuid>:att:<n>" (mime,
  size_bytes = decoded base64 size, detail paste_id from imagePasteIds; paste ids with no image
  block get a metadata-only row), pasted text in human/queued prompts (<pasted_content ...>
  bodies; "[Pasted text #N +M lines]" placeholders as metadata), tool-result images/documents
  "<call_uid>:att:<n>" (source tool_result), `file` attachment records (kind file, text = content).
File touches (FileTouchRow, touch_uid "<call_uid>:0", written once at the call): Read/NotebookRead
  -> read; Write -> create, lines_added = lines of content; Edit -> edit, lines from a line diff of
  old_string vs new_string (difflib; plain line counts when either side exceeds 2000 lines);
  MultiEdit -> edit, summed over edits; NotebookEdit -> edit (new_source lines). The call half
  wins because the row is DO NOTHING and precedes its result; the result's structuredPatch stays
  in tool_io.result_json. Glob/Grep are searches, not touches.
Continuations (ContinuationRow): `continued-in` -> (child continuedInSessionId, parent this
  session, kind resume); a main-file record whose sessionId differs from the path -> (child path
  session, parent that id, kind other, evidence sessionId_mismatch); fork-context-ref with a
  parentSessionId other than the path session -> kind fork. Resumes that copy history into a new
  file without a marker, /clear and cross-file compaction are not visible to a per-file parser.
"""

from __future__ import annotations

import difflib
import json
import re
from datetime import datetime, timedelta
from pathlib import PurePosixPath
from typing import Any, Iterable

from .common import (BOUNDED_STATE_SECONDS, as_bool, as_int, as_str, artifact_kind, cmd_verb, ssh_target,
                     git_ops_from_command, git_event_extras, git_from_output, json_size, key_set, linked_paths, mcp_split, parse_ts,
                     prompt_origin, sha256_text, split_legacy_peer_injections, split_prompt_injections, text_blocks)
from .model import (ArtifactRow, AttachmentRow, CompactionRow, ContinuationRow, CostStateRow,
                    FileContext, FileTouchRow, GitEventRow, HookEventRow, LinePos, LlmCallRow,
                    MessageRow, ParseIssueRow, RateLimitRow, RecordTypeRow, Row, SessionEventRow,
                    SessionKey, SessionRow, SubagentSpawnRow, ToolCallRow, ToolIoRow, TurnRow)

AGENT = "claude"

KNOWN_TYPES = {
    "user", "assistant", "attachment", "system", "last-prompt", "mode", "permission-mode",
    "ai-title", "custom-title", "atis-latch", "queue-operation", "bridge-session",
    "file-history-snapshot", "file-history-delta", "pr-link", "cost-state", "agent-name",
    "agent-setting", "frame-link", "artifact-comment-monitor", "artifact-autoreact-ledger",
    "fork-context-ref", "summary", "tag", "worktree-state", "continued-in",
}
SPAWN_TOOLS = {"Agent", "Task"}
COLLAB_TOOLS = {"Agent", "Task", "SendMessage", "SubagentHandback", "StructuredOutput", "Workflow",
                "TeamCreate", "TeamDelete", "TaskStop"}
TASK_TOOLS = {"TaskCreate", "TaskUpdate", "TaskList", "TaskGet", "TodoWrite", "TaskStop"}
FILE_TOOLS = {"Read": "read", "Write": "written", "Edit": "edited", "MultiEdit": "edited",
              "NotebookEdit": "edited", "NotebookRead": "read"}
HOOK_ATTACHMENTS = {"hook_success", "hook_non_blocking_error", "hook_blocking_error",
                    "hook_cancelled", "hook_additional_context", "hook_system_message",
                    "async_hook_response", "hook_error_during_execution", "hook_stopped_continuation"}
STRONG = {"human", "queued", "sdk", "task_notification", "peer", "coordinator", "slash_command",
          "compaction_summary", "bash", "subagent_brief", "scheduled"}
EXIT_CODE = re.compile(r"^(?:Error: )?Exit code (-?\d+)")
TN_TAG = re.compile(r"<(task-id|tool-use-id|status|subagent_tokens|tool_uses|duration_ms)>\s*([^<]{0,200}?)\s*</\1>")
TN_SUMMARY = re.compile(r"<summary>(.*?)</summary>", re.S)
TN_RESULT = re.compile(r"<result>(.*?)</result>", re.S)
SLASH = re.compile(r"<command-name>\s*(/?[\w:.\-]+)")
STATE_KEYS_MAX = 512
RESPONSE_SEEN_BITS = 65536  # Fixed-size identity filter; collisions only suppress unknown durations.

# --- parser v4 -------------------------------------------------------------------------------
TOUCH_TOOLS = {"Read": "read", "NotebookRead": "read", "Write": "create", "Edit": "edit",
               "MultiEdit": "edit", "NotebookEdit": "edit"}
DIFF_MAX_LINES = 2000
PASTED_BLOCK = re.compile(r"<pasted_content(\s[^>]*)?>(.*?)</pasted_content>", re.S)
PASTED_PLACEHOLDER = re.compile(r"\[Pasted text #(\d+)(?: \+(\d+) lines)?\]")
PASTE_ID_ATTR = re.compile(r"""\b(?:id|name)=["']?([^"'\s>]{1,80})""")
BINARY_PARTS = {"image", "document"}
# attachment type -> natural text field (None: compact JSON of the attachment minus `type`)
REMINDER_ATTACHMENTS = {
    "total_tokens_reminder": "text", "output_style": "turnReminder", "silent_turn_reminder": "text",
    "batching_reminder_sent": "text", "task_reminder": None, "date": "date", "date_change": "newDate",
    "auto_mode": None, "auto_mode_exit": None, "plan_mode": None, "plan_mode_exit": None,
    "plan_mode_reentry": None, "ultra_effort_enter": None, "ultra_effort_exit": None,
    "ultrathink_effort": None, "budget_usd": None, "max_turns_reached": None,
    "read_truncation_notice": "banner", "critical_system_reminder": "content", "todo_reminder": None,
    "thinking_stripped": None,
}
CONTEXT_TEXT_FIELDS = {"skill_listing": "content", "edited_text_file": "snippet", "model": "text",
                       "directory": "content"}
SKIP_ATTACHMENTS = {"queued_command", "structured_output", "bash_output_audience_note", "thinking_drop"}
HOOK_TEXT_FIELDS = ("content", "stdout", "stderr", "response", "message")


def _bg(value: Any) -> bool | None:
    b = as_bool(value)
    return b


def _strip_prefix(text: str) -> str:
    return text.lstrip()


def _telemetry_int(value: Any, *, bigint: bool = False) -> int | None:
    """Recorded non-negative integer, without coercion or SQL overflow."""
    maximum = 2**63 - 1 if bigint else 2**31 - 1
    return value if type(value) is int and 0 <= value <= maximum else None


def _transform_types(value: Any) -> list[str] | None:
    if not isinstance(value, list):
        return None
    if any(not isinstance(item, dict) or as_str(item.get("type")) is None for item in value):
        return None
    return [item["type"] for item in value]


class ClaudeParser:
    def __init__(self, ctx: FileContext, state: dict[str, Any]) -> None:
        self.ctx = ctx
        s = dict(state or {})
        self.s = s
        s.setdefault("last_ts", None)
        s.setdefault("turn", None)            # current turn key
        s.setdefault("turn_final", False)     # current turn has a strong origin
        s.setdefault("turn_pending", None)    # [origin, byte_offset, ts] weak origin not yet emitted
        s.setdefault("turn_model", None)
        s.setdefault("turn_effort", None)
        s.setdefault("open_calls", {})        # id -> [name, ts_iso, turn, byte_offset]
        s.setdefault("closed_calls", {})      # recently answered calls, same shape (bounded LRU)
        s.setdefault("early_results", {})     # results seen before their call: id -> [ts_iso, had_duration]
        s.setdefault("git_cmds", {})          # Bash tool_use id -> command's git ops (only when it has any)
        s.setdefault("spawns", {})            # tool_use id -> [kind, ts_iso] (Agent/Task/Workflow calls)
        s.setdefault("msg_lines", {})         # message.id -> line count (bounded LRU)
        s.setdefault("record_ts", {})         # uuid -> explicit timestamp or NULL (bounded)
        s.setdefault("response_parents", {})  # response id -> first line's causal parent uuid
        # Never forget that a response was observed, even after its exact lineage is evicted.
        # An older snapshot lacks this history, so only its retained exact parents remain usable.
        s.setdefault("response_seen", "f" * (RESPONSE_SEEN_BITS // 4) if state else "0")
        s.setdefault("last", {})              # change detectors: perm, mode, model, effort, rl:<kind>
        s.setdefault("perm_pending", None)    # [value, byte_offset] seen before any timestamp
        s.setdefault("first_offset", None)
        s.setdefault("spawn_kind", None)
        s.setdefault("first_user_seen", False)
        s.setdefault("mismatch", False)
        s.setdefault("after_cmd", None)       # v4: 'skill' | 'slash_command' until its expansion line
        s.setdefault("brief_seen", False)     # v4: a subagent file's first non-meta user line was seen
        self.role = ctx.file_role
        self.sid, self.aid, self.wf_id = self._path_ids(ctx)
        self.key = SessionKey(AGENT, self.sid, self.aid or "")
        self.kprefix = f"{self.sid}/{self.aid or ''}"
        self._reset_batch()

    # -- bookkeeping ------------------------------------------------------------------------

    @staticmethod
    def _path_ids(ctx: FileContext) -> tuple[str, str | None, str | None]:
        parts = PurePosixPath(ctx.rel_path or ctx.path).parts
        name = parts[-1] if parts else ""
        stem = name[:-6] if name.endswith(".jsonl") else name
        wf_id = None
        if "subagents" in parts:
            idx = len(parts) - 1 - list(reversed(parts)).index("subagents")
            sid = parts[idx - 1] if idx > 0 else ""
            if idx + 2 < len(parts) and parts[idx + 1] == "workflows":
                wf_id = parts[idx + 2]
            aid = stem[6:] if stem.startswith("agent-") else None
            if name == "journal.jsonl":
                aid = None
            return sid, aid, wf_id
        return stem, None, None

    def _reset_batch(self) -> None:
        self.b: dict[str, Any] = {"first": None, "last": None, "first_h": None, "last_h": None,
                                  "cwd": None, "branch": None, "ver_first": None, "ver_last": None,
                                  "entry": None, "title": None, "custom": None, "nick": None,
                                  "commit": None,
                                  "seen": False}
        self.types: dict[tuple[str, str], list[Any]] = {}

    def state(self) -> dict[str, Any]:
        self._prune()
        return self.s

    def _prune(self) -> None:
        last = parse_ts(self.s.get("last_ts"))
        for name in ("open_calls", "spawns", "early_results"):
            table = self.s[name]
            if last is None:
                continue
            cutoff = last - timedelta(seconds=BOUNDED_STATE_SECONDS)
            for k in list(table):
                v = table[k]
                t = parse_ts(v[0] if name == "early_results" else v[1])
                if t is not None and t < cutoff:
                    del table[k]
        ml = self.s["msg_lines"]
        if len(ml) > STATE_KEYS_MAX:
            for k in list(ml)[: len(ml) - STATE_KEYS_MAX]:
                del ml[k]
        cc = self.s["closed_calls"]
        if len(cc) > STATE_KEYS_MAX:
            for k in list(cc)[: len(cc) - STATE_KEYS_MAX]:
                del cc[k]
        er = self.s["early_results"]
        if len(er) > STATE_KEYS_MAX:
            for k in list(er)[: len(er) - STATE_KEYS_MAX]:
                del er[k]
        gc = self.s["git_cmds"]
        if len(gc) > STATE_KEYS_MAX:
            for k in list(gc)[: len(gc) - STATE_KEYS_MAX]:
                del gc[k]
        oc = self.s["open_calls"]
        if len(oc) > 4 * STATE_KEYS_MAX:
            for k in list(oc)[: len(oc) - 4 * STATE_KEYS_MAX]:
                del oc[k]

    def _stale_calls(self) -> list[Row]:
        """Open calls older than 24 h of transcript time become outcome no_result (M1)."""
        rows: list[Row] = []
        last = parse_ts(self.s.get("last_ts"))
        if last is None:
            return rows
        cutoff = last - timedelta(seconds=BOUNDED_STATE_SECONDS)
        for cid, (name, ts_iso, turn, off) in list(self.s["open_calls"].items()):
            t = parse_ts(ts_iso)
            if t is not None and t < cutoff:
                rows.append(ToolCallRow(agent=AGENT, call_uid=cid, session=self.key, tool_name=name,
                                        byte_offset=off, turn_key=turn, outcome="no_result"))
                del self.s["open_calls"][cid]
        return rows

    def flush(self) -> Iterable[Row]:
        rows: list[Row] = []
        rows.extend(self._stale_calls())
        if self.role != "workflow_journal" and self.b["seen"]:
            rows.append(self._session_row())
        for (rtype, sub), (count, ks, ver, ts) in self.types.items():
            rows.append(RecordTypeRow(agent=AGENT, record_type=rtype, subtype=sub,
                                      ts=ts or datetime.fromtimestamp(0).astimezone(), count=count,
                                      key_set=ks, cli_version=ver))
        self._reset_batch()
        return rows

    def _session_row(self) -> SessionRow:
        b = self.b
        sub = self.role in ("subagent", "workflow_agent")
        kind = None
        if self.role == "workflow_agent":
            kind = "workflow"
        elif self.role == "subagent":
            kind = self.s.get("spawn_kind")
        return SessionRow(
            session=self.key, byte_offset=self.s.get("first_offset") or 0, is_subagent=sub,
            root_session_uid=self.sid if sub else None,
            parent_session_uid=self.sid if sub else None, spawn_kind=kind,
            workflow_id=self.wf_id, title=b["title"], custom_title=b["custom"], cwd=b["cwd"],
            git_branch=b["branch"], git_commit_start=b["commit"],
            cli_version_first=b["ver_first"], cli_version_last=b["ver_last"],
            entrypoint=b["entry"], agent_nickname=b["nick"], first_event_at=b["first"],
            last_event_at=b["last"], first_human_at=b["first_h"], last_human_at=b["last_h"])

    # -- helpers ----------------------------------------------------------------------------

    def _uid(self, rec: dict[str, Any], pos: LinePos) -> str:
        u = as_str(rec.get("uuid"))
        return u if u else f"{self.kprefix}@{pos.byte_offset}"

    def _count_type(self, rec: dict[str, Any], rtype: str, sub: str, ts: datetime | None) -> None:
        entry = self.types.get((rtype, sub))
        if entry is None:
            self.types[(rtype, sub)] = [1, key_set(rec), as_str(rec.get("version")), ts]
        else:
            entry[0] += 1
            entry[1] = key_set(rec)
            entry[2] = as_str(rec.get("version")) or entry[2]
            if ts is not None:
                entry[3] = ts

    def _event(self, out: list[Row], uid: str, ts: datetime | None, kind: str, pos: LinePos,
               value: Any = None, detail: dict[str, Any] | None = None) -> None:
        if ts is None:
            return
        out.append(SessionEventRow(agent=AGENT, event_uid=uid, session=self.key, ts=ts, kind=kind,
                                   byte_offset=pos.byte_offset, turn_key=self.s["turn"],
                                   value=None if value is None else str(value)[:200],
                                   detail=detail or None))

    def _changed(self, name: str, value: Any) -> bool:
        if value is None:
            return False
        last = self.s["last"]
        if last.get(name) == value:
            return False
        last[name] = value
        return True

    # -- turns ------------------------------------------------------------------------------

    def _close_pending(self, out: list[Row]) -> None:
        pend = self.s.get("turn_pending")
        if pend and not self.s["turn_final"] and self.s["turn"]:
            origin, off, ts_iso = pend
            out.append(TurnRow(session=self.key, turn_key=self.s["turn"], byte_offset=off,
                               origin=origin, started_at=parse_ts(ts_iso)))
        self.s["turn_pending"] = None
        self.s["turn_final"] = True

    def _start_or_join_turn(self, out: list[Row], key: str, origin: str | None, strong: bool,
                            ts: datetime | None, pos: LinePos, perm: str | None) -> None:
        if key != self.s["turn"]:
            self._close_pending(out)
            self.s["turn"] = key
            self.s["turn_final"] = False
            self.s["turn_pending"] = None
            self.s["turn_model"] = None
            self.s["turn_effort"] = None
        if origin is None:
            return
        if strong and not self.s["turn_final"]:
            self.s["turn_final"] = True
            self.s["turn_pending"] = None
            out.append(TurnRow(session=self.key, turn_key=key, byte_offset=pos.byte_offset,
                               origin=origin, started_at=ts, permission_mode=perm))
        elif not self.s["turn_final"]:
            if self.s["turn_pending"] is None:
                self.s["turn_pending"] = [origin, pos.byte_offset, ts.isoformat() if ts else None]
            out.append(TurnRow(session=self.key, turn_key=key, byte_offset=pos.byte_offset,
                               started_at=ts, permission_mode=perm))
        elif perm or ts:
            out.append(TurnRow(session=self.key, turn_key=key, byte_offset=pos.byte_offset,
                               started_at=ts, permission_mode=perm))

    # -- entry point ------------------------------------------------------------------------

    def line(self, record: dict[str, Any], pos: LinePos) -> Iterable[Row]:
        out: list[Row] = []
        rtype = record.get("type")
        if not isinstance(rtype, str):
            self._count_type(record, "<none>", "", None)
            out.append(ParseIssueRow(byte_offset=pos.byte_offset, kind="missing_field",
                                     line_number=pos.line_number, detail="type"))
            return out
        sub = ""
        if rtype == "system":
            sub = as_str(record.get("subtype")) or ""
        elif rtype == "attachment" and isinstance(record.get("attachment"), dict):
            sub = as_str(record["attachment"].get("type")) or ""
        ts = parse_ts(record.get("timestamp"))
        if ts is not None:
            self.s["last_ts"] = ts.isoformat()
        eff_ts = ts or parse_ts(self.s.get("last_ts"))
        self._count_type(record, rtype, sub, eff_ts)

        if self.role == "workflow_journal":
            self._journal(record, pos, out)
            return out

        if self.s.get("first_offset") is None:
            self.s["first_offset"] = pos.byte_offset
        self._session_fields(record, ts, pos, out)

        handler = getattr(self, "_t_" + rtype.replace("-", "_"), None)
        if handler is not None:
            handler(record, pos, eff_ts, sub, out)
        elif rtype not in KNOWN_TYPES:
            out.append(ParseIssueRow(byte_offset=pos.byte_offset, kind="unknown_type",
                                     line_number=pos.line_number, detail=f"{rtype[:60]}"))
        # Only the record's own timestamp is evidence, never eff_ts inherited from a neighbour.
        uuid = as_str(record.get("uuid"))
        if uuid:
            stamps = self.s["record_ts"]
            stamp = ts.isoformat() if ts is not None else None
            if uuid in stamps and stamps[uuid] != stamp:
                stamp = None  # Conflicting/missing observations of this key are unattributable.
            stamps[uuid] = stamp
            self._bound_telemetry(stamps, 4 * STATE_KEYS_MAX)
        return out

    @staticmethod
    def _bound_telemetry(table: dict[str, Any], maximum: int) -> None:
        # Bound at each insertion, not flush, so eviction is independent of batch size.
        while len(table) > maximum:
            del table[next(iter(table))]

    def _call_duration(self, rec: dict[str, Any], msg: dict[str, Any]) -> int | None:
        mid = as_str(msg.get("id"))
        if mid is None:
            return None  # A fallback line uuid cannot establish a streaming response identity.
        parents = self.s["response_parents"]
        seen = int(self.s["response_seen"], 16)
        digest = sha256_text(mid)
        mask = 0
        for offset in range(0, 16, 4):
            mask |= 1 << int(digest[offset:offset + 4], 16)
        previously_seen = seen & mask == mask
        self.s["response_seen"] = format(seen | mask, "x")
        if mid not in parents:
            # A bounded identity filter has no false negatives. False positives conservatively
            # leave the observation unknown, never relabel a continuation's parent as its first.
            parents[mid] = None if previously_seen else as_str(rec.get("parentUuid"))
            self._bound_telemetry(parents, STATE_KEYS_MAX)
        parent = parents[mid]
        start = parse_ts(self.s["record_ts"].get(parent)) if parent else None
        end = parse_ts(rec.get("timestamp"))
        if start is None or end is None:
            return None
        interval = end - start
        if interval < timedelta(0):
            return None
        return _telemetry_int(interval // timedelta(milliseconds=1))

    def _session_fields(self, rec: dict[str, Any], ts: datetime | None, pos: LinePos,
                        out: list[Row]) -> None:
        b = self.b
        b["seen"] = True
        sid = rec.get("sessionId")
        if isinstance(sid, str) and sid and sid != self.sid and not self.s["mismatch"]:
            self.s["mismatch"] = True
            out.append(ParseIssueRow(byte_offset=pos.byte_offset, kind="session_mismatch",
                                     line_number=pos.line_number, detail="sessionId differs from path"))
            when = ts or parse_ts(self.s.get("last_ts"))
            if self.role == "main" and when is not None:
                out.append(ContinuationRow(agent=AGENT, child_uid=self.sid, parent_uid=sid, kind="other",
                                           session=self.key, ts=when, byte_offset=pos.byte_offset,
                                           evidence="sessionId_mismatch"))
        if rec.get("type") in ("user", "assistant", "attachment", "system") and not sid:
            out.append(ParseIssueRow(byte_offset=pos.byte_offset, kind="missing_field",
                                     line_number=pos.line_number, detail="sessionId"))
        if ts is not None:
            if b["first"] is None or ts < b["first"]:
                b["first"] = ts
            if b["last"] is None or ts > b["last"]:
                b["last"] = ts
            pend = self.s.get("perm_pending")
            if pend:
                self.s["perm_pending"] = None
                self._event(out, f"{self.kprefix}@{pend[1]}", ts, "permission_mode",
                            LinePos(pend[1], 0, 0), pend[0])
        cwd = as_str(rec.get("cwd"))
        if cwd:
            self.s["cwd"] = cwd
            if b["cwd"] is None:
                b["cwd"] = cwd
        br = as_str(rec.get("gitBranch"))
        if br and b["branch"] is None:
            b["branch"] = br
        ver = as_str(rec.get("version"))
        if ver:
            if b["ver_first"] is None:
                b["ver_first"] = ver
            b["ver_last"] = ver
        ep = as_str(rec.get("entrypoint"))
        if ep:
            b["entry"] = ep

    # -- simple record types ----------------------------------------------------------------

    def _t_ai_title(self, rec, pos, ts, sub, out):
        self.b["title"] = as_str(rec.get("aiTitle")) or self.b["title"]

    def _t_custom_title(self, rec, pos, ts, sub, out):
        self.b["custom"] = as_str(rec.get("customTitle")) or self.b["custom"]

    def _t_agent_name(self, rec, pos, ts, sub, out):
        self.b["nick"] = as_str(rec.get("agentName")) or self.b["nick"]

    def _t_continued_in(self, rec, pos, ts, sub, out):
        nxt = as_str(rec.get("continuedInSessionId"))
        if nxt:
            self._event(out, self._uid(rec, pos), ts, "continued_in", pos, nxt)
            if ts is not None and nxt != self.sid:
                out.append(ContinuationRow(agent=AGENT, child_uid=nxt, parent_uid=self.sid, kind="resume",
                                           session=self.key, ts=ts, byte_offset=pos.byte_offset,
                                           evidence="continued-in"))

    def _t_fork_context_ref(self, rec, pos, ts, sub, out):
        parent = as_str(rec.get("parentSessionId"))
        if parent and parent != self.sid and ts is not None:
            out.append(ContinuationRow(agent=AGENT, child_uid=self.sid, parent_uid=parent, kind="fork",
                                       session=self.key, ts=ts, byte_offset=pos.byte_offset,
                                       evidence="fork-context-ref"))

    def _t_worktree_state(self, rec, pos, ts, sub, out):
        ws = rec.get("worktreeSession") if isinstance(rec.get("worktreeSession"), dict) else {}
        head = as_str(ws.get("originalHeadCommit"))
        if head and self.b["commit"] is None:
            self.b["commit"] = head
        branch = as_str(ws.get("worktreeBranch"))
        if branch and self._changed("worktree", branch):
            self._event(out, self._uid(rec, pos), ts, "worktree", pos, branch,
                        {"original_branch": as_str(ws.get("originalBranch"))})

    def _t_permission_mode(self, rec, pos, ts, sub, out):
        self._perm(as_str(rec.get("permissionMode")), rec, pos, ts, out)

    def _perm(self, value, rec, pos, ts, out):
        if not self._changed("perm", value):
            return
        if ts is None:
            self.s["perm_pending"] = [value, pos.byte_offset]
            return
        self._event(out, self._uid(rec, pos), ts, "permission_mode", pos, value)

    def _t_mode(self, rec, pos, ts, sub, out):
        value = as_str(rec.get("mode"))
        if self._changed("mode", value) and ts is not None:
            self._event(out, self._uid(rec, pos), ts, "plan_mode", pos, value,
                        {"source": "mode_record"})

    def _t_queue_operation(self, rec, pos, ts, sub, out):
        op = as_str(rec.get("operation"))
        if op:
            detail = {"reason": rec["reason"]} if as_str(rec.get("reason")) else None
            self._event(out, self._uid(rec, pos), ts, "queue_op", pos, op, detail)

    def _t_pr_link(self, rec, pos, ts, sub, out):
        num = as_int(rec.get("prNumber"))
        repo = as_str(rec.get("prRepository"))
        if ts is None or num is None:
            return
        out.append(GitEventRow(agent=AGENT, event_uid=f"{self.kprefix}:pr_link:{repo}#{num}",
                               session=self.key, ts=ts, op="pr", evidence="pr_link",
                               byte_offset=pos.byte_offset, turn_key=self.s["turn"], pr_number=num,
                               pr_url=as_str(rec.get("prUrl")), pr_repo=repo, cwd=self.s.get("cwd")))

    def _t_file_history_delta(self, rec, pos, ts, sub, out):
        path = as_str(rec.get("trackingPath"))
        if not path or not path.startswith("/") or ts is None:
            return
        mid = as_str(rec.get("messageId"))
        uid = f"fhd:{mid}" if mid else f"{self.kprefix}@{pos.byte_offset}"
        out.append(ArtifactRow(agent=AGENT, event_uid=uid, session=self.key, ts=ts,
                               kind=artifact_kind(path), action="modified", path=path,
                               evidence_type="claude-file-history", byte_offset=pos.byte_offset,
                               turn_key=self.s["turn"], display_name=PurePosixPath(path).name or path))

    def _t_cost_state(self, rec, pos, ts, sub, out):
        when = ts or parse_ts(rec.get("startTime"))
        if when is None:
            return
        mu = rec.get("modelUsage")
        out.append(CostStateRow(
            agent=AGENT, event_uid=f"{self.kprefix}@{pos.byte_offset}", session=self.key, ts=when,
            byte_offset=pos.byte_offset,
            total_cost_usd=rec.get("totalCostUSD") if isinstance(rec.get("totalCostUSD"), (int, float)) else None,
            api_duration_ms=as_int(rec.get("totalAPIDuration")),
            api_duration_no_retry_ms=as_int(rec.get("totalAPIDurationWithoutRetries")),
            tool_duration_ms=as_int(rec.get("totalToolDuration")),
            total_duration_ms=as_int(rec.get("totalDuration")),
            lines_added=as_int(rec.get("totalLinesAdded")),
            lines_removed=as_int(rec.get("totalLinesRemoved")),
            model_usage=_numeric_only(mu) if isinstance(mu, dict) else None,
            start_time=parse_ts(rec.get("startTime")),
            has_unknown_model_cost=rec.get("hasUnknownModelCost")
            if type(rec.get("hasUnknownModelCost")) is bool else None))

    # -- system -----------------------------------------------------------------------------

    def _t_system(self, rec, pos, ts, sub, out):
        uid = self._uid(rec, pos)
        if sub == "turn_duration":
            turn = self.s["turn"]
            self._close_pending(out)
            if turn and ts is not None:
                out.append(TurnRow(session=self.key, turn_key=turn, byte_offset=pos.byte_offset,
                                   completed_at=ts, duration_ms=as_int(rec.get("durationMs")),
                                   message_count=as_int(rec.get("messageCount")), status="complete",
                                   pending_bg_agents=_telemetry_int(rec.get("pendingBackgroundAgentCount")),
                                   pending_workflows=_telemetry_int(rec.get("pendingWorkflowCount"))))
        elif sub == "compact_boundary" and ts is not None:
            meta = rec.get("compactMetadata") if isinstance(rec.get("compactMetadata"), dict) else {}
            out.append(CompactionRow(
                agent=AGENT, event_uid=uid, session=self.key, ts=ts, byte_offset=pos.byte_offset,
                turn_key=self.s["turn"], trigger=as_str(meta.get("trigger")),
                pre_tokens=as_int(meta.get("preTokens")), post_tokens=as_int(meta.get("postTokens")),
                duration_ms=as_int(meta.get("durationMs")),
                dropped_tokens=as_int(meta.get("cumulativeDroppedTokens"))))
        elif sub == "stop_hook_summary" and ts is not None:
            infos = [h for h in rec.get("hookInfos") or [] if isinstance(h, dict)]
            errors = rec.get("hookErrors") or []
            prevented = as_bool(rec.get("preventedContinuation"))
            outcome = "error" if errors else ("prevented" if prevented else "success")
            call = as_str(rec.get("toolUseID"))
            if not infos:
                infos = [{}]
            for i, info in enumerate(infos):
                cmd = info.get("command")
                out.append(HookEventRow(
                    agent=AGENT, event_uid=f"{uid}:{i}", session=self.key, ts=ts,
                    byte_offset=pos.byte_offset, turn_key=self.s["turn"], call_uid=call,
                    hook_event="SubagentStop" if self.aid else "Stop", outcome=outcome,
                    hook_name="stop_hook_summary",
                    command_sha256=sha256_text(cmd) if isinstance(cmd, str) else None,
                    duration_ms=as_int(info.get("durationMs")), prevented_continuation=prevented))
            for field, suffix in (("hookErrors", "errors"), ("hookAdditionalContext", "context"),
                                  ("stopReason", "stop_reason")):
                text = _join_text(rec.get(field))
                if text:
                    self._inject(out, f"{uid}:{suffix}", ts, "hook_output", text, pos, rec,
                                 {"source": f"stop_hook_summary.{field}"}, role="system")
        elif sub == "api_error":
            err = rec.get("error") if isinstance(rec.get("error"), dict) else {}
            detail = {k: rec.get(k) for k in ("retryAttempt", "maxRetries", "retryInMs")
                      if isinstance(rec.get(k), (int, float))}
            if isinstance(err.get("isNetworkDown"), bool):
                detail["network_down"] = err["isNetworkDown"]
            self._event(out, uid, ts, "api_error", pos, as_int(err.get("status")) or "error", detail)
            rl = err.get("rateLimits")
            if isinstance(rl, dict):
                self._rate_limit(out, uid, ts, pos, rl)
        elif sub in ("model_fallback", "model_refusal_fallback"):
            self._fallback(out, uid, ts, pos, rec)
        elif sub == "local_command":
            run = rec.get("commandRun")
            name = run.get("name") if isinstance(run, dict) else None
            if isinstance(name, str):
                self._event(out, uid, ts, "slash_command", pos, name[:80], {"source": "local_command"})
            content = rec.get("content")
            if ts is not None and isinstance(content, str) and content.lstrip().startswith(
                    ("<local-command-stdout>", "<local-command-stderr>")):
                self._inject(out, uid, ts, "local_command_output", content, pos, rec,
                             {"source": "system.local_command"}, role="system")
        elif sub == "agents_killed":
            self._event(out, uid, ts, "interrupt", pos, "agents_killed")
        elif sub == "scheduled_task_fire":
            self._event(out, uid, ts, "scheduled", pos, "fire")

    def _fallback(self, out, uid, ts, pos, src):
        detail = {k: src.get(k) for k in ("originalModel", "fallbackModel", "trigger")
                  if isinstance(src.get(k), str) and len(src.get(k)) < 120}
        self._event(out, uid, ts, "fallback", pos, detail.get("fallbackModel"), detail)

    def _rate_limit(self, out, uid, ts, pos, q):
        if ts is None:
            return
        rtype = as_str(q.get("rateLimitType")) or "unknown"
        kind = f"claude_{rtype}"
        resets = parse_ts(q.get("resetsAt"))
        status = as_str(q.get("status"))
        sig = [status, resets.isoformat() if resets else None, as_str(q.get("overageStatus"))]
        if not self._changed("rl:" + kind, sig):
            return
        util = q.get("utilization")
        out.append(RateLimitRow(agent=AGENT, event_uid=uid, session=self.key, ts=ts,
                                window_kind=kind, byte_offset=pos.byte_offset, limit_id=rtype,
                                used_percent=float(util) * (100 if util <= 1 else 1)
                                if isinstance(util, (int, float)) else None,
                                resets_at=resets, reached_type=status))

    # -- attachments ------------------------------------------------------------------------

    def _t_attachment(self, rec, pos, ts, sub, out):
        a = rec.get("attachment") if isinstance(rec.get("attachment"), dict) else {}
        uid = self._uid(rec, pos)
        if ts is None:
            return
        if sub in HOOK_ATTACHMENTS:
            cmd = a.get("command")
            outb = sum(json_size(a.get(k)) for k in ("stdout", "stderr", "content", "response")
                       if a.get(k) is not None)
            outcome = sub[5:] if sub.startswith("hook_") else "async_response"
            out.append(HookEventRow(
                agent=AGENT, event_uid=uid, session=self.key, ts=ts, byte_offset=pos.byte_offset,
                turn_key=self.s["turn"], call_uid=as_str(a.get("toolUseID")),
                hook_event=as_str(a.get("hookEvent")), outcome=outcome,
                hook_name=as_str(a.get("hookName")),
                command_sha256=sha256_text(cmd) if isinstance(cmd, str) else None,
                exit_code=as_int(a.get("exitCode")), duration_ms=as_int(a.get("durationMs")),
                timed_out=as_bool(a.get("timedOut")), output_bytes=outb or None))
        elif sub == "queued_command":
            mode = a.get("commandMode")
            prompt = a.get("prompt")
            text = text_blocks(prompt, {"text"}) if prompt is not None else ""
            if mode == "task-notification" and text:
                self._task_notification(text, ts, pos, out, "attachment")
            elif mode in ("prompt", None) and text and self.role == "main":
                og = a.get("origin")
                okind = og.get("kind") if isinstance(og, dict) else (og if isinstance(og, str) else None)
                origin = "agent_message" if (a.get("isMeta") or okind not in (None, "human")) else prompt_origin(text)
                out.append(self._msg(uid, ts, "user", "queued_prompt", text, pos, rec, origin=origin))
                self._human_time(ts)
        elif sub == "deferred_tools_delta":
            detail = {}
            for src, dst in (("pendingMcpServers", "pending"), ("failedMcpServers", "failed"),
                             ("needsAuthMcpServers", "needs_auth")):
                v = a.get(src)
                if isinstance(v, list) and v:
                    detail[dst] = [x for x in v if isinstance(x, str)][:50]
            if detail:
                self._event(out, uid, ts, "mcp_server_state", pos,
                            ",".join(sorted(detail)), detail)
        elif sub == "budget_usd":
            detail = {k: a.get(k) for k in ("total", "used", "remaining") if isinstance(a.get(k), (int, float))}
            self._event(out, uid, ts, "budget", pos, detail.get("total"), detail)
        elif sub == "max_turns_reached":
            detail = {k: a.get(k) for k in ("maxTurns", "turnCount") if isinstance(a.get(k), int)}
            self._event(out, uid, ts, "max_turns", pos, detail.get("maxTurns"), detail)
        elif sub == "model":
            ident = a.get("identity") if isinstance(a.get("identity"), dict) else {}
            mid = as_str(ident.get("modelId"))
            if mid and self._changed("model_ident", mid):
                self._event(out, uid, ts, "model_switch", pos, mid, {"source": "model_attachment"})
        elif sub == "model_refusal_fallback":
            self._fallback(out, uid, ts, pos, a)
        elif sub == "plan_mode_exit":
            self._event(out, uid, ts, "plan_mode", pos, "exit", {"source": "attachment"})
        elif sub == "auto_mode_exit":
            self._event(out, uid, ts, "permission_mode", pos, "auto_exit")
        elif sub == "invoked_skills":
            skills = a.get("skills")
            n = len(skills) if isinstance(skills, (list, dict)) else None
            self._event(out, uid, ts, "skill_invoke", pos, "invoked_skills", {"count": n})
        elif sub == "structured_output" and self.role in ("subagent", "workflow_agent"):
            pass  # the StructuredOutput tool_use carries the same data; stored from there
        self._attachment_v4(rec, a, uid, ts, sub, pos, out)

    def _attachment_v4(self, rec, a, uid, ts, sub, pos, out):
        """Parser v4: the content of an attachment record (see the module docstring)."""
        if sub in HOOK_ATTACHMENTS:
            fields = [(f, _join_text(a.get(f))) for f in HOOK_TEXT_FIELDS]
            fields = [(f, t) for f, t in fields if t]
            for f, t in fields:
                detail = {"source": sub, "field": f}
                if as_str(a.get("hookEvent")):
                    detail["hook_event"] = a["hookEvent"][:60]
                self._inject(out, uid if len(fields) == 1 else f"{uid}:{f}", ts, "hook_output", t, pos, rec,
                             detail)
            return
        if sub == "queued_command":
            prompt = a.get("prompt")
            main_prompt = self.role == "main" and a.get("commandMode") in ("prompt", None)
            nimg = len(self._images(out, uid, prompt, ts, pos, "queued_prompt" if main_prompt else "prompt",
                                    event_uid=uid, paste_ids=a.get("imagePasteIds")))
            text = text_blocks(prompt, {"text"}) if prompt is not None else ""
            if main_prompt and text:
                self._pasted(out, uid, text, ts, pos, "queued_prompt", nimg)
            elif self.role != "main" and a.get("commandMode") in ("prompt", None):
                og = a.get("origin")
                okind = og.get("kind") if isinstance(og, dict) else (og if isinstance(og, str) else None)
                self._inject(out, uid, ts, "agent_message", _raw_text(prompt), pos, rec,
                             {"source": "queued_command", "kind": okind} if okind else {"source": "queued_command"})
            return
        if sub in SKIP_ATTACHMENTS:
            return
        if sub == "read_truncation_notice":
            call = as_str(a.get("toolUseID"))
            if call:
                out.append(ToolIoRow(agent=AGENT, io_uid=call, session=self.key, ts=ts,
                                     byte_offset=pos.byte_offset, kind="call", call_uid=call,
                                     output_truncated=True, output_byte_offset=pos.byte_offset))
        if sub == "prompt_snapshot":
            sp = a.get("systemPrompt")
            if isinstance(sp, list):
                blocks = [x if isinstance(x, str) else _cjson(x) for x in sp]
                self._inject(out, uid, ts, "system_prompt", "\n\n".join(b for b in blocks if b), pos, rec,
                             {"source": "prompt_snapshot.systemPrompt", "blocks": len(blocks)}, role="system")
            elif isinstance(sp, str):
                self._inject(out, uid, ts, "system_prompt", sp, pos, rec,
                             {"source": "prompt_snapshot.systemPrompt"}, role="system")
            tools = a.get("tools")
            if tools:
                self._inject(out, f"{uid}:tools", ts, "context_injection", _cjson(_scrub(tools)), pos, rec,
                             {"source": "prompt_snapshot.tools",
                              "count": len(tools) if isinstance(tools, (list, dict)) else None})
            rest = {k: v for k, v in a.items() if k not in ("type", "systemPrompt", "tools")}
            if any(not isinstance(v, bool) and v not in (None, "", [], {}) for v in rest.values()):
                self._inject(out, f"{uid}:ctx", ts, "context_injection", _cjson(_scrub(rest)), pos, rec,
                             {"source": "prompt_snapshot"})
            return
        if sub in ("instructions", "invoked_skills"):
            items = a.get("files") if sub == "instructions" else a.get("skills")
            rows = [x for x in items if isinstance(x, dict) and isinstance(x.get("content"), str)
                    and x["content"]] if isinstance(items, list) else []
            cls = "context_injection" if sub == "instructions" else "skill_body"
            for n, item in enumerate(rows):
                detail = {"source": sub}
                for key in ("path", "type", "name"):
                    if as_str(item.get(key)):
                        detail["file_type" if key == "type" else key] = item[key][:300]
                self._inject(out, uid if len(rows) == 1 else f"{uid}:{n}", ts, cls, item["content"], pos, rec,
                             detail)
            if not rows:
                self._inject(out, uid, ts, cls, _attachment_json(a), pos, rec, {"source": sub})
            return
        detail = {"source": sub}
        if sub == "nested_memory":
            c = a.get("content")
            text = c.get("content") if isinstance(c, dict) else c
            if as_str(a.get("path")):
                detail["path"] = a["path"][:300]
            if not isinstance(text, str):
                text = _attachment_json(a)
        elif sub == "file":
            c = a.get("content")
            f = c.get("file") if isinstance(c, dict) and isinstance(c.get("file"), dict) else {}
            text = f.get("content") if isinstance(f.get("content"), str) else None
            name = as_str(a.get("filename")) or as_str(f.get("filePath"))
            if name:
                detail["path"] = name[:300]
            out.append(AttachmentRow(agent=AGENT, attachment_uid=f"{uid}:att:0", session=self.key, ts=ts,
                                     byte_offset=pos.byte_offset, kind="file", source="attachment_record",
                                     event_uid=uid, turn_key=self.s["turn"], file_name=name,
                                     size_bytes=len(text.encode("utf-8", "surrogatepass")) if text else None,
                                     text=text,
                                     detail={"display_path": a["displayPath"][:300]}
                                     if as_str(a.get("displayPath")) else None))
            if text is None:
                text = _attachment_json(a)
        elif sub == "output_style_instructions":
            st = a.get("style")
            text = st.get("prompt") if isinstance(st, dict) and isinstance(st.get("prompt"), str) else _attachment_json(a)
        elif sub in REMINDER_ATTACHMENTS or sub in CONTEXT_TEXT_FIELDS:
            field = REMINDER_ATTACHMENTS.get(sub) or CONTEXT_TEXT_FIELDS.get(sub)
            value = a.get(field) if field else None
            text = value if isinstance(value, str) else _attachment_json(a)
            if sub == "edited_text_file" and as_str(a.get("filename")):
                detail["path"] = a["filename"][:300]
        else:
            text = _attachment_json(a)
        cls = "system_reminder" if sub in REMINDER_ATTACHMENTS else "context_injection"
        self._inject(out, uid, ts, cls, text, pos, rec, detail)

    # -- user lines -------------------------------------------------------------------------

    def _msg(self, uid, ts, role, cls, text, pos, rec, turn=None, model=None, origin=None,
             detail=None) -> MessageRow:
        return MessageRow(agent=AGENT, event_uid=uid, session=self.key, ts=ts, role=role,
                          message_class=cls, text=text, byte_offset=pos.byte_offset,
                          byte_length=pos.byte_length, line_number=pos.line_number,
                          turn_key=turn if turn is not None else self.s["turn"], model=model,
                          is_sidechain=bool(rec.get("isSidechain")),
                          raw_record_origin=rec.get("type"), prompt_origin=origin,
                          detail=detail or None)

    def _inject(self, out, uid, ts, cls, text, pos, rec, detail, role="user", turn=None) -> None:
        """A v4 harness-content row; skipped when there is no text."""
        if ts is None or not isinstance(text, str) or not text:
            return
        out.append(self._msg(uid, ts, role, cls, text, pos, rec, turn=turn, detail=detail))

    def _images(self, out, prefix, blocks, ts, pos, source, event_uid=None, call_uid=None,
                paste_ids=None) -> list[dict[str, Any]]:
        """AttachmentRows for image/document blocks (metadata only); returns output_parts."""
        parts: list[dict[str, Any]] = []
        n = 0
        ids = [p for p in paste_ids if isinstance(p, (str, int))] if isinstance(paste_ids, list) else []
        for index, block in enumerate(blocks if isinstance(blocks, list) else []):
            if not isinstance(block, dict) or block.get("type") not in BINARY_PARTS:
                continue
            mime, size = _source_meta(block.get("source"))
            parts.append({"index": index, "type": block["type"], "mime": mime, "bytes": size})
            detail = {"paste_id": ids[n]} if n < len(ids) and len(ids) == sum(
                1 for b in blocks if isinstance(b, dict) and b.get("type") == "image") else None
            if ts is not None:
                out.append(AttachmentRow(agent=AGENT, attachment_uid=f"{prefix}:att:{n}", session=self.key,
                                         ts=ts, byte_offset=pos.byte_offset, kind=block["type"],
                                         source=source, event_uid=event_uid, call_uid=call_uid,
                                         turn_key=self.s["turn"], mime=mime, size_bytes=size,
                                         detail=detail))
            n += 1
        if n == 0 and ids and ts is not None:
            for pid in ids:
                out.append(AttachmentRow(agent=AGENT, attachment_uid=f"{prefix}:att:{n}", session=self.key,
                                         ts=ts, byte_offset=pos.byte_offset, kind="image", source=source,
                                         event_uid=event_uid, call_uid=call_uid, turn_key=self.s["turn"],
                                         detail={"paste_id": pid}))
                n += 1
        return parts

    def _pasted(self, out, uid, text, ts, pos, source, start: int) -> None:
        n = start
        for m in PASTED_BLOCK.finditer(text):
            attr = PASTE_ID_ATTR.search(m.group(1) or "")
            out.append(AttachmentRow(agent=AGENT, attachment_uid=f"{uid}:att:{n}", session=self.key, ts=ts,
                                     byte_offset=pos.byte_offset, kind="pasted_text", source=source,
                                     event_uid=uid, turn_key=self.s["turn"], text=m.group(2),
                                     size_bytes=len(m.group(2).encode("utf-8", "surrogatepass")),
                                     detail={"paste_id": attr.group(1)} if attr else None))
            n += 1
        for m in PASTED_PLACEHOLDER.finditer(text):
            detail: dict[str, Any] = {"placeholder": True, "paste_id": m.group(1)}
            if m.group(2):
                detail["lines"] = int(m.group(2))
            out.append(AttachmentRow(agent=AGENT, attachment_uid=f"{uid}:att:{n}", session=self.key, ts=ts,
                                     byte_offset=pos.byte_offset, kind="pasted_text", source=source,
                                     event_uid=uid, turn_key=self.s["turn"], detail=detail))
            n += 1

    def _human_time(self, ts):
        b = self.b
        if b["first_h"] is None or ts < b["first_h"]:
            b["first_h"] = ts
        if b["last_h"] is None or ts > b["last_h"]:
            b["last_h"] = ts

    def _classify(self, rec: dict[str, Any], text: str) -> tuple[str, bool, str | None]:
        """(origin, strong, message_class) for a user line that is not a tool result."""
        t = _strip_prefix(text)
        og = rec.get("origin")
        kind = og.get("kind") if isinstance(og, dict) else (og if isinstance(og, str) else None)
        ps = rec.get("promptSource")
        if rec.get("isCompactSummary"):
            return "compaction_summary", True, "compaction_summary"
        if t.startswith("<command-name>") or t.startswith("<command-message>"):
            return "slash_command", True, None
        if t.startswith("<local-command-stdout>") or t.startswith("<local-command-stderr>"):
            return "slash_command", True, None
        if t.startswith("<bash-input>") or t.startswith("<bash-stdout>") or t.startswith("<bash-stderr>"):
            return "bash", True, None
        if kind:
            if kind == "human":
                if rec.get("isMeta"):
                    return "meta", False, None
                if ps == "queued":
                    return "queued", True, "queued_prompt" if self.role == "main" else None
                return "human", True, "human_prompt" if self.role == "main" else None
            return kind.replace("-", "_"), True, None
        if t.startswith("[Request interrupted"):
            return "interrupt", False, None
        if rec.get("isMeta"):
            return "meta", False, None
        if t.startswith("<task-notification>"):
            return "task_notification", True, None
        if ps == "sdk":
            return "sdk", True, None
        if ps == "queued":
            return "queued", True, "queued_prompt" if self.role == "main" else None
        if ps == "system":
            return "meta", False, None
        if self.role in ("subagent", "workflow_agent"):
            first = not self.s["first_user_seen"]
            cls = "subagent_brief" if (first and self.role == "workflow_agent") else None
            return "subagent_brief", True, cls
        if ps not in (None, "typed"):
            return str(ps), True, None
        return "human", True, "human_prompt"

    def _t_user(self, rec, pos, ts, sub, out):
        msg = rec.get("message") if isinstance(rec.get("message"), dict) else {}
        content = msg.get("content")
        uid = self._uid(rec, pos)
        pid = as_str(rec.get("promptId"))
        results = (
            [b for b in content if isinstance(b, dict) and b.get("type") == "tool_result"]
            if isinstance(content, list)
            else []
        )
        perm = as_str(rec.get("permissionMode"))
        if perm:
            self._perm(perm, rec, pos, ts, out)
        if results:
            # Result lines never open a turn: in subagent files their promptId tracks the parent's
            # turn, which would create prompt-less phantom turns. They join the call's turn.
            for i, block in enumerate(results):
                self._tool_result(rec, block, i, len(results), ts, pos, out)
            return
        if ts is None:
            return
        text = text_blocks(content, {"text"})
        images = (
            sum(1 for b in content if isinstance(b, dict) and b.get("type") == "image")
            if isinstance(content, list)
            else 0
        )
        origin, strong, cls = self._classify(rec, text)
        is_brief = False
        if self.role != "main" and not rec.get("isMeta"):
            is_brief = not self.s.get("brief_seen")
            self.s["brief_seen"] = True  # v4: the first non-meta user line of a subagent file
        if self.role == "subagent" and not self.s["first_user_seen"]:
            self.s["spawn_kind"] = "fork" if "<fork-boilerplate>" in text else "agent"
        if self.role == "subagent" and "<fork-boilerplate>" in text:
            self.s["spawn_kind"] = "fork"
        self.s["first_user_seen"] = True
        key = pid or f"u:{uid}"
        prompt = cls in ("human_prompt", "queued_prompt")
        peer_split = split_legacy_peer_injections(text_blocks(content, {"text"}, strip=False), rec) if prompt else None
        human, injections = peer_split if peer_split is not None else (
            split_prompt_injections(text) if prompt else (text, []))
        if injections and not human:
            origin, strong = "meta", False
        self._start_or_join_turn(out, key, origin, strong, ts, pos, perm)
        position = rec.get("turnPosition") if isinstance(rec.get("turnPosition"), dict) else {}
        telemetry = {"origin_hint": as_str(rec.get("turnOrigin")),
                     "prompt_index": _telemetry_int(position.get("promptIndex")),
                     "turn_index": _telemetry_int(position.get("turnIndex"))}
        if any(value is not None for value in telemetry.values()):
            out.append(TurnRow(session=self.key, turn_key=key, byte_offset=pos.byte_offset, **telemetry))
        if cls and text:
            if human:
                out.append(
                    self._msg(
                        uid, ts, "user", cls, human, pos, rec, turn=key, origin=prompt_origin(human) if prompt else None
                    )
                )
            self._prompt_injections(out, injections, human, uid, key, ts, pos, rec)
        if prompt and (human or not injections):
            self._human_time(ts)
        nimg = len(
            self._images(
                out,
                uid,
                content,
                ts,
                pos,
                "queued_prompt" if cls == "queued_prompt" else "prompt",
                event_uid=uid,
                paste_ids=rec.get("imagePasteIds"),
            )
        )
        if prompt and human:
            self._pasted(out, uid, human, ts, pos, "queued_prompt" if cls == "queued_prompt" else "prompt", nimg)
        if not (cls and text):
            self._user_v4(rec, content, text, origin, is_brief, uid, key, ts, pos, out)
        if images or rec.get("imagePasteIds"):
            n = images or (len(rec["imagePasteIds"]) if isinstance(rec.get("imagePasteIds"), list) else None)
            self._event(out, uid, ts, "image_attach", pos, n)
        t = _strip_prefix(text)
        if origin == "interrupt":
            self._event(out, uid, ts, "interrupt", pos, "tool_use" if "for tool use" in t[:60] else "user")
        if origin == "slash_command":
            m = SLASH.search(t[:400])
            if m:
                name = m.group(1)[:80]
                self._event(out, uid, ts, "slash_command", pos, name)
                if name == "/clear":
                    self._event(out, uid, ts, "clear", pos, name)
        if origin == "task_notification" or t.startswith("<task-notification>"):
            self._task_notification(text, ts, pos, out, "user")
        if rec.get("isCompactSummary"):
            pass  # message emitted above as compaction_summary

    def _prompt_injections(self, out, injections, human, uid, key, ts, pos, rec) -> None:
        for n, (cls, tag, body, start, stop) in enumerate(injections):
            event_uid = uid if not human and n == 0 else f"{uid}:injection:{n}"
            out.append(
                self._msg(
                    event_uid,
                    ts,
                    "user",
                    cls,
                    body,
                    pos,
                    rec,
                    turn=key,
                    detail={"source": tag, "text_start": start, "text_end": stop},
                )
            )


    def _user_v4(self, rec, content, text, origin, is_brief, uid, key, ts, pos, out) -> None:
        """Parser v4: a class for the user lines v3 left unstored (see the module docstring)."""
        t = _strip_prefix(text)
        raw = _raw_text(content)
        meta = bool(rec.get("isMeta"))
        og = rec.get("origin")
        kind = og.get("kind") if isinstance(og, dict) else (og if isinstance(og, str) else None)
        ps = as_str(rec.get("promptSource"))
        main = self.role == "main"
        after = self.s.get("after_cmd")
        if not meta:
            self.s["after_cmd"] = None

        def emit(cls, detail, body=None, porigin=None):
            body = raw if body is None else body
            if body:
                human, injections = split_prompt_injections(body) if cls == "human_prompt" else (body, [])
                if human:
                    out.append(
                        self._msg(uid, ts, "user", cls, human, pos, rec, turn=key, origin=porigin, detail=detail)
                    )
                self._prompt_injections(out, injections, human, uid, key, ts, pos, rec)

        if main and not meta and t.startswith(("<command-name>", "<command-message>")):
            m = SLASH.search(t[:400])
            name = m.group(1)[:80] if m else None
            skill = t.startswith("<command-message>") or (name is not None and ":" in name)
            porigin = "skill" if skill else "slash_command"
            emit(
                "human_prompt",
                {"source": "command", "command": name} if name else {"source": "command"},
                body=text,
                porigin=porigin,
            )
            self.s["after_cmd"] = porigin
            return
        if main and not meta and t.startswith("<bash-input>"):
            emit("human_prompt", {"source": "bash-input"}, body=text, porigin="local_command")
            return
        for tag, cls in (
            ("<local-command-stdout>", "local_command_output"),
            ("<local-command-stderr>", "local_command_output"),
            ("<bash-stdout>", "local_command_output"),
            ("<bash-stderr>", "local_command_output"),
            ("<local-command-caveat>", "system_reminder"),
            ("<system-reminder>", "system_reminder"),
        ):
            if t.startswith(tag):
                emit(cls, {"source": tag[1:-1]})
                return
        if t.startswith("[Request interrupted"):
            emit("interrupt_marker", {"source": "interrupt"})
            return
        if origin == "task_notification" or t.startswith("<task-notification>"):
            return  # stored by _task_notification
        if meta:
            if rec.get("sourceToolUseID") or t.startswith("Base directory for this skill"):
                self.s["after_cmd"] = None
                emit("skill_body", {"source": "Skill" if rec.get("sourceToolUseID") else "skill_command"})
            elif after:
                self.s["after_cmd"] = None
                emit("skill_body" if after == "skill" else "command_expansion", {"source": after})
            elif kind:
                emit("agent_message", {"source": kind})
            elif t.startswith("Stop hook feedback"):
                emit("hook_output", {"source": "stop_hook_feedback"})
            elif t.startswith("[Image"):
                emit("context_injection", {"source": "image_annotation"})
            else:
                emit("system_reminder", {"source": "isMeta"})
            return
        if is_brief:
            if self.role == "workflow_agent":
                emit("agent_message", {"source": "brief"})  # v3 stored an earlier (meta) line as the brief
            return  # a subagent's brief is the parent's "<call_id>:brief" row
        if kind and kind != "human":
            emit("agent_message", {"source": kind})
        elif not main:
            emit("agent_message", {"source": "followup"})
        elif ps and ps not in ("typed", "queued"):
            emit("agent_message", {"source": ps})

    def _task_notification(self, text: str, ts: datetime, pos: LinePos, out: list[Row],
                           record_origin: str) -> None:
        tags: dict[str, str] = {}
        for m in TN_TAG.finditer(text):
            tags.setdefault(m.group(1), m.group(2).strip())
        task_id = tags.get("task-id")
        call = tags.get("tool-use-id")
        base = f"tn:{task_id or '-'}:{sha256_text(text)[:16]}"
        out.append(MessageRow(agent=AGENT, event_uid=base + ":text", session=self.key, ts=ts, role="user",
                              message_class="agent_message", text=text, byte_offset=pos.byte_offset,
                              byte_length=pos.byte_length, line_number=pos.line_number,
                              turn_key=self.s["turn"], is_sidechain=self.role != "main",
                              raw_record_origin=record_origin,
                              detail={"source": "task-notification"}))
        spawn = self.s["spawns"].get(call) if call else None
        if spawn:
            status = tags.get("status")
            out.append(SubagentSpawnRow(
                agent=AGENT, spawn_uid=call, session=self.key, byte_offset=pos.byte_offset,
                child_session_uid=self.sid, child_agent_id=task_id if spawn[0] == "agent" else None,
                completion_status=status[:40] if status else None, completed_at=ts,
                reported_tokens=as_int(tags.get("subagent_tokens")),
                reported_tool_uses=as_int(tags.get("tool_uses")),
                reported_duration_ms=as_int(tags.get("duration_ms"))))
        sm = TN_SUMMARY.search(text)
        if sm and sm.group(1).strip():
            out.append(MessageRow(agent=AGENT, event_uid=base + ":summary", session=self.key, ts=ts,
                                  role="user", message_class="task_notification_summary",
                                  text=sm.group(1).strip(), byte_offset=pos.byte_offset,
                                  byte_length=pos.byte_length, line_number=pos.line_number,
                                  turn_key=self.s["turn"], is_sidechain=self.role != "main",
                                  raw_record_origin=record_origin))
        rm = TN_RESULT.search(text)
        if rm and rm.group(1).strip():
            out.append(MessageRow(agent=AGENT, event_uid=base + ":result", session=self.key, ts=ts,
                                  role="user", message_class="subagent_report",
                                  text=rm.group(1).strip(), byte_offset=pos.byte_offset,
                                  byte_length=pos.byte_length, line_number=pos.line_number,
                                  turn_key=self.s["turn"], is_sidechain=self.role != "main"))

    def _tool_result(self, rec, block, i, n, ts, pos, out):
        cid = as_str(block.get("tool_use_id"))
        if not cid:
            out.append(ParseIssueRow(byte_offset=pos.byte_offset, kind="missing_field",
                                     line_number=pos.line_number, detail="tool_use_id"))
            return
        call = self.s["open_calls"].pop(cid, None)
        closed = self.s["closed_calls"]
        if call is None and cid in closed:
            call = closed[cid]          # a repeated result for an already answered call
        early = call is None
        if early:
            # The result line can be written before its tool_use line. Emit the result half now
            # under a placeholder name; the call half (UPDATE policy) replaces the name later.
            name, call_ts, turn, off = "unknown", None, self.s["turn"], pos.byte_offset
        else:
            name, call_ts, turn, off = call[0], parse_ts(call[1]), call[2], call[3]
            closed.pop(cid, None)
            closed[cid] = call
        tur = rec.get("toolUseResult") if n == 1 else None
        content = block.get("content")
        ctext = content if isinstance(content, str) else text_blocks(content, {"text"})
        is_error = block.get("is_error") if isinstance(block.get("is_error"), bool) else None
        denial = as_str(rec.get("toolDenialKind")) if n == 1 else None
        tur_d = tur if isinstance(tur, dict) else {}
        kind = name
        if early:
            if "stdout" in tur_d and "interrupted" in tur_d:
                kind = "Bash"
            elif "runId" in tur_d and "workflowName" in tur_d:
                kind = "Workflow"
            elif ("agentId" in tur_d or "agent_id" in tur_d) and "status" in tur_d:
                kind = "Agent"
        interrupted = as_bool(tur_d.get("interrupted"))
        timed_out = True if tur_d.get("timedOutAfterMs") is not None else None
        head = ctext[:300] if isinstance(ctext, str) else ""
        if denial is None and isinstance(tur, str) and tur.startswith("User rejected tool use"):
            denial = "user-rejected"
        if denial is None and head.startswith("The user doesn't want to proceed with this tool use"):
            denial = "user-rejected"
        if "[Request interrupted by user for tool use]" in head:
            interrupted = True
        if is_error and ("Command timed out" in head or "timed out after" in head.lower()):
            timed_out = True
        if denial:
            outcome = "denied"
        elif interrupted:
            outcome = "interrupted"
        elif timed_out:
            outcome = "timeout"
        elif is_error:
            outcome = "error"
        else:
            outcome = "ok"
        exit_code = None
        m = EXIT_CODE.match(head)
        if m:
            exit_code = int(m.group(1))
        elif kind == "Bash" and is_error is False and not tur_d.get("backgroundTaskId") and not interrupted:
            exit_code = 0
        duration = as_int(tur_d.get("durationMs")) or as_int(tur_d.get("totalDurationMs"))
        if duration is None and isinstance(tur_d.get("durationSeconds"), (int, float)):
            duration = int(tur_d["durationSeconds"] * 1000)
        if duration is None and call_ts is not None and ts is not None:
            duration = max(0, int((ts - call_ts).total_seconds() * 1000))
        if isinstance(content, str):
            out_bytes = json_size(content)
        elif isinstance(content, list):
            out_bytes = sum(json_size(b.get("text")) if isinstance(b, dict) and b.get("type") == "text"
                            else json_size(b) for b in content)
        else:
            out_bytes = 0
        background = True if tur_d.get("backgroundTaskId") or tur_d.get("isAsync") else None
        if early and ts is not None:
            self.s["early_results"][cid] = [ts.isoformat(), duration is not None]
        out.append(ToolCallRow(
            agent=AGENT, call_uid=cid, session=self.key, tool_name=name, byte_offset=off,
            turn_key=turn, ended_at=ts,
            duration_ms=duration, output_bytes=out_bytes,
            persisted_output_bytes=as_int(tur_d.get("persistedOutputSize")), outcome=outcome,
            is_error=bool(is_error) if is_error is not None else (True if outcome != "ok" else None),
            exit_code=exit_code, denial_kind=denial, interrupted=interrupted or None,
            timed_out=timed_out, background=background))
        if denial and ts is not None:
            self._event(out, f"{cid}:denial", ts, "denial", pos, denial, {"tool": name[:120]})
        if ts is None:
            return
        self._tool_io_result(rec, block, cid, None if early else name, turn, off, tur, ts, pos, out)
        if kind in SPAWN_TOOLS or kind == "Workflow":
            self._spawn_result(cid, kind, tur_d, ts, pos, out)
        if kind == "Bash":
            self._git(cid, tur_d, ctext if isinstance(ctext, str) else "", ts, pos, rec, out,
                      None if is_error else self.s["git_cmds"].get(cid))
        self.s["git_cmds"].pop(cid, None)

    def _tool_io_result(self, rec, block, cid, name, turn, off, tur, ts, pos, out) -> None:
        content = block.get("content")
        if isinstance(content, str):
            output = content
        elif isinstance(content, list) and any(isinstance(b, dict) and b.get("type") == "text" for b in content):
            output = text_blocks(content, {"text"})
        else:
            output = None
        parts = self._images(out, cid, content, ts, pos, "tool_result", event_uid=self._uid(rec, pos),
                             call_uid=cid) if isinstance(content, list) else []
        if isinstance(content, list):
            for index, b in enumerate(content):
                if not isinstance(b, dict) or b.get("type") in ("text", *BINARY_PARTS):
                    continue
                part: dict[str, Any] = {"index": index, "type": as_str(b.get("type")) or "unknown"}
                if b.get("type") == "tool_reference" and as_str(b.get("tool_name")):
                    part["tool_name"] = b["tool_name"][:200]
                parts.append(part)
            parts.sort(key=lambda p: p["index"])
        stdout = stderr = result = None
        truncated = None
        if isinstance(tur, dict):
            stdout = tur["stdout"] if isinstance(tur.get("stdout"), str) else None
            stderr = tur["stderr"] if isinstance(tur.get("stderr"), str) else None
            rest = {k: v for k, v in tur.items() if k not in ("stdout", "stderr")}
            result = json.dumps(_scrub(rest), ensure_ascii=False) if rest else None
            if as_str(tur.get("persistedOutputPath")) or tur.get("truncated") is True:
                truncated = True
        elif tur is not None:
            result = json.dumps(_scrub(tur), ensure_ascii=False)
        out.append(ToolIoRow(agent=AGENT, io_uid=cid, session=self.key, ts=ts, byte_offset=off, kind="call",
                             tool_name=name, call_uid=cid, turn_key=turn, output_text=output,
                             output_truncated=truncated, stdout_text=stdout, stderr_text=stderr,
                             result_json=result, output_parts=parts or None, output_at=ts,
                             output_byte_offset=pos.byte_offset))

    def _spawn_result(self, cid, name, tur, ts, pos, out):
        if not tur:
            return
        status = as_str(tur.get("status"))
        if name == "Workflow":
            out.append(SubagentSpawnRow(
                agent=AGENT, spawn_uid=cid, session=self.key, byte_offset=pos.byte_offset,
                child_session_uid=self.sid, launch_status=status,
                workflow_id=as_str(tur.get("runId")), child_task_name=as_str(tur.get("workflowName"))))
            return
        child = as_str(tur.get("agentId")) or as_str(tur.get("agent_id"))
        row = SubagentSpawnRow(agent=AGENT, spawn_uid=cid, session=self.key,
                               byte_offset=pos.byte_offset, child_session_uid=self.sid,
                               child_agent_id=child, resolved_model=as_str(tur.get("resolvedModel"))
                               or as_str(tur.get("model")), launch_status=status)
        if status == "teammate_spawned":
            row.name = as_str(tur.get("name"))
        if status == "completed":
            row.completion_status = "completed"
            row.completed_at = ts
            row.reported_tokens = as_int(tur.get("totalTokens"))
            row.reported_tool_uses = as_int(tur.get("totalToolUseCount"))
            row.reported_duration_ms = as_int(tur.get("totalDurationMs"))
        if tur.get("isAsync") is True:
            row.background = True
        out.append(row)

    def _git(self, cid, tur, output, ts, pos, rec, out, cmd_ops=None):
        cwd = as_str(rec.get("cwd"))
        turn = self.s["turn"]

        def ev(op, suffix, evidence, **kw):
            out.append(GitEventRow(agent=AGENT, event_uid=f"{cid}:{suffix}", session=self.key,
                                   ts=ts, op=op, evidence=evidence, byte_offset=pos.byte_offset,
                                   turn_key=turn, call_uid=cid, cwd=cwd, **kw))

        gop = tur.get("gitOperation") if isinstance(tur.get("gitOperation"), dict) else None
        if gop:
            c = gop.get("commit") if isinstance(gop.get("commit"), dict) else None
            if c and as_str(c.get("sha")):
                sha = c["sha"]
                op = "cherry_pick" if c.get("kind") == "cherry-picked" else "commit"
                ev(op, f"commit:{sha[:7]}", "gitOperation", sha_short=sha[:12],
                   branch=as_str(c.get("branch")))
            p = gop.get("push") if isinstance(gop.get("push"), dict) else None
            if p is not None:
                br = as_str(p.get("branch"))
                ev("push", f"push:{br or '-'}", "gitOperation", branch=br)
            pr = gop.get("pr") if isinstance(gop.get("pr"), dict) else None
            if pr is not None:
                num = as_int(pr.get("number"))
                ev("pr", f"pr:{num if num is not None else '-'}", "gitOperation", pr_number=num,
                   pr_action=as_str(pr.get("action")), pr_url=as_str(pr.get("url")))
            b = gop.get("branch") if isinstance(gop.get("branch"), dict) else None
            if b is not None:
                act = b.get("action")
                op = "rebase" if act == "rebased" else "merge" if act == "merged" else None
                if op:
                    ref = as_str(b.get("ref"))
                    ev(op, f"{op}:{ref or '-'}", "gitOperation", branch=ref)
            return
        text = tur.get("stdout") if isinstance(tur.get("stdout"), str) else output
        if isinstance(tur.get("stderr"), str):
            text = f"{text}\n{tur['stderr']}"   # git push reports its ref ranges on stderr
        commits, pushes = git_from_output(text)
        for branch, sha in commits:
            ev("commit", f"commit:{sha[:7]}", "output_regex", sha_short=sha[:12], branch=branch)
        for _old, new, _src, dst in pushes:
            br = dst.removeprefix("refs/heads/")
            ev("push", f"push:{br}", "output_regex", branch=br, sha_short=new[:12])
        # a command that succeeded but printed no commit line or push range (`git commit -q`)
        for op, n in git_event_extras(cmd_ops or [], len(commits), len(pushes)):
            ev(op, f"{'push' if op == 'push' else 'commit'}:cmd{n}", "command")

    # -- assistant lines --------------------------------------------------------------------

    def _t_assistant(self, rec, pos, ts, sub, out):
        msg = rec.get("message") if isinstance(rec.get("message"), dict) else {}
        duration = self._call_duration(rec, msg)
        uid = self._uid(rec, pos)
        if ts is None:
            return
        self._close_pending(out)
        self.s["after_cmd"] = None
        turn = self.s["turn"]
        model = as_str(msg.get("model"))
        mid = as_str(msg.get("id")) or uid
        ml = self.s["msg_lines"]
        ml[mid] = ml.pop(mid, 0) + 1
        usage = msg.get("usage") if isinstance(msg.get("usage"), dict) else None
        effort = as_str(rec.get("effort")) or as_str(rec.get("perTurnEffort"))
        is_err = bool(rec.get("isApiErrorMessage"))
        row = LlmCallRow(agent=AGENT, response_id=mid, session=self.key, ts=ts,
                         byte_offset=pos.byte_offset, turn_key=turn, model=model,
                         request_id=as_str(rec.get("requestId")),
                         stop_reason=as_str(msg.get("stop_reason")), effort=effort,
                         is_api_error=is_err, error_kind=as_str(rec.get("error")) if is_err else None,
                         api_error_status=as_int(rec.get("apiErrorStatus")),
                         is_sidechain=bool(rec.get("isSidechain")), line_count=ml[mid],
                         duration_ms=duration,
                         latency_basis="claude_parent_to_last_line" if duration is not None else None,
                         thinking_ms=_telemetry_int(rec.get("thinkingDurationMs")),
                         advisor_model=as_str(rec.get("advisorModel")),
                         input_transform_types=_transform_types(msg.get("input_transformations")))
        diagnostics = msg.get("diagnostics") if isinstance(msg.get("diagnostics"), dict) else {}
        miss = diagnostics.get("cache_miss_reason")
        if isinstance(miss, dict):
            row.cache_miss_type = as_str(miss.get("type"))
            row.cache_missed_tokens = _telemetry_int(miss.get("cache_missed_input_tokens"), bigint=True)
        if usage:
            _fill_usage(row, usage)
            row.inference_geo = as_str(usage.get("inference_geo"))
            iterations = usage.get("iterations")
            if isinstance(iterations, list) and all(isinstance(item, dict) for item in iterations):
                row.iterations = _telemetry_int(len(iterations))
        out.append(row)
        if model and model != "<synthetic>":
            prev = self.s["last"].get("model")
            if self._changed("model", model) and prev is not None:
                self._event(out, uid, ts, "model_switch", pos, model, {"from": prev})
            if turn and model != self.s.get("turn_model"):
                self.s["turn_model"] = model
                out.append(TurnRow(session=self.key, turn_key=turn, byte_offset=pos.byte_offset,
                                   model=model))
        if effort:
            prev = self.s["last"].get("effort")
            if self._changed("effort", effort) and prev is not None:
                self._event(out, uid, ts, "effort_change", pos, effort, {"from": prev})
            if turn and effort != self.s.get("turn_effort"):
                self.s["turn_effort"] = effort
                out.append(TurnRow(session=self.key, turn_key=turn, byte_offset=pos.byte_offset,
                                   effort=effort))
        if is_err:
            self._event(out, uid, ts, "api_error", pos, as_str(rec.get("error")) or "error",
                        {"status": as_int(rec.get("apiErrorStatus"))})
        if isinstance(rec.get("quotaLimits"), dict):
            self._rate_limit(out, uid, ts, pos, rec["quotaLimits"])
        if rec.get("isAbortedMidStream"):
            self._event(out, uid + ":abort", ts, "interrupt", pos, "aborted_mid_stream")
            if turn:
                out.append(TurnRow(session=self.key, turn_key=turn, byte_offset=pos.byte_offset,
                                   status="aborted", abort_reason="aborted_mid_stream"))
        content = msg.get("content")
        text = text_blocks(content, {"text"})
        if text and not is_err:
            out.append(self._msg(uid, ts, "assistant", "assistant_text", text, pos, rec, model=model))
            for path in linked_paths(text):
                out.append(ArtifactRow(agent=AGENT, event_uid=uid, session=self.key, ts=ts,
                                       kind=artifact_kind(path), action="linked", path=path,
                                       evidence_type="assistant-markdown", byte_offset=pos.byte_offset,
                                       turn_key=turn, display_name=PurePosixPath(path).name or path))
        if isinstance(content, list):
            for index, block in enumerate(content):
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "thinking":
                    body = block.get("thinking")
                    if isinstance(body, str) and body:
                        out.append(self._msg(f"{uid}:think:{index}", ts, "assistant", "reasoning", body, pos, rec,
                                             model=model))
                    elif block.get("signature"):
                        out.append(self._msg(f"{uid}:think:{index}", ts, "assistant", "reasoning", "", pos, rec,
                                             model=model, detail={"signature_only": True}))
                elif btype == "redacted_thinking":
                    out.append(self._msg(f"{uid}:think:{index}", ts, "assistant", "reasoning", "", pos, rec,
                                         model=model, detail={"redacted": True}))
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    self._tool_use(rec, block, mid, ts, pos, out)

    def _tool_use(self, rec, block, mid, ts, pos, out):
        cid = as_str(block.get("id"))
        name = as_str(block.get("name")) or "unknown"
        if not cid:
            out.append(ParseIssueRow(byte_offset=pos.byte_offset, kind="missing_field",
                                     line_number=pos.line_number, detail="tool_use.id"))
            return
        inp = block.get("input") if isinstance(block.get("input"), dict) else {}
        turn = self.s["turn"]
        server, tool = mcp_split(name)
        server = as_str(rec.get("attributionMcpServer")) or server
        tool = as_str(rec.get("attributionMcpTool")) or tool
        family = "mcp" if name.startswith("mcp__") else ("collaboration" if name in COLLAB_TOOLS else "builtin")
        meta: dict[str, Any] = {}
        caller = block.get("caller")
        if isinstance(caller, dict) and as_str(caller.get("type")):
            meta["caller"] = caller["type"]
        if name == "Bash":
            ops = git_ops_from_command(inp.get("command"))
            if ops:
                self.s["git_cmds"][cid] = ops
            verb = cmd_verb(inp.get("command"))
            if verb:
                meta["cmd_verb"] = verb
            remote = ssh_target(inp.get("command"))
            if remote:
                meta["target_host"] = remote[0]
                if remote[1]:
                    meta["remote_verb"] = remote[1]
        elif name == "Skill" and as_str(inp.get("skill")):
            meta["skill"] = inp["skill"][:120]
        elif name in SPAWN_TOOLS and as_str(inp.get("subagent_type")):
            meta["subagent_type"] = inp["subagent_type"][:120]
        background = _bg(inp.get("run_in_background"))
        early = self.s["early_results"].pop(cid, None)
        out.append(ToolCallRow(
            agent=AGENT, call_uid=cid, session=self.key, tool_name=name, byte_offset=pos.byte_offset,
            turn_key=turn, response_id=mid, tool_family=family,
            mcp_server=server if family == "mcp" else None, mcp_tool=tool if family == "mcp" else None,
            started_at=ts, input_bytes=json_size(block.get("input")), background=background,
            ended_at=parse_ts(early[0]) if early else None,
            duration_ms=_ms_between(ts, parse_ts(early[0])) if early and not early[1] else None,
            attribution_skill=as_str(rec.get("attributionSkill")),
            attribution_plugin=as_str(rec.get("attributionPlugin")), meta=meta or None))
        if early is None:
            self.s["open_calls"][cid] = [name, ts.isoformat(), turn, pos.byte_offset]
        else:
            self.s["closed_calls"][cid] = [name, ts.isoformat(), turn, pos.byte_offset]
        out.append(ToolIoRow(agent=AGENT, io_uid=cid, session=self.key, ts=ts, byte_offset=pos.byte_offset,
                             kind="call", tool_name=name, call_uid=cid, turn_key=turn,
                             input_text=json.dumps(_scrub(block["input"]), ensure_ascii=False)
                             if "input" in block else None))
        touch = _file_touch(name, inp)
        if touch:
            path, op, added, removed = touch
            out.append(FileTouchRow(agent=AGENT, touch_uid=f"{cid}:0", session=self.key, ts=ts,
                                    byte_offset=pos.byte_offset, path=path, op=op, tool=name, call_uid=cid,
                                    turn_key=turn, lines_added=added, lines_removed=removed))
        # artifacts from file tools (v1 parity plus tool inputs)
        action = FILE_TOOLS.get(name)
        if action:
            path = inp.get("file_path") or inp.get("notebook_path") or inp.get("path")
            if isinstance(path, str) and path.startswith("/"):
                out.append(ArtifactRow(agent=AGENT, event_uid=cid, session=self.key, ts=ts,
                                       kind=artifact_kind(path), action=action, path=path,
                                       evidence_type="tool_input", byte_offset=pos.byte_offset,
                                       turn_key=turn, call_uid=cid,
                                       display_name=PurePosixPath(path).name or path))
        if name in SPAWN_TOOLS:
            self.s["spawns"][cid] = ["agent", ts.isoformat()]
            desc = as_str(inp.get("description"))
            out.append(SubagentSpawnRow(
                agent=AGENT, spawn_uid=cid, session=self.key, byte_offset=pos.byte_offset,
                turn_key=turn, child_session_uid=self.sid, spawned_at=ts,
                requested_type=as_str(inp.get("subagent_type")) or "general-purpose",
                requested_type_source="explicit" if as_str(inp.get("subagent_type")) else "default",
                requested_model=as_str(inp.get("model")),
                background=background, isolation=as_str(inp.get("isolation")),
                name=as_str(inp.get("name")), description=desc[:200] if desc else None,
                fork_scope="fork" if inp.get("subagent_type") == "fork" else None))
            prompt = inp.get("prompt")
            if isinstance(prompt, str) and prompt.strip():
                out.append(self._msg(f"{cid}:brief", ts, "user", "subagent_brief", prompt.strip(),
                                     pos, rec, turn=turn))
        elif name == "Workflow":
            self.s["spawns"][cid] = ["workflow", ts.isoformat()]
            desc = as_str(inp.get("description"))
            out.append(SubagentSpawnRow(
                agent=AGENT, spawn_uid=cid, session=self.key, byte_offset=pos.byte_offset,
                turn_key=turn, child_session_uid=self.sid, spawned_at=ts, requested_type="workflow",
                description=desc[:200] if desc else None))
        elif name in ("SubagentHandback", "StructuredOutput"):
            if name == "SubagentHandback":
                report = inp.get("message")
                report = report.strip() if isinstance(report, str) else ""
            else:
                report = json.dumps(inp, ensure_ascii=False) if inp else ""
            if report and self.role != "main":
                out.append(self._msg(f"{cid}:report", ts, "assistant", "subagent_report", report,
                                     pos, rec, turn=turn, model=as_str((rec.get("message") or {}).get("model"))))
        elif name == "Skill" and as_str(inp.get("skill")):
            self._event(out, cid, ts, "skill_invoke", pos, inp["skill"][:120])
        elif name in TASK_TOOLS:
            detail: dict[str, Any] = {}
            if as_str(inp.get("status")) and len(inp["status"]) < 40:
                detail["status"] = inp["status"]
            if isinstance(inp.get("todos"), list):
                detail["count"] = len(inp["todos"])
            if isinstance(inp.get("tasks"), list):
                detail["count"] = len(inp["tasks"])
            self._event(out, cid, ts, "task_list_op", pos, name, detail)
        elif name in ("EnterPlanMode", "ExitPlanMode"):
            self._event(out, cid, ts, "plan_mode", pos, "enter" if name == "EnterPlanMode" else "exit")
        elif name == "AskUserQuestion":
            qs = inp.get("questions")
            self._event(out, cid, ts, "user_question", pos, len(qs) if isinstance(qs, list) else None,
                        {"questions": len(qs)} if isinstance(qs, list) else None)

    # -- workflow journal -------------------------------------------------------------------

    def _journal(self, rec, pos, out):
        agent_id = as_str(rec.get("agentId"))
        rtype = rec.get("type")
        if not agent_id or not self.wf_id:
            out.append(ParseIssueRow(byte_offset=pos.byte_offset, kind="missing_field",
                                     line_number=pos.line_number, detail="journal agentId"))
            return
        row = SubagentSpawnRow(agent=AGENT, spawn_uid=f"wf:{self.wf_id}:{agent_id}",
                               session=SessionKey(AGENT, self.sid, ""), byte_offset=pos.byte_offset,
                               child_session_uid=self.sid, child_agent_id=agent_id,
                               workflow_id=self.wf_id, requested_type="workflow_agent")
        if rtype == "started":
            row.launch_status = "started"
        elif rtype == "result":
            row.completion_status = "completed"
        else:
            out.append(ParseIssueRow(byte_offset=pos.byte_offset, kind="unknown_type",
                                     line_number=pos.line_number, detail=f"journal:{str(rtype)[:40]}"))
            return
        out.append(row)


def _b64_size(data: str) -> int:
    """Decoded size of a base64 string without decoding it."""
    n = len(data) - data.count("\n") - data.count("\r") - data.count(" ")
    return max(0, n * 3 // 4 - (2 if data.endswith("==") else 1 if data.endswith("=") else 0))


def _source_meta(src: Any) -> tuple[str | None, int | None]:
    if not isinstance(src, dict):
        return None, None
    mime = as_str(src.get("media_type"))
    data = src.get("data")
    return mime, _b64_size(data) if isinstance(data, str) and src.get("type") == "base64" else None


def _scrub(value: Any) -> Any:
    """Copy of a JSON value with every base64 payload replaced by {"type","mime","bytes"}: API
    source blocks ({"source": {"type": "base64", "data"}}), `base64` fields (Read of an image) and
    `content` flagged isBase64. Everything else is copied unchanged, so dumps() of an untouched
    value equals dumps() of the original."""
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        mime_hint = value.get("type") if isinstance(value.get("type"), str) and "/" in value["type"] else None
        for k, v in value.items():
            if k == "source" and isinstance(v, dict) and v.get("type") == "base64" and isinstance(v.get("data"), str):
                out[k] = {"type": "base64", "mime": as_str(v.get("media_type")), "bytes": _b64_size(v["data"])}
            elif k == "base64" and isinstance(v, str):
                out[k] = {"type": "base64", "mime": mime_hint, "bytes": _b64_size(v)}
            elif k == "content" and isinstance(v, str) and value.get("isBase64") is True:
                out[k] = {"type": "base64", "mime": as_str(value.get("contentType")), "bytes": _b64_size(v)}
            else:
                out[k] = _scrub(v)
        return out
    if isinstance(value, list):
        return [_scrub(v) for v in value]
    return value


def _cjson(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _attachment_json(a: dict[str, Any]) -> str | None:
    rest = {k: v for k, v in a.items() if k != "type"}
    return _cjson(_scrub(rest)) if rest else None


def _join_text(value: Any) -> str | None:
    """Text of a hook/summary field: a string as-is, a list's items joined with blank lines, a
    dict as compact JSON."""
    if isinstance(value, str):
        return value or None
    if isinstance(value, list):
        items = [v if isinstance(v, str) else _cjson(_scrub(v)) for v in value if v not in (None, "")]
        return "\n\n".join(items) or None
    if isinstance(value, dict):
        return _cjson(_scrub(value)) if value else None
    return None


def _raw_text(content: Any) -> str:
    """Text of a user message for the v4 classes: a string as-is, else the text blocks joined with
    blank lines, unstripped (v3 classes keep common.text_blocks)."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n\n".join(b["text"] for b in content
                        if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str)
                        and b["text"])


def _nlines(text: Any) -> int | None:
    if not isinstance(text, str):
        return None
    if not text:
        return 0
    return text.count("\n") + (0 if text.endswith("\n") else 1)


def _diff_counts(old: Any, new: Any) -> tuple[int | None, int | None]:
    """(added, removed) lines between an Edit's old_string and new_string."""
    if not isinstance(old, str) or not isinstance(new, str):
        return _nlines(new), _nlines(old)
    a, b = old.splitlines(), new.splitlines()
    if max(len(a), len(b)) > DIFF_MAX_LINES:
        return _nlines(new), _nlines(old)
    added = removed = 0
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag in ("replace", "delete"):
            removed += i2 - i1
        if tag in ("replace", "insert"):
            added += j2 - j1
    return added, removed


def _file_touch(name: str, inp: dict[str, Any]) -> tuple[str, str, int | None, int | None] | None:
    op = TOUCH_TOOLS.get(name)
    if op is None:
        return None
    path = inp.get("file_path") or inp.get("notebook_path")
    if not isinstance(path, str) or not path:
        return None
    if name == "Write":
        return path, op, _nlines(inp.get("content")), None
    if name == "Edit":
        added, removed = _diff_counts(inp.get("old_string"), inp.get("new_string"))
        return path, op, added, removed
    if name == "MultiEdit":
        edits = [e for e in inp.get("edits") or [] if isinstance(e, dict)] if isinstance(inp.get("edits"), list) else []
        added = removed = 0
        for e in edits:
            x, y = _diff_counts(e.get("old_string"), e.get("new_string"))
            added += x or 0
            removed += y or 0
        return path, op, added if edits else None, removed if edits else None
    if name == "NotebookEdit":
        if inp.get("edit_mode") == "delete":
            return path, op, 0, None
        return path, op, _nlines(inp.get("new_source")), None
    return path, op, None, None


def _fill_usage(row: LlmCallRow, u: dict[str, Any]) -> None:
    row.input_uncached = as_int(u.get("input_tokens"))
    row.cache_read = as_int(u.get("cache_read_input_tokens"))
    cc = u.get("cache_creation")
    total_cw = as_int(u.get("cache_creation_input_tokens"))
    if isinstance(cc, dict):
        w5 = as_int(cc.get("ephemeral_5m_input_tokens"))
        w1 = as_int(cc.get("ephemeral_1h_input_tokens"))
        if (w5 or 0) + (w1 or 0) == 0 and total_cw:
            w5 = total_cw
        row.cache_write_5m, row.cache_write_1h = w5, w1
    else:
        row.cache_write_5m = total_cw
    row.output = as_int(u.get("output_tokens"))
    otd = u.get("output_tokens_details")
    if isinstance(otd, dict):
        row.reasoning = as_int(otd.get("thinking_tokens"))
    stu = u.get("server_tool_use")
    if isinstance(stu, dict):
        row.web_search_requests = as_int(stu.get("web_search_requests"))
        row.web_fetch_requests = as_int(stu.get("web_fetch_requests"))
    row.service_tier = as_str(u.get("service_tier"))
    row.speed = as_str(u.get("speed"))


def _ms_between(start: datetime | None, end: datetime | None) -> int | None:
    if start is None or end is None:
        return None
    return max(0, int((end - start).total_seconds() * 1000))


def _numeric_only(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k)[:120]: _numeric_only(v) for k, v in value.items()
                if isinstance(v, (dict, int, float, bool))}
    return value

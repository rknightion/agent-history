"""Frozen parser seam for the agent-history catalogue.

Parsers turn JSONL records into the row dataclasses below; they never touch the database. The
loader (load.py) owns reading, batching, upserts, source bookkeeping and post-passes.

Rules every parser must follow:
- Rows reference sessions only by natural key (SessionKey). Never invent surrogate ids.
- Every row carries the byte_offset of the line that produced it. The loader adds source_id,
  and for MessageRow the denormalised agent/namespace/profile.
- Keys are global content identity: re-observing the same content from another file (a fork
  replay, an archived copy) must produce the same key so the upsert is a no-op.
- Content lives in the text columns built for it, including tool I/O, reasoning, system prompts
  and attachments: MessageRow.text, ToolIoRow's
  *_text / result_json, AttachmentRow.text. `detail`/`meta` JSON stays structure and scalars.
  Never redact, omit or scrub content in a parser; privacy is the operator's call, not the
  catalogue's (CONTRACT.md).
- Rows that existed before parser v4 keep their exact key, class and text, so a rebuild re-uses
  every cached embedding (header and text are the cache key).
- Sums (tokens per turn/session/loop) are never accumulated here; SQL derives them.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from datetime import datetime
from typing import Any, ClassVar, Iterable, Protocol

# Merge policies for ON CONFLICT DO UPDATE, per non-key column.
KEEP = "keep"        # COALESCE(existing, excluded): first non-null observation wins
UPDATE = "update"    # COALESCE(excluded, existing): latest non-null observation wins
MAX = "max"          # GREATEST(existing, excluded)
MIN = "min"          # LEAST(existing, excluded)
OR = "or"            # existing OR excluded (booleans that only ever become true)
NOTHING = "nothing"  # ON CONFLICT DO NOTHING for the whole row


@dataclass(frozen=True)
class SessionKey:
    agent: str            # 'claude' | 'codex'
    session_uid: str      # Claude sessionId (also for subagents) | Codex thread id
    agent_id: str = ""    # Claude subagent agentId, else ''


@dataclass(frozen=True)
class FileContext:
    path: str             # absolute path being read (hot or cold copy)
    rel_path: str         # "<namespace>/<path under namespace>", stable across tiers
    namespace: str
    agent: str
    profile: str
    machine: str | None
    file_role: str        # main | subagent | workflow_agent | workflow_journal | pi_artifact


@dataclass(frozen=True)
class LinePos:
    byte_offset: int
    byte_length: int
    line_number: int      # 1-based


class Row:
    TABLE: ClassVar[str]
    KEY: ClassVar[tuple[str, ...]]
    POLICY: ClassVar[dict[str, str]] = {}
    DEFAULT_POLICY: ClassVar[str] = UPDATE

    def columns(self) -> dict[str, Any]:
        return {f.name: getattr(self, f.name) for f in fields(self)}  # type: ignore[arg-type]


# --- rows keyed on a session -------------------------------------------------------------------


@dataclass
class SessionRow(Row):
    """Emitted by the file that owns the session. Loader also creates stubs for referenced keys."""
    TABLE: ClassVar[str] = "session"
    KEY: ClassVar[tuple[str, ...]] = ("session",)
    POLICY: ClassVar[dict[str, str]] = {
        "first_event_at": MIN, "last_event_at": MAX, "first_human_at": MIN, "last_human_at": MAX,
        "cli_version_first": KEEP, "cli_version_last": UPDATE, "git_commit_start": KEEP,
        "git_branch": KEEP, "cwd": KEEP, "is_subagent": OR, "inherited_skipped": MAX,
    }
    session: SessionKey
    byte_offset: int = 0
    is_subagent: bool = False
    root_session_uid: str | None = None
    parent_session_uid: str | None = None
    parent_agent_id: str | None = None
    parent_call_uid: str | None = None
    spawn_kind: str | None = None
    spawn_depth: int | None = None
    agent_type: str | None = None
    agent_type_source: str | None = None
    agent_role: str | None = None
    agent_path: str | None = None
    agent_nickname: str | None = None
    forked_from_uid: str | None = None
    workflow_id: str | None = None
    title: str | None = None
    custom_title: str | None = None
    cwd: str | None = None
    git_branch: str | None = None
    git_commit_start: str | None = None
    git_remote_url: str | None = None
    cli_version_first: str | None = None
    cli_version_last: str | None = None
    entrypoint: str | None = None
    model_provider: str | None = None
    thread_source: str | None = None
    history_mode: str | None = None
    first_event_at: datetime | None = None
    last_event_at: datetime | None = None
    first_human_at: datetime | None = None
    last_human_at: datetime | None = None
    inherited_skipped: int | None = None


@dataclass
class TurnRow(Row):
    TABLE: ClassVar[str] = "turn"
    KEY: ClassVar[tuple[str, ...]] = ("session", "turn_key")
    POLICY: ClassVar[dict[str, str]] = {
        "started_at": MIN, "completed_at": MAX, "origin": KEEP, "message_count": MAX,
    }
    session: SessionKey
    turn_key: str
    byte_offset: int
    origin: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    duration_ms: int | None = None
    ttft_ms: int | None = None
    status: str | None = None
    abort_reason: str | None = None
    model: str | None = None
    effort: str | None = None
    permission_mode: str | None = None
    approval_policy: str | None = None
    sandbox_type: str | None = None
    collaboration_mode: str | None = None
    context_window: int | None = None
    message_count: int | None = None


MESSAGE_CLASSES = (
    # text surface before v4 (embedded; keep key, class and text byte-identical)
    "human_prompt", "queued_prompt", "assistant_text", "subagent_brief", "subagent_report",
    "compaction_summary", "task_notification_summary",
    # v4 additions (never embedded by default)
    "reasoning",             # Claude thinking / Codex reasoning summary or raw text
    "system_prompt",         # Claude prompt_snapshot.systemPrompt, Codex session_meta.base_instructions
    "system_reminder",       # <system-reminder> / isMeta reminders / reminder attachments
    "context_injection",     # instruction files, environment, tool/skill/agent listings, Codex developer
                             # and injected user-role context (environment_context, AGENTS.md, ...)
    "hook_output",           # hook stdout/stderr/additional context/system messages
    "command_expansion",     # slash-command prompt bodies expanded by the harness
    "skill_body",            # skill bodies loaded by Skill / $skill / <skill>
    "local_command_output",  # <local-command-stdout|stderr>, bash-mode stdout/stderr
    "agent_message",         # peer/coordinator/teammate/inter-agent messages and parent follow-ups
                             # delivered to a thread as user-role records
    "interrupt_marker",      # "[Request interrupted by user...]" and equivalents
)
PROMPT_ORIGINS = ("typed", "pasted", "slash_command", "skill", "hook", "local_command", "launch_message",
                  "compaction_continuation", "agent_message", "interrupt", "other")


@dataclass
class MessageRow(Row):
    TABLE: ClassVar[str] = "message"
    KEY: ClassVar[tuple[str, ...]] = ("agent", "event_uid")
    DEFAULT_POLICY: ClassVar[str] = NOTHING
    agent: str
    event_uid: str            # Claude line uuid | Codex "<thread>:<ordinal or byte_offset>"
    session: SessionKey
    ts: datetime
    role: str                 # user | assistant | system
    message_class: str        # MESSAGE_CLASSES
    text: str
    byte_offset: int
    byte_length: int
    line_number: int | None = None
    turn_key: str | None = None
    model: str | None = None
    is_sidechain: bool = False
    prompt_origin: str | None = None       # PROMPT_ORIGINS; human_prompt and queued_prompt only
    detail: dict[str, Any] | None = None   # structure only, e.g. {"source": "nested_memory"},
                                           # {"redacted": true}, {"signature_only": true}
    raw_record_origin: str | None = None  # Claude top-level JSONL type; NULL for legacy/other agents


@dataclass
class LlmCallRow(Row):
    TABLE: ClassVar[str] = "llm_call"
    KEY: ClassVar[tuple[str, ...]] = ("agent", "response_id")
    POLICY: ClassVar[dict[str, str]] = {
        "session": KEEP, "turn_key": KEEP, "ts": MIN,
        "input_uncached": MAX, "cache_read": MAX, "cache_write_5m": MAX, "cache_write_1h": MAX,
        "output": MAX, "reasoning": MAX, "web_search_requests": MAX, "web_fetch_requests": MAX,
        "line_count": MAX, "byte_offset": KEEP, "is_api_error": OR,
    }
    agent: str
    response_id: str
    session: SessionKey
    ts: datetime
    byte_offset: int
    turn_key: str | None = None
    model: str | None = None
    request_id: str | None = None
    stop_reason: str | None = None
    input_uncached: int | None = None
    cache_read: int | None = None
    cache_write_5m: int | None = None
    cache_write_1h: int | None = None
    output: int | None = None
    reasoning: int | None = None
    context_window: int | None = None
    web_search_requests: int | None = None
    web_fetch_requests: int | None = None
    service_tier: str | None = None
    speed: str | None = None
    effort: str | None = None
    is_api_error: bool = False
    error_kind: str | None = None
    api_error_status: int | None = None
    is_sidechain: bool = False
    line_count: int | None = None


@dataclass
class ToolCallRow(Row):
    """Call and result halves may arrive separately; both upsert the same (agent, call_uid)."""
    TABLE: ClassVar[str] = "tool_call"
    KEY: ClassVar[tuple[str, ...]] = ("agent", "call_uid")
    POLICY: ClassVar[dict[str, str]] = {
        "session": KEEP, "turn_key": KEEP, "response_id": KEEP, "started_at": MIN,
        "ended_at": MAX, "byte_offset": KEEP, "interrupted": OR, "timed_out": OR,
    }
    agent: str
    call_uid: str
    session: SessionKey
    tool_name: str
    byte_offset: int
    turn_key: str | None = None
    response_id: str | None = None
    tool_family: str | None = None
    mcp_server: str | None = None
    mcp_tool: str | None = None
    codex_namespace: str | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    duration_ms: int | None = None
    input_bytes: int | None = None
    output_bytes: int | None = None
    persisted_output_bytes: int | None = None
    outcome: str | None = None
    is_error: bool | None = None
    exit_code: int | None = None
    denial_kind: str | None = None
    interrupted: bool | None = None
    timed_out: bool | None = None
    background: bool | None = None
    attribution_skill: str | None = None
    attribution_plugin: str | None = None
    meta: dict[str, Any] | None = None


@dataclass
class ToolOpRow(Row):
    TABLE: ClassVar[str] = "tool_op"
    KEY: ClassVar[tuple[str, ...]] = ("agent", "item_uid")
    POLICY: ClassVar[dict[str, str]] = {"session": KEEP, "byte_offset": KEEP}
    agent: str
    item_uid: str
    session: SessionKey
    item_type: str
    byte_offset: int
    turn_key: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    duration_ms: int | None = None
    status: str | None = None
    exit_code: int | None = None
    exec_source: str | None = None
    cmd_verb: str | None = None
    parsed_cmd_types: list[str] | None = None
    mcp_server: str | None = None
    mcp_tool: str | None = None
    is_error: bool | None = None
    output_bytes: int | None = None
    file_count: int | None = None
    call_uid: str | None = None
    link_method: str | None = None


@dataclass
class SubagentSpawnRow(Row):
    """`session` is the PARENT. Launch, result and completion halves upsert the same spawn_uid."""
    TABLE: ClassVar[str] = "subagent_spawn"
    KEY: ClassVar[tuple[str, ...]] = ("agent", "spawn_uid")
    POLICY: ClassVar[dict[str, str]] = {"session": KEEP, "spawned_at": MIN, "byte_offset": KEEP}
    agent: str
    spawn_uid: str
    session: SessionKey
    byte_offset: int
    turn_key: str | None = None
    child_session_uid: str | None = None
    child_agent_id: str | None = None
    child_task_name: str | None = None
    spawned_at: datetime | None = None
    requested_type: str | None = None
    requested_type_source: str | None = None
    requested_model: str | None = None
    resolved_model: str | None = None
    reasoning_effort: str | None = None
    background: bool | None = None
    fork_scope: str | None = None
    isolation: str | None = None
    name: str | None = None
    description: str | None = None      # the short label only, never the prompt
    launch_status: str | None = None
    completion_status: str | None = None
    completed_at: datetime | None = None
    reported_tokens: int | None = None
    reported_tool_uses: int | None = None
    reported_duration_ms: int | None = None
    workflow_id: str | None = None


@dataclass
class PiRunResponseRow(Row):
    """Structural pi-subagents artifact evidence; never a second LLM call or message."""
    TABLE: ClassVar[str] = "pi_run_response"
    KEY: ClassVar[tuple[str, ...]] = ("run_id", "response_id", "source_id")
    POLICY: ClassVar[dict[str, str]] = {"agent_type": KEEP, "byte_offset": KEEP}
    run_id: str
    response_id: str
    byte_offset: int
    agent_type: str | None = None


@dataclass
class HookEventRow(Row):
    TABLE: ClassVar[str] = "hook_event"
    KEY: ClassVar[tuple[str, ...]] = ("agent", "event_uid")
    DEFAULT_POLICY: ClassVar[str] = NOTHING
    agent: str
    event_uid: str
    session: SessionKey
    ts: datetime
    byte_offset: int
    turn_key: str | None = None
    call_uid: str | None = None
    hook_event: str | None = None
    outcome: str | None = None
    hook_name: str | None = None
    command_sha256: str | None = None
    exit_code: int | None = None
    duration_ms: int | None = None
    timed_out: bool | None = None
    prevented_continuation: bool | None = None
    output_bytes: int | None = None


@dataclass
class CompactionRow(Row):
    TABLE: ClassVar[str] = "compaction"
    KEY: ClassVar[tuple[str, ...]] = ("agent", "event_uid")
    DEFAULT_POLICY: ClassVar[str] = NOTHING
    agent: str
    event_uid: str
    session: SessionKey
    ts: datetime
    byte_offset: int
    turn_key: str | None = None
    trigger: str | None = None
    pre_tokens: int | None = None
    post_tokens: int | None = None
    duration_ms: int | None = None
    dropped_tokens: int | None = None
    window_number: int | None = None
    window_id: str | None = None
    previous_window_id: str | None = None


@dataclass
class GitEventRow(Row):
    TABLE: ClassVar[str] = "git_event"
    KEY: ClassVar[tuple[str, ...]] = ("agent", "event_uid")
    DEFAULT_POLICY: ClassVar[str] = NOTHING
    agent: str
    event_uid: str            # e.g. "<call_uid>:commit:<sha>" so structured + regex sightings merge
    session: SessionKey
    ts: datetime
    op: str
    evidence: str
    byte_offset: int
    turn_key: str | None = None
    call_uid: str | None = None
    cwd: str | None = None
    branch: str | None = None
    sha_short: str | None = None
    pr_number: int | None = None
    pr_action: str | None = None
    pr_url: str | None = None
    pr_repo: str | None = None
    remote_url: str | None = None


@dataclass
class ArtifactRow(Row):
    TABLE: ClassVar[str] = "artifact"
    KEY: ClassVar[tuple[str, ...]] = ("agent", "event_uid", "path", "action")
    DEFAULT_POLICY: ClassVar[str] = NOTHING
    agent: str
    event_uid: str
    session: SessionKey
    ts: datetime
    kind: str
    action: str
    path: str
    evidence_type: str
    byte_offset: int
    turn_key: str | None = None
    call_uid: str | None = None
    display_name: str | None = None


@dataclass
class SessionEventRow(Row):
    TABLE: ClassVar[str] = "session_event"
    KEY: ClassVar[tuple[str, ...]] = ("agent", "event_uid", "kind")
    DEFAULT_POLICY: ClassVar[str] = NOTHING
    agent: str
    event_uid: str
    session: SessionKey
    ts: datetime
    kind: str
    byte_offset: int
    turn_key: str | None = None
    value: str | None = None
    detail: dict[str, Any] | None = None


@dataclass
class RateLimitRow(Row):
    TABLE: ClassVar[str] = "rate_limit_sample"
    KEY: ClassVar[tuple[str, ...]] = ("agent", "event_uid", "window_kind")
    DEFAULT_POLICY: ClassVar[str] = NOTHING
    agent: str
    event_uid: str
    session: SessionKey
    ts: datetime
    window_kind: str
    byte_offset: int
    limit_id: str | None = None
    limit_name: str | None = None
    plan_type: str | None = None
    used_percent: float | None = None
    window_minutes: int | None = None
    resets_at: datetime | None = None
    reached_type: str | None = None


@dataclass
class CostStateRow(Row):
    TABLE: ClassVar[str] = "cost_state"
    KEY: ClassVar[tuple[str, ...]] = ("agent", "event_uid")
    DEFAULT_POLICY: ClassVar[str] = NOTHING
    agent: str
    event_uid: str
    session: SessionKey
    ts: datetime
    byte_offset: int
    total_cost_usd: float | None = None
    api_duration_ms: int | None = None
    api_duration_no_retry_ms: int | None = None
    tool_duration_ms: int | None = None
    total_duration_ms: int | None = None
    lines_added: int | None = None
    lines_removed: int | None = None
    model_usage: dict[str, Any] | None = None
    start_time: datetime | None = None


@dataclass
class ToolIoRow(Row):
    """Full tool input and output. Call and result halves upsert the same (agent, io_uid).

    io_uid = call_uid for model-level calls (Claude tool_use, Codex function/custom/local shell/web
    search calls); "item:<item_uid>" for Codex executed operations (tool_op rows). Text columns hold
    exactly what the source recorded (never trimmed); the loader adds sizes and sha256 of each.
    Images and other binary parts are metadata only in output_parts: [{"type", "mime", "bytes"}].
    """
    TABLE: ClassVar[str] = "tool_io"
    KEY: ClassVar[tuple[str, ...]] = ("agent", "io_uid")
    POLICY: ClassVar[dict[str, str]] = {"session": KEEP, "ts": MIN, "byte_offset": KEEP, "turn_key": KEEP,
                                        "input_truncated": OR, "output_truncated": OR}
    agent: str
    io_uid: str
    session: SessionKey
    ts: datetime                     # call time (or result time when the call half is unseen)
    byte_offset: int
    kind: str                        # 'call' | 'op'
    output_byte_offset: int | None = None  # result record's own offset, not the call's
    tool_name: str | None = None
    call_uid: str | None = None      # links ah.tool_call
    item_uid: str | None = None      # links ah.tool_op
    turn_key: str | None = None
    input_text: str | None = None    # arguments JSON / custom tool input / command line / patch
    input_truncated: bool | None = None
    output_text: str | None = None   # model-visible output (text parts joined) / aggregated output
    output_truncated: bool | None = None   # the SOURCE truncated or persisted it elsewhere
    stdout_text: str | None = None
    stderr_text: str | None = None
    result_json: str | None = None   # structured harness result (Claude toolUseResult, Codex item/result)
                                     # as JSON text, minus fields already stored in the columns above
    output_parts: list[dict[str, Any]] | None = None
    output_at: datetime | None = None


@dataclass
class AttachmentRow(Row):
    """Images, files and pasted text attached to a prompt, tool result or attachment record.
    Binary payloads are metadata only; textual attachments carry their text."""
    TABLE: ClassVar[str] = "attachment"
    KEY: ClassVar[tuple[str, ...]] = ("agent", "attachment_uid")
    DEFAULT_POLICY: ClassVar[str] = NOTHING
    agent: str
    attachment_uid: str              # "<event_uid>:att:<n>" or "<call_uid>:att:<n>"
    session: SessionKey
    ts: datetime
    byte_offset: int
    kind: str                        # image | file | pasted_text | document | other
    source: str                      # prompt | queued_prompt | tool_result | tool_input | attachment_record
    event_uid: str | None = None     # the message line it arrived on
    call_uid: str | None = None
    turn_key: str | None = None
    mime: str | None = None
    file_name: str | None = None
    size_bytes: int | None = None
    text: str | None = None
    detail: dict[str, Any] | None = None


@dataclass
class FileTouchRow(Row):
    """A file read or change by a tool. `path` is as given (absolute when the tool gave one); the
    post-pass maps it to ah.repo + repo-relative path."""
    TABLE: ClassVar[str] = "file_touch"
    KEY: ClassVar[tuple[str, ...]] = ("agent", "touch_uid")
    DEFAULT_POLICY: ClassVar[str] = NOTHING
    agent: str
    touch_uid: str                   # "<call_uid or item:item_uid>:<n>"
    session: SessionKey
    ts: datetime
    byte_offset: int
    path: str
    op: str                          # read | create | edit | delete | move
    tool: str
    call_uid: str | None = None
    item_uid: str | None = None
    turn_key: str | None = None
    lines_added: int | None = None
    lines_removed: int | None = None
    move_from: str | None = None


@dataclass
class ContinuationRow(Row):
    """`child_uid` continues `parent_uid`. Emitted by whichever file carries the evidence; `session`
    is that file's own session (the loader needs it for source ownership)."""
    TABLE: ClassVar[str] = "session_continuation"
    KEY: ClassVar[tuple[str, ...]] = ("agent", "child_uid", "parent_uid", "kind")
    DEFAULT_POLICY: ClassVar[str] = NOTHING
    agent: str
    child_uid: str                   # session_uid of the continuing session (agent_id '')
    parent_uid: str                  # session_uid it continues
    kind: str                        # resume | fork | compaction_continuation | clear | other
    session: SessionKey
    ts: datetime
    byte_offset: int
    evidence: str                    # e.g. 'continued-in', 'forked_from_id'


# --- rows keyed on the source file -------------------------------------------------------------


@dataclass
class ParseIssueRow(Row):
    TABLE: ClassVar[str] = "parse_issue"
    KEY: ClassVar[tuple[str, ...]] = ("source", "byte_offset", "kind")
    DEFAULT_POLICY: ClassVar[str] = NOTHING
    byte_offset: int
    kind: str
    line_number: int | None = None
    detail: str | None = None            # structure only


@dataclass
class RecordTypeRow(Row):
    """Drift detection. Emit once per (record_type, subtype) per file batch, not per line."""
    TABLE: ClassVar[str] = "record_type_seen"
    KEY: ClassVar[tuple[str, ...]] = ("agent", "record_type", "subtype")
    agent: str
    record_type: str
    subtype: str
    ts: datetime
    count: int
    key_set: str | None = None           # sorted top-level keys, comma-joined
    cli_version: str | None = None


ALL_ROWS: tuple[type[Row], ...] = (
    SessionRow, TurnRow, MessageRow, LlmCallRow, ToolCallRow, ToolOpRow, SubagentSpawnRow,
    HookEventRow, CompactionRow, GitEventRow, ArtifactRow, SessionEventRow, RateLimitRow,
    CostStateRow, ToolIoRow, AttachmentRow, FileTouchRow, ContinuationRow, ParseIssueRow, RecordTypeRow,
)


class Parser(Protocol):
    """One instance per (file, refresh). The loader reads whole lines from the saved offset,
    JSON-decodes them (decode failures become ParseIssueRow without calling the parser), and
    calls `line` for each record. After each batch the loader persists `state()` together with
    the offset in the same transaction, then resumes a later run with that state.

    `state()` must be JSON-serialisable and bounded (drop entries older than 24 h of transcript
    time). It is the only memory carried between batches and runs.
    """

    def __init__(self, ctx: FileContext, state: dict[str, Any]) -> None: ...

    def line(self, record: dict[str, Any], pos: LinePos) -> Iterable[Row]: ...

    def flush(self) -> Iterable[Row]:
        """Called at the end of every batch; emit buffered rows (e.g. RecordTypeRow counts)."""
        ...

    def state(self) -> dict[str, Any]: ...


PARSER_VERSION_CLAUDE = "9"
PARSER_VERSION_CODEX = "8"

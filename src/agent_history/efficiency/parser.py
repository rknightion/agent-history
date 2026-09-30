"""Incremental cross-harness efficiency classifier.

The trigger and series rules are ported from the original session collector. The
public adapter supplies namespace roots and a catalogue loop map, not host paths.
"""

from __future__ import annotations
import hashlib
import json
import math
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from .context_windows import PI_CONTEXT_WINDOWS, claude_context_window


def pi_format():
    from agent_history import parse_pi  # loaded only for pi transcript parsing

    return parse_pi


EFFICIENCY_STATE_VERSION = 2
EFFICIENCY_BUDGET_SECONDS = 20.0
EFFICIENCY_FIRST_PARSE_SECONDS = 3 * 86400
EFFICIENCY_ACTIVE_SECONDS = 600
EFFICIENCY_GAP_SECONDS = 1800
EFFICIENCY_MAX_MODELS = 20
EFFICIENCY_MAX_PENDING = 64
EFFICIENCY_MAX_PROCESSES = 256
EFFICIENCY_MAX_IDS = 32
EFFICIENCY_MAX_RECENT_CALLS = 50000
EFFICIENCY_HEAD_BYTES = 4 * 1024 * 1024
EFFICIENCY_BUDGET_CHECK_LINES = 256
EFFICIENCY_ROLES = ("root", "worker", "solo")

EFFICIENCY_COUNTERS: dict[str, tuple[tuple[str, ...], str]] = {
    "agent_efficiency_time_seconds_total": (
        ("agent", "namespace", "role", "state"),
        "Wall seconds attributed to each agent thread state.",
    ),
    "agent_efficiency_llm_calls_total": (
        ("agent", "namespace", "role", "trigger"),
        "Model calls by what the call reacted to.",
    ),
    "agent_efficiency_input_tokens_total": (
        ("agent", "namespace", "role", "trigger", "cache"),
        "Input tokens per model call split into cached (hit) and uncached (miss).",
    ),
    "agent_efficiency_output_tokens_total": (
        ("agent", "namespace", "role"),
        "Model output tokens including reasoning.",
    ),
    "agent_efficiency_model_seconds_total": (
        ("agent", "namespace", "role", "model"),
        "Model-state wall seconds by model.",
    ),
    "agent_efficiency_model_calls_total": (("agent", "namespace", "role", "model"), "Model calls by model."),
    "agent_efficiency_tool_calls_total": (("agent", "namespace", "role", "class"), "Tool calls by efficiency class."),
    "agent_efficiency_poll_calls_total": (
        ("agent", "namespace", "role", "target", "result"),
        "Completed wait and poll tool calls by what they wait on and whether they timed out or delivered an event.",
    ),
    "agent_efficiency_poll_seconds_total": (
        ("agent", "namespace", "role", "target", "result"),
        "Seconds blocked in wait and poll tool calls by what they wait on and their result.",
    ),
    "agent_efficiency_wait_timeout_ms_total": (
        ("agent", "namespace", "role", "tool"),
        "Requested wait timeout or yield milliseconds summed over wait requests.",
    ),
    "agent_efficiency_wait_requests_total": (
        ("agent", "namespace", "role", "tool"),
        "Wait requests that carried an explicit timeout or yield.",
    ),
    "agent_efficiency_spawns_total": (("agent", "namespace"), "Agents spawned."),
    "agent_efficiency_compactions_total": (("agent", "namespace", "role"), "Context compactions."),
    "agent_efficiency_turn_errors_total": (("agent", "namespace", "kind"), "Turns ending in an error or abort."),
    # v1.2
    "agent_efficiency_spawns_by_route_total": (
        ("agent", "namespace", "role", "spawn_model", "effort", "agent_type", "fork"),
        "Agent spawns by requested route.",
    ),
    "agent_efficiency_spawn_errors_total": (("agent", "namespace", "kind"), "Failed agent spawns by failure kind."),
    "agent_efficiency_git_pushes_total": (
        ("agent", "namespace", "role", "outcome"),
        "git push commands by exit outcome.",
    ),
    "agent_efficiency_ci_waits_total": (
        ("agent", "namespace", "role", "outcome"),
        "Blocking CI watch commands by terminal outcome.",
    ),
    "agent_efficiency_gate_runs_total": (
        ("agent", "namespace", "role", "outcome"),
        "Gate commands (just or make check, test, ci) by exit outcome.",
    ),
    "agent_efficiency_coderabbit_findings_total": (
        ("agent", "namespace", "role", "severity"),
        "CodeRabbit agent-mode findings by severity.",
    ),
    "agent_efficiency_coderabbit_reviews_total": (
        ("agent", "namespace", "role", "outcome"),
        "CodeRabbit agent-mode reviews by outcome.",
    ),
    "agent_efficiency_tool_failures_total": (
        ("agent", "namespace", "role", "class"),
        "Tool calls whose result indicates failure, by efficiency class.",
    ),
    "agent_efficiency_interventions_total": (
        ("agent", "namespace", "role", "kind"),
        "Human messages into an already-running thread and turn aborts by the user.",
    ),
    # v1.3 (root and solo threads only)
    "agent_efficiency_root_llm_calls_by_protocol_total": (
        ("agent", "namespace", "protocol", "poll"),
        "Root and solo thread model calls by loop protocol and whether the call reacted to a timed-out wait.",
    ),
    "agent_efficiency_root_time_seconds_by_protocol_total": (
        ("agent", "namespace", "protocol", "state"),
        "Root and solo thread wall seconds by loop protocol and state.",
    ),
}
# Histograms persist cumulative buckets in the state totals as <name>_bucket (key ends in the le
# bound), <name>_sum and <name>_count, and render as one Prometheus histogram family.
EFFICIENCY_HISTOGRAMS: dict[str, tuple[tuple[str, ...], tuple[int, ...], str]] = {
    "agent_efficiency_lane_seconds": (
        ("agent", "namespace"),
        (300, 900, 1800, 3600, 7200, 14400),
        "Worker thread lifetime from first to last event, observed once it goes quiet for 30 minutes.",
    ),
    "agent_efficiency_first_spawn_seconds": (
        ("agent", "namespace"),
        (60, 300, 900, 1800, 3600),
        "Root thread start to its first spawn.",
    ),
}
# v1.4: every counter and histogram also carries `loop`, the fan-out loop the thread belonged to when
# the event was counted (see "Loop attribution" below). It is appended last, so persisted keys are
# "<values...>\t<loop>" and histogram bucket keys "<values...>\t<loop>\t<le>".
EFFICIENCY_COUNTERS = {name: ((*labels, "loop"), text) for name, (labels, text) in EFFICIENCY_COUNTERS.items()}
EFFICIENCY_HISTOGRAMS = {
    name: ((*labels, "loop"), bounds, text) for name, (labels, bounds, text) in EFFICIENCY_HISTOGRAMS.items()
}
EFFICIENCY_EFFORTS = frozenset({"low", "medium", "high", "xhigh", "max"})
EFFICIENCY_SEVERITIES = frozenset({"critical", "major", "minor", "trivial", "info"})
EFFICIENCY_RATE_WINDOWS = ("primary", "secondary")
EFFICIENCY_MAX_AGENT_TYPES = 20
EFFICIENCY_MAX_DELIVERY_PROCESSES = 64
# agent-workflows subagent definitions and their pinned models; other Claude names map to `other`.
EFFICIENCY_CLAUDE_PINNED = {
    "complex-worker": "opus",
    "gate-runner": "sonnet",
    "lane-worker": "sonnet",
    "mapper": "sonnet",
    "poller": "sonnet",
    "rescue-specialist": "opus",
    "reviewer": "opus",
    "security-reviewer": "opus",
}
EFFICIENCY_CLAUDE_AGENT_TYPES = frozenset({*EFFICIENCY_CLAUDE_PINNED, "general-purpose", "Explore", "Plan"})
EFFICIENCY_SAFE_LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/\[\]+-]{0,63}")
EFFICIENCY_SPAWN_OK_KEYS = ("task_name", "agent_id", "thread_id", "nickname")
EFFICIENCY_SPAWN_ERRORS = (
    ("thread_limit", re.compile(r"thread limit", re.I)),
    ("unknown_model", re.compile(r"unknown model", re.I)),
    ("path_exists", re.compile(r"agent path .{0,512} already exists", re.I | re.S)),
    ("fork_type", re.compile(r"forked agents inherit|full-history fork", re.I)),
    ("parse", re.compile(r"failed to parse function arguments", re.I)),
)
EFFICIENCY_DELIVERY_HINT = re.compile(r"\b(?:git|gh|just|make|coderabbit)\b")
EFFICIENCY_HEREDOC = re.compile(r"(?<!<)<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")
EFFICIENCY_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")
EFFICIENCY_WRAPPERS = frozenset({"env", "command", "nohup", "time", "exec", "sudo"})
EFFICIENCY_MAX_JSON_RESULT = 65536
EFFICIENCY_HEADER_END = re.compile(r'\nOutput:|"output"\s*:')
EFFICIENCY_EXIT_CODE = re.compile(r'Process exited with code (-?\d+)|"?exit_code"?\s*:\s*(-?\d+)')
EFFICIENCY_PLAIN_EXIT = re.compile(r"^Process exited with code (-?\d+)", re.M)
EFFICIENCY_PLAIN_RUNNING = re.compile(r"^Process running with session ID (\d+)", re.M)
EFFICIENCY_APPLY_PATCH_FAILED = re.compile(r"\s*(apply_patch verification failed|failed to apply|error)", re.I)
EFFICIENCY_CR_RECORD = re.compile(r'\{\s*"type"\s*:\s*"([a-z_]+)"')
EFFICIENCY_CR_SEVERITY = re.compile(r'"severity"\s*:\s*"([a-z]+)"')
EFFICIENCY_RATE_LIMIT = re.compile(r"rate[ _-]?limit", re.I)
# Codex role=user response items: leading structural markers of injected context. Any
# other leading <tag> is also treated as injected; only these tags mark human input.
EFFICIENCY_CODEX_INJECTED_PREFIXES = (
    "# AGENTS.md instructions",
    "Another language model started to solve this problem",
)
EFFICIENCY_CODEX_HUMAN_TAGS = frozenset({"image", "send_user_message_question_reply"})
EFFICIENCY_LEADING_TAG = re.compile(r"<([A-Za-z_][\w\-]*)")
EFFICIENCY_CELL_SPAWN = re.compile(r"tools\.(?:collaboration__)?spawn_agent\(")
EFFICIENCY_CELL_NO_SPAWN = re.compile(r"spawn_agent is not a function")
EFFICIENCY_SPAWN_OK_TEXT = re.compile(r'"(?:task_name|agent_id)"\s*:')
EFFICIENCY_ROUTE_KEYS = ("model", "reasoning_effort", "agent_type", "fork_turns")
# v1.3: the loop protocol a thread's first human prompt declares, and the stalled-loop windows.
EFFICIENCY_PROTOCOL_RE = re.compile(r"Contract:\s*`?loop-v(\d+(?:\.\d+)*)")
EFFICIENCY_PROTOCOLS = frozenset({"v2.1", "v2.0", "other", "none"})
EFFICIENCY_PROTOCOL_SCAN_BYTES = 16 * 1024 * 1024
EFFICIENCY_CHILD_FLIGHT_SECONDS = 3600
EFFICIENCY_LIVE_ROOT_SECONDS = 1500
EFFICIENCY_CLAUDE_NOT_HUMAN = (
    "<task-notification>",
    "<local-command-stdout>",
    "<local-command-stderr>",
    "<local-command-caveat>",
    "<bash-stdout>",
    "<bash-stderr>",
    "<system-reminder>",
    "[Request interrupted",
)

EFFICIENCY_WAIT_RE = re.compile(
    r'write_stdin\(\{[^}]*chars:\s*""|"chars":\s*""|tools\.wait\(|\bsleep\s+\d|tools\.sleep|setTimeout|wait_agent'
    r"|gh run watch|--watch\b|\buntil\b[^\n]{0,200}\bdo\b|while [^\n]{0,200}sleep|timeout \d+ .*(tail -f|wait)",
    re.S,
)
EFFICIENCY_STATUS_RE = re.compile(
    r"gh (run|pr) (view|list|checks|status)|gh api [^\n]*(actions/runs|check-runs|pulls)"
    r"|git (fetch|log|status|rev-parse|ls-remote|worktree list)|backlog task (list|view)|list_agents"
    r"|\b(cat|tail|head|sed -n|wc)\b[^\n]{0,160}(state-|report-|outcomes-|\.notified|\.claim|lane|loop\d|wave\d)"
    r"|\bps\b|pgrep|curr_time",
    re.S,
)
EFFICIENCY_CLOCK_ONLY = re.compile(r"\s*const r\s*=\s*await tools\.clock__curr_time\(\{\}\);\s*text\(r\)\s*")
EFFICIENCY_EXTRACT_CMD = re.compile(r'cmd:\s*"((?:[^"\\]|\\.)*)"')
EFFICIENCY_TARGET_CMD = re.compile(r'"?cmd"?\s*:\s*"((?:[^"\\]|\\.)*)"')
EFFICIENCY_STDIN_CALL = re.compile(r"write_stdin\(\s*\{([^}]*)\}?")
EFFICIENCY_EMPTY_CHARS = re.compile(r'"?chars"?\s*:\s*""')
EFFICIENCY_STDIN_SESSION = re.compile(r'"?session_id"?\s*:\s*(\d+)')
EFFICIENCY_JS_WAIT_MS = {
    "wait_agent": re.compile(r'wait_agent\(\s*\{[^}]*?"?timeout_ms"?\s*:\s*(\d+)'),
    "write_stdin": re.compile(r'"?yield_time_ms"?\s*:\s*(\d+)'),
    "cell_wait": re.compile(r'tools\.wait\(\s*\{[^}]*?"?(?:yield_time_ms|timeout_ms)"?\s*:\s*(\d+)'),
    "sleep": re.compile(r'tools\.sleep\(\s*\{[^}]*?"?duration_ms"?\s*:\s*(\d+)'),
}
EFFICIENCY_EXITED = re.compile(r'Process exited with code -?\d+|"?exit_code"?\s*:\s*-?\d+')
EFFICIENCY_NOT_TIMED_OUT = re.compile(r'"?timed_out"?\s*:\s*false')
EFFICIENCY_PROCESS_ID = re.compile(r'"?session_id"?:\s*(\d+)|session ID (\d+)')
EFFICIENCY_POLL_TARGETS = (
    ("ci", re.compile(r"gh run (watch|view)|gh pr checks|check-runs|actions/runs")),
    ("coderabbit", re.compile(r"coderabbit")),
    ("review_harness", re.compile(r"xreview|claude -p|claude --print|codex exec")),
    ("sleep", re.compile(r"^\s*sleep|;\s*sleep|until |while ")),
    ("gate", re.compile(r"\bjust\b|make |go test|pytest|npm|pnpm|cargo|tofu|terraform|kubectl|helm|docker")),
    ("other", re.compile(r"git (push|pull|fetch|rebase|merge)")),
    ("other", re.compile(r"\bssh\b")),
)
EFFICIENCY_CODEX_TOOL = {
    "wait": "wait",
    "wait_agent": "wait",
    "sleep": "wait",
    "list_agents": "status",
    "get_goal": "status",
    "spawn_agent": "orchestrate",
    "send_message": "orchestrate",
    "followup_task": "orchestrate",
    "interrupt_agent": "orchestrate",
    "request_user_input": "human",
    "request_user_input_async": "human",
}
EFFICIENCY_CODEX_EXEC = frozenset({"exec", "exec_command", "js", "run", "shell", "write_stdin"})
EFFICIENCY_CODEX_WAIT_TARGET = {"wait": "cell", "wait_agent": "agent", "sleep": "sleep"}
EFFICIENCY_CLAUDE_TOOL = {
    "TaskOutput": "wait",
    "BashOutput": "wait",
    "Monitor": "wait",
    "ScheduleWakeup": "wait",
    "Sleep": "wait",
    "TaskList": "status",
    "TaskGet": "status",
    "ListAgents": "status",
    "Agent": "orchestrate",
    "Task": "orchestrate",
    "SendMessage": "orchestrate",
    "TaskStop": "orchestrate",
    "KillShell": "orchestrate",
    "Workflow": "orchestrate",
    "TeamCreate": "orchestrate",
    "AskUserQuestion": "human",
}
EFFICIENCY_CLAUDE_ERRORS = {
    "rate_limit": "usage_limit",
    "server_error": "upstream",
    "overloaded": "upstream",
    "api_error": "upstream",
}
EFFICIENCY_CODEX_HEADER = re.compile(
    rb'^\{"timestamp":"([^"]*)",(?:"ordinal":\d+,)?"type":"([a-z_]+)"(?:,"payload":\{"type":"([a-z_]+)")?'
)
EFFICIENCY_CODEX_PARSE_TOP = frozenset({b"session_meta", b"turn_context", b"token_usage_record"})
EFFICIENCY_CODEX_PARSE_PAYLOAD = {
    b"response_item": frozenset(
        {
            b"function_call",
            b"custom_tool_call",
            b"function_call_output",
            b"custom_tool_call_output",
            b"agent_message",
        }
    ),
    b"event_msg": frozenset(
        {
            b"task_started",
            b"task_complete",
            b"turn_aborted",
            b"user_message",
            b"thread_settings_applied",
            b"token_count",
        }
    ),
}
EFFICIENCY_CLAUDE_MARKERS = (
    b'"type":"user"',
    b'"type":"assistant"',
    b"compact_boundary",
    b"isCompactSummary",
    b'"turn_duration"',
)

# ---------------------------------------------------------------------------
# Loop attribution (v1.4). The `loops` section reads loop membership from the agent-history
# ParadeDB catalogue (read-only, role ah_reader) and the efficiency parser labels each counted
# event with the loop its thread belonged to at that moment:
# - a loop root is attributed per event time: [launch_ts, end) of each ah.loop_run it roots,
#   where end is the next launch or the report write; a loop that has neither and whose root was
#   live when the map was fetched stays open;
# - a lane (ah.session.loop_link_method lineage|heuristic) carries its loop for its whole life;
# - a thread unknown to the catalogue inherits its parent thread's attribution, so a new lane is
#   attributed before the catalogue's five-minute refresh has seen it;
# - everything else, and everything while no loop map is available, is `none`.
# The value is "<repo_slug>/<naming><loop_number>" (e.g. "agentic-journal/loop16"), falling back to
# campaign_slug then "unknown" for the slug and to the UTC launch minute when the number is unknown.
# Catalogue surrogate ids are never used: they are not stable across a catalogue rebuild.
OPTIONAL_SECTIONS = frozenset({"loops"})
LOOP_NONE = "none"
LOOP_OTHER = "other"
LOOP_DSN_FILE = ""
LOOP_LOOKBACK_DAYS = 7  # loops that ended (or launched) this recently are mapped
LOOP_OPEN_GRACE_SECONDS = 1800  # catalogue refresh lag tolerated before a live root's loop closes
LOOP_MAP_MAX_AGE_SECONDS = 6 * 3600  # a cached map is used this long while the catalogue is down
EFFICIENCY_LOOP_RETAIN_SECONDS = 2 * 86400  # a loop's series leave the textfile this long after its last event
EFFICIENCY_MAX_LOOPS = 40  # concurrently emitted loop labels; beyond this new loops read `other`
LOOP_SLUG_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")
LOOP_ROOTS_SQL = (
    "SELECT rs.agent, rs.session_uid, rs.agent_id, r.repo_slug, r.campaign_slug, r.naming, r.loop_number, "
    "extract(epoch FROM r.launch_ts)::float8, extract(epoch FROM r.end_ts)::float8, r.end_evidence "
    "FROM ah.loop_run r JOIN ah.session rs ON rs.id = r.root_session_id "
    f"WHERE r.launch_ts IS NOT NULL AND COALESCE(r.end_ts, r.launch_ts) >= now() - interval '{LOOP_LOOKBACK_DAYS} days'"
)
LOOP_MEMBERS_SQL = (
    "SELECT s.agent, s.session_uid, s.agent_id, r.repo_slug, r.campaign_slug, r.naming, r.loop_number, "
    "extract(epoch FROM r.launch_ts)::float8 "
    "FROM ah.session s JOIN ah.loop_run r ON r.id = s.loop_run_id "
    "WHERE s.loop_link_method IN ('lineage', 'heuristic') AND r.launch_ts IS NOT NULL "
    f"AND COALESCE(r.end_ts, r.launch_ts) >= now() - interval '{LOOP_LOOKBACK_DAYS} days'"
)


def loop_label_value(repo: Any, campaign: Any, naming: Any, number: Any, launch: float) -> str:
    """Stable, bounded label value for one loop run."""
    safe = (
        LOOP_SLUG_UNSAFE.sub("-", candidate).strip("-.")[:48]
        for candidate in (repo, campaign)
        if isinstance(candidate, str) and candidate
    )
    slug = next((value for value in safe if value), "unknown")
    naming = naming if naming in ("loop", "wave") else "loop"
    if isinstance(number, int) and not isinstance(number, bool) and 0 <= number < 1_000_000:
        suffix = f"{naming}{number}"
    else:
        suffix = f"{naming}-" + datetime.fromtimestamp(float(launch), tz=timezone.utc).strftime("%Y%m%dT%H%M")
    return f"{slug}/{suffix}"


def loop_session_key(agent: Any, session_uid: Any, agent_id: Any) -> str:
    return f"{agent}\t{session_uid}\t{agent_id or ''}"


def build_loop_map(roots: Iterable[Iterable[Any]], members: Iterable[Iterable[Any]], fetched: float) -> dict[str, Any]:
    """JSON-serialisable loop map: root windows and lane membership keyed by catalogue natural key."""
    windows: dict[str, list[list[Any]]] = defaultdict(list)
    for agent, uid, agent_id, repo, campaign, naming, number, launch, end, evidence in roots:
        if launch is None:
            continue
        label = loop_label_value(repo, campaign, naming, number, launch)
        if end is None or (evidence == "root_last_event" and float(end) >= fetched - LOOP_OPEN_GRACE_SECONDS):
            stop = None  # not concluded and the root was live when fetched: still running
        elif evidence == "root_last_event":
            stop = float(end) + LOOP_OPEN_GRACE_SECONDS
        else:
            stop = float(end)
        windows[loop_session_key(agent, uid, agent_id)].append([float(launch), stop, label])
    for spans in windows.values():
        spans.sort(key=lambda span: span[0])
    lanes = {
        loop_session_key(agent, uid, agent_id): loop_label_value(repo, campaign, naming, number, launch)
        for agent, uid, agent_id, repo, campaign, naming, number, launch in members
        if launch is not None
    }
    return {"fetched": fetched, "roots": dict(windows), "members": lanes}


def fetch_loop_map(dsn_file: Path, now: float) -> dict[str, Any]:
    """Read loop membership from the catalogue with the read-only reader role. Raises on any failure."""
    dsn = dsn_file.read_text(encoding="utf-8").strip()
    if not dsn:
        raise ValueError("loop catalogue DSN file is empty")
    import psycopg  # noqa: PLC0415 - only this optional section needs the driver

    with psycopg.connect(
        dsn,
        connect_timeout=5,
        autocommit=True,
        application_name="collect-agent-session-metrics",
        options="-c statement_timeout=10000 -c default_transaction_read_only=on",
    ) as connection:
        roots = connection.execute(LOOP_ROOTS_SQL).fetchall()
        members = connection.execute(LOOP_MEMBERS_SQL).fetchall()
    return build_loop_map(roots, members, now)


class LoopMap:
    """Resolve a thread's loop label at an event time."""

    def __init__(self, data: dict[str, Any] | None) -> None:
        data = data if isinstance(data, dict) else {}
        self.roots: dict[str, list[list[Any]]] = data.get("roots") if isinstance(data.get("roots"), dict) else {}
        self.members: dict[str, str] = data.get("members") if isinstance(data.get("members"), dict) else {}

    def label(self, key: str | None, parent: str | None, ts: float | None) -> str:
        for candidate in (key, parent):
            if not candidate:
                continue
            member = self.members.get(candidate)
            if isinstance(member, str):
                return member
            spans = self.roots.get(candidate)
            if spans:
                if ts is None:
                    return LOOP_NONE
                for start, stop, label in reversed(spans):
                    if ts >= start:
                        return label if stop is None or ts < stop else LOOP_NONE
                return LOOP_NONE
        return LOOP_NONE


def efficiency_session_keys(relative: str, file_state: dict[str, Any]) -> tuple[str | None, str | None]:
    """Catalogue natural key of a transcript file, and of its parent thread when it is a worker."""
    if relative.split("/", 1)[0].startswith("claude-"):
        name = relative.rsplit("/", 1)[-1]
        stem = name[:-6] if name.endswith(".jsonl") else name
        if "/subagents/" in relative:
            session = relative.rsplit("/subagents/", 1)[0].rsplit("/", 1)[-1]
            agent_id = stem[6:] if stem.startswith("agent-") else stem
            return loop_session_key("claude", session, agent_id), loop_session_key("claude", session, "")
        return loop_session_key("claude", stem, ""), None
    if relative.startswith("pi-"):
        info = pi_format().lineage(relative)
        thread = file_state.get("tid")
        root = info.get("root") if info.get("depth", 0) else None
        return (
            loop_session_key("pi", thread, "") if thread else None,
            loop_session_key("pi", root, "") if root else None,
        )
    thread, parent = file_state.get("tid"), file_state.get("parent")
    return (
        loop_session_key("codex", thread, "") if thread else None,
        loop_session_key("codex", parent, "") if parent else None,
    )


def efficiency_cls_exec(text: str) -> str:
    """Port of the audit extractor's cls_exec: classify a shell or code-mode call."""
    if not text:
        return "noop"
    if "tools." not in text and "$" not in text and len(text) < 80:
        return "noop"
    if EFFICIENCY_CLOCK_ONLY.fullmatch(text):
        return "status"
    if EFFICIENCY_WAIT_RE.search(text):
        return "wait"
    commands = EFFICIENCY_EXTRACT_CMD.findall(text) or [text]
    if all(EFFICIENCY_STATUS_RE.search(command) for command in commands):
        return "status"
    return "work"


def efficiency_poll_target(command: str) -> str:
    """Map a command to a poll target with the audit's waitwhat.cls() precedence."""
    lowered = command.lower()
    for target, pattern in EFFICIENCY_POLL_TARGETS:
        if pattern.search(lowered):
            return target
    return "other"


def efficiency_event_ts(value: Any) -> float | None:
    if isinstance(value, bytes):
        value = value.decode("ascii", "replace")
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def efficiency_codex_error_kind(error: Any) -> str:
    info: Any = error.get("codex_error_info") if isinstance(error, dict) else None
    if isinstance(info, dict):
        info = next(iter(info), None)
    message = str(error.get("message") or "") if isinstance(error, dict) else str(error)
    lowered = message.lower()
    if info == "usage_limit_exceeded" or "usage limit" in lowered:
        return "usage_limit"
    if info == "cyber_policy" or "flagged" in lowered:
        return "flagged"
    if info in {
        "server_overloaded",
        "internal_server_error",
        "http_connection_failed",
        "response_stream_disconnected",
        "response_stream_connection_failed",
        "response_too_many_failed_attempts",
    } or lowered.startswith(("unexpected status", "stream disconnected", "stream error", '{"type":"error"')):
        return "upstream"
    return "other"


def efficiency_json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return {}
    try:
        parsed = json.loads(value)
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def efficiency_js_ms(tool: str, text: str) -> int | None:
    match = EFFICIENCY_JS_WAIT_MS[tool].search(text)
    return int(match.group(1)) if match else None


def efficiency_output_text(output: Any) -> str:
    if isinstance(output, list):
        return " ".join(item["text"] for item in output if isinstance(item, dict) and isinstance(item.get("text"), str))
    return output if isinstance(output, str) else ""


def efficiency_codex_result(rule: str, text: str) -> str:
    """event when a Codex wait delivered something; timed_out when it only elapsed."""
    if rule == "agent":
        return "event" if EFFICIENCY_NOT_TIMED_OUT.search(text) else "timed_out"
    # Only an explicit exit marker is an event; marker-less output is common when a script
    # prints only the chunk text and must not hide a still-running poll.
    return "event" if EFFICIENCY_EXITED.search(text) else "timed_out"


def efficiency_claude_result(rule: str, tool_result: Any) -> str:
    """event when a Claude wait delivered something; timed_out when it only elapsed."""
    if rule == "event":
        return "event"
    if not isinstance(tool_result, dict):
        return "timed_out"
    if rule == "task":
        task = tool_result.get("task") if isinstance(tool_result.get("task"), dict) else tool_result
        status = task.get("status")
        return "event" if isinstance(status, str) and status not in ("running", "pending") else "timed_out"
    if tool_result.get("interrupted") or tool_result.get("timedOutAfterMs") or tool_result.get("backgroundTaskId"):
        return "timed_out"
    return "event"


# -- v1.2 command and result classification (in-process only; nothing here is persisted) --------


def efficiency_unescape(value: str) -> str:
    """Undo JSON/JS string escaping of a command captured from call arguments or source."""
    try:
        decoded = json.loads(f'"{value}"')
        return decoded if isinstance(decoded, str) else value
    except ValueError:
        return value.replace("\\n", "\n").replace('\\"', '"')


def efficiency_shell_segments(command: str) -> list[list[str]]:
    """Split a shell command into simple commands (word lists).

    Heredoc bodies and comments are dropped, quotes are honoured, and `;`, `&`, `|`, newlines,
    parentheses and backticks separate commands, so only a command's first word says what runs.
    """
    lines = command.split("\n")
    kept: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        kept.append(line)
        index += 1
        for _, delimiter in EFFICIENCY_HEREDOC.findall(line):
            while index < len(lines) and lines[index].strip() != delimiter:
                index += 1
            index += 1
    text = "\n".join(kept)
    segments: list[list[str]] = []
    words: list[str] = []
    word: list[str] = []
    has_word = False
    quote = ""
    position = 0
    while position < len(text):
        char = text[position]
        if quote:
            if char == quote:
                quote = ""
            elif char == "\\" and quote == '"' and position + 1 < len(text):
                position += 1
                word.append(text[position])
            else:
                word.append(char)
        elif char in "'\"":
            quote = char
            has_word = True
        elif char == "\\" and position + 1 < len(text):
            position += 1
            if text[position] != "\n":
                word.append(text[position])
                has_word = True
        elif char == "#" and not has_word:
            newline = text.find("\n", position)
            if newline < 0:
                break
            position = newline
            continue
        elif char in " \t\r" or char in ";&|\n()`":
            if has_word:
                words.append("".join(word))
                word, has_word = [], False
            if char not in " \t\r" and words:
                segments.append(words)
                words = []
        else:
            word.append(char)
            has_word = True
        position += 1
    if has_word:
        words.append("".join(word))
    if words:
        segments.append(words)
    return segments


def efficiency_command_kind(words: list[str]) -> set[str]:
    """Delivery kinds of one simple command: push, ci, gate and/or cr (coderabbit review --agent)."""
    index = 0
    while index < len(words):
        word = words[index]
        if EFFICIENCY_ASSIGNMENT.match(word) or word in EFFICIENCY_WRAPPERS:
            index += 1
        elif word == "timeout":
            index += 1
            while index < len(words) and words[index].startswith("-"):
                index += 1
            index += 1
        else:
            break
    words = words[index:]
    if not words:
        return set()
    program = words[0].rsplit("/", 1)[-1]
    rest = words[1:]

    def positional(takes_value: frozenset[str]) -> list[str]:
        found: list[str] = []
        skip = False
        for item in rest:
            if skip:
                skip = False
            elif item in takes_value:
                skip = True
            elif not item.startswith("-"):
                found.append(item)
        return found

    if program == "git":
        args = positional(frozenset({"-C", "-c", "--git-dir", "--work-tree", "--namespace"}))
        return {"push"} if args[:1] == ["push"] else set()
    if program == "gh":
        args = positional(frozenset({"-R", "--repo"}))
        if (args[:2] == ["run", "watch"] and "--exit-status" in rest) or (
            args[:2] == ["pr", "checks"] and "--watch" in rest
        ):
            return {"ci"}
        return set()
    if program == "just":
        args = positional(
            frozenset({"-f", "--justfile", "-d", "--working-directory", "--dotenv-path", "--dotenv-filename"})
        )
        return {"gate"} if args[:1] and args[0] in ("check", "test", "ci") else set()
    if program == "make":
        args = positional(frozenset({"-C", "-f", "--directory", "--file", "--makefile", "-j", "-l", "-o", "-W", "-I"}))
        return {"gate"} if {"check", "test"} & {arg for arg in args if "=" not in arg} else set()
    if program == "coderabbit":
        args = positional(frozenset())
        # A review whose stdout goes to a file cannot be judged from the tool output; leave it uncounted.
        redirected = any(item.startswith((">", "1>", "&>")) for item in rest)
        return {"cr"} if args[:1] == ["review"] and "--agent" in rest and not redirected else set()
    return set()


def efficiency_delivery_kinds(command: str) -> str:
    """'+'-joined delivery kinds for a shell command ('' when it has none)."""
    if not EFFICIENCY_DELIVERY_HINT.search(command):
        return ""
    kinds: set[str] = set()
    for words in efficiency_shell_segments(command):
        kinds |= efficiency_command_kind(words)
    return "+".join(sorted(kinds))


def efficiency_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def efficiency_chunk_starts(text: str) -> list[int]:
    """Offsets of exec results: a line starting `Chunk ID:` or a JSON object starting `{"chunk_id"`."""
    starts: list[int] = []
    for marker, before in (("Chunk ID:", "\n"), ('"chunk_id"', "{")):
        position = text.find(marker)
        while position >= 0:
            if (position == 0 and marker == "Chunk ID:") or (position > 0 and text[position - 1] == before):
                starts.append(position)
            position = text.find(marker, position + 1)
    return sorted(starts)


def efficiency_chunks(text: str) -> list[tuple[int | None, str | None, str]]:
    """(exit code, still-running process id, body) for each exec result in a Codex tool output.

    Exit and process markers are read only from a result's header, never from the program output.
    """
    starts = efficiency_chunk_starts(text)
    if not starts:
        stripped = text.lstrip()
        if stripped.startswith("{") and len(stripped) <= EFFICIENCY_MAX_JSON_RESULT:
            parsed = efficiency_json_object(stripped)
            if parsed:
                metadata = parsed.get("metadata") if isinstance(parsed.get("metadata"), dict) else {}
                code = efficiency_int(parsed.get("exit_code"))
                code = code if code is not None else efficiency_int(metadata.get("exit_code"))
                process = efficiency_int(parsed.get("session_id"))
                body = parsed.get("output") if isinstance(parsed.get("output"), str) else ""
                return [(code, str(process) if process is not None and code is None else None, body)]
        exit_match = EFFICIENCY_PLAIN_EXIT.search(text) if "Process exited" in text else None
        running = EFFICIENCY_PLAIN_RUNNING.search(text) if "Process running" in text else None
        code = int(exit_match.group(1)) if exit_match else None
        return [(code, running.group(1) if running and code is None else None, text)]
    chunks: list[tuple[int | None, str | None, str]] = []
    for index, start in enumerate(starts):
        part = text[start : starts[index + 1] if index + 1 < len(starts) else len(text)]
        end = EFFICIENCY_HEADER_END.search(part)
        header = part[: end.start()] if end else part
        exit_match = EFFICIENCY_EXIT_CODE.search(header)
        code = int(exit_match.group(1) or exit_match.group(2)) if exit_match else None
        process_match = EFFICIENCY_PROCESS_ID.search(header)
        process = (process_match.group(1) or process_match.group(2)) if process_match and code is None else None
        chunks.append((code, process, part[end.start() :] if end else ""))
    return chunks


def efficiency_coderabbit_scan(body: str) -> tuple[list[str], bool, bool]:
    """(finding severities, complete line seen, rate limit seen) in `coderabbit review --agent` output."""
    text = body
    for _ in range(3):  # code-mode output nests the JSON lines inside an escaped JSON string
        if '\\"type\\"' not in text:
            break
        text = text.replace('\\"', '"').replace("\\\\", "\\")
    severities: list[str] = []
    complete = rate_limited = False
    starts = [(match.start(), match.group(1)) for match in EFFICIENCY_CR_RECORD.finditer(text)]
    for index, (start, kind) in enumerate(starts):
        record = text[start : starts[index + 1][0] if index + 1 < len(starts) else len(text)]
        if kind == "finding":
            severity = EFFICIENCY_CR_SEVERITY.search(record)
            if severity and severity.group(1) in EFFICIENCY_SEVERITIES:
                severities.append(severity.group(1))
        elif kind == "complete":
            complete = True
        elif kind == "error" and EFFICIENCY_RATE_LIMIT.search(record):
            rate_limited = True
    if not rate_limited:
        rate_limited = any(
            EFFICIENCY_RATE_LIMIT.search(line)
            for line in text.splitlines()
            if line.strip() and not line.lstrip().startswith("{")
        )
    return severities, complete, rate_limited


def efficiency_spawn_error(text: str) -> str | None:
    """None for a successful Codex spawn_agent result, else the spawn error kind."""
    parsed = efficiency_json_object(text.strip())
    if any(key in parsed for key in EFFICIENCY_SPAWN_OK_KEYS):
        return None
    for kind, pattern in EFFICIENCY_SPAWN_ERRORS:
        if pattern.search(text):
            return kind
    return "other"


def efficiency_codex_human_item(content: Any) -> bool:
    """Whether a Codex role=user message carries human input rather than only injected context."""
    if not isinstance(content, list):
        return False
    for item in content:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "input_image":
            return True
        text = item.get("text")
        if item.get("type") != "input_text" or not isinstance(text, str) or not text.strip():
            continue
        text = text.lstrip()
        tag = EFFICIENCY_LEADING_TAG.match(text)
        if tag:
            if tag.group(1) in EFFICIENCY_CODEX_HUMAN_TAGS:
                return True
            continue
        if not text.startswith(EFFICIENCY_CODEX_INJECTED_PREFIXES):
            return True
    return False


def efficiency_protocol(text: str) -> str:
    """The loop protocol a first human prompt declares: v2.1, v2.0, other or none."""
    match = EFFICIENCY_PROTOCOL_RE.search(text)
    if not match:
        return "none"
    version = match.group(1)
    return "v2.1" if version == "2.1" else "v2.0" if version in ("2", "2.0") else "other"


def efficiency_codex_human_text(content: Any) -> str:
    """The human (non-injected) input_text items of a Codex role=user message, joined."""
    texts: list[str] = []
    for item in content if isinstance(content, list) else ():
        text = item.get("text") if isinstance(item, dict) and item.get("type") == "input_text" else None
        if not isinstance(text, str) or not text.strip():
            continue
        stripped = text.lstrip()
        tag = EFFICIENCY_LEADING_TAG.match(stripped)
        if (tag and tag.group(1) in EFFICIENCY_CODEX_HUMAN_TAGS) or (
            not tag and not stripped.startswith(EFFICIENCY_CODEX_INJECTED_PREFIXES)
        ):
            texts.append(text)
    return "\n".join(texts)


def efficiency_js_object(text: str, start: int) -> dict[str, str | None] | None:
    """Top-level entries of a JS object literal starting at or after `start`.

    A string-literal value is returned as its content; any other value (a variable, shorthand, a
    template with substitutions) is None, and a spread adds the key "...". None when unbalanced.
    """
    opening = text.find("{", start, start + 8)
    if opening < 0:
        return None
    entries: list[str] = []
    current: list[str] = []
    depth = 0
    position = opening
    while position < len(text):
        char = text[position]
        if char in "\"'`":
            end = position + 1
            while end < len(text) and text[end] != char:
                end += 2 if text[end] == "\\" else 1
            if end >= len(text):
                return None
            if depth == 1:
                current.append(text[position : end + 1])
            position = end + 1
            continue
        if char in "{[(":
            depth += 1
            if depth == 1:
                position += 1
                continue
        elif char in "}])":
            depth -= 1
            if depth == 0:
                entries.append("".join(current))
                break
        elif char == "," and depth == 1:
            entries.append("".join(current))
            current = []
            position += 1
            continue
        if depth >= 1:
            current.append(char)
        position += 1
    else:
        return None
    found: dict[str, str | None] = {}
    for entry in entries:
        entry = entry.strip()
        if not entry:
            continue
        if entry.startswith("..."):
            found["..."] = None
            continue
        match = re.fullmatch(r"[\"']?([A-Za-z_$][\w$]*)[\"']?\s*(?::\s*(.*))?", entry, re.S)
        if not match:
            continue
        value = match.group(2)
        literal = None
        if value is not None:
            value = value.strip()
            if (
                len(value) >= 2
                and value[0] == value[-1]
                and value[0] in "\"'`"
                and not (value[0] == "`" and "${" in value)
            ):
                literal = efficiency_unescape(value[1:-1]) if value[0] == '"' else value[1:-1]
        found[match.group(1)] = literal
    return None if "..." in found else found


def efficiency_cell_spawn_result(text: str) -> str | None:
    """'ok' when a code-mode cell's spawn visibly succeeded, a spawn error kind, or None when unknown."""
    for kind, pattern in EFFICIENCY_SPAWN_ERRORS:
        if pattern.search(text):
            return kind
    if text.startswith("Script completed") and EFFICIENCY_SPAWN_OK_TEXT.search(text):
        return "ok"
    return None


def efficiency_block_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            item["text"] for item in content if isinstance(item, dict) and isinstance(item.get("text"), str)
        )
    return ""


def efficiency_claude_human(record: dict[str, Any], text: str) -> bool:
    """Whether a Claude user record without tool results is a message typed by the human."""
    origin = record.get("origin")
    if isinstance(origin, dict) and origin.get("kind"):
        return origin.get("kind") == "human" and not text.startswith("[Request interrupted")
    if record.get("isMeta") or record.get("isCompactSummary") or not text.strip():
        return False
    return not text.lstrip().startswith(EFFICIENCY_CLAUDE_NOT_HUMAN)


EFFICIENCY_FILE_UPGRADE: dict[str, Any] = {
    # v1.2 per-file fields. -1 marks a thread whose start predates v1.2 state, so no lane or
    # first-spawn duration is observed for it.
    "t0": -1,
    "lane": -1,
    "tch": None,
    "turn": 0,
    "ctxw": None,
    "dprocs": {},
    "hum": False,
    "hsrc": None,
    # v1.3: "?" = protocol not yet recovered from the already-parsed first prompt.
    "proto": "?",
    "spawn_ts": None,
    "pi_spawns": [],
    "pi_launch_ts": None,
}


def efficiency_file_state(relative: str, skip: float) -> dict[str, Any]:
    namespace = relative.split("/", 1)[0]
    worker = namespace.startswith("claude-") and "/subagents/" in relative
    if namespace.startswith("pi-"):
        worker = bool(pi_format().lineage(relative).get("depth", 0))
    return {
        "size": 0,
        "mtime_ns": 0,
        "offset": 0,
        "head": None,
        "skip": skip,
        "counted": 0.0,
        "last": None,
        "in_task": True,
        "pending": [],
        "trig": "user",
        "role": "worker" if worker else "solo",
        "model": None,
        "last_call": None,
        "meta": False,
        "tid": None,
        "parent": None,
        "procs": {},
        "recs": False,
        "tok_total": None,
        "mids": [],
        "agents": [],
        "cb": False,
        "first_user": False,
        "t0": None,
        "lane": None,
        "tch": None,
        "turn": 0,
        "ctxw": None,
        "dprocs": {},
        "hum": False,
        "hsrc": None,
        "proto": None,
        "spawn_ts": None,
        "pi_spawns": [],
        "pi_launch_ts": None,
    }


def efficiency_upgrade_file(file_state: dict[str, Any]) -> None:
    """Give a v1.1 per-file state the v1.2 fields without touching its offsets or counters."""
    for key, default in EFFICIENCY_FILE_UPGRADE.items():
        if key not in file_state:
            file_state[key] = default.copy() if isinstance(default, dict) else default


class EfficiencyRun:
    """Persisted counter totals plus a per-file delta that commits or rolls back atomically."""

    def __init__(self, state: dict[str, Any], loops: LoopMap | None = None) -> None:
        self.state = state
        self.baseline = float(state["baseline_ts"])
        self.loops = loops or LoopMap(None)
        self.delta: dict[tuple[str, str], float] = defaultdict(float)
        self.recent: list[list[Any]] = []
        self.limits: dict[tuple[str, str], list[Any]] = {}
        self.loop_seen: dict[str, float] = {}

    def loop_label(self, relative: str, file_state: dict[str, Any], ts: float | None, *, record: bool = True) -> str:
        """The bounded loop label of one thread at `ts`; counting records the label's latest event time."""
        key, parent = efficiency_session_keys(relative, file_state)
        label = self.loops.label(key, parent, ts)
        if label == LOOP_NONE:
            return label
        known: dict[str, float] = self.state["loops"]
        if (
            label not in known
            and label not in self.loop_seen
            and len(set(known) | set(self.loop_seen)) >= EFFICIENCY_MAX_LOOPS
        ):
            label = LOOP_OTHER
        if not record:
            return label
        if ts is not None:
            self.loop_seen[label] = max(ts, self.loop_seen.get(label, ts), float(known.get(label, 0)))
        elif label not in known:
            self.loop_seen.setdefault(label, self.baseline)
        return label

    def add(self, metric: str, values: tuple[str, ...], amount: float) -> None:
        self.delta[(metric, "\t".join(values))] += amount

    def observe(self, family: str, values: tuple[str, ...], amount: float) -> None:
        """Add one histogram observation as cumulative bucket, sum and count deltas."""
        key = "\t".join(values)
        for bound in EFFICIENCY_HISTOGRAMS[family][1]:
            if amount <= bound:
                self.delta[(family + "_bucket", f"{key}\t{bound}")] += 1
        self.delta[(family + "_bucket", key + "\t+Inf")] += 1
        self.delta[(family + "_sum", key)] += amount
        self.delta[(family + "_count", key)] += 1

    def model_label(self, model: str | None) -> str:
        model = model or "unknown"
        models: list[str] = self.state["models"]
        if model in models:
            return model
        if len(models) < EFFICIENCY_MAX_MODELS:
            models.append(model)
            return model
        return "other"

    def agent_type_label(self, agent_type: str) -> str:
        known: list[str] = self.state["agent_types"]
        if agent_type in known:
            return agent_type
        if len(known) < EFFICIENCY_MAX_AGENT_TYPES:
            known.append(agent_type)
            return agent_type
        return "other"

    def rate_limit(self, namespace: str, window: str, ts: float, values: list[Any]) -> None:
        """Keep the newest rate-limit reading per namespace and window."""
        key = (namespace, window)
        current = self.limits.get(key) or self.state["rate_limits"].get(namespace, {}).get(window)
        if current is None or ts >= current[0]:
            self.limits[key] = [ts, *values]

    def commit(self) -> None:
        totals: dict[str, dict[str, float]] = self.state["totals"]
        for (metric, key), amount in self.delta.items():
            bucket = totals.setdefault(metric, {})
            bucket[key] = bucket.get(key, 0) + amount
        self.state["recent_calls"].extend(self.recent)
        for (namespace, window), values in self.limits.items():
            self.state["rate_limits"].setdefault(namespace, {})[window] = values
        for label, ts in self.loop_seen.items():
            self.state["loops"][label] = max(ts, float(self.state["loops"].get(label, 0)))
        self.rollback()

    def rollback(self) -> None:
        self.delta = defaultdict(float)
        self.recent = []
        self.limits = {}
        self.loop_seen = {}


class EfficiencyParser:
    """Line-at-a-time port of the audit extractor's Acc, codex() and claude() logic."""

    def __init__(self, run: EfficiencyRun, file_state: dict[str, Any], relative: str) -> None:
        self.run = run
        self.s = file_state
        self.relative = relative
        self.namespace = relative.split("/", 1)[0]
        self.agent = self.namespace.split("-", 1)[0]
        self.line = (
            self.codex_line if self.agent == "codex" else self.pi_line if self.agent == "pi" else self.claude_line
        )
        self._loop: tuple[Any, str] | None = None

    # -- counting primitives ------------------------------------------------
    def countable(self, ts: float | None) -> bool:
        if ts is None or ts < self.run.baseline or ts <= self.s["skip"]:
            return False
        if ts > self.s["counted"]:
            self.s["counted"] = ts
        return True

    def loop(self) -> str:
        """Loop label of this thread at its latest event (every count happens at or after tick)."""
        at = (self.s["last"], self.s.get("tid"), self.s.get("parent"))
        if self._loop is None or self._loop[0] != at:
            self._loop = (at, self.run.loop_label(self.relative, self.s, self.s["last"]))
        return self._loop[1]

    def add(self, metric: str, amount: float, *values: str) -> None:
        self.run.add(metric, (self.agent, self.namespace, *values, self.loop()), amount)

    def observe(self, family: str, amount: float) -> None:
        self.run.observe(family, (self.agent, self.namespace, self.loop()), amount)

    def lane_close(self, first: float, last: float) -> None:
        """Observe one worker lane segment once, if it is wholly countable."""
        if last >= first >= self.run.baseline and self.countable(last):
            self.observe("agent_efficiency_lane_seconds", last - first)

    def tick(self, ts: float) -> None:
        s = self.s
        last = s["last"]
        if s["t0"] is None:
            s["t0"] = ts
        if s["role"] == "worker":
            # A worker lane is its span of events; a quiet gap over 30 minutes closes it.
            if s["lane"] is None:
                s["lane"] = ts
            elif last is not None and ts - last > EFFICIENCY_GAP_SECONDS:
                if s["lane"] >= 0:
                    self.lane_close(s["lane"], last)
                s["lane"] = ts
        if last is not None and ts >= last:
            if not s["in_task"]:
                bucket = "idle"
            elif s["pending"]:
                bucket = "tool_" + s["pending"][-1][1]
            else:
                bucket = "model"
            if bucket == "model" and ts - last > EFFICIENCY_GAP_SECONDS:
                bucket = "gap"
            floor = max(self.run.baseline, s["skip"])
            seconds = ts - max(last, floor)
            if seconds > 0 and self.countable(ts):
                self.add("agent_efficiency_time_seconds_total", seconds, s["role"], bucket)
                if s["role"] != "worker":
                    self.add("agent_efficiency_root_time_seconds_by_protocol_total", seconds, self.protocol(), bucket)
                if bucket == "model":
                    self.add(
                        "agent_efficiency_model_seconds_total", seconds, s["role"], self.run.model_label(s["model"])
                    )
        if last is None or ts > last:
            s["last"] = ts

    def context_window(self) -> int | None:
        """Codex uses its recorded window; pi configuration and Claude owner policy are explicit."""
        if self.agent == "codex":
            return efficiency_int(self.s["ctxw"])
        if self.agent == "pi":
            model = self.s["model"]
            return PI_CONTEXT_WINDOWS.get(model) if isinstance(model, str) else None
        if self.agent == "claude":
            return claude_context_window(self.s["model"])
        return None

    def llm_call(self, ts: float | None, total: int, hit: int, output: int) -> None:
        s = self.s
        s["hsrc"] = None
        if ts is not None and (s["last_call"] is None or ts > s["last_call"]):
            s["last_call"] = ts
        self.count_call(ts, s["role"], s["trig"], s["model"], total, hit, output, self.context_window())
        s["trig"] = "model"

    def count_call(
        self,
        ts: float | None,
        role: str,
        trigger: str,
        model: str | None,
        total: int,
        hit: int,
        output: int,
        window: int | None,
    ) -> None:
        if self.countable(ts):
            self.add("agent_efficiency_llm_calls_total", 1, role, trigger)
            if role != "worker":
                # poll follows poll_calls_total: a call reacting to a wait that timed out (event wakes are not polls).
                poll = "true" if trigger == "wait" else "false"
                self.add("agent_efficiency_root_llm_calls_by_protocol_total", 1, self.protocol(), poll)
            self.add("agent_efficiency_input_tokens_total", hit, role, trigger, "hit")
            self.add("agent_efficiency_input_tokens_total", max(0, total - hit), role, trigger, "miss")
            self.add("agent_efficiency_output_tokens_total", output, role)
            self.add("agent_efficiency_model_calls_total", 1, role, self.run.model_label(model))
            self.run.recent.append([ts, self.namespace, role, total, window, self.loop()])

    def spawn(self, ts: float | None) -> None:
        """Count a spawn request; its route is counted by spawn_route once the result shows success."""
        first = self.s["role"] == "solo"
        if first:
            self.s["role"] = "root"
        if ts is not None and (self.s["spawn_ts"] is None or ts > self.s["spawn_ts"]):
            self.s["spawn_ts"] = ts
        if self.countable(ts):
            self.add("agent_efficiency_spawns_total", 1)
            start = self.s["t0"]
            if first and start is not None and start >= 0 and ts >= start:
                self.observe("agent_efficiency_first_spawn_seconds", ts - start)

    @staticmethod
    def spawn_value(value: Any, absent: str) -> str:
        """A requested model or agent type, unregistered: `absent`, `!other` when unsafe, else the value."""
        if not value:
            return absent
        return value if isinstance(value, str) and EFFICIENCY_SAFE_LABEL.fullmatch(value) else "!other"

    def codex_route(self, arguments: dict[str, Any]) -> list[str]:
        """Pending spawn hint: ["spawn", model, effort, agent_type, fork], registered only on success."""
        effort = arguments.get("reasoning_effort")
        effort = (
            "inherit" if not effort else effort if isinstance(effort, str) and effort in EFFICIENCY_EFFORTS else "other"
        )
        fork = arguments.get("fork_turns")
        fork = "none" if fork in (None, "", "none") else "all" if fork == "all" else "other"
        return [
            "spawn",
            self.spawn_value(arguments.get("model"), "inherit"),
            effort,
            self.spawn_value(arguments.get("agent_type"), "default"),
            fork,
        ]

    def claude_route(self, arguments: dict[str, Any]) -> list[str]:
        """An explicit `model` argument overrides the subagent_type's pinned model."""
        name = arguments.get("subagent_type")
        agent_type = "default"
        pinned = None
        if name:
            name = str(name).removeprefix("agent-workflows:")
            agent_type = name if name in EFFICIENCY_CLAUDE_AGENT_TYPES else "other"
            pinned = EFFICIENCY_CLAUDE_PINNED.get(name)
        model = self.spawn_value(arguments.get("model"), pinned or "inherit")
        return ["spawn", model, "inherit", agent_type, "none"]

    def spawn_route(self, ts: float | None, hint: list[str]) -> None:
        """Count a successful spawn's route, registering its model and Codex agent type now."""
        if len(hint) != 5 or not self.countable(ts):
            return
        _, model, effort, agent_type, fork = hint
        model = model if model == "inherit" else "other" if model == "!other" else self.run.model_label(model)
        if self.agent in ("codex", "pi") and agent_type != "default":
            agent_type = "other" if agent_type == "!other" else self.run.agent_type_label(agent_type)
        self.add("agent_efficiency_spawns_by_route_total", 1, self.s["role"], model, effort, agent_type, fork)

    def tool_failure(self, ts: float | None, cls: str) -> None:
        if self.countable(ts):
            self.add("agent_efficiency_tool_failures_total", 1, self.s["role"], cls)

    def intervention(self, ts: float | None, kind: str) -> None:
        if self.s["role"] != "worker" and self.countable(ts):
            self.add("agent_efficiency_interventions_total", 1, self.s["role"], kind)

    def protocol(self) -> str:
        proto = self.s["proto"]
        return proto if proto in EFFICIENCY_PROTOCOLS else "none"

    def human_message(self, ts: float | None, text: str = "") -> None:
        """A human message after the thread's first prompt (or after it has called the model) intervenes.

        The first human prompt of a non-worker thread fixes its protocol; nothing later changes it.
        """
        if self.s["proto"] is None and self.s["role"] != "worker":
            self.s["proto"] = efficiency_protocol(text)
        if self.s["hum"] or self.s["last_call"] is not None:
            self.intervention(ts, "user_message")
        self.s["hum"] = True

    def deliver(self, ts: float | None, kinds: str, code: int | None, flags: str) -> None:
        """Count a finished delivery command. flags: c = CodeRabbit complete, r = rate limited, x = cancelled."""
        if not self.countable(ts):
            return
        outcome = "unknown" if code is None else "success" if code == 0 else "failure"
        role = self.s["role"]
        for kind in kinds.split("+"):
            if kind == "push":
                self.add("agent_efficiency_git_pushes_total", 1, role, outcome)
            elif kind == "gate":
                self.add("agent_efficiency_gate_runs_total", 1, role, outcome)
            elif kind == "ci":
                self.add("agent_efficiency_ci_waits_total", 1, role, "cancelled" if "x" in flags else outcome)
            elif kind == "cr" and (code is not None or "c" in flags or "r" in flags):
                # A review whose end is not visible (no exit, no complete or rate-limit line) is not judged.
                review = "complete" if "c" in flags else "rate_limited" if "r" in flags else "failed"
                self.add("agent_efficiency_coderabbit_reviews_total", 1, role, review)

    def delivery_output(self, ts: float | None, kinds: str, body: str, flags: str) -> str:
        """Count CodeRabbit findings in one output chunk and return the accumulated flags."""
        if "cr" in kinds.split("+"):
            severities, complete, rate_limited = efficiency_coderabbit_scan(body)
            if self.countable(ts):
                for severity in severities:
                    self.add("agent_efficiency_coderabbit_findings_total", 1, self.s["role"], severity)
            flags += ("c" if complete else "") + ("r" if rate_limited else "")
        if "ci" in kinds.split("+") and "cancelled" in body.lower():
            flags += "x"
        return "".join(sorted(set(flags)))

    def tool_call(
        self,
        ts: float | None,
        call_id: Any,
        cls: str,
        target: str,
        start_target: str | None = None,
        rule: str = "exec",
        request: tuple[str, Any] | None = None,
        extra: list[str] | None = None,
    ) -> None:
        """Record a tool call. `rule` says how its result is judged; `request` is (tool, requested ms);
        `extra` holds bounded v1.2 result hints (spawn, delivery kinds per command, polled process)."""
        if self.countable(ts):
            self.add("agent_efficiency_tool_calls_total", 1, self.s["role"], cls)
            if cls == "wait" and request is not None:
                tool, milliseconds = request
                if isinstance(milliseconds, (int, float)) and not isinstance(milliseconds, bool) and milliseconds >= 0:
                    self.add("agent_efficiency_wait_requests_total", 1, self.s["role"], tool)
                    self.add("agent_efficiency_wait_timeout_ms_total", milliseconds, self.s["role"], tool)
        self.s["hsrc"] = None
        pending: list[list[Any]] = self.s["pending"]
        pending.append([call_id, cls, ts, target, start_target, rule, extra])
        if len(pending) > EFFICIENCY_MAX_PENDING:
            del pending[0]

    def tool_output(
        self,
        ts: float | None,
        call_id: Any,
        resolve: Callable[[str], str] | None = None,
    ) -> list[Any] | None:
        """Close a pending call. A wait's result (event or timed_out) sets the next call's trigger."""
        pending: list[list[Any]] = self.s["pending"]
        for index, entry in enumerate(pending):
            if entry[0] == call_id:
                del pending[index]
                break
        else:
            return None
        if entry[1] != "wait":
            self.s["trig"] = entry[1]
            return entry
        rule = entry[5] if len(entry) > 5 else "exec"
        result = "timed_out" if rule == "sleep" or resolve is None else resolve(rule)
        self.s["trig"] = "event" if result == "event" else "wait"
        if self.countable(ts):
            self.add("agent_efficiency_poll_calls_total", 1, self.s["role"], entry[3], result)
            if entry[2] is not None:
                seconds = ts - max(entry[2], self.run.baseline, self.s["skip"])
                if seconds > 0:
                    self.add("agent_efficiency_poll_seconds_total", seconds, self.s["role"], entry[3], result)
        return entry

    def compaction(self, ts: float | None) -> None:
        if self.countable(ts):
            self.add("agent_efficiency_compactions_total", 1, self.s["role"])

    def turn_error(self, ts: float | None, kind: str) -> None:
        if self.countable(ts):
            self.add("agent_efficiency_turn_errors_total", 1, kind)

    # -- Codex rollout ------------------------------------------------------
    def codex_line(self, raw: bytes) -> None:
        header = EFFICIENCY_CODEX_HEADER.match(raw)
        if header:
            kind, payload_type = header.group(2), header.group(3)
            wanted = EFFICIENCY_CODEX_PARSE_PAYLOAD.get(kind)
            if kind == b"compacted":
                ts = efficiency_event_ts(header.group(1))
                if ts is not None:
                    self.tick(ts)
                self.compaction(ts if ts is not None else self.s["last"])
                return
            skip = (
                (payload_type is not None and payload_type not in wanted)
                if wanted is not None
                else (kind not in EFFICIENCY_CODEX_PARSE_TOP)
            )
            # A role=user message item; the role follows the type and an id in real rollouts.
            if (
                skip
                and payload_type == b"message"
                and raw.find(b'"role":"user"', header.end(), header.end() + 160) >= 0
            ):
                skip = False
            if skip:
                ts = efficiency_event_ts(header.group(1))
                if ts is not None:
                    self.tick(ts)
                return
        try:
            record = json.loads(raw)
        except ValueError:
            return
        if isinstance(record, dict):
            self.codex_record(record)

    def codex_record(self, record: dict[str, Any]) -> None:
        s = self.s
        kind = record.get("type")
        payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}
        payload_type = payload.get("type")
        ts = efficiency_event_ts(record.get("timestamp"))
        if kind == "session_meta" and not s["meta"]:
            s["meta"] = True
            source = payload.get("source")
            spawn_info: Any = {}
            if isinstance(source, dict) and isinstance(source.get("subagent"), dict):
                spawn_info = source["subagent"].get("thread_spawn") or {}
            if (isinstance(source, dict) and "subagent" in source) or payload.get("thread_source") == "subagent":
                s["role"] = "worker"
                parent = (spawn_info.get("parent_thread_id") if isinstance(spawn_info, dict) else None) or payload.get(
                    "parent_thread_id"
                )
                s["parent"] = str(parent) if parent else None
            thread = payload.get("id") or payload.get("session_id")
            s["tid"] = str(thread) if thread else None
        if ts is not None:
            self.tick(ts)
        event_ts = ts if ts is not None else s["last"]
        if kind == "turn_context" and payload.get("model"):
            s["model"] = str(payload["model"])
        elif kind == "event_msg" and payload_type == "thread_settings_applied":
            settings = payload.get("thread_settings")
            if isinstance(settings, dict) and settings.get("model"):
                s["model"] = str(settings["model"])
        if kind == "compacted":
            self.compaction(event_ts)
        if kind == "event_msg" and payload_type == "task_started":
            self.codex_flush_count()
            s["turn"] = int(s["turn"] or 0) + 1
            window = efficiency_int(payload.get("model_context_window"))
            if window and window > 0:
                s["ctxw"] = window
            s["in_task"] = True
        elif kind == "event_msg" and payload_type in ("task_complete", "turn_aborted"):
            self.codex_flush_count()
            s["in_task"] = False
            s["pending"] = []
            if payload_type == "turn_aborted":
                self.turn_error(event_ts, "aborted")
                if payload.get("reason") == "interrupted":
                    self.intervention(event_ts, "interrupt")
            elif payload.get("error"):
                self.turn_error(event_ts, efficiency_codex_error_kind(payload["error"]))
        elif kind == "event_msg" and payload_type == "user_message":
            # One human message can appear both as this event and as a role=user item; count it once.
            if s["hsrc"] == "item":
                s["hsrc"] = None
            else:
                message = payload.get("message")
                self.human_message(event_ts, message if isinstance(message, str) else "")
                s["hsrc"] = "event"
            s["in_task"] = True
            s["trig"] = "user"
        elif kind == "response_item" and payload_type == "message" and payload.get("role") == "user":
            self.codex_user_item(event_ts, payload.get("content"))
        elif kind == "response_item" and payload_type in ("function_call", "custom_tool_call"):
            self.codex_call(event_ts, payload, payload_type)
        elif kind == "response_item" and payload_type in ("function_call_output", "custom_tool_call_output"):
            text = efficiency_output_text(payload.get("output"))
            entry = self.tool_output(event_ts, payload.get("call_id"), lambda rule: efficiency_codex_result(rule, text))
            if entry is not None and entry[4] is not None:
                self.codex_map_processes(text, entry[4])
            if entry is not None:
                self.codex_result(event_ts, entry, text)
        elif kind == "response_item" and payload_type == "agent_message":
            s["trig"] = "agent_msg"

        usage_record: Any = None
        if kind == "token_usage_record":
            s["recs"] = True
            usage_record = payload.get("usage")
            held = s["tch"]
            if held is not None:
                # A token_count is not a call of its own when its turn has a usage record.
                s["tch"] = None
                if held[7] == s["turn"]:
                    if s["trig"] == "model":
                        s["trig"] = held[2]
                else:
                    self.count_call(*held[:7], held[8])
        elif kind == "event_msg" and payload_type == "token_count":
            limits = payload.get("rate_limits")
            if isinstance(limits, dict) and ts is not None:
                self.codex_rate_limits(ts, limits)
            info = payload.get("info") or {}
            total = (info.get("total_token_usage") or {}).get("total_tokens") if isinstance(info, dict) else None
            if not s["recs"] and total is not None and total != s["tok_total"]:
                s["tok_total"] = total
                self.codex_hold_count(event_ts, info.get("last_token_usage"))
        if isinstance(usage_record, dict) and usage_record:
            self.llm_call(
                event_ts,
                int(usage_record.get("input_tokens") or 0),
                int(usage_record.get("cached_input_tokens") or 0),
                int(usage_record.get("output_tokens") or 0),
            )

    def codex_user_item(self, ts: float | None, content: Any) -> None:
        """A human follow-up recorded as a role=user response item (user threads only)."""
        s = self.s
        if s["role"] == "worker" or not efficiency_codex_human_item(content):
            return
        if s["hsrc"] == "event":
            s["hsrc"] = None
        else:
            self.human_message(ts, efficiency_codex_human_text(content))
            s["hsrc"] = "item"
        s["in_task"] = True
        s["trig"] = "user"

    def codex_hold_count(self, ts: float | None, usage_record: Any) -> None:
        """Hold a token_count call until its turn ends; a usage record in the same turn replaces it."""
        if not isinstance(usage_record, dict) or not usage_record:
            return
        s = self.s
        s["hsrc"] = None
        self.codex_flush_count()
        # [ts, role, trigger, model, input, cached, output, turn, context window]
        s["tch"] = [
            ts,
            s["role"],
            s["trig"],
            s["model"],
            int(usage_record.get("input_tokens") or 0),
            int(usage_record.get("cached_input_tokens") or 0),
            int(usage_record.get("output_tokens") or 0),
            s["turn"],
            self.context_window(),
        ]
        if ts is not None and (s["last_call"] is None or ts > s["last_call"]):
            s["last_call"] = ts
        s["trig"] = "model"

    def codex_flush_count(self) -> None:
        held = self.s["tch"]
        if held is not None:
            self.s["tch"] = None
            self.count_call(*held[:7], held[8])

    def codex_rate_limits(self, ts: float, limits: dict[str, Any]) -> None:
        for window in EFFICIENCY_RATE_WINDOWS:
            reading = limits.get(window)
            if not isinstance(reading, dict):
                continue
            values = [reading.get(key) for key in ("used_percent", "resets_at", "window_minutes")]
            values = [
                value if isinstance(value, (int, float)) and not isinstance(value, bool) else None for value in values
            ]
            if values[0] is not None:
                self.run.rate_limit(self.namespace, window, ts, values)

    def codex_result(self, ts: float | None, entry: list[Any], text: str) -> None:
        """v1.2 accounting for a closed Codex call: failures, spawn errors and delivery outcomes."""
        extra: list[str] | None = entry[6] if len(entry) > 6 else None
        chunks = efficiency_chunks(text)
        failed = text.startswith("Script failed") or any(code for code, _, _ in chunks)
        if extra == ["ap"] and EFFICIENCY_APPLY_PATCH_FAILED.match(text):
            failed = True
        if failed:
            self.tool_failure(ts, entry[1])
        if not extra or extra == ["ap"]:
            return
        if extra[0] == "cell":
            if EFFICIENCY_CELL_NO_SPAWN.search(text):
                return  # the spawn tool is not exposed in code mode: nothing was spawned
            sites = int(extra[1]) if len(extra) > 1 and extra[1].isdigit() else 1
            for _ in range(sites):
                self.spawn(ts)
            if sites == 1:
                kind = efficiency_cell_spawn_result(text)
                if kind == "ok" and len(extra) == 6:
                    self.spawn_route(ts, ["spawn", *extra[2:]])
                elif kind not in (None, "ok") and self.countable(ts):
                    self.add("agent_efficiency_spawn_errors_total", 1, kind)
            return
        if extra[0] == "spawn":
            kind = efficiency_spawn_error(text)
            if kind is None:
                self.spawn_route(ts, extra)
            elif self.countable(ts):
                self.add("agent_efficiency_spawn_errors_total", 1, kind)
            return
        processes: dict[str, list[str]] = self.s["dprocs"]
        if extra[0].startswith("@"):
            process = extra[0][1:]
            tracked = processes.get(process)
            if tracked is None:
                return
            kinds, flags = tracked
            for code, _, body in chunks:
                flags = self.delivery_output(ts, kinds, body, flags)
                if code is not None:
                    processes.pop(process, None)
                    self.deliver(ts, kinds, code, flags)
                    return
            tracked[1] = flags
            return
        commands = [item[1:] for item in extra]
        if len(commands) == 1:
            code = next((code for code, _, _ in reversed(chunks) if code is not None), None)
            process = None if code is not None else next((pid for _, pid, _ in reversed(chunks) if pid), None)
            pairs = [(commands[0], code, process, text)]
        elif len(chunks) == len(commands):
            pairs = [(kinds, code, process, body) for kinds, (code, process, body) in zip(commands, chunks)]
        else:
            pairs = [(kinds, None, None, text) for kinds in commands]
        for kinds, code, process, body in pairs:
            if not kinds or (code is None and process is None and text.startswith("Script running")):
                continue  # a cell still running at the cell level reports its commands later
            flags = self.delivery_output(ts, kinds, body, "")
            if code is None and process is not None:
                processes.pop(process, None)
                processes[process] = [kinds, flags]
                while len(processes) > EFFICIENCY_MAX_DELIVERY_PROCESSES:
                    del processes[next(iter(processes))]
            else:
                self.deliver(ts, kinds, code, flags)

    def codex_call(self, ts: float | None, payload: dict[str, Any], payload_type: str) -> None:
        name = payload.get("name") or ""
        target = "other"
        start_target = None
        rule = "exec"
        request: tuple[str, Any] | None = None
        extra: list[str] | None = None
        if name == "apply_patch":
            extra = ["ap"]
        if name in EFFICIENCY_CODEX_TOOL:
            cls = EFFICIENCY_CODEX_TOOL[name]
            target = EFFICIENCY_CODEX_WAIT_TARGET.get(name, "other")
            if cls == "wait":
                arguments = efficiency_json_object(payload.get("arguments"))
                if name == "wait_agent":
                    rule, request = "agent", ("wait_agent", arguments.get("timeout_ms"))
                elif name == "sleep":
                    rule, request = "sleep", ("sleep", arguments.get("duration_ms"))
                else:
                    request = ("cell_wait", arguments.get("yield_time_ms", arguments.get("timeout_ms")))
        elif name in EFFICIENCY_CODEX_EXEC:
            text = payload.get("input") if payload_type == "custom_tool_call" else payload.get("arguments")
            text = text if isinstance(text, str) else json.dumps(text)
            cls = efficiency_cls_exec(text)
            poll, process, yield_ms = self.codex_empty_stdin(name, text)
            commands = EFFICIENCY_TARGET_CMD.findall(text)
            if commands and not poll:
                start_target = efficiency_poll_target(commands[0])
            if cls == "wait":
                if poll:
                    target = self.s["procs"].get(process, "other") if process else "other"
                    request = ("write_stdin", yield_ms)
                elif "wait_agent" in text:
                    target, rule = "agent", "agent"
                    request = ("wait_agent", efficiency_js_ms("wait_agent", text))
                elif "tools.wait(" in text:
                    target = "cell"
                    request = ("cell_wait", efficiency_js_ms("cell_wait", text))
                elif "tools.sleep" in text or "setTimeout" in text:
                    target = "sleep"
                    request = ("sleep", efficiency_js_ms("sleep", text))
                else:
                    target = efficiency_poll_target(" ; ".join(commands))
                if target == "sleep":
                    rule = "sleep"
            extra = self.codex_delivery_extra(name, text, commands)
            sites = len(EFFICIENCY_CELL_SPAWN.findall(text)) if "spawn_agent(" in text else 0
            if sites:
                # Code-mode spawns are counted from the cell's result, which also decides the
                # route (one call site only): ["cell", sites] or ["cell", "1", model, effort, agent_type, fork].
                hint: list[str] = ["cell", str(sites)]
                if sites == 1:
                    arguments = efficiency_js_object(text, EFFICIENCY_CELL_SPAWN.search(text).end())
                    if arguments is not None and all(
                        arguments.get(key, "") is not None for key in EFFICIENCY_ROUTE_KEYS
                    ):
                        hint += self.codex_route(arguments)[1:]
                extra = hint
        else:
            cls = "work"
        call_id = payload.get("call_id") or payload.get("id")
        if name == "spawn_agent":
            self.spawn(ts)
            extra = self.codex_route(efficiency_json_object(payload.get("arguments")))
        self.tool_call(ts, call_id, cls, target, start_target, rule, request, extra)

    def codex_delivery_extra(self, name: str, text: str, commands: list[str]) -> list[str] | None:
        """Delivery kinds per started command ('=kinds'), or the tracked process a write_stdin reads ('@id')."""
        if name == "shell" and not commands:
            argv = efficiency_json_object(text).get("command")
            commands = [argv[-1]] if isinstance(argv, list) and argv and isinstance(argv[-1], str) else []
        else:
            commands = [efficiency_unescape(command) for command in commands]
        kinds = [efficiency_delivery_kinds(command) for command in commands]
        if any(kinds):
            return ["=" + item for item in kinds]
        if not self.s["dprocs"] or (name != "write_stdin" and "write_stdin" not in text):
            return None
        match = EFFICIENCY_STDIN_CALL.search(text)
        if match:
            session = EFFICIENCY_STDIN_SESSION.search(match.group(1))
            process = session.group(1) if session else None
        else:
            value = efficiency_json_object(text).get("session_id") if name == "write_stdin" else None
            process = str(value) if efficiency_int(value) is not None else None
        return ["@" + process] if process in self.s["dprocs"] else None

    @staticmethod
    def codex_empty_stdin(name: str, text: str) -> tuple[bool, str | None, int | None]:
        """Return (is an empty-chars write_stdin poll, numeric process session id, yield ms)."""
        match = EFFICIENCY_STDIN_CALL.search(text)
        if match and EFFICIENCY_EMPTY_CHARS.search(match.group(1)):
            session = EFFICIENCY_STDIN_SESSION.search(match.group(1))
            yield_ms = EFFICIENCY_JS_WAIT_MS["write_stdin"].search(match.group(1))
            return True, session.group(1) if session else None, int(yield_ms.group(1)) if yield_ms else None
        if name == "write_stdin":
            arguments = efficiency_json_object(text)
            if arguments.get("chars") == "":
                session = arguments.get("session_id")
                return True, str(session) if session is not None else None, arguments.get("yield_time_ms")
        return False, None, None

    def codex_map_processes(self, output: str, target: str) -> None:
        processes: dict[str, str] = self.s["procs"]
        for first, second in EFFICIENCY_PROCESS_ID.findall(output):
            processes.pop(first or second, None)
            processes[first or second] = target
        while len(processes) > EFFICIENCY_MAX_PROCESSES:
            del processes[next(iter(processes))]

    # -- pi v3 session transcript -------------------------------------------
    def pi_line(self, raw: bytes) -> None:
        try:
            record = json.loads(raw)
        except ValueError:
            return
        if isinstance(record, dict):
            self.pi_record(record)

    def pi_record(self, record: dict[str, Any]) -> None:
        """Follow parse_pi's header, message, custom-message and tool-result shapes."""
        s = self.s
        kind = record.get("type")
        ts = efficiency_event_ts(record.get("timestamp"))
        if kind == "session":
            s["tid"] = record.get("id")
            s["meta"] = True
        if ts is not None:
            self.tick(ts)
        event_ts = ts if ts is not None else s["last"]
        if (
            s["role"] == "solo"
            and s["tid"]
            and self.run.loops.label(loop_session_key("pi", s["tid"], ""), None, event_ts) != LOOP_NONE
        ):
            s["role"] = "root"
        if kind == "model_change":
            s["model"] = record.get("modelId") or s["model"]
        elif kind == "compaction":
            self.compaction(event_ts)
        elif kind == "custom_message":
            custom = record.get("customType")
            if custom == "loop-watch":
                s["trig"] = "event"
                s["in_task"] = True
            elif custom == "loop-wake":
                s["trig"] = "event"
                s["in_task"] = True
            elif custom == "loop-continuation":
                s["trig"] = "orchestrate"
                s["in_task"] = True
            elif custom in ("subagent-notify", "subagent-incremental-child-notify"):
                s["trig"] = "agent_msg"
                s["in_task"] = True
                self.pi_notification(event_ts, record)
        elif kind == "message":
            message = record.get("message") if isinstance(record.get("message"), dict) else {}
            role = message.get("role")
            if role == "user":
                if s["role"] != "worker":
                    self.human_message(event_ts, pi_format()._text(message.get("content")))
                s["trig"] = "user"
                s["in_task"] = True
            elif role == "assistant":
                self.pi_assistant(event_ts, message)
            elif role == "toolResult":
                self.pi_result(event_ts, message)

    def pi_assistant(self, ts: float | None, message: dict[str, Any]) -> None:
        s = self.s
        if message.get("model"):
            s["model"] = message["model"]
        usage = message.get("usage")
        if isinstance(usage, dict) and usage:
            cache = int(usage.get("cacheRead") or 0)
            total = int(usage.get("input") or 0) + cache + int(usage.get("cacheWrite") or 0)
            self.llm_call(ts, total, cache, int(usage.get("output") or 0))
        stop = message.get("stopReason")
        if stop in ("error", "aborted"):
            self.turn_error(ts, "api_error" if stop == "error" else "aborted")
        for block in message.get("content") or []:
            if not isinstance(block, dict) or block.get("type") != "toolCall":
                continue
            name = block.get("name")
            args = block.get("arguments") if isinstance(block.get("arguments"), dict) else {}
            cls, target, rule, extra = "work", "other", "exec", None
            request = None
            if name == "bash":
                command = args.get("command") if isinstance(args.get("command"), str) else ""
                cls = efficiency_cls_exec("tools." + command)
                if cls == "wait":
                    target = efficiency_poll_target(command)
                    if target == "sleep":
                        rule = "sleep"
                kinds = efficiency_delivery_kinds(command)
                if kinds:
                    extra = ["=" + kinds]
            elif name == "subagent":
                action = args.get("action")
                if action in ("list", "status"):
                    cls = "status"
                elif action == "wait":
                    cls, target, rule = "wait", "agent", "agent"
                elif args.get("workflowScript") is not None or action in ("run", "start", "spawn") or args.get("tasks"):
                    cls = "orchestrate"
                    extra = ["pi-spawn"]
                    if self.s["pi_launch_ts"] is None:
                        self.s["pi_launch_ts"] = ts
            elif name in ("watch_start", "wake_at"):
                # These calls arm a future wake. Their immediate result is not a completed wait.
                cls = "orchestrate"
            self.tool_call(ts, block.get("id"), cls, target, None, rule, request, extra)
        if stop in ("stop", "length", "error", "aborted") and not s["pending"]:
            s["in_task"] = False

    def pi_count_spawn(self, ts: float | None, key: str, agent_type: str | None = None) -> None:
        seen: list[str] = self.s["pi_spawns"]
        digest = hashlib.sha256(key.encode()).hexdigest()[:16]
        if digest in seen:
            return
        if self.s["spawn_ts"] is None:
            self.pi_spawn_started(self.s.get("pi_launch_ts") or ts)
        seen.append(digest)
        del seen[:-EFFICIENCY_MAX_IDS]
        if self.countable(ts):
            self.add("agent_efficiency_spawns_total", 1)
        hint = ["spawn", "inherit", "inherit", self.spawn_value(agent_type, "default"), "none"]
        self.spawn_route(ts, hint)

    def pi_spawn_started(self, ts: float | None) -> None:
        """Mark the root and first launch at the tool call; child count needs child evidence."""
        first = self.s["spawn_ts"] is None
        if self.s["role"] == "solo":
            self.s["role"] = "root"
        if first:
            start = self.s["t0"]
            if ts is not None and start is not None and start >= 0 and ts >= start and self.countable(ts):
                self.observe("agent_efficiency_first_spawn_seconds", ts - start)
        if ts is not None:
            previous = self.s["spawn_ts"]
            self.s["spawn_ts"] = max(previous or ts, ts)

    def pi_notification(self, ts: float | None, record: dict[str, Any]) -> None:
        fmt = pi_format()
        content = record.get("content")
        text = fmt._text(content)
        custom = record.get("customType")
        if custom == "subagent-incremental-child-notify":
            match = fmt.CHILD_RUN_RE.search(text)
            done = fmt.CHILD_DONE_RE.search(text)
            if match:
                self.pi_count_spawn(ts, match.group(1), done.group(2).strip() if done else None)
        else:
            match = fmt.CHILD_RUNS_RE.search(text)
            for child in fmt.CHILD_RUN_ITEM_RE.finditer(match.group(1) if match else ""):
                self.pi_count_spawn(ts, child.group(2), child.group(1))

    def pi_result(self, ts: float | None, message: dict[str, Any]) -> None:
        call_id = message.get("toolCallId")
        result_text = pi_format()._text(message.get("content"))
        details = message.get("details") if isinstance(message.get("details"), dict) else {}
        entry = self.tool_output(
            ts, call_id, lambda rule: "event" if rule == "agent" and details.get("results") else "timed_out"
        )
        if entry is None:
            return
        if message.get("isError"):
            self.tool_failure(ts, entry[1])
            if entry[6] == ["pi-spawn"] and self.countable(ts):
                self.add("agent_efficiency_spawn_errors_total", 1, "other")
        if entry[6] == ["pi-spawn"]:
            launched = bool(details.get("runId") or details.get("asyncId"))
            for index, result in enumerate(details.get("results") or []):
                if not isinstance(result, dict) or not result.get("agent"):
                    continue
                child_launched = result.get("sessionFile") or result.get("exitCode") == 0
                if child_launched:
                    launched = True
                    self.pi_count_spawn(ts, f"{call_id}:{result.get('index', index)}", result["agent"])
                elif result.get("exitCode") not in (None, 0) and self.countable(ts):
                    self.add("agent_efficiency_spawn_errors_total", 1, "other")
            if launched and self.s["spawn_ts"] is None:
                self.pi_spawn_started(entry[2])
            elif not launched:
                self.s["pi_launch_ts"] = None
        if entry[6] and entry[6][0].startswith("="):
            kinds = entry[6][0][1:]
            flags = self.delivery_output(ts, kinds, result_text, "")
            self.deliver(ts, kinds, 1 if message.get("isError") else 0, flags)

    # -- Claude project transcript -------------------------------------------
    def claude_line(self, raw: bytes) -> None:
        if not any(marker in raw for marker in EFFICIENCY_CLAUDE_MARKERS):
            return
        try:
            record = json.loads(raw)
        except ValueError:
            return
        if isinstance(record, dict):
            self.claude_record(record)

    def claude_record(self, record: dict[str, Any]) -> None:
        s = self.s
        kind = record.get("type")
        ts = efficiency_event_ts(record.get("timestamp"))
        event_ts = ts if ts is not None else s["last"]
        # One compaction writes a compact_boundary plus an isCompactSummary user line; count it once.
        if kind == "system" and record.get("subtype") == "compact_boundary":
            s["cb"] = True
            self.compaction(event_ts)
        elif record.get("isCompactSummary"):
            if s["cb"]:
                s["cb"] = False
            else:
                self.compaction(event_ts)
        if kind == "system" and record.get("subtype") == "turn_duration":
            # The turn is over, so the time until the next message is idle.
            if ts is not None:
                self.tick(ts)
            s["in_task"] = False
            s["pending"] = []
            return
        if kind not in ("user", "assistant"):
            return
        message = record.get("message") if isinstance(record.get("message"), dict) else {}
        content = message.get("content")
        model = message.get("model")
        if kind == "assistant" and model and model != "<synthetic>":
            s["model"] = str(model)
        if ts is not None:
            self.tick(ts)
            event_ts = ts
        if kind == "assistant":
            self.claude_assistant(event_ts, record, message, content)
        else:
            self.claude_user(event_ts, record, content)

    def claude_assistant(self, ts: float | None, record: dict[str, Any], message: dict[str, Any], content: Any) -> None:
        s = self.s
        if record.get("isApiErrorMessage"):
            self.turn_error(ts, EFFICIENCY_CLAUDE_ERRORS.get(str(record.get("error")), "other"))
        elif message.get("stop_reason") == "refusal":
            self.turn_error(ts, "flagged")
        message_id = message.get("id")
        usage_record = message.get("usage")
        if (
            message_id
            and isinstance(usage_record, dict)
            and usage_record
            and message_id not in s["mids"]
            and message.get("model") != "<synthetic>"
        ):
            s["mids"].append(message_id)
            del s["mids"][:-EFFICIENCY_MAX_IDS]
            cache_read = int(usage_record.get("cache_read_input_tokens") or 0)
            total = (
                int(usage_record.get("input_tokens") or 0)
                + cache_read
                + int(usage_record.get("cache_creation_input_tokens") or 0)
            )
            self.llm_call(ts, total, cache_read, int(usage_record.get("output_tokens") or 0))
        if isinstance(content, list):
            self.claude_tool_uses(ts, content)
        if message.get("stop_reason") == "end_turn" and not s["pending"]:
            # A final answer with no tool use outstanding ends the turn.
            s["in_task"] = False

    def claude_tool_uses(self, ts: float | None, content: list[Any]) -> None:
        s = self.s
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            name = block.get("name") or ""
            arguments = block.get("input") if isinstance(block.get("input"), dict) else {}
            target = "other"
            rule = "bash"
            extra: list[str] | None = None
            if name in EFFICIENCY_CLAUDE_TOOL:
                cls = EFFICIENCY_CLAUDE_TOOL[name]
                if name in ("ScheduleWakeup", "Sleep"):
                    target, rule = "sleep", "sleep"
                elif name == "Monitor":
                    rule = "event"
                elif name in ("TaskOutput", "BashOutput"):
                    rule = "task"
                    if name == "TaskOutput" and str(arguments.get("task_id")) in s["agents"]:
                        target = "agent"
            elif name == "Bash":
                command = str(arguments.get("command", ""))
                cls = efficiency_cls_exec("tools." + command)
                if arguments.get("run_in_background"):
                    cls = "work"
                if cls == "wait":
                    target = efficiency_poll_target(command)
                    if target == "sleep":
                        rule = "sleep"
                kinds = efficiency_delivery_kinds(command)
                if kinds:
                    extra = [("~" if arguments.get("run_in_background") else "=") + kinds]
            else:
                cls = "work"
            if name in ("Agent", "Task"):
                self.spawn(ts)
                extra = self.claude_route(arguments)
            self.tool_call(ts, block.get("id"), cls, target, None, rule, None, extra)

    def claude_result(self, ts: float | None, entry: list[Any] | None, block: dict[str, Any], tool_result: Any) -> None:
        """v1.2 accounting for a closed Claude tool call: failures and delivery outcomes."""
        if entry is None:
            return
        is_error = bool(block.get("is_error"))
        if is_error:
            self.tool_failure(ts, entry[1])
        extra: list[str] | None = entry[6] if len(entry) > 6 else None
        if not extra:
            return
        if extra[0] == "spawn":
            if not is_error:
                self.spawn_route(ts, extra)
            return
        kinds = extra[0][1:]
        if extra[0].startswith("~") or (isinstance(tool_result, dict) and tool_result.get("backgroundTaskId")):
            # Backgrounded: the exit is not in this result. A review is only judged from its own output.
            kinds = "+".join(kind for kind in kinds.split("+") if kind != "cr")
            if kinds:
                self.deliver(ts, kinds, None, "")
            return
        flags = self.delivery_output(ts, kinds, efficiency_block_text(block.get("content")), "")
        self.deliver(ts, kinds, 1 if is_error else 0, flags)

    def claude_user(self, ts: float | None, record: dict[str, Any], content: Any) -> None:
        s = self.s
        s["in_task"] = True
        if isinstance(content, list):
            results = [block for block in content if isinstance(block, dict) and block.get("type") == "tool_result"]
            tool_result = record.get("toolUseResult")
            for block in results:
                entry = self.tool_output(
                    ts, block.get("tool_use_id"), lambda rule: efficiency_claude_result(rule, tool_result)
                )
                self.claude_result(ts, entry, block, tool_result)
            spawned = tool_result
            if isinstance(spawned, dict) and spawned.get("agentId"):
                s["agents"].append(str(spawned["agentId"]))
                del s["agents"][:-EFFICIENCY_MAX_IDS]
            if any(
                isinstance(block, dict)
                and isinstance(block.get("text"), str)
                and block["text"].startswith("[Request interrupted by user")
                for block in content
            ):
                self.turn_error(ts, "aborted")
                self.intervention(ts, "interrupt")
            elif not results and efficiency_claude_human(record, efficiency_block_text(content)):
                self.human_message(ts, efficiency_block_text(content))
            if not results and not s["first_user"]:
                s["first_user"] = True
                s["trig"] = "user"
        elif isinstance(content, str):
            s["first_user"] = True
            s["trig"] = "event" if content.startswith("<task-notification>") else "user"
            if content.startswith("[Request interrupted by user"):
                self.turn_error(ts, "aborted")
                self.intervention(ts, "interrupt")
            elif efficiency_claude_human(record, content):
                self.human_message(ts, content)


def efficiency_head(handle: Any) -> str | None:
    handle.seek(0)
    line = handle.readline(EFFICIENCY_HEAD_BYTES)
    if not line or (not line.endswith(b"\n") and len(line) < EFFICIENCY_HEAD_BYTES):
        return None
    return hashlib.sha256(line).hexdigest()[:16]


def efficiency_recover_protocol(path: Path, agent: str, limit: int) -> str | None:
    """Protocol of a thread whose first prompt an older collector already consumed (limit = consumed bytes).

    Reads only records that can hold a human prompt; None when the first prompt lies beyond `limit`.
    """
    read = 0
    with path.open("rb") as handle:
        for raw in handle:
            read += len(raw)
            if read > limit:
                return None
            if agent == "codex":
                if b'"user_message"' not in raw[:160] and b'"role":"user"' not in raw[:200]:
                    continue
            elif b'"type":"user"' not in raw:
                continue
            try:
                record = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(record, dict):
                continue
            payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}
            if agent == "codex":
                if record.get("type") == "event_msg" and payload.get("type") == "user_message":
                    message = payload.get("message")
                    return efficiency_protocol(message if isinstance(message, str) else "")
                if (
                    record.get("type") == "response_item"
                    and payload.get("type") == "message"
                    and payload.get("role") == "user"
                    and efficiency_codex_human_item(payload.get("content"))
                ):
                    return efficiency_protocol(efficiency_codex_human_text(payload.get("content")))
                continue
            if record.get("type") != "user":
                continue
            message = record.get("message") if isinstance(record.get("message"), dict) else {}
            content = message.get("content")
            if isinstance(content, list) and any(
                isinstance(block, dict) and block.get("type") == "tool_result" for block in content
            ):
                continue
            text = content if isinstance(content, str) else efficiency_block_text(content)
            if not text.startswith("[Request interrupted") and efficiency_claude_human(record, text):
                return efficiency_protocol(text)
    return None


def efficiency_parse_file(
    run: EfficiencyRun,
    hot: Path,
    relative: str,
    file_state: dict[str, Any],
    size: int,
    mtime_ns: int,
    deadline: float,
    monotonic: Callable[[], float],
) -> tuple[bool, int]:
    """Consume complete new lines of one file.

    Returns whether the file was consumed within the run budget, and how many malformed records
    were skipped. A malformed record is consumed and skipped so it cannot pin the file's offset.
    """
    namespace = relative.split("/", 1)[0]
    with (hot / relative).open("rb") as handle:
        head = efficiency_head(handle)
        if size < file_state["offset"] or (file_state["head"] and head and head != file_state["head"]):
            skip = max(float(file_state["skip"]), float(file_state["counted"] or 0))
            file_state.clear()
            file_state.update(efficiency_file_state(relative, skip))
        if file_state["head"] is None:
            file_state["head"] = head
        if file_state.get("proto") == "?":
            # v1.3 upgrade: recover the protocol from the already-consumed first prompt.
            proto = None
            if file_state["role"] != "worker" and file_state["offset"]:
                limit = min(file_state["offset"], EFFICIENCY_PROTOCOL_SCAN_BYTES)
                proto = efficiency_recover_protocol(hot / relative, namespace.split("-", 1)[0], limit)
                if proto is None and file_state["offset"] > EFFICIENCY_PROTOCOL_SCAN_BYTES:
                    proto = "none"
            file_state["proto"] = proto
        handle.seek(file_state["offset"])
        parser = EfficiencyParser(run, file_state, relative)
        lines = 0
        skipped = 0
        finished = True
        for raw in handle:
            if not raw.endswith(b"\n"):
                break
            try:
                parser.line(raw)
            except (ValueError, TypeError, AttributeError, KeyError, IndexError, OverflowError):
                skipped += 1
            file_state["offset"] += len(raw)
            lines += 1
            if lines % EFFICIENCY_BUDGET_CHECK_LINES == 0 and monotonic() > deadline:
                finished = False
                break
    file_state["size"] = size
    file_state["mtime_ns"] = mtime_ns
    return finished, skipped


def efficiency_close_lanes(
    run: EfficiencyRun,
    files: dict[str, dict[str, Any]],
    sources: dict[str, tuple[int, int]],
    now: float,
) -> None:
    """Observe worker lanes whose file has gone quiet for 30 minutes (fully parsed, or stopped)."""
    quiet_ns = int((now - EFFICIENCY_GAP_SECONDS) * 1_000_000_000)
    for relative, file_state in files.items():
        lane, last = file_state.get("lane"), file_state.get("last")
        if file_state.get("role") != "worker" or lane is None or last is None or now - last <= EFFICIENCY_GAP_SECONDS:
            continue
        size, mtime_ns = sources.get(relative, (0, 0))
        if size > file_state["offset"] and mtime_ns >= quiet_ns:
            continue  # unparsed bytes of a file still being written
        if lane >= 0:
            EfficiencyParser(run, file_state, relative).lane_close(lane, last)
        file_state["lane"] = None


def efficiency_key_loop_index(metric: str) -> int:
    """Position of the loop value in a persisted totals key: last, or before `le` in a bucket key."""
    return -2 if metric.endswith("_bucket") else -1


def efficiency_upgrade_v1(state: dict[str, Any]) -> None:
    """v1 -> v2: every persisted series gains loop="none"; offsets, counters and the baseline are kept."""
    totals = state.get("totals") if isinstance(state.get("totals"), dict) else {}
    for metric, series in totals.items():
        if not isinstance(series, dict):
            continue
        upgraded = {}
        for key, value in series.items():
            parts = key.split("\t")
            if metric.endswith("_bucket"):
                parts.insert(len(parts) - 1, LOOP_NONE)
            else:
                parts.append(LOOP_NONE)
            upgraded["\t".join(parts)] = value
        totals[metric] = upgraded
    state["version"] = EFFICIENCY_STATE_VERSION


def read_efficiency_state(path: Path, now: float) -> dict[str, Any]:
    state = read_state(path)
    if state.get("version") == 1 and "baseline_ts" in state:
        efficiency_upgrade_v1(state)
    if state.get("version") != EFFICIENCY_STATE_VERSION or "baseline_ts" not in state:
        state = {"version": EFFICIENCY_STATE_VERSION, "baseline_ts": now}
    for key, default in (
        ("models", []),
        ("totals", {}),
        ("recent_calls", []),
        ("files", {}),
        ("agent_types", []),
        ("rate_limits", {}),
        ("loops", {}),
    ):
        if not isinstance(state.get(key), type(default)):
            state[key] = default
    for file_state in state["files"].values():
        efficiency_upgrade_file(file_state)
    return state


def efficiency_prune_loops(state: dict[str, Any], now: float) -> None:
    """Drop a loop's persisted series once it has had no counted event for the retention window.

    Grafana keeps the history; this only stops the textfile carrying finished loops forever.
    """
    expired = {label for label, seen in state["loops"].items() if now - float(seen) > EFFICIENCY_LOOP_RETAIN_SECONDS}
    if not expired:
        return
    for metric, series in state["totals"].items():
        index = efficiency_key_loop_index(metric)
        for key in [key for key in series if key.split("\t")[index] in expired]:
            del series[key]
    for label in expired:
        del state["loops"][label]


def efficiency_select_loop_map(
    state: dict[str, Any], fresh: dict[str, Any] | None, now: float
) -> dict[str, Any] | None:
    """The fresh catalogue map (cached in state), else a cached one young enough to trust, else None."""
    if isinstance(fresh, dict):
        state["loop_map"] = fresh
        return fresh
    cached = state.get("loop_map")
    if isinstance(cached, dict) and now - float(cached.get("fetched") or 0) <= LOOP_MAP_MAX_AGE_SECONDS:
        return cached
    state.pop("loop_map", None)
    return None


def efficiency_quantile(values: list[Any], quantile: float) -> Any:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(quantile * len(ordered)) - 1)]


def efficiency_emit(metrics: Any, state: dict[str, Any], now: float, run: EfficiencyRun | None = None) -> None:
    run = run or EfficiencyRun(state)

    def loop_of(relative: str, file_state: dict[str, Any], ts: float | None) -> str:
        return run.loop_label(relative, file_state, ts, record=False)

    for metric, (label_names, help_text) in EFFICIENCY_COUNTERS.items():
        for key, value in state["totals"].get(metric, {}).items():
            metrics.add(
                metric, value, dict(zip(label_names, key.split("\t"))), help_text=help_text, metric_type="counter"
            )
    for family, (label_names, bounds, help_text) in EFFICIENCY_HISTOGRAMS.items():
        buckets = state["totals"].get(family + "_bucket", {})
        sums = state["totals"].get(family + "_sum", {})
        for key, count in state["totals"].get(family + "_count", {}).items():
            labels = dict(zip(label_names, key.split("\t")))
            for bound in (*map(str, bounds), "+Inf"):
                metrics.add(
                    family + "_bucket",
                    buckets.get(f"{key}\t{bound}", 0),
                    {**labels, "le": bound},
                    help_text=help_text,
                    metric_type="histogram",
                    family=family,
                )
            metrics.add(
                family + "_sum", sums.get(key, 0), labels, help_text=help_text, metric_type="histogram", family=family
            )
            metrics.add(family + "_count", count, labels, help_text=help_text, metric_type="histogram", family=family)
    for namespace, windows in sorted(state["rate_limits"].items()):
        if namespace not in {p.split("/", 1)[0] for p in state["files"]} or not isinstance(windows, dict):
            continue
        for window, reading in sorted(windows.items()):
            if window not in EFFICIENCY_RATE_WINDOWS or not isinstance(reading, list) or len(reading) != 4:
                continue
            labels = {"agent": namespace.split("-", 1)[0], "namespace": namespace, "window": window}
            for value, metric, help_text in zip(
                reading[1:],
                (
                    "agent_efficiency_rate_limit_used_percent",
                    "agent_efficiency_rate_limit_resets_at_seconds",
                    "agent_efficiency_rate_limit_window_minutes",
                ),
                (
                    "Latest Codex rate-limit used percent seen in the namespace for the window.",
                    "Reset time of the latest Codex rate-limit reading for the window.",
                    "Window length in minutes of the latest Codex rate-limit reading.",
                ),
            ):
                if value is not None:
                    metrics.add(metric, value, labels, help_text=help_text)

    files: dict[str, dict[str, Any]] = state["files"]
    active_after = now - EFFICIENCY_ACTIVE_SECONDS
    active: dict[tuple[str, str, str], int] = defaultdict(int)
    namespaces = {relative.split("/", 1)[0] for relative in files}
    codex_parent: dict[str, str | None] = {}
    active_files: set[str] = set()
    for relative, file_state in files.items():
        if file_state.get("tid"):
            codex_parent[file_state["tid"]] = file_state.get("parent")
        if file_state.get("last_call") is not None and file_state["last_call"] >= active_after:
            active_files.add(relative)
            loop = loop_of(relative, file_state, file_state.get("last"))
            active[(relative.split("/", 1)[0], file_state["role"], loop)] += 1

    def root_key(relative: str, file_state: dict[str, Any]) -> str | None:
        if relative.startswith("pi-"):
            return pi_format().lineage(relative).get("root")
        if relative.split("/", 1)[0].startswith("claude-"):
            if "/subagents/" in relative:
                return relative.rsplit("/subagents/", 1)[0] + ".jsonl"
            return relative
        thread = file_state.get("tid")
        for _ in range(8):
            parent = codex_parent.get(thread) if thread else None
            if not parent:
                return thread
            thread = parent
        return thread

    busy_roots: set[str] = set()
    for relative in active_files:
        file_state = files[relative]
        if file_state["role"] == "worker":
            key = root_key(relative, file_state)
            if key:
                busy_roots.add(key)
    idle_roots: dict[tuple[str, str], int] = defaultdict(int)
    for relative in active_files:
        file_state = files[relative]
        if file_state["role"] == "root" and root_key(relative, file_state) not in busy_roots:
            idle_roots[(relative.split("/", 1)[0], loop_of(relative, file_state, file_state.get("last")))] += 1

    # `none` series always exist per namespace (and role) so a quiet namespace reads 0, not absent;
    # a loop's series exist only while it has something to report.
    for namespace in sorted(namespaces):
        for role in EFFICIENCY_ROLES:
            active.setdefault((namespace, role, LOOP_NONE), 0)
        idle_roots.setdefault((namespace, LOOP_NONE), 0)
    for (namespace, role, loop), count in sorted(active.items()):
        metrics.add(
            "agent_efficiency_active_threads",
            count,
            {"agent": namespace.split("-", 1)[0], "namespace": namespace, "role": role, "loop": loop},
            help_text="Agent threads with a model call in the last 10 minutes.",
        )
    for (namespace, loop), count in sorted(idle_roots.items()):
        metrics.add(
            "agent_efficiency_active_roots_with_idle_workers",
            count,
            {"agent": namespace.split("-", 1)[0], "namespace": namespace, "loop": loop},
            help_text="Active root threads with zero active workers in the last 10 minutes.",
        )

    efficiency_emit_stalls(metrics, files, namespaces, now, loop_of)

    windows: dict[tuple[str, str, str], list[int]] = defaultdict(list)
    fills: dict[tuple[str, str, str], list[float]] = defaultdict(list)
    for entry in state["recent_calls"]:
        ts, namespace, role, tokens = entry[:4]
        loop = entry[5] if len(entry) > 5 and isinstance(entry[5], str) else LOOP_NONE
        if ts >= active_after:
            windows[(namespace, role, loop)].append(int(tokens))
            if len(entry) > 4 and entry[4]:
                fills[(namespace, role, loop)].append(int(tokens) / entry[4])
    for (namespace, role, loop), values in sorted(fills.items()):
        for quantile in ("0.5", "0.9"):
            metrics.add(
                "agent_efficiency_context_fill_ratio",
                efficiency_quantile(values, float(quantile)),
                {
                    "agent": namespace.split("-", 1)[0],
                    "namespace": namespace,
                    "role": role,
                    "quantile": quantile,
                    "loop": loop,
                },
                help_text="Input tokens over the model context window per model call in the last 10 minutes.",
            )
    for (namespace, role, loop), values in sorted(windows.items()):
        for quantile in ("0.5", "0.9"):
            metrics.add(
                "agent_efficiency_context_tokens",
                efficiency_quantile(values, float(quantile)),
                {
                    "agent": namespace.split("-", 1)[0],
                    "namespace": namespace,
                    "role": role,
                    "quantile": quantile,
                    "loop": loop,
                },
                help_text="Input tokens per model call over calls in the last 10 minutes.",
            )
    metrics.add(
        "agent_efficiency_tracked_files",
        len(files),
        help_text="Transcript files held in the incremental efficiency parser state.",
    )
    metrics.add(
        "agent_efficiency_baseline_timestamp_seconds",
        state["baseline_ts"],
        help_text="Efficiency events before this time are never counted.",
    )


def efficiency_emit_stalls(
    metrics: Any,
    files: dict[str, dict[str, Any]],
    namespaces: set[str],
    now: float,
    loop_of: Callable[[str, dict[str, Any], float | None], str] = lambda relative, file_state, ts: LOOP_NONE,
) -> None:
    """Stalled-loop gauges: live spawning loop roots with no child task in flight, and for how long.

    Only loop roots count: a root whose first prompt declared a protocol (v2.1, v2.0 or other). Interactive
    sessions (none) and legacy threads whose protocol is not yet recovered (?) are excluded.
    """
    roots: dict[str, dict[str, Any]] = {}
    by_thread: dict[tuple[str, str], str] = {}
    for relative, file_state in files.items():
        if file_state.get("role") == "root" and file_state.get("proto") in EFFICIENCY_PROTOCOLS - {"none"}:
            roots[relative] = file_state
            if file_state.get("tid"):
                by_thread[(relative.split("/", 1)[0], file_state["tid"])] = relative
    children: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for relative, file_state in files.items():
        if file_state.get("role") != "worker":
            continue
        namespace = relative.split("/", 1)[0]
        if namespace.startswith("claude-"):
            parent = relative.rsplit("/subagents/", 1)[0] + ".jsonl" if "/subagents/" in relative else None
        else:
            parent = by_thread.get((namespace, file_state.get("parent")))
        if parent in roots:
            children[parent].append(file_state)

    stalled: dict[tuple[str, str], int] = defaultdict(int)
    longest: dict[tuple[str, str], float] = defaultdict(float)
    for relative, root in roots.items():
        last = root.get("last")
        if not root.get("in_task") or last is None or now - last > EFFICIENCY_LIVE_ROOT_SECONDS:
            continue
        namespace = relative.split("/", 1)[0]
        in_flight = False
        since = root.get("spawn_ts")
        for child in children[relative]:
            child_last = child.get("last")
            if child_last is None:
                continue
            if child.get("in_task") and now - child_last <= EFFICIENCY_CHILD_FLIGHT_SECONDS:
                in_flight = True
                break
            # A task that ended was in flight until its last event; a silent one until the 60-minute cut-off.
            until = child_last + EFFICIENCY_CHILD_FLIGHT_SECONDS if child.get("in_task") else child_last
            since = until if since is None else max(since, until)
        if in_flight:
            continue
        key = (namespace, loop_of(relative, root, last))
        stalled[key] += 1
        longest.setdefault(key, 0.0)
        if since is not None:
            longest[key] = max(longest[key], max(0.0, now - since))
    for namespace in namespaces:
        stalled.setdefault((namespace, LOOP_NONE), 0)
    for key in sorted(stalled):
        namespace, loop = key
        labels = {"agent": namespace.split("-", 1)[0], "namespace": namespace, "loop": loop}
        metrics.add(
            "agent_efficiency_roots_without_lanes",
            stalled[key],
            labels,
            help_text="Live spawning loop-protocol root threads with no child task in flight.",
        )
        metrics.add(
            "agent_efficiency_root_no_lane_seconds",
            longest.get(key, 0),
            labels,
            help_text="Longest time a live spawning loop-protocol root has had no child task in flight.",
        )


def read_state(path: Path) -> dict[str, Any]:
    try:
        result = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return result if isinstance(result, dict) else {}

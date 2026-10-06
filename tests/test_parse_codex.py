"""Codex rollout parser tests over synthetic fixtures (structure mirrors real rollouts, no real content)."""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest

from agent_history.memstore import MemStore, run_file
from agent_history.model import FileContext, SessionKey
from agent_history.parse_codex import CodexParser

FIX = Path(__file__).parent / "fixtures" / "codex"
P = "11111111-1111-4111-8111-111111111111"
C = "22222222-2222-4222-8222-222222222222"
D = "33333333-3333-4333-8333-333333333333"
N = "44444444-4444-4444-8444-444444444444"
M = "55555555-5555-4555-8555-555555555555"
PARENT = "66666666-6666-4666-8666-666666666666"
V3_CLASSES = {"human_prompt", "queued_prompt", "assistant_text", "subagent_brief", "subagent_report",
              "compaction_summary", "task_notification_summary"}


def ctx(path: Path, rel: str | None = None) -> FileContext:
    return FileContext(str(path), rel or f"codex-local/sessions/{path.name}", "codex-local", "codex",
                       "local", None, "main")


def load(name: str, batch_lines: int = 5000, store: MemStore | None = None, rel: str | None = None,
         path: Path | None = None) -> tuple[MemStore, dict]:
    store = store or MemStore()
    path = path or (FIX.parent / "codex_v4" / name if name.startswith("v4_") else FIX / name)
    _, state = run_file(CodexParser, ctx(path, rel), store, batch_lines=batch_lines)
    return store, state


def by(store: MemStore, table: str, **match) -> list[dict]:
    return [r for r in store.rows(table) if all(r.get(k) == v for k, v in match.items())]


def one(store: MemStore, table: str, **match) -> dict:
    rows = by(store, table, **match)
    assert len(rows) == 1, (table, match, len(rows))
    return rows[0]


def snapshot(store: MemStore) -> dict:
    # record_type_seen counts are per batch by contract, so they legitimately differ by batch size.
    return {t: sorted(map(repr, rows.values())) for t, rows in store.tables.items() if t != "record_type_seen"}


# --- nullable transcript telemetry --------------------------------------------------------------


TELEMETRY = {"llm_call": {"duration_ms", "latency_basis", "service_tier"},
             "message": {"phase"}, "turn": {"reasoning_summary", "trace_id", "root_turn_key"},
             "tool_op": {"mcp_plugin_id", "mcp_read_only"}}


def telemetry_records():
    return [json.loads(line) for line in (FIX / "nullable_telemetry.jsonl").read_text().splitlines()]


def load_records(tmp_path, records):
    path = tmp_path / "telemetry.jsonl"
    path.write_text("".join(json.dumps(record) + "\n" for record in records))
    return load(path.name, path=path, batch_lines=1)[0]


def test_nullable_telemetry_real_parser_surface():
    store, _ = load("nullable_telemetry.jsonl", batch_lines=1)
    calls = {row["response_id"]: row for row in store.rows("llm_call")}
    assert {rid: (row["duration_ms"], row["latency_basis"], row["service_tier"])
            for rid, row in calls.items()} == {
        "telemetry-first": (2000, "codex_prev_boundary_to_usage", "default"),
        # Internal executed ops are not model request boundaries; the last parallel output is.
        "telemetry-parallel": (2000, "codex_prev_boundary_to_usage", "priority"),
        "telemetry-zero": (0, "codex_prev_boundary_to_usage", "priority"),
        "telemetry-next": (1000, "codex_prev_boundary_to_usage", "priority"),
    }
    assert calls["telemetry-zero"]["output"] == 0
    message = one(store, "message", message_class="assistant_text")
    assert (message["phase"], message["text"], message["event_uid"]) == (
        "commentary", "Synthetic telemetry reply.", "77777777-7777-4777-8777-777777777777:6")
    turn = one(store, "turn", turn_key="telemetry-turn")
    assert (turn["reasoning_summary"], turn["trace_id"], turn["root_turn_key"]) == (
        "detailed", "synthetic-trace", "synthetic-root-turn")
    assert one(store, "turn", turn_key="telemetry-next")["reasoning_summary"] == "none"
    op = one(store, "tool_op", item_uid="synthetic-mcp-op")
    assert (op["mcp_plugin_id"], op["mcp_read_only"]) == ("synthetic-plugin", False)
    assert one(store, "tool_io", io_uid="item:synthetic-mcp-op")["output_text"] == "Synthetic operation output."


@pytest.mark.parametrize("table,match,field,expected", [
    ("message", {"message_class": "assistant_text"}, "phase", "commentary"),
    ("turn", {"turn_key": "telemetry-turn"}, "reasoning_summary", "detailed"),
    ("turn", {"turn_key": "telemetry-turn"}, "trace_id", "synthetic-trace"),
    ("turn", {"turn_key": "telemetry-turn"}, "root_turn_key", "synthetic-root-turn"),
    ("tool_op", {"item_uid": "synthetic-mcp-op"}, "mcp_plugin_id", "synthetic-plugin"),
    ("tool_op", {"item_uid": "synthetic-mcp-op"}, "mcp_read_only", False),
    ("llm_call", {"response_id": "telemetry-parallel"}, "service_tier", "priority"),
])
def test_explicit_telemetry_fields_independently(table, match, field, expected):
    store, _ = load("nullable_telemetry.jsonl", batch_lines=1)
    assert one(store, table, **match)[field] == expected


def test_turn_first_usage_includes_calls_emitted_by_that_response(tmp_path):
    records = telemetry_records()
    call = {"type": "response_item", "timestamp": "2026-09-01T10:00:02.500Z",
            "payload": {"type": "function_call", "call_id": "synthetic-inflight",
                        "name": "synthetic", "arguments": "{}"}}
    records.insert(7, call)
    for record in records:
        record.pop("ordinal", None)
    row = one(load_records(tmp_path, records), "llm_call", response_id="telemetry-first")
    assert (row["duration_ms"], row["latency_basis"]) == (2000, "codex_prev_boundary_to_usage")


def test_legacy_calls_carry_active_tier_without_guessed_duration(tmp_path):
    records = telemetry_records()
    records[0]["payload"]["cli_version"] = "0.145.0"
    usage = records[7]["payload"]["usage"]
    records[7].update(type="event_msg", payload={"type": "token_count", "info": {
        "last_token_usage": usage, "total_token_usage": usage}})
    records = records[:8]
    row = one(load_records(tmp_path, records), "llm_call")
    assert row["service_tier"] == "default"
    assert (row["duration_ms"], row["latency_basis"]) == (None, None)


@pytest.mark.parametrize("case", ["missing-start", "invalid-start", "missing-end", "negative",
                                  "unknown-turn", "pending-parallel", "no-new-boundary", "overlap"])
def test_nullable_call_timing_rejects_unattributable_endpoints(tmp_path, case):
    records = telemetry_records()
    target = "telemetry-first"
    if case in {"missing-start", "invalid-start"}:
        records[2]["timestamp"] = None if case == "missing-start" else "not-a-timestamp"
    elif case == "missing-end":
        records[7].pop("timestamp")
    elif case == "negative":
        records[7]["timestamp"] = "2026-09-01T10:00:00Z"
    elif case == "unknown-turn":
        records[7]["payload"]["turn_id"] = "unobserved-turn"
    elif case == "pending-parallel":
        records[13], records[15] = records[15], records[13]
        for record in records:
            record.pop("ordinal", None)
        target = "telemetry-parallel"
    elif case == "no-new-boundary":
        records.pop(16)
        target = "telemetry-zero"
    elif case == "overlap":
        records.insert(7, {"type": "event_msg", "timestamp": "2026-09-01T10:00:02.500Z",
                           "payload": {"type": "task_started", "turn_id": "overlapping-turn"}})
        records[8]["payload"].pop("turn_id")
        for record in records:
            record.pop("ordinal", None)
    row = one(load_records(tmp_path, records), "llm_call", response_id=target)
    assert (row["duration_ms"], row["latency_basis"]) == (None, None)


@pytest.mark.parametrize("value", [None, 123, {}, ""])
def test_missing_invalid_metadata_is_null(tmp_path, value):
    records = telemetry_records()
    records[2]["payload"].update(trace_id=value, root_turn_id=value)
    records[3]["payload"]["summary"] = value
    records[6]["payload"]["phase"] = value
    records[14]["payload"]["item"].update(pluginId=value, readOnlyHint=value)
    store = load_records(tmp_path, records)
    assert one(store, "message", message_class="assistant_text")["phase"] is None
    turn = one(store, "turn", turn_key="telemetry-turn")
    assert all(turn[name] is None for name in ("reasoning_summary", "trace_id", "root_turn_key"))
    op = one(store, "tool_op", item_uid="synthetic-mcp-op")
    assert (op["mcp_plugin_id"], op["mcp_read_only"]) == (None, None)


def test_unknown_summary_mode_and_string_boolean_are_not_evidence(tmp_path):
    records = telemetry_records()
    records[3]["payload"]["summary"] = "Synthetic reasoning text is not a mode."
    records[14]["payload"]["item"]["readOnlyHint"] = "false"
    store = load_records(tmp_path, records)
    assert one(store, "turn", turn_key="telemetry-turn")["reasoning_summary"] is None
    assert one(store, "tool_op", item_uid="synthetic-mcp-op")["mcp_read_only"] is None


def test_telemetry_enrichment_latest_nonnull_preserves_content_and_keys(tmp_path):
    records = telemetry_records()
    store = load_records(tmp_path, records)
    keys = {table: set(rows) for table, rows in store.tables.items()}
    records[2]["payload"].update(trace_id="synthetic-trace-new", root_turn_id="synthetic-root-new")
    records[3]["payload"]["summary"] = "none"
    records[6]["payload"].update(phase="final_answer")
    records[6]["payload"]["content"][0]["text"] = "This must not replace immutable content."
    records[14]["payload"]["item"].update(pluginId="synthetic-plugin-new", readOnlyHint=True)
    path = tmp_path / "enrichment.jsonl"
    path.write_text("".join(json.dumps(record) + "\n" for record in records))
    load(path.name, path=path, store=store, batch_lines=1)
    assert {table: set(rows) for table, rows in store.tables.items()} == keys
    message = one(store, "message", message_class="assistant_text")
    assert (message["phase"], message["text"]) == ("final_answer", "Synthetic telemetry reply.")
    turn = one(store, "turn", turn_key="telemetry-turn")
    assert (turn["reasoning_summary"], turn["trace_id"], turn["root_turn_key"]) == (
        "none", "synthetic-trace-new", "synthetic-root-new")
    op = one(store, "tool_op", item_uid="synthetic-mcp-op")
    assert (op["mcp_plugin_id"], op["mcp_read_only"]) == ("synthetic-plugin-new", True)
    records[6]["payload"].pop("phase")
    records[14]["payload"]["item"].update(pluginId=None, readOnlyHint=False)
    path.write_text("".join(json.dumps(record) + "\n" for record in records))
    load(path.name, path=path, store=store, batch_lines=1)
    assert one(store, "message", message_class="assistant_text")["phase"] == "final_answer"
    op = one(store, "tool_op", item_uid="synthetic-mcp-op")
    assert (op["mcp_plugin_id"], op["mcp_read_only"]) == ("synthetic-plugin-new", False)


def test_telemetry_does_not_change_content_keys_or_classification():
    # Captured from the frozen pre-telemetry parser over this exact synthetic fixture. Comparing
    # the same bytes preserves offsets and MCP result_json, unlike removing metadata from input.
    store, _ = load("nullable_telemetry.jsonl", batch_lines=1)
    surface = {table: sorted(json.dumps({name: value for name, value in row.items()
                                        if name not in TELEMETRY.get(table, set())},
                                       sort_keys=True, default=str) for row in rows.values())
               for table, rows in store.tables.items() if table != "record_type_seen"}
    assert hashlib.sha256(json.dumps(surface, sort_keys=True).encode()).hexdigest() == (
        "5a73a28860e927e396717458dc1992f6d2d4abcff3953293c7d1e5e1bcdc3eb8")


@pytest.mark.parametrize("batch", [1, 2, 7])
def test_nullable_telemetry_resume_each_boundary(batch):
    whole, state = load("nullable_telemetry.jsonl")
    parts, parts_state = load("nullable_telemetry.jsonl", batch_lines=batch)
    assert snapshot(parts) == snapshot(whole)
    assert parts_state == state
    for cut in range(1, len(telemetry_records())):
        store = MemStore()
        c = ctx(FIX / "nullable_telemetry.jsonl")
        end, saved = run_file(CodexParser, c, store, stop_after_lines=cut, batch_lines=batch)
        run_file(CodexParser, c, store, start=end, state=saved, line_base=cut, batch_lines=batch)
        assert snapshot(store) == snapshot(whole), cut


# --- session identity --------------------------------------------------------------------------


def test_first_session_meta_wins_in_fork():
    store, _ = load("fork_legacy_child.jsonl")
    sessions = store.rows("session")
    assert [s["session"] for s in sessions] == [SessionKey("codex", C, "")]
    s = sessions[0]
    assert s["parent_session_uid"] == P and s["root_session_uid"] == P and s["forked_from_uid"] == P
    assert s["spawn_kind"] == "codex_fork" and s["spawn_depth"] == 1 and s["is_subagent"] is True
    assert s["agent_path"] == "/root/lane_one" and s["agent_role"] == "worker"
    assert s["agent_type"] is None and s["agent_type_source"] is None


def test_type_provenance_from_spawn_request_and_child_metadata(tmp_path):
    parent, _ = load("main_v145.jsonl")
    requested = one(parent, "subagent_spawn", spawn_uid="call_spawn1")
    assert (requested["requested_type"], requested["requested_type_source"]) == ("worker", "explicit")
    records = [json.loads(line) for line in (FIX / "main_v145.jsonl").read_text().splitlines()]
    for record in records:
        payload = record.get("payload", {})
        if payload.get("name") == "spawn_agent":
            arguments = json.loads(payload["arguments"])
            arguments.pop("agent_type")
            payload["arguments"] = json.dumps(arguments)
    default_path = tmp_path / "default.jsonl"
    default_path.write_text("".join(json.dumps(record) + "\n" for record in records))
    default, _ = load("default.jsonl", path=default_path)
    requested = one(default, "subagent_spawn", spawn_uid="call_spawn1")
    assert (requested["requested_type"], requested["requested_type_source"]) == ("default", "default")

    source = FIX / "fork_legacy_child.jsonl"
    records = [json.loads(line) for line in source.read_text().splitlines()]
    records[0]["payload"]["source"]["subagent"]["thread_spawn"]["agent_type"] = "reviewer"
    target = tmp_path / "typed.jsonl"
    target.write_text("".join(json.dumps(record) + "\n" for record in records))
    typed, _ = load("typed.jsonl", path=target)
    child = one(typed, "session", session=SessionKey("codex", C, ""))
    assert (child["agent_type"], child["agent_type_source"]) == ("reviewer", "explicit")


def test_main_session_fields_and_git_start():
    store, _ = load("main_v145.jsonl")
    s = one(store, "session", session=SessionKey("codex", P, ""))
    assert s["entrypoint"] == "codex-tui" and s["cli_version_first"] == "0.145.0"
    assert s["git_branch"] == "main" and s["git_commit_start"] == "abcdef1234567890"
    assert s["spawn_kind"] is None and s["is_subagent"] is False
    g = one(store, "git_event", op="session_start")
    assert g["evidence"] == "session_meta" and g["sha_short"] == "abcdef123456"


# --- fork replay ---------------------------------------------------------------------------------


def test_legacy_fork_burst_replay_skipped_and_counted():
    store, state = load("fork_legacy_child.jsonl")
    s = store.rows("session")[0]
    assert s["inherited_skipped"] == 8
    turns = store.rows("turn")
    assert [t["turn_key"] for t in turns] == ["turnC1"]
    t = turns[0]
    assert t["origin"] == "subagent_brief" and t["status"] == "complete"
    assert t["model"] == "model-b" and t["effort"] == "medium"  # held turn_context from the burst tail
    classes = sorted(m["message_class"] for m in store.rows("message") if m["message_class"] in V3_CLASSES)
    assert classes == ["assistant_text", "subagent_report"]  # no replayed parent text, no child "prompt"
    # v4: the child's own brief inside the burst tail is an agent_message, not a prompt
    brief = one(store, "message", message_class="agent_message")
    assert brief["role"] == "user" and brief["detail"] == {"source": "user_message"} and brief["turn_key"] == "turnC1"
    assert not store.rows("tool_call") and not store.rows("subagent_spawn")


def test_paginated_fork_ordinal_replay_and_thread_filter():
    store, _ = load("fork_paginated_child.jsonl")
    s = one(store, "session", session=SessionKey("codex", D, ""))
    assert s["inherited_skipped"] == 4  # ordinals 1-3 plus one item stamped with the parent's thread_id
    assert [r["response_id"] for r in store.rows("llm_call")] == ["resp_d1"]
    assert not store.rows("compaction") and not store.rows("tool_op")
    assert [m["message_class"] for m in store.rows("message") if m["message_class"] in V3_CLASSES] == ["subagent_report"]
    assert not store.rows("tool_io") and not store.rows("session_continuation")  # replayed op, subagent fork


# --- tokens --------------------------------------------------------------------------------------


def test_legacy_token_dedupe_and_normalisation():
    store, _ = load("main_v145.jsonl")
    calls = sorted(store.rows("llm_call"), key=lambda r: r["byte_offset"])
    assert len(calls) == 2  # the repeated identical sample and the info=None sample are not calls
    first, second = calls
    assert first["input_uncached"] == 60 and first["cache_read"] == 40 and first["output"] == 10
    assert second["input_uncached"] == 90 and second["cache_read"] == 60 and second["reasoning"] == 5
    assert first["response_id"].startswith("codex-legacy:" + P)
    assert first["model"] == "model-a" and first["context_window"] == 258000


def test_fork_legacy_tokens_baseline_at_inherited_total():
    store, state = load("fork_legacy_child.jsonl")
    calls = store.rows("llm_call")
    assert len(calls) == 1 and calls[0]["output"] == 30 and calls[0]["input_uncached"] == 100
    assert state["base"] == 280  # inherited total, never subtracted into a call
    assert calls[0]["turn_key"] == "turnC1"


def test_token_usage_record_per_response_and_turn_sum():
    store, _ = load("main_v155.jsonl")
    calls = {r["response_id"]: r for r in store.rows("llm_call")}
    assert set(calls) == {"resp_b1", "resp_b2"}  # token_count ignored once records exist
    assert calls["resp_b2"]["input_uncached"] == 100 and calls["resp_b2"]["cache_write_5m"] == 100
    total_in = sum(c["input_uncached"] + c["cache_read"] + (c["cache_write_5m"] or 0) for c in calls.values())
    assert total_in == 2200 and sum(c["output"] for c in calls.values()) == 120
    assert calls["resp_b1"]["model"] == "model-n" and calls["resp_b1"]["context_window"] == 400000


def test_codex_cache_write_zero_is_known_and_priced_as_the_5m_write():
    # b1 reports no cache writes: 0 is a known value, not NULL; Codex has no TTL split so 1h is 0
    store, _ = load("main_v155.jsonl")
    calls = {r["response_id"]: r for r in store.rows("llm_call")}
    assert (calls["resp_b1"]["cache_write_5m"], calls["resp_b1"]["cache_write_1h"]) == (0, 0)
    assert (calls["resp_b2"]["cache_write_5m"], calls["resp_b2"]["cache_write_1h"]) == (100, 0)


# --- messages and turns --------------------------------------------------------------------------


@pytest.mark.parametrize("version", ["0.145.0", "0.156.0"])
@pytest.mark.parametrize(
    "tag,cls",
    [
        ("system-reminder", "system_reminder"),
        ("environment_context", "context_injection"),
        ("hook_prompt", "hook_output"),
        ("skill", "skill_body"),
    ],
)
@pytest.mark.parametrize("mixed", [False, True])
def test_user_injections_are_not_human_prompts(tmp_path, version, tag, cls, mixed):
    block = f"<{tag}>syntheticboundaryneedle</{tag}>"
    text = f"Human request before.\n{block}\nHuman request after." if mixed else block
    records = [
        {"type": "session_meta", "payload": {"id": P, "cli_version": version, "source": "cli"}},
        {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "boundary-turn"}},
        {
            "type": "response_item",
            "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]},
        },
        {"type": "event_msg", "payload": {"type": "user_message", "message": text}},
    ]
    if version == "0.156.0":
        records[-1]["payload"] = {
            "type": "item_completed",
            "thread_id": P,
            "turn_id": "boundary-turn",
            "item": {"type": "UserMessage", "id": "boundary", "content": [{"type": "text", "text": text}]},
        }
    for record in records:
        record["timestamp"] = "2026-10-01T10:00:00Z"
    path = tmp_path / "boundary.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in records))
    store, _ = load(path.name, path=path, batch_lines=1)
    prompts = by(store, "message", message_class="human_prompt")
    assert len(prompts) == int(mixed)
    if mixed:
        assert prompts[0]["text"] == "Human request before.\n\nHuman request after."
    assert not any("syntheticboundaryneedle" in m["text"] for m in prompts)
    injected = one(store, "message", message_class=cls)
    assert injected["text"] == block
    assert injected["detail"]["source"] == tag
    assert (one(store, "session")["first_human_at"] is not None) == mixed



def test_prompt_source_by_version():
    old, _ = load("main_v145.jsonl")
    prompts = sorted(m["text"] for m in by(old, "message", message_class="human_prompt"))
    assert prompts == ["synthetic human prompt one", "synthetic human prompt two"]
    assert all("injected" not in m["text"] for m in old.rows("message") if m["message_class"] in V3_CLASSES)
    assert one(old, "message", message_class="context_injection")["detail"] == {"source": "environment_context"}
    new, _ = load("main_v155.jsonl")
    prompt = one(new, "message", message_class="human_prompt")
    assert prompt["text"] == "synthetic modern prompt" and prompt["turn_key"] == "turnB1"
    assert prompt["event_uid"] == f"{N}:4"
    assert len(by(new, "message", message_class="assistant_text")) == 1  # AgentMessage item not duplicated
    assert len(by(old, "message", message_class="assistant_text")) == 1  # event agent_message not duplicated


def test_turn_fields_and_session_events():
    store, _ = load("main_v145.jsonl")
    t1 = one(store, "turn", turn_key="turnA1")
    assert t1["origin"] == "human" and t1["status"] == "complete" and t1["duration_ms"] == 20000
    assert t1["ttft_ms"] == 900 and t1["sandbox_type"] == "danger-full-access" and t1["permission_mode"] == "full"
    t2 = one(store, "turn", turn_key="turnA2")
    assert t2["status"] == "aborted" and t2["abort_reason"] == "interrupted"
    kinds = sorted(e["kind"] for e in store.rows("session_event"))
    assert kinds == ["effort_change", "image_attach", "interrupt", "model_switch"]
    s = store.rows("session")[0]
    assert s["first_human_at"] < s["last_human_at"]
    new, _ = load("main_v155.jsonl")
    assert one(new, "turn", turn_key="turnB1")["status"] == "error"
    assert {e["kind"] for e in new.rows("session_event")} == {"image_attach", "api_error"}
    assert one(new, "session_event", kind="api_error")["value"] == "server_overloaded"


# --- tools, spawns, artifacts, git ---------------------------------------------------------------


def test_tool_calls_and_outcomes():
    store, _ = load("main_v145.jsonl")
    ex = one(store, "tool_call", call_uid="call_exec1")
    assert ex["tool_family"] == "custom" and ex["outcome"] == "ok" and ex["duration_ms"] == 2000
    assert ex["meta"] is None  # exec input is a script: never mined for a verb
    sh = one(store, "tool_call", call_uid="call_sh1")
    assert sh["outcome"] == "interrupted" and sh["meta"] == {"cmd_verb": "git"}
    sp = one(store, "tool_call", call_uid="call_spawn1")
    assert sp["tool_family"] == "collaboration" and sp["codex_namespace"] == "collaboration"
    assert [i.kind for i in store.issues] == ["orphan_output", "unknown_type"]


def test_spawn_merges_activity():
    store, _ = load("main_v145.jsonl")
    sp = one(store, "subagent_spawn", spawn_uid="call_spawn1")
    assert sp["child_task_name"] == "lane_one" and sp["requested_model"] == "model-b"
    assert sp["fork_scope"] == "all" and sp["launch_status"] == "launched"
    assert sp["child_session_uid"] == C and sp["completion_status"] == "completed"
    assert sp["completed_at"] is not None and sp["spawned_at"] < sp["completed_at"]
    brief = one(store, "message", message_class="subagent_brief")
    assert brief["text"] == "synthetic brief for lane one" and brief["session"].session_uid == P


def test_tool_ops_git_and_artifacts():
    store, _ = load("main_v155.jsonl")
    ce = one(store, "tool_op", item_uid="ce-1")
    assert ce["cmd_verb"] == "git" and ce["duration_ms"] == 1500 and ce["is_error"] is False
    assert ce["parsed_cmd_types"] == ["unknown"] and ce["output_bytes"] > 0
    mcp = one(store, "tool_op", item_uid="mcp-1")
    assert mcp["mcp_server"] == "context7" and mcp["is_error"] is True
    collab = one(store, "tool_op", item_uid="call_wait1")
    assert collab["call_uid"] == "call_wait1" and collab["link_method"] == "item_id"
    assert one(store, "tool_op", item_uid="fc-1")["file_count"] == 2
    ops = sorted((g["op"], g["sha_short"]) for g in store.rows("git_event"))
    assert ops == [("commit", "1a2b3c4"), ("push", "1a2b3c4"), ("session_start", "0123456789ab")]
    arts = sorted((a["action"], a["path"]) for a in store.rows("artifact"))
    assert arts == [("add", "/tmp/synthetic-repo/b.md"), ("move", "/tmp/synthetic-repo/new.txt")]
    comp = one(store, "compaction", window_id="w2")
    assert comp["window_number"] == 2 and comp["pre_tokens"] == 1200
    old, _ = load("main_v145.jsonl")
    arts = sorted((a["action"], a["evidence_type"]) for a in old.rows("artifact"))
    assert arts == [("add", "patch_apply"), ("linked", "assistant_link"), ("update", "patch_apply")]


def test_rate_limit_only_on_change():
    store, _ = load("main_v145.jsonl")
    primary = sorted(r["used_percent"] for r in by(store, "rate_limit_sample", window_kind="primary"))
    assert primary == [5.0, 7.0]
    assert len(by(store, "rate_limit_sample", window_kind="credits")) == 1


# --- incremental resumption and identity ---------------------------------------------------------


@pytest.mark.parametrize("name", ["main_v145.jsonl", "main_v155.jsonl", "fork_legacy_child.jsonl",
                                  "fork_paginated_child.jsonl", "v4_modern.jsonl", "v4_legacy.jsonl",
                                  "v4_sub.jsonl"])
@pytest.mark.parametrize("batch", [1, 2, 3, 7])
def test_incremental_equals_one_shot(name, batch):
    whole, whole_state = load(name)
    parts, parts_state = load(name, batch_lines=batch)
    assert snapshot(parts) == snapshot(whole)
    assert parts_state == whole_state


def test_resume_from_saved_offset_equals_one_shot():
    name = "fork_legacy_child.jsonl"
    whole, _ = load(name)
    store = MemStore()
    c = ctx(FIX / name)
    for cut in range(1, 16):
        store = MemStore()
        end, state = run_file(CodexParser, c, store, stop_after_lines=cut)
        run_file(CodexParser, c, store, start=end, state=state, line_base=cut)
        assert snapshot(store) == snapshot(whole), cut


def test_archived_copy_has_identical_keys(tmp_path):
    archived = tmp_path / "rollout-archived.jsonl"
    shutil.copy(FIX / "main_v155.jsonl", archived)
    store, _ = load("main_v155.jsonl")
    before = {t: set(rows) for t, rows in store.tables.items()}
    load("main_v155.jsonl", store=store, rel="codex-local/archived_sessions/rollout-archived.jsonl",
         path=archived)
    after = {t: set(rows) for t, rows in store.tables.items()}
    assert {t: k for t, k in after.items() if t != "parse_issue"} == \
        {t: k for t, k in before.items() if t != "parse_issue"}


def test_state_is_json_and_bounded():
    import json
    _, state = load("main_v145.jsonl")
    json.dumps(state)
    assert set(state["calls"]) == set()  # every call closed
    assert "lane_one" in state["spawns"]


# --- parser v4: content surface ------------------------------------------------------------------


def test_v4_injected_context_classes():
    store, _ = load("v4_modern.jsonl")
    sp = one(store, "message", message_class="system_prompt")
    assert sp["event_uid"] == f"{M}:system_prompt" and sp["role"] == "system"
    assert sp["detail"] == {"source": "base_instructions", "provenance": {"type": "model", "model": "model-x"}}
    rows = {m["event_uid"]: m for m in store.rows("message")}
    dev = [rows[f"{M}:5:{n}"] for n in range(3)]
    assert [(m["role"], m["message_class"], m["detail"]["source"]) for m in dev] == [
        ("system", "context_injection", "permissions"), ("system", "context_injection", "developer"),
        ("system", "interrupt_marker", "turn_aborted")]
    user = [rows[f"{M}:6:{n}"] for n in range(3)]
    assert [(m["role"], m["message_class"], m["detail"]["source"]) for m in user] == [
        ("user", "context_injection", "agents_md"), ("user", "context_injection", "environment_context"),
        ("user", "skill_body", "skill")]
    assert user[2]["detail"]["name"] == "synthetic-skill"
    assert len(by(store, "message", message_class="context_injection", detail={"source": "world_state", "full": True})) == 1
    am = one(store, "message", message_class="agent_message")
    assert am["text"] == "synthetic lane message" and am["detail"]["author"] == "/root/lane_x"
    assert not [m for m in store.rows("message") if "image name=" in m["text"] or m["text"] == "</image>"]


def test_v4_prompt_copy_dedupe_origin_and_attachment():
    store, _ = load("v4_modern.jsonl")
    prompt = one(store, "message", message_class="human_prompt")
    assert prompt["prompt_origin"] == "skill" and prompt["event_uid"] == f"{M}:10"
    assert [m for m in store.rows("message") if m["text"].strip() == prompt["text"]] == [prompt]
    att = one(store, "attachment", source="prompt")
    assert att["event_uid"] == prompt["event_uid"] and att["file_name"] == "/tmp/synthetic.png"
    assert att["mime"] == "image/png" and att["size_bytes"] == 298  # metadata from the dropped copy's input_image
    legacy, _ = load("v4_legacy.jsonl")  # copy written AFTER the prompt record
    prompt = one(legacy, "message", message_class="human_prompt")
    assert prompt["prompt_origin"] == "typed" and len(legacy.rows("message")) == 4
    assert one(legacy, "attachment", source="prompt")["size_bytes"] == 298
    sub, _ = load("v4_sub.jsonl")  # subagent: the brief is an agent_message, never a human_prompt
    assert not by(sub, "message", message_class="human_prompt")
    briefs = sorted((m["text"], m["detail"]["source"]) for m in by(sub, "message", message_class="agent_message"))
    assert briefs == [("synthetic brief for lane s", "user_message"), ("synthetic follow-up", "agent_message")]


def test_v4_reasoning_one_row_per_item():
    store, _ = load("v4_modern.jsonl")
    r = one(store, "message", message_class="reasoning")
    assert r["event_uid"] == f"{M}:12" and r["role"] == "assistant" and r["model"] == "model-m"
    assert r["text"] == "synthetic reasoning part one\n\nsynthetic reasoning part two"
    assert r["detail"] == {"source": "response_item", "summary_parts": 2, "raw_parts": 0}
    legacy, _ = load("v4_legacy.jsonl")
    rows = sorted(by(legacy, "message", message_class="reasoning"), key=lambda m: m["byte_offset"])
    assert [m["text"] for m in rows] == ["synthetic legacy thought A\n\nsynthetic legacy thought B",
                                          "synthetic event-only thought"]
    assert rows[1]["detail"]["source"] == "agent_reasoning"  # the response_item had only encrypted content


def test_v4_tool_io_calls_ops_and_no_binary():
    store, _ = load("v4_modern.jsonl")
    io = {r["io_uid"]: r for r in store.rows("tool_io")}
    ex = io["call_exec_m"]
    assert ex["kind"] == "call" and ex["input_text"] == "const r = await tools.shell('ls');"
    assert ex["output_text"] == "Script completed\nWall time 1.0 seconds\nOutput:\na.txt …12 tokens truncated… b.txt"
    assert ex["output_truncated"] is True and ex["output_parts"] == [
        {"index": 2, "type": "input_image", "mime": "image/png", "bytes": 298}]
    assert one(store, "attachment", source="tool_result", call_uid="call_exec_m")["size_bytes"] == 298
    ce = io["item:exec-ce-1"]
    assert ce["kind"] == "op" and ce["item_uid"] == "exec-ce-1" and ce["call_uid"] is None
    assert ce["input_text"] == '["/bin/zsh", "-lc", "ls"]' and ce["output_text"] == "a.txt\nb.txt\n"
    assert ce["stdout_text"] is None and ce["stderr_text"] is None  # duplicate / empty
    assert "formatted_output" not in ce["result_json"] and '"exit_code": 0' in ce["result_json"]
    mcp = io["item:exec-mcp-1"]
    assert mcp["output_text"] == '{"a": 1}' and "structuredContent" not in mcp["result_json"]
    assert mcp["output_parts"][0]["bytes"] == 598
    assert io["item:ws_synthetic1"]["call_uid"] == "ws_synthetic1" and io["ws_synthetic1"]["kind"] == "call"
    assert "call_replayed" not in io
    for fixture in ("v4_modern.jsonl", "v4_legacy.jsonl"):
        dump = repr(load(fixture)[0].tables)
        assert "base64," not in dump and "iVBORw0KGgo" not in dump


def test_v4_end_events_enrich_calls_or_become_ops():
    store, _ = load("v4_legacy.jsonl")
    io = {r["io_uid"]: r for r in store.rows("tool_io")}
    patch = io["call_patch_l"]
    assert patch["stdout_text"] == "Success\n" and patch["output_text"] == "Success."
    op = io["item:exec-patch-1"]
    assert op["kind"] == "op" and op["tool_name"] == "FileChange" and op["item_uid"] == "exec-patch-1"
    mcp = io["call_mcp_l"]
    assert mcp["output_text"] == "synthetic mcp text" and "synthetic mcp text" not in mcp["result_json"]
    ws = io["ws_legacy1"]  # the id-less web_search_call joined the end event written just before it
    assert ws["input_text"] == '{"type": "search", "query": "synthetic q"}' and "synthetic title" in ws["output_text"]
    img = io["call_img_l"]
    assert img["output_parts"] == [{"index": 0, "type": "image", "mime": "image/png", "bytes": 598}]
    assert one(store, "attachment", call_uid="call_img_l")["file_name"] == "/tmp/generated/synthetic.png"


def test_v4_file_touches():
    store, _ = load("v4_modern.jsonl")
    touches = {t["touch_uid"]: t for t in store.rows("file_touch")}
    fc = touches["item:call_patch_fc:0"]  # FileChange with the call id wins over the patch input
    assert (fc["op"], fc["lines_added"], fc["lines_removed"], fc["tool"]) == ("edit", 2, 1, "FileChange")
    assert not [k for k in touches if k.startswith("call_patch_fc:")]
    from_input = sorted((t["op"], t["path"].rsplit("/", 1)[-1], t["move_from"] and t["move_from"].rsplit("/", 1)[-1],
                         t["lines_added"]) for k, t in touches.items() if k.startswith("call_patch_in:"))
    assert from_input == [("create", "c.txt", None, 2), ("delete", "f.txt", None, None), ("move", "e.txt", "d.txt", 1)]
    legacy, _ = load("v4_legacy.jsonl")
    got = sorted((t["touch_uid"], t["op"], t["lines_added"]) for t in legacy.rows("file_touch"))
    assert got == [("call_patch_l:0", "edit", 1), ("item:exec-patch-1:0", "create", 3)]  # failed patch: none


def test_v4_continuations_and_resume():
    store, _ = load("v4_modern.jsonl")
    cont = one(store, "session_continuation")
    assert (cont["child_uid"], cont["parent_uid"], cont["kind"], cont["evidence"]) == (M, PARENT, "fork", "forked_from_id")
    assert one(store, "session_event", kind="resume")["value"] == "0.156.0"
    assert [i.kind for i in store.issues] == ["late_session_meta"]  # v3 row kept
    sub, _ = load("fork_legacy_child.jsonl")
    assert not sub.rows("session_continuation")  # a subagent fork is a spawn, not a continuation


def _with_ce1(tmp_path, command: list[str], output: str, exit_code: int = 0) -> MemStore:
    """The v155 fixture with its git CommandExecution (ce-1) rewritten to the given command and output."""
    records = [json.loads(line) for line in (FIX / "main_v155.jsonl").read_text().splitlines()]
    for record in records:
        item = (record.get("payload") or {}).get("item")
        if isinstance(item, dict) and item.get("id") == "ce-1":
            item["command"], item["aggregated_output"], item["exit_code"] = command, output, exit_code
            item["status"] = "completed" if exit_code == 0 else "failed"
    path = tmp_path / "rewritten.jsonl"
    path.write_text("".join(json.dumps(record) + "\n" for record in records))
    return load("rewritten.jsonl", path=path)[0]


def _ce1_events(store: MemStore) -> list[tuple]:
    return sorted((g["event_uid"], g["op"], g["evidence"], g["sha_short"]) for g in store.rows("git_event")
                  if g["event_uid"].startswith("ce-1:"))


def test_quiet_git_commands_are_captured_by_command_text(tmp_path):
    store = _with_ce1(tmp_path, ["/bin/zsh", "-lc", 'git -C "/w" commit -qm x && git push -q'], "")
    assert _ce1_events(store) == [("ce-1:commit:cmd0", "commit", "command", None),
                                  ("ce-1:push:cmd0", "push", "command", None)]


def test_command_text_is_skipped_when_output_matched_or_the_command_failed(tmp_path):
    out = "[feature 1a2b3c4] synthetic\n   1111111..1a2b3c4  feature -> feature\n"
    matched = _with_ce1(tmp_path, ["/bin/zsh", "-lc", "git commit -qm x && git push"], out)
    assert [e[2] for e in _ce1_events(matched)] == ["output_regex", "output_regex"]
    failed = _with_ce1(tmp_path, ["/bin/zsh", "-lc", "git commit -qm x"], "nothing to commit", exit_code=1)
    assert _ce1_events(failed) == []

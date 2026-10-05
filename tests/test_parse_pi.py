"""pi session parser tests (parse_pi) over fixtures/pi.

The fixtures are synthetic: tests/fixtures/pi/generate.py builds every record by hand in the pi v3
session format and the pi-subagents shapes. See its docstring for what each session exercises.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from agent_history.memstore import MemStore, run_file
from agent_history.model import FileContext, SessionKey
from agent_history.parse_pi import PiArtifactParser, PiParser, lineage

FIX = Path(__file__).parent / "fixtures" / "pi"
REAL = FIX / "sessions" / "--example-project--"
ROOT_UID = "01900000-0000-7000-8000-00000000a001"
ASYNC_ROOT = f"2026-09-01T10-00-00-000Z_{ROOT_UID}"
MAPPER_DIR, WORKER_DIR = "6e000000-0000-4000-8000-000000000001", "6e000000-0000-4000-8000-000000000002"
MAPPER_RUN, WORKER_RUN = "5f000000-0000-4000-8000-000000000002", "5f000000-0000-4000-8000-000000000003"
WORKFLOW = "5f000000-0000-4000-8000-000000000001"
FAILED = "2026-09-01T09-00-00-000Z_01900000-0000-7000-8000-00000000a002.jsonl"
LISTED = "2026-09-01T09-30-00-000Z_01900000-0000-7000-8000-00000000a003.jsonl"
FAUX = REAL / "2026-09-01T11-00-00-000Z_01900000-0000-7000-8000-00000000a004.jsonl"


def parse(path: Path, batch_lines: int = 5000) -> MemStore:
    rel = "pi-local/" + str(path.relative_to(FIX))
    role = "main" if len(path.relative_to(FIX).parts) == 3 else "subagent"
    store = MemStore()
    run_file(PiParser, FileContext(str(path), rel, "pi-local", "pi", "local", None, role), store,
             batch_lines=batch_lines)
    return store


def child(run_dir: str) -> Path:
    return REAL / ASYNC_ROOT / run_dir / "run-0" / "session.jsonl"


def by(store: MemStore, table: str, **match) -> list[dict]:
    return [r for r in store.rows(table) if all(r.get(k) == v for k, v in match.items())]


def one(store: MemStore, table: str, **match) -> dict:
    rows = by(store, table, **match)
    assert len(rows) == 1, (table, match, len(rows))
    return rows[0]


# --- inventory ---------------------------------------------------------------------------------


def test_inventory_parses_sessions_children_and_artifact_evidence(tmp_path):
    pytest.importorskip("psycopg")
    from agent_history import load
    shutil.copytree(FIX, tmp_path / "hot" / "pi-local")
    entries = load.inventory(tmp_path / "hot", None)
    roles = sorted((e.role, e.logical_uid) for e in entries.values())
    assert [r for r, _ in roles] == ["main"] * 4 + ["pi_artifact"] * 2 + ["subagent"] * 2
    assert (("subagent", f"{ROOT_UID}/{MAPPER_DIR}/run-0") in roles
            and ("main", ROOT_UID) in roles)
    assert sum("subagent-artifacts" in rel for rel in entries) == 2  # metadata-only parser avoids double count
    assert {e.agent for e in entries.values()} == {"pi"} and load.parser_for("pi")[0] is PiParser
    assert load.parser_for("pi", "pi_artifact")[0] is PiArtifactParser


@pytest.mark.parametrize("details,text,expected", [
    ({"truncation": {"truncated": True}}, "prefix", True),
    (None, "prefix\n\n[5 more lines in file. Use offset=3 to continue.]", True),
    ({"truncation": {"truncated": False, "outputLines": 2, "totalLines": 7}}, "prefix", True),
    ({"truncation": {"truncated": False, "outputLines": 2, "totalLines": 2}}, "complete", False),
])
def test_real_parser_projects_source_output_completeness(tmp_path, details, text, expected):
    path = tmp_path / "read.jsonl"
    records = [
        {"type": "session", "id": ROOT_UID, "timestamp": "2026-10-04T12:00:00Z", "cwd": "/tmp/synthetic", "version": 3},
        {"type": "message", "id": "call", "timestamp": "2026-10-04T12:00:00Z", "message": {
            "role": "assistant", "stopReason": "toolUse", "content": [{"type": "toolCall", "id": "read", "name": "read",
                "arguments": {"path": "/tmp/synthetic/launch.txt"}}]}},
        {"type": "message", "id": "result", "timestamp": "2026-10-04T12:00:00Z", "message": {
            "role": "toolResult", "toolCallId": "read", "toolName": "read", "isError": False,
            "content": [{"type": "text", "text": text}], "details": details}},
    ]
    path.write_text("".join(json.dumps(record) + "\n" for record in records))
    store = MemStore()
    run_file(PiParser, FileContext(str(path), "pi-test/sessions/slug/read.jsonl", "pi-test", "pi", "test", None, "main"), store)
    io = one(store, "tool_io", io_uid="read")
    assert io["output_truncated"] is expected
    assert io["output_text"] == text
    assert json.loads(io["result_json"]) == details if details is not None else io["result_json"] is None


def test_lineage_of_nested_children():
    rel = f"pi-local/sessions/slug/{ASYNC_ROOT}/{MAPPER_DIR}/run-0/session/{WORKER_DIR}/run-1/session.jsonl"
    assert lineage(rel) == {"root": ROOT_UID, "depth": 2, "task": f"{WORKER_DIR}/run-1",
                            "path": f"{ROOT_UID}/{MAPPER_DIR}/run-0/{WORKER_DIR}/run-1"}


def test_async_launch_types_and_artifact_links_are_structural(tmp_path):
    root_uid = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    runs = ("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb", "cccccccc-cccc-4ccc-8ccc-cccccccccccc")
    dirs = ("dddddddd-dddd-4ddd-8ddd-dddddddddddd", "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee")
    types = ("mapper", "lane-worker")
    base = tmp_path / "pi-local" / "sessions" / "-synthetic-"
    base.mkdir(parents=True)
    root_base = f"2026-09-28T07-27-09-000Z_{root_uid}"
    at = "2026-09-28T07:27:09Z"

    def write(path, records):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(row) + "\n" for row in records))

    root = base / f"{root_base}.jsonl"
    records = [{"type": "session", "id": root_uid, "timestamp": at, "cwd": "/tmp/synthetic", "version": 3}]
    for index, (run, directory, agent_type) in enumerate(zip(runs, dirs, types)):
        records += [
            {"type": "message", "id": f"a{index}", "timestamp": at,
             "message": {"role": "assistant", "timestamp": at, "model": "test", "responseId": f"root-{index}",
                         "stopReason": "toolUse", "usage": {"input": 1, "output": 1},
                         "content": [{"type": "toolCall", "id": f"call-{index}", "name": "subagent",
                                      "arguments": {"agent": agent_type, "task": "synthetic brief", "async": True}}]}},
            {"type": "message", "id": f"r{index}", "timestamp": at,
             "message": {"role": "toolResult", "timestamp": at, "toolCallId": f"call-{index}",
                         "toolName": "subagent", "isError": False, "content": [],
                         "details": {"mode": "async", "runId": run, "results": []}}},
            {"type": "custom_message", "id": f"n{index}", "timestamp": at,
             "customType": "subagent-notify",
             "content": (f"run {run} finished at /{directory}/run-0/session.jsonl"
                         if index == 0 else f"run {run} finished")},
        ]
    write(root, records)
    store = MemStore()
    root_rel = f"pi-local/sessions/-synthetic-/{root.name}"
    run_file(PiParser, FileContext(str(root), root_rel, "pi-local", "pi", "local", None, "main"), store)
    spawns = {row["spawn_uid"]: row for row in store.rows("subagent_spawn")}
    assert set(spawns) == {"call-0", "call-1"}
    for index, (agent_type, directory) in enumerate(zip(types, dirs)):
        assert spawns[f"call-{index}"]["requested_type"] == agent_type
        assert spawns[f"call-{index}"]["requested_type_source"] == "explicit"
        # A run id and a path in free text do not establish a pair; the artifact response does.
        assert spawns[f"call-{index}"]["child_task_name"] is None

    untyped_dir = "ffffffff-ffff-4fff-8fff-ffffffffffff"
    for index, directory in enumerate((*dirs, untyped_dir)):
        child = base / root_base / directory / "run-0" / "session.jsonl"
        write(child, [{"type": "session", "id": f"00000000-0000-4000-8000-00000000000{index}",
                       "timestamp": at, "cwd": "/tmp/synthetic", "version": 3},
                      {"type": "session_info", "id": f"i{index}", "timestamp": at,
                       "name": "WIRE-HARNESS: display label"},
                      {"type": "message", "id": f"m{index}", "timestamp": at,
                       "message": {"role": "assistant", "timestamp": at, "model": "test",
                                   "responseId": f"child-{index}", "stopReason": "stop",
                                   "usage": {"input": 1, "output": 1}, "content": []}}])
        rel = f"pi-local/sessions/-synthetic-/{root_base}/{directory}/run-0/session.jsonl"
        child_store = MemStore()
        run_file(PiParser, FileContext(str(child), rel, "pi-local", "pi", "local", None, "subagent"),
                 child_store)
        assert child_store.rows("session")[0]["agent_type"] is None

    for index, (run, agent_type) in enumerate(zip(runs, types)):
        artifact = base / "subagent-artifacts" / f"{run}_{agent_type}_transcript.jsonl"
        write(artifact, [{"version": 1, "recordType": "message", "runId": run, "agent": agent_type,
                          "timestamp": at, "message": {"role": "assistant", "responseId": f"child-{index}"}}])
        artifact_store = MemStore()
        rel = f"pi-local/sessions/-synthetic-/subagent-artifacts/{artifact.name}"
        run_file(PiArtifactParser, FileContext(str(artifact), rel, "pi-local", "pi", "local", None,
                                               "pi_artifact"), artifact_store)
        evidence = artifact_store.rows("pi_run_response")
        assert len(evidence) == 1 and evidence[0]["agent_type"] == agent_type
        assert artifact_store.rows("message") == [] and artifact_store.rows("llm_call") == []


def test_workflow_notification_label_never_becomes_agent_type(tmp_path):
    root_uid = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    workflow = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
    run = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
    at = "2026-09-28T07:27:09Z"
    path = tmp_path / "2026-09-28T07-27-09-000Z_root.jsonl"
    path.write_text("".join(json.dumps(row) + "\n" for row in [
        {"type": "session", "id": root_uid, "timestamp": at, "cwd": "/tmp/synthetic", "version": 3},
        {"type": "custom_message", "id": "notice", "timestamp": at,
         "customType": "subagent-incremental-child-notify",
         "content": f"Workflow run: {workflow}\nChild run: {run}\nWorkflow child completed: **WIRE-HARNESS**"},
    ]))
    store = MemStore()
    context = FileContext(str(path), f"pi-local/sessions/-synthetic-/{path.name}", "pi-local",
                          "pi", "local", None, "main")
    run_file(PiParser, context, store)
    spawn = one(store, "subagent_spawn", spawn_uid=run)
    assert spawn["name"] == "WIRE-HARNESS"
    assert spawn["requested_type"] is None and spawn["requested_type_source"] is None


def test_sync_subagent_result_does_not_duplicate_direct_spawn(tmp_path):
    at = "2026-09-28T07:27:09Z"
    path = tmp_path / "sync.jsonl"
    path.write_text("".join(json.dumps(row) + "\n" for row in [
        {"type": "session", "id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", "timestamp": at,
         "cwd": "/tmp/synthetic", "version": 3},
        {"type": "message", "id": "a", "timestamp": at,
         "message": {"role": "assistant", "timestamp": at, "model": "test", "responseId": "root-a",
                     "stopReason": "toolUse", "usage": {"input": 1, "output": 1},
                     "content": [{"type": "toolCall", "id": "call-a", "name": "subagent",
                                  "arguments": {"agent": "mapper", "task": "synthetic brief"}}]}},
        {"type": "message", "id": "r", "timestamp": at,
         "message": {"role": "toolResult", "timestamp": at, "toolCallId": "call-a",
                     "toolName": "subagent", "isError": False, "content": [],
                     "details": {"mode": "sync", "runId": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
                                 "results": [{"agent": "mapper", "index": 0}]}}},
    ]))
    store = MemStore()
    run_file(PiParser, FileContext(str(path), f"pi-local/sessions/-synthetic-/{path.name}",
                                   "pi-local", "pi", "local", None, "main"), store)
    assert len(store.rows("subagent_spawn")) == 1
    assert store.rows("subagent_spawn")[0]["requested_type"] == "mapper"


# --- root and child linkage ----------------------------------------------------------------------


def test_async_workflow_root_spawns_and_links_its_children_by_path():
    root = parse(REAL / f"{ASYNC_ROOT}.jsonl")
    assert one(root, "session")["session"] == SessionKey("pi", ROOT_UID, "")
    call = one(root, "tool_call", call_uid=next(c["call_uid"] for c in root.rows("tool_call")
                                                if (c["meta"] or {}).get("workflow")))
    assert call["meta"]["run_id"] == WORKFLOW and call["background"] is True
    spawns = {s["spawn_uid"]: s for s in root.rows("subagent_spawn")}
    assert set(spawns) == {MAPPER_RUN, WORKER_RUN}
    assert {s["workflow_id"] for s in spawns.values()} == {call["meta"]["run_id"]}
    assert {s["turn_key"] for s in spawns.values()} == {call["turn_key"]}
    assert all(s["completion_status"] == "completed" and s["spawned_at"] == call["started_at"]
               for s in spawns.values())
    # Free text may include a path, but it does not prove a run/path pairing.
    assert all(s["child_task_name"] is None for s in spawns.values())

    mapper, worker = parse(child(MAPPER_DIR)), parse(child(WORKER_DIR))
    m, w = one(mapper, "session"), one(worker, "session")
    for s, run_dir in ((m, MAPPER_DIR), (w, WORKER_DIR)):
        assert s["is_subagent"] and s["spawn_kind"] == "pi_subagent" and s["spawn_depth"] == 1
        assert s["parent_session_uid"] == ROOT_UID == s["root_session_uid"]
        assert s["agent_type"] is None and s["agent_path"] == f"{ROOT_UID}/{run_dir}/run-0"
    assert one(mapper, "message", message_class="subagent_report")["text"].startswith("`ls -la`")
    assert one(worker, "turn")["origin"] == "subagent_brief"


def test_per_call_model_and_effort():
    root, mapper, worker = parse(REAL / f"{ASYNC_ROOT}.jsonl"), parse(child(MAPPER_DIR)), parse(child(WORKER_DIR))
    assert {(c["model"], c["effort"]) for c in root.rows("llm_call")} == {("model-large", "medium")}
    assert {(c["model"], c["effort"]) for c in mapper.rows("llm_call")} == {("model-small", "medium")}
    assert {(c["model"], c["effort"]) for c in worker.rows("llm_call")} == {("model-small", "max")}
    first = min(root.rows("llm_call"), key=lambda c: c["ts"])
    assert first["response_id"].startswith("resp_") and (first["input_uncached"], first["cache_read"],
                                                         first["output"], first["reasoning"]) == (1500, 0, 56, 31)


def test_waiting_turn_then_notification_turn():
    root = parse(REAL / f"{ASYNC_ROOT}.jsonl")
    turns = sorted(root.rows("turn"), key=lambda t: t["started_at"])
    assert [(t["origin"], t["status"]) for t in turns] == [("human", "completed"), ("task_notification", "completed")]
    # the two incremental notifies after the last reply opened no turn of their own
    notes = by(root, "message", message_class="task_notification_summary")
    assert len(notes) == 3 and len({n["turn_key"] for n in notes}) == 2
    assert one(root, "message", message_class="human_prompt")["prompt_origin"] == "typed"


# --- errors, compaction, custom-message origins, usage entries -----------------------------------


def test_error_calls_are_failed_llm_calls_and_context_edits_do_not_break_parsing():
    store = parse(REAL / FAILED)
    errors = by(store, "llm_call", is_api_error=True)
    assert len(errors) == 5 and not store.issues
    assert sorted((e["error_kind"], e["api_error_status"]) for e in errors) == \
        [("http_error", 400)] + [("upstream_request_timeout", None)] * 4
    assert len(by(store, "session_event", kind="context_edit")) == 4
    [turn] = store.rows("turn")
    assert turn["status"] == "error" and all(e["turn_key"] == turn["turn_key"] for e in errors)


def test_faux_session_compaction_turn_origins_and_thinking_change():
    store = parse(FAUX)
    comp = one(store, "compaction")
    assert comp["pre_tokens"] > 0
    assert one(store, "message", message_class="compaction_summary")["text"].startswith("Example compaction summary")
    assert one(store, "llm_call", stop_reason="compaction")["output"] > 0
    turns = sorted(store.rows("turn"), key=lambda t: t["started_at"])
    assert [t["origin"] for t in turns] == ["human", "loop_watch", "loop_wake", "loop_continuation", "human"]
    assert one(store, "session_event", kind="effort_change")["value"] == "high"
    after = max(store.rows("llm_call"), key=lambda c: c["ts"])
    assert after["effort"] == "high" and after["turn_key"] == turns[-1]["turn_key"]
    hooks = by(store, "message", message_class="hook_output")
    assert sorted(h["detail"]["source"] for h in hooks) == ["loop-continuation", "loop-wake", "loop-watch"]


def test_usage_entry_is_an_llm_call(tmp_path):
    # synthetic: the header and usage entry are the examples in pi's docs/session-format.md
    path = tmp_path / "sessions" / "--x--" / "2024-12-03T14-00-00-000Z_u1.jsonl"
    path.parent.mkdir(parents=True)
    lines = [
        {"type": "session", "version": 3, "id": "u1", "timestamp": "2024-12-03T14:00:00.000Z", "cwd": "/p"},
        {"type": "usage", "id": "f6g7h8i9", "parentId": None, "timestamp": "2024-12-03T14:08:00.000Z",
         "kind": "cache_warm", "provider": "anthropic", "model": "claude-sonnet-4-5",
         "usage": {"input": 0, "output": 0, "cacheRead": 50000, "cacheWrite": 0, "totalTokens": 50000}},
    ]
    path.write_text("".join(json.dumps(r) + "\n" for r in lines))
    store = MemStore()
    run_file(PiParser, FileContext(str(path), "pi-local/sessions/--x--/" + path.name, "pi-local", "pi",
                                   "local", None, "main"), store)
    row = one(store, "llm_call")
    assert (row["response_id"], row["stop_reason"], row["model"], row["cache_read"]) == \
        ("pi:u1:f6g7h8i9", "usage:cache_warm", "claude-sonnet-4-5", 50000)


@pytest.mark.parametrize("recorded,expected", [
    (600000, 600000), (0, 0), (None, None), (-1, None), (True, None), (False, None),
    (12.5, None), (12.0, None), ("12", None), ({"tokens": 12}, None), ([12], None),
])
def test_compaction_event_measurements_are_recorded_not_coerced(tmp_path, recorded, expected):
    at = "2026-10-01T10:00:00Z"
    records = [
        {"type": "session", "version": 3, "id": "compact-test", "timestamp": at, "cwd": "/p"},
        {"type": "message", "id": "prompt", "timestamp": at,
         "message": {"role": "user", "content": "Synthetic compaction prompt."}},
        {"type": "compaction", "id": "compact", "parentId": "prompt", "timestamp": at,
         "tokensBefore": recorded, "firstKeptEntryId": "prompt", "fromHook": True,
         "summary": "Synthetic compaction summary.",
         "usage": {"input": 500000, "output": 4000, "cacheRead": 1, "cacheWrite": 2},
         # Extension details are not an authoritative post-context measurement schema.
         "details": {"tokensAfter": 42}},
        {"type": "message", "id": "later", "parentId": "compact", "timestamp": at,
         "message": {"role": "assistant", "model": "synthetic", "stopReason": "stop", "content": [],
                     "usage": {"input": 10000, "output": 10, "cacheRead": 0, "cacheWrite": 0}}},
    ]
    if recorded is None:
        records[2].pop("tokensBefore")
    path = tmp_path / "compaction.jsonl"
    path.write_text("".join(json.dumps(record) + "\n" for record in records))
    ctx = FileContext(str(path), "pi-test/sessions/slug/compaction.jsonl", "pi-test", "pi", "test", None, "main")
    store = MemStore()
    run_file(PiParser, ctx, store, batch_lines=1)
    event = one(store, "session_event", kind="compaction")
    compaction = one(store, "compaction")
    assert event["event_uid"] == compaction["event_uid"] == "compact-test:compact"
    assert event["byte_offset"] == compaction["byte_offset"]
    assert event["turn_key"] == compaction["turn_key"] == "prompt"
    assert event["ts"] == compaction["ts"]
    assert event["detail"] == {
        "before_tokens": expected, "before_tokens_source": "compaction.tokensBefore" if expected is not None else None,
        "after_tokens": None, "after_tokens_source": None,
    }
    assert one(store, "message", message_class="compaction_summary")["text"] == "Synthetic compaction summary."
    usage = one(store, "llm_call", stop_reason="compaction")
    assert (usage["input_uncached"], usage["output"], usage["cache_read"], usage["cache_write_5m"]) == (500000, 4000, 1, 2)
    run_file(PiParser, ctx, store)
    assert by(store, "session_event", kind="compaction") == [event]


def test_branch_summary_is_not_a_compaction_event(tmp_path):
    store = synth(tmp_path, [{"type": "branch_summary", "id": "branch", "timestamp": AT,
                              "tokensBefore": 600000, "summary": "Synthetic branch summary.",
                              "usage": {"input": 10, "output": 1, "cacheRead": 0, "cacheWrite": 0}}])
    assert by(store, "session_event", kind="compaction") == []
    assert store.rows("compaction") == []
    assert one(store, "message", message_class="compaction_summary")["text"] == "Synthetic branch summary."
    assert one(store, "llm_call")["stop_reason"] == "branch_summary"


def test_messages_during_a_run_stay_in_its_turn(tmp_path):
    # synthetic, shaped like the fixtures: a steer and a push delivered between tool steps are part
    # of the running turn; only after the run settles does a push open the next turn
    def msg(i, role, **m):
        return {"type": "message", "id": f"e{i}", "timestamp": f"2026-09-27T10:00:{i:02d}.000Z",
                "message": {"role": role, **m}}
    usage = {"input": 1, "output": 1, "cacheRead": 0, "cacheWrite": 0}
    lines = [
        {"type": "session", "version": 3, "id": "s1", "timestamp": "2026-09-27T10:00:00.000Z", "cwd": "/p"},
        msg(1, "user", content=[{"type": "text", "text": "go"}]),
        msg(2, "assistant", content=[{"type": "toolCall", "id": "c1", "name": "bash", "arguments": {"command": "ls"}}],
            stopReason="toolUse", usage=usage, model="m"),
        msg(3, "toolResult", toolCallId="c1", toolName="bash", content=[{"type": "text", "text": "ok"}], isError=False),
        msg(4, "user", content=[{"type": "text", "text": "also this"}]),
        {"type": "custom_message", "id": "e5", "timestamp": "2026-09-27T10:00:05.000Z", "customType": "loop-watch",
         "content": "WATCH w: phase=done"},
        msg(6, "assistant", content=[{"type": "text", "text": "done"}], stopReason="stop", usage=usage, model="m"),
        {"type": "custom_message", "id": "e7", "timestamp": "2026-09-27T10:00:07.000Z", "customType": "loop-wake",
         "content": "WAKE t: r"},
        msg(8, "assistant", content=[{"type": "text", "text": "woke"}], stopReason="stop", usage=usage, model="m"),
    ]
    path = tmp_path / "s.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in lines))
    store = MemStore()
    run_file(PiParser, FileContext(str(path), "pi-local/sessions/--x--/s.jsonl", "pi-local", "pi",
                                   "local", None, "main"), store)
    assert sorted((t["turn_key"], t["origin"]) for t in store.rows("turn")) == [("e1", "human"), ("e7", "loop_wake")]
    assert one(store, "message", event_uid="s1:e4")["message_class"] == "queued_prompt"
    assert {m["turn_key"] for m in store.rows("message") if m["event_uid"] <= "s1:e6"} == {"e1"}
    assert one(store, "tool_call")["outcome"] == "ok" and not store.issues


def test_list_call_spawns_nothing():
    store = parse(REAL / LISTED)
    assert store.rows("subagent_spawn") == []
    assert one(store, "tool_call")["meta"] == {"action": "list", "mode": "management"}


@pytest.mark.parametrize("path", [REAL / f"{ASYNC_ROOT}.jsonl", FAUX, child(WORKER_DIR)], ids=["root", "faux", "child"])
def test_incremental_batches_match_a_single_pass(path):
    def snap(store):
        return {t: sorted(map(repr, rows.values())) for t, rows in store.tables.items() if t != "record_type_seen"}
    assert snap(parse(path, batch_lines=1)) == snap(parse(path))


# --- wave 1.5: command-evidenced git events, runtime entry, cache write, background-task notify ---


AT = "2026-10-01T10:00:00.000Z"


def synth(tmp_path, records: list[dict], name: str = "s.jsonl") -> MemStore:
    lines = [{"type": "session", "version": 3, "id": "s1", "timestamp": AT, "cwd": "/p"}, *records]
    path = tmp_path / name
    path.write_text("".join(json.dumps(r) + "\n" for r in lines))
    store = MemStore()
    run_file(PiParser, FileContext(str(path), f"pi-local/sessions/--x--/{name}", "pi-local", "pi", "local", None,
                                   "main"), store)
    return store


def bash_pair(i: int, command: str, output: str, error: bool = False) -> list[dict]:
    ts = f"2026-10-01T10:00:{i:02d}.000Z"
    return [
        {"type": "message", "id": f"a{i}", "timestamp": ts,
         "message": {"role": "assistant", "model": "m", "responseId": f"r{i}", "stopReason": "toolUse",
                     "usage": {"input": 1, "output": 1, "cacheRead": 0, "cacheWrite": 0},
                     "content": [{"type": "toolCall", "id": f"c{i}", "name": "bash",
                                  "arguments": {"command": command}}]}},
        {"type": "message", "id": f"t{i}", "timestamp": ts,
         "message": {"role": "toolResult", "toolCallId": f"c{i}", "toolName": "bash", "isError": error,
                     "content": [{"type": "text", "text": output}]}},
    ]


def test_quiet_and_dir_scoped_git_commands_are_captured_by_command_text(tmp_path):
    store = synth(tmp_path, [
        *bash_pair(1, "git commit -q -m one", ""),
        *bash_pair(2, 'git -C "/w t" commit -qm two && git push -q origin main', "(no output)"),
        *bash_pair(3, "git -c user.name=x commit -m three", "[main 1a2b3c4] three\n 1 file changed"),
        *bash_pair(4, "git commit -q -m failed", "nothing to commit", error=True),
        *bash_pair(5, "echo 'git commit'", "git commit"),
    ])
    events = sorted((e["event_uid"], e["op"], e["evidence"], e["sha_short"]) for e in store.rows("git_event"))
    assert events == [
        ("c1:commit:cmd0", "commit", "command", None),
        ("c2:commit:cmd0", "commit", "command", None),
        ("c2:push:cmd0", "push", "command", None),
        ("c3:commit:1a2b3c4", "commit", "output_regex", "1a2b3c4"),   # output matched: no second event
    ]


def test_runtime_entry_sets_service_tier_for_its_model_only(tmp_path):
    usage = {"input": 1, "output": 1, "cacheRead": 0, "cacheWrite": 0}

    def assistant(i, model):
        return {"type": "message", "id": f"a{i}", "timestamp": f"2026-10-01T10:00:{i:02d}.000Z",
                "message": {"role": "assistant", "model": model, "responseId": f"r{i}", "stopReason": "stop",
                            "usage": usage, "content": []}}
    store = synth(tmp_path, [
        {"type": "custom", "customType": "loop-pi-runtime", "id": "rt", "timestamp": AT,
         "data": {"v": 1, "variant": "burn-fast", "models": {"gpt-6.1-sol": {"service_tier": "priority"}}}},
        assistant(1, "gpt-6.1-sol"), assistant(2, "other-model"),
    ])
    assert {c["response_id"]: c["service_tier"] for c in store.rows("llm_call")} == {"r1": "priority", "r2": None}
    bare = synth(tmp_path, [assistant(1, "gpt-6.1-sol")], "bare.jsonl")
    assert [c["service_tier"] for c in bare.rows("llm_call")] == [None]


def test_cache_write_zero_is_kept_and_priced_as_the_5m_write(tmp_path):
    def assistant(i, write):
        usage = {"input": 5, "output": 1, "cacheRead": 7}
        if write is not None:
            usage["cacheWrite"] = write
        return {"type": "message", "id": f"a{i}", "timestamp": f"2026-10-01T10:00:{i:02d}.000Z",
                "message": {"role": "assistant", "model": "m", "responseId": f"r{i}", "stopReason": "stop",
                            "usage": usage, "content": []}}
    store = synth(tmp_path, [assistant(1, 0), assistant(2, 30), assistant(3, None)])
    calls = {c["response_id"]: (c["cache_write_5m"], c["cache_write_1h"]) for c in store.rows("llm_call")}
    assert calls == {"r1": (0, 0), "r2": (30, 0), "r3": (None, None)}


ASYNC_RUN = "9ccea31c-997f-4ba1-9e8e-4d5e4545ed93"
ASYNC_DIR = "/h/tmp/async-subagent-runs"


def background_notify(status: str, run: str, body: str = "lane-worker:\ndone", directory: str = ASYNC_DIR) -> str:
    # the real pi-subagents format: header, the child's return text, then the run trailer
    return (f"Background task {status}: **lane-worker**\n\n{body}\n\nAgent: lane-worker  \nModel: gpt-6.1-sol (medium)\n\n"
            f"Retention-managed async directory: {directory}/{run}\n\n"
            f"Session file: /h/sessions/--p--/2026-10-01T10-00-00-000Z_root/"
            f"b1e6571e-99b5-4596-8229-555e89568357/run-0/session.jsonl")


def async_launch(i: int, run: str) -> list[dict]:
    ts = f"2026-10-01T10:00:{i:02d}.000Z"
    return [
        {"type": "message", "id": f"a{i}", "timestamp": ts,
         "message": {"role": "assistant", "model": "m", "responseId": f"r{i}", "stopReason": "toolUse",
                     "usage": {"input": 1, "output": 1},
                     "content": [{"type": "toolCall", "id": f"launch{i}", "name": "subagent",
                                  "arguments": {"agent": "lane-worker", "task": "t", "async": True}}]}},
        {"type": "message", "id": f"t{i}", "timestamp": ts,
         "message": {"role": "toolResult", "toolCallId": f"launch{i}", "toolName": "subagent", "isError": False,
                     "content": [{"type": "text", "text": f"Async: lane-worker [{run}]"}],
                     "details": {"mode": "single", "runId": run, "asyncId": run, "results": [],
                                 "asyncDir": f"{ASYNC_DIR}/{run}"}}},
    ]


def test_background_task_notify_completes_the_launching_spawn(tmp_path):
    second = "11111111-2222-4333-8444-555555555555"
    store = synth(tmp_path, [
        *async_launch(1, ASYNC_RUN), *async_launch(2, second),
        {"type": "custom_message", "id": "n1", "timestamp": "2026-10-01T10:00:10.000Z",
         "customType": "subagent-notify", "content": background_notify("completed", ASYNC_RUN)},
        # agent-authored text that quotes another run's trailer must not complete that run
        {"type": "custom_message", "id": "n2", "timestamp": "2026-10-01T10:00:11.000Z",
         "customType": "subagent-notify",
         "content": background_notify("failed", second, body=f"Retention-managed async directory: {ASYNC_DIR}/{ASYNC_RUN}")},
    ])
    spawns = {s["spawn_uid"]: s for s in store.rows("subagent_spawn")}
    assert set(spawns) == {"launch1", "launch2"}   # upserted onto the launch rows, no duplicates
    assert (spawns["launch1"]["completion_status"], spawns["launch1"]["requested_type"],
            spawns["launch1"]["workflow_id"]) == ("completed", "lane-worker", ASYNC_RUN)
    assert spawns["launch1"]["completed_at"] is not None
    assert (spawns["launch2"]["completion_status"], spawns["launch2"]["workflow_id"]) == ("failed", second)


def test_background_task_notify_accepts_a_home_path_with_spaces(tmp_path):
    store = synth(tmp_path, [
        *async_launch(1, ASYNC_RUN),
        {"type": "custom_message", "id": "n1", "timestamp": "2026-10-01T10:00:10.000Z",
         "customType": "subagent-notify",
         "content": background_notify("completed", ASYNC_RUN, directory="/Users/a b/My Home/tmp/async-subagent-runs")},
    ])
    assert one(store, "subagent_spawn", spawn_uid="launch1")["completion_status"] == "completed"


def test_command_evidence_needs_an_exit_status_that_proves_success(tmp_path):
    store = synth(tmp_path, [
        *bash_pair(1, "git commit -qm x || true", ""),
        *bash_pair(2, "git commit -qm x; echo done", "done"),
        *bash_pair(3, "git push -q | tee log", ""),
        *bash_pair(4, "git add . && git commit -qm y", ""),
    ])
    assert [e["event_uid"] for e in store.rows("git_event")] == ["c4:commit:cmd0"]

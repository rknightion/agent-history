"""parse_claude against synthetic fixtures (structure mirrors real transcripts, content is invented)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agent_history.memstore import MemStore, run_file
from agent_history.model import FileContext, SessionKey
from agent_history.parse_claude import ClaudeParser

FIX = Path(__file__).parent / "fixtures" / "claude"
SID = "11111111-1111-4111-8111-111111111111"
PROJ = FIX / "projects" / "-home-tester-repo"
MAIN = PROJ / f"{SID}.jsonl"
SUB = PROJ / SID / "subagents"
MAIN_KEY = SessionKey("claude", SID, "")


def ctx(path: Path, role: str | None = None) -> FileContext:
    base = FIX if path.is_relative_to(FIX) else FIX.parent / "claude_v4"
    rel = path.relative_to(base).as_posix()
    if role is None:
        parts = rel.split("/")
        role = "main"
        if "subagents" in parts:
            role = ("workflow_journal" if parts[-1] == "journal.jsonl"
                    else "workflow_agent" if "workflows" in parts else "subagent")
    return FileContext(path=str(path), rel_path=f"claude-test/{rel}", namespace="claude-test",
                       agent="claude", profile="test", machine=None, file_role=role)


def load(*paths: Path, batch_lines: int = 5000) -> MemStore:
    store = MemStore()
    for p in paths:
        run_file(ClaudeParser, ctx(p), store, batch_lines=batch_lines)
    return store


def by(store: MemStore, table: str, **match):
    return [r for r in store.rows(table) if all(r.get(k) == v for k, v in match.items())]


def one(store: MemStore, table: str, **match):
    rows = by(store, table, **match)
    assert len(rows) == 1, (table, match, len(rows))
    return rows[0]


@pytest.fixture(scope="module")
def main_store() -> MemStore:
    return load(MAIN)


def test_multi_line_message_usage_merges_to_last_line(main_store):
    call = one(main_store, "llm_call", response_id="msg_1")
    assert call["output"] == 40            # MAX over the three content-block lines
    assert call["line_count"] == 3
    assert call["input_uncached"] == 10 and call["cache_read"] == 100
    assert call["cache_write_5m"] == 50 and call["cache_write_1h"] == 0
    assert call["reasoning"] == 5
    assert call["stop_reason"] == "tool_use"
    assert call["turn_key"] == "p1"


def test_tool_only_response_has_usage(main_store):
    call = one(main_store, "llm_call", response_id="msg_2")
    assert call["output"] == 60 and call["cache_read"] == 200
    assert call["line_count"] == 2
    # no assistant_text message for a tool-only response
    assert not [m for m in main_store.rows("message") if m["event_uid"] in ("u-007", "u-008")]


def test_web_search_and_api_error_rows(main_store):
    assert one(main_store, "llm_call", response_id="msg_9")["web_search_requests"] == 1
    err = one(main_store, "llm_call", response_id="msg_synth")
    assert err["is_api_error"] and err["error_kind"] == "rate_limit"
    assert err["api_error_status"] == 429 and err["model"] == "<synthetic>"
    rl = one(main_store, "rate_limit_sample", window_kind="claude_five_hour")
    assert rl["reached_type"] == "rejected" and rl["resets_at"] is not None
    assert by(main_store, "session_event", kind="api_error")


def test_tool_results_match_with_outcomes(main_store):
    calls = {r["call_uid"]: r for r in main_store.rows("tool_call")}
    assert calls["toolu_b1"]["outcome"] == "ok" and calls["toolu_b1"]["exit_code"] == 0
    assert calls["toolu_b1"]["meta"] == {"caller": "direct", "cmd_verb": "git"}
    assert calls["toolu_b1"]["duration_ms"] == 1000
    assert calls["toolu_r1"]["outcome"] == "ok" and calls["toolu_r1"]["tool_name"] == "Read"
    assert calls["toolu_b2"]["outcome"] == "error" and calls["toolu_b2"]["exit_code"] == 1
    assert calls["toolu_e1"]["outcome"] == "denied"
    assert calls["toolu_e1"]["denial_kind"] == "permission-rule"
    assert calls["toolu_b4"]["outcome"] == "interrupted" and calls["toolu_b4"]["interrupted"]
    assert calls["toolu_m1"]["mcp_server"] == "srv_a" and calls["toolu_m1"]["mcp_tool"] == "do_thing"
    assert calls["toolu_m1"]["tool_family"] == "mcp"
    assert calls["toolu_s1"]["attribution_skill"] == "writing"
    assert all(c["started_at"] and c["ended_at"] for c in calls.values())
    assert not [i for i in main_store.issues if i.kind == "orphan_tool_result"]
    assert one(main_store, "session_event", kind="denial")["value"] == "permission-rule"


def test_tool_meta_carries_no_content(main_store):
    for row in main_store.rows("tool_call"):
        for value in (row["meta"] or {}).values():
            assert " " not in str(value)


def test_spawn_launch_and_notification_merge(main_store):
    spawn = one(main_store, "subagent_spawn", spawn_uid="toolu_a1")
    assert spawn["session"] == MAIN_KEY
    assert spawn["child_agent_id"] == "a0000000000000001"
    assert spawn["child_session_uid"] == SID
    assert spawn["background"] is True              # "true" string normalised
    assert spawn["requested_type"] == "Explore" and spawn["requested_model"] == "sonnet"
    assert spawn["requested_type_source"] == "explicit"
    assert spawn["resolved_model"] == "claude-sonnet-5"
    assert spawn["launch_status"] == "async_launched"
    assert spawn["completion_status"] == "completed" and spawn["completed_at"] is not None
    assert (spawn["reported_tokens"], spawn["reported_tool_uses"], spawn["reported_duration_ms"]) == (1234, 7, 5555)
    brief = one(main_store, "message", event_uid="toolu_a1:brief")
    assert brief["message_class"] == "subagent_brief" and brief["session"] == MAIN_KEY
    # the notification is written twice (attachment mirror + user line) and stored once
    assert len(by(main_store, "message", message_class="task_notification_summary")) == 1
    report = by(main_store, "message", message_class="subagent_report")
    assert len(report) == 1 and report[0]["session"] == MAIN_KEY
    wf = one(main_store, "subagent_spawn", spawn_uid="toolu_w1")
    assert wf["workflow_id"] == "wf_abc" and wf["launch_status"] == "async_launched"


def test_task_without_subagent_type_records_harness_default(tmp_path):
    records = [json.loads(line) for line in MAIN.read_text().splitlines()]
    changed = False
    for record in records:
        if record.get("type") != "assistant":
            continue
        for block in record.get("message", {}).get("content", []):
            if isinstance(block, dict) and block.get("name") in ("Task", "Agent") and "subagent_type" in block.get("input", {}):
                del block["input"]["subagent_type"]
                changed = True
                break
        if changed:
            break
    assert changed
    target = tmp_path / "main.jsonl"
    target.write_text("".join(json.dumps(record) + "\n" for record in records))
    store = MemStore()
    context = FileContext(str(target), f"claude-test/projects/synthetic/{SID}.jsonl", "claude-test",
                          "claude", "test", None, "main")
    run_file(ClaudeParser, context, store)
    spawn = one(store, "subagent_spawn", spawn_uid="toolu_a1")
    assert (spawn["requested_type"], spawn["requested_type_source"]) == ("general-purpose", "default")


def test_genuine_prompt_classification(main_store):
    human = by(main_store, "message", message_class="human_prompt")
    # v4 intentional change 1: the typed slash command (u-027) and bash-mode input (u-031) join
    assert sorted(m["event_uid"] for m in human) == ["u-001", "u-018", "u-027", "u-031"]
    origins = {m["event_uid"]: m["prompt_origin"] for m in human}
    assert origins == {"u-001": "typed", "u-018": "typed", "u-027": "slash_command", "u-031": "local_command"}
    assert [m["event_uid"] for m in by(main_store, "message", message_class="queued_prompt")] == ["u-033"]
    turns = {t["turn_key"]: t for t in main_store.rows("turn")}
    assert turns["p1"]["origin"] == "human"
    assert turns["p2"]["origin"] == "human"          # interrupt marker first, then the prompt
    assert turns["p3"]["origin"] == "task_notification"
    assert turns["p4"]["origin"] == "slash_command"  # isMeta caveat first, then the command
    assert turns["p5"]["origin"] == "compaction_summary"
    assert turns["p6"]["origin"] == "bash"
    assert turns["p7"]["origin"] == "queued"
    assert turns["p8"]["origin"] == "sdk"
    assert turns["p1"]["duration_ms"] is None and turns["p2"]["duration_ms"] == 25000
    assert turns["p2"]["message_count"] == 21 and turns["p2"]["status"] == "complete"
    session = one(main_store, "session", session=MAIN_KEY)
    assert session["first_human_at"].isoformat().startswith("2026-09-20T10:00:01")
    assert session["title"] == "Synthetic widget fix"
    assert session["cli_version_first"] == "2.1.280" and session["cwd"] == "/home/tester/repo"
    kinds = {e["kind"] for e in main_store.rows("session_event")}
    assert {"slash_command", "interrupt", "queue_op", "skill_invoke", "permission_mode"} <= kinds


def test_compaction(main_store):
    comp = one(main_store, "compaction", event_uid="u-029")
    assert (comp["trigger"], comp["pre_tokens"], comp["post_tokens"]) == ("manual", 150000, 9000)
    assert comp["dropped_tokens"] == 141000
    assert one(main_store, "message", event_uid="u-030")["message_class"] == "compaction_summary"


def test_git_events(main_store):
    rows = {r["event_uid"]: r for r in main_store.rows("git_event")}
    assert rows["toolu_b1:commit:abc1234"]["evidence"] == "gitOperation"
    assert rows["toolu_b3:push:main"]["evidence"] == "output_regex"
    assert rows["toolu_b3:push:main"]["sha_short"] == "2222222"
    assert not [k for k in rows if k.startswith("toolu_b2:")]   # failed push, no evidence
    assert len([k for k in rows if k.startswith("toolu_b1:")]) == 1   # regex skipped
    assert one(main_store, "git_event", evidence="pr_link")["pr_number"] == 42


def test_git_structured_and_regex_sightings_merge():
    store = load(MAIN, FIX / "archive" / "-home-tester-repo" / f"{SID}.jsonl")
    commits = [r for r in store.rows("git_event") if r["op"] == "commit"]
    assert len(commits) == 1 and commits[0]["evidence"] == "gitOperation"


def test_artifacts_and_hooks_and_cost(main_store):
    arts = {(a["path"], a["action"], a["evidence_type"]) for a in main_store.rows("artifact")}
    assert ("/tmp/notes.md", "linked", "assistant-markdown") in arts
    assert ("/home/tester/repo/widget.py", "read", "tool_input") in arts
    assert ("/home/tester/repo/widget.py", "edited", "tool_input") in arts
    assert ("/home/tester/repo/widget.py", "modified", "claude-file-history") in arts
    hook = one(main_store, "hook_event", event_uid="u-002")
    assert hook["outcome"] == "success" and hook["hook_event"] == "UserPromptSubmit"
    assert len(hook["command_sha256"]) == 64
    cost = one(main_store, "cost_state")
    assert cost["total_cost_usd"] == 1.5 and cost["model_usage"]["claude-opus-5-5"]["outputTokens"] == 2
    # cost-state totals are per process: the row keeps the process start so reconciliation can split them
    assert cost["start_time"] == datetime.fromtimestamp(1790000000, timezone.utc)


def test_drift_rows(main_store):
    assert [i.detail for i in main_store.issues if i.kind == "unknown_type"] == ["brand-new-record-type"]
    types = {(r["record_type"], r["subtype"]) for r in main_store.rows("record_type_seen")}
    assert ("attachment", "hook_success") in types and ("system", "turn_duration") in types


def test_no_tool_io_in_messages(main_store):
    texts = " ".join(m["text"] for m in main_store.rows("message"))
    for leaked in ("print()", "failed to push", "Permission to use Edit", "git commit"):
        assert leaked not in texts


def _comparable(store: MemStore) -> dict:
    return {t: {k: v for k, v in rows.items()} for t, rows in store.tables.items()
            if t != "record_type_seen"}      # per-batch counts by design


@pytest.mark.parametrize("batch", [1, 2, 3, 7])
def test_incremental_resumption_matches_one_shot(batch):
    paths = [MAIN, SUB / "agent-a0000000000000001.jsonl"]
    assert _comparable(load(*paths, batch_lines=batch)) == _comparable(load(*paths))


def test_resume_from_saved_offset_and_state():
    one_shot = load(MAIN)
    store = MemStore()
    c = ctx(MAIN)
    offset, state = run_file(ClaudeParser, c, store, stop_after_lines=17)
    lines_done = 17
    run_file(ClaudeParser, c, store, start=offset, state=state, line_base=lines_done)
    assert _comparable(store) == _comparable(one_shot)


def test_subagent_session_and_report():
    store = load(MAIN, SUB / "agent-a0000000000000001.jsonl")
    key = SessionKey("claude", SID, "a0000000000000001")
    sess = one(store, "session", session=key)
    assert sess["is_subagent"] and sess["parent_session_uid"] == SID and sess["spawn_kind"] == "agent"
    report = one(store, "message", event_uid="toolu_h1:report")
    assert report["session"] == key and report["message_class"] == "subagent_report"
    assert one(store, "turn", session=key)["origin"] == "subagent_brief"
    # the child's brief line is not stored again (it lives in the parent as toolu_a1:brief)
    assert not by(store, "message", event_uid="s-001")
    assert one(store, "llm_call", response_id="msg_s1")["session"] == key


def test_fork_replay_counted_once():
    store = load(SUB / "agent-a0000000000000002.jsonl", SUB / "agent-a0000000000000003.jsonl")
    pre = one(store, "llm_call", response_id="msg_pre")
    assert pre["session"] == SessionKey("claude", SID, "a0000000000000002")   # first observer owns it
    assert len(by(store, "message", event_uid="f-002")) == 1
    assert {s["spawn_kind"] for s in store.rows("session")} == {"fork"}
    assert len(store.rows("llm_call")) == 3      # msg_pre once + one own response per fork


def test_workflow_journal_and_agent():
    store = load(SUB / "workflows" / "wf_abc" / "journal.jsonl",
                 SUB / "workflows" / "wf_abc" / "agent-a0000000000000004.jsonl")
    spawn = one(store, "subagent_spawn", spawn_uid="wf:wf_abc:a0000000000000004")
    assert spawn["session"] == MAIN_KEY and spawn["child_agent_id"] == "a0000000000000004"
    assert spawn["launch_status"] == "started" and spawn["completion_status"] == "completed"
    sessions = store.rows("session")
    assert len(sessions) == 1                         # the journal emits no session
    assert sessions[0]["spawn_kind"] == "workflow" and sessions[0]["workflow_id"] == "wf_abc"
    assert one(store, "message", event_uid="w-001")["message_class"] == "subagent_brief"
    assert one(store, "message", event_uid="toolu_so1:report")["message_class"] == "subagent_report"
    assert not by(store, "message", message_class="human_prompt")


def test_state_is_json_and_content_free():
    import json
    store = MemStore()
    _, state = run_file(ClaudeParser, ctx(MAIN), store)
    blob = json.dumps(state)
    for leaked in ("widget", "Synthetic", "continue please", "git push"):
        assert leaked not in blob


def test_result_written_before_call():
    path = FIX / "projects" / "-home-tester-order" / "22222222-2222-4222-8222-222222222222.jsonl"
    for batch in (1, 5000):
        store = load(path, batch_lines=batch)
        call = one(store, "tool_call", call_uid="toolu_o1")
        assert call["tool_name"] == "Bash" and call["outcome"] == "ok" and call["exit_code"] == 0
        assert call["duration_ms"] == 3000
        assert one(store, "git_event", op="commit")["event_uid"] == "toolu_o1:commit:fedcba9"
        assert not store.issues
        assert one(store, "session_event", kind="continued_in")["value"].startswith("33333333")


# --- parser v4 ---------------------------------------------------------------------------------

V4_SID = "66666666-6666-4666-8666-666666666666"
V4_MAIN = FIX.parent / "claude_v4" / "projects" / "-home-tester-v4" / f"{V4_SID}.jsonl"
V4_SUB = FIX.parent / "claude_v4" / "projects" / "-home-tester-v4" / V4_SID / "subagents" / "agent-a0000000000000009.jsonl"
B64 = "iVBORw0KGgo="


@pytest.fixture(scope="module")
def v4_store() -> MemStore:
    return load(V4_MAIN, V4_SUB)


def _msgs(store):
    return {m["event_uid"]: m for m in store.rows("message")}


def test_v4_reasoning_rows(v4_store):
    m = _msgs(v4_store)
    assert m["v-007:think:0"]["message_class"] == "reasoning" and m["v-007:think:0"]["role"] == "assistant"
    assert m["v-007:think:0"]["text"] == "Synthetic reasoning about the widget."
    assert m["v-007:think:0"]["model"] == "claude-opus-5-5" and m["v-007:think:0"]["turn_key"] == "v1"
    assert (m["v-008:think:0"]["text"], m["v-008:think:0"]["detail"]) == ("", {"signature_only": True})
    assert (m["v-008:think:1"]["text"], m["v-008:think:1"]["detail"]) == ("", {"redacted": True})


def test_v4_harness_content_classes(v4_store):
    m = _msgs(v4_store)
    got = {uid: (row["message_class"], (row["detail"] or {}).get("source")) for uid, row in m.items()}
    assert got["v-001"] == ("system_prompt", "prompt_snapshot.systemPrompt")
    assert m["v-001"]["role"] == "system"
    assert m["v-001"]["text"] == "Synthetic system block one.\n\nSynthetic block two."
    assert got["v-001:tools"] == ("context_injection", "prompt_snapshot.tools")
    assert got["v-001:ctx"] == ("context_injection", "prompt_snapshot")        # cliPrefix
    assert [m[f"v-002:{n}"]["text"] for n in (0, 1)] == ["Synthetic rule A.", "Synthetic rule B."]
    assert m["v-002:0"]["detail"]["path"] == "/home/tester/v4/CLAUDE.md"
    assert got["v-003"] == ("system_reminder", "total_tokens_reminder")
    assert got["v-004"] == ("context_injection", "file")
    # a hook with two non-empty fields yields one row per field; the HookEventRow is still there
    assert (m["v-006:stdout"]["text"], m["v-006:stderr"]["text"]) == ("synthetic hook out", "synthetic hook err")
    assert got["v-006:stdout"] == ("hook_output", "hook_success")
    assert one(v4_store, "hook_event", event_uid="v-006")["outcome"] == "success"
    assert got["v-019"] == ("system_reminder", "local-command-caveat")
    assert got["v-021"] == ("command_expansion", "slash_command")    # the isMeta body after /review
    assert got["v-024"] == ("skill_body", "skill_command")
    assert got["v-025"] == ("system_reminder", "system-reminder")    # not taken for the skill command
    assert got["v-028"] == ("local_command_output", "bash-stdout")
    assert got["v-029"] == ("interrupt_marker", "interrupt")
    # peer message v3 stores as queued_prompt keeps its class and gains an honest origin
    assert (m["v-030"]["message_class"], m["v-030"]["prompt_origin"]) == ("queued_prompt", "agent_message")


def test_v4_prompt_origins(v4_store):
    m = _msgs(v4_store)
    assert (m["v-005"]["message_class"], m["v-005"]["prompt_origin"]) == ("human_prompt", "pasted")
    assert (m["v-020"]["message_class"], m["v-020"]["prompt_origin"]) == ("human_prompt", "slash_command")
    assert (m["v-023"]["message_class"], m["v-023"]["prompt_origin"]) == ("human_prompt", "skill")
    assert (m["v-027"]["message_class"], m["v-027"]["prompt_origin"]) == ("human_prompt", "local_command")
    turns = {t["turn_key"]: t["origin"] for t in v4_store.rows("turn")}
    assert turns["v2"] == "slash_command" and turns["v4"] == "bash"    # v3 turn origins unchanged


def test_v4_tool_io(v4_store):
    io = {r["io_uid"]: r for r in v4_store.rows("tool_io")}
    bash = io["toolu_v_bash"]
    assert bash["input_text"] == '{"command": "ls -la", "description": "list é"}'
    assert (bash["output_text"], bash["stdout_text"], bash["stderr_text"]) == ("synthetic listing", "synthetic listing", "warn")
    assert bash["output_truncated"] is True and bash["tool_name"] == "Bash" and bash["turn_key"] == "v1"
    rj = json.loads(bash["result_json"])
    assert "stdout" not in rj and "stderr" not in rj and rj["persistedOutputSize"] == 99999
    assert bash["ts"] < bash["output_at"]
    read = io["toolu_v_read"]
    assert read["output_text"] is None
    assert read["output_parts"] == [{"index": 0, "type": "image", "mime": "image/png", "bytes": 8}]
    assert json.loads(read["result_json"])["file"]["base64"] == {"type": "base64", "mime": "image/png", "bytes": 8}
    assert read["output_truncated"] is True                        # read_truncation_notice merged in
    ts_row = io["toolu_v_ts"]
    assert ts_row["output_text"] == "synthetic ref"
    assert ts_row["output_parts"] == [{"index": 0, "type": "tool_reference", "tool_name": "Monitor"}]
    for table in ("tool_io", "attachment", "message"):
        for row in v4_store.rows(table):
            assert B64 not in json.dumps(row, default=str)


def test_v4_early_result_merges():
    path = FIX / "projects" / "-home-tester-order" / "22222222-2222-4222-8222-222222222222.jsonl"
    for batch in (1, 5000):
        row = one(load(path, batch_lines=batch), "tool_io", io_uid="toolu_o1")
        assert row["tool_name"] == "Bash" and row["input_text"] and row["output_text"]
        assert row["output_at"] is not None and row["byte_offset"] > 0


def test_v4_attachments(v4_store):
    att = {a["attachment_uid"]: a for a in v4_store.rows("attachment")}
    img = att["v-005:att:0"]
    assert (img["kind"], img["source"], img["mime"], img["size_bytes"]) == ("image", "prompt", "image/png", 8)
    assert img["detail"] == {"paste_id": 7} and img["text"] is None
    pasted = att["v-005:att:1"]
    assert (pasted["kind"], pasted["text"], pasted["detail"]) == ("pasted_text", "synthetic pasted block", {"paste_id": "p1"})
    assert _msgs(v4_store)["v-005"]["text"].startswith("Look at this <pasted_content")    # prompt keeps full text
    tr = att["toolu_v_read:att:0"]
    assert (tr["source"], tr["call_uid"], tr["event_uid"]) == ("tool_result", "toolu_v_read", "v-012")
    f = att["v-004:att:0"]
    assert (f["kind"], f["file_name"], f["text"]) == ("file", "/home/tester/v4/notes.txt", "synthetic file body")


def test_v4_file_touches(v4_store):
    t = {r["touch_uid"]: r for r in v4_store.rows("file_touch")}
    assert (t["toolu_v_read:0"]["op"], t["toolu_v_read:0"]["path"]) == ("read", "/home/tester/v4/pic.png")
    edit = t["toolu_v_edit:0"]
    assert (edit["op"], edit["lines_added"], edit["lines_removed"], edit["tool"]) == ("edit", 2, 1, "Edit")
    assert (t["toolu_v_write:0"]["op"], t["toolu_v_write:0"]["lines_added"]) == ("create", 3)
    assert "toolu_v_ts:0" not in t


def test_v4_continuation(v4_store):
    row = one(v4_store, "session_continuation")
    assert (row["child_uid"], row["parent_uid"], row["kind"], row["evidence"]) == (
        "55555555-5555-4555-8555-555555555555", V4_SID, "resume", "continued-in")


def test_v4_subagent_follow_ups(v4_store):
    m = _msgs(v4_store)
    assert "x-001" not in m                                   # the brief lives in the parent
    assert (m["x-003"]["message_class"], m["x-003"]["detail"]) == ("agent_message", {"source": "followup"})
    assert (m["x-004"]["message_class"], m["x-004"]["detail"]) == ("agent_message", {"source": "coordinator"})


@pytest.mark.parametrize("batch", [1, 3, 7])
def test_v4_batch_independence(batch):
    paths = [V4_MAIN, V4_SUB]
    assert _comparable(load(*paths, batch_lines=batch)) == _comparable(load(*paths))

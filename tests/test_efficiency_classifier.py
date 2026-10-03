"""A tool result is the next model call's trigger, not the tool invocation."""

from agent_history.efficiency import classify_exec, classify_tool_result


def test_exec_trigger_rules():
    assert classify_exec("") == "noop"
    assert classify_exec("const r = await tools.clock__curr_time({}); text(r)") == "status"
    assert classify_exec('const r = await tools.exec({cmd:"git status"}); text(r)') == "status"
    assert classify_exec('const r = await tools.exec({cmd:"git status"}); await tools.exec({cmd:"pytest"})') == "work"
    assert classify_exec("tools.gh run watch 10 --exit-status") == "wait"
    assert classify_exec("tools.git commit -- file") == "work"


def test_tool_result_across_harnesses():
    assert classify_tool_result("pi", "bash", {"command": "git status"}, {}) == "status"
    assert classify_tool_result("pi", "subagent", {"action": "wait"}, {"results": []}) == "wait"
    assert classify_tool_result("pi", "subagent", {"action": "wait"}, {"results": [1]}) == "event"
    assert classify_tool_result("pi", "subagent", {"workflowScript": "return 1"}, {}) == "orchestrate"
    assert classify_tool_result("codex", "wait_agent", {}, {"timed_out": True}) == "wait"
    assert classify_tool_result("codex", "wait_agent", {}, {"timed_out": False}) == "event"
    assert classify_tool_result("codex", "spawn_agent", {}, {}) == "orchestrate"
    assert classify_tool_result("claude", "TaskOutput", {}, {"task": {"status": "running"}}) == "wait"
    assert classify_tool_result("claude", "TaskOutput", {}, {"task": {"status": "completed"}}) == "event"
    assert classify_tool_result("claude", "Agent", {}, {}) == "orchestrate"


def test_markerless_process_poll_output_counts_as_timed_out():
    from agent_history.efficiency.parser import efficiency_codex_result

    assert efficiency_codex_result("process", "Script completed\nOutput: building...") == "timed_out"
    assert efficiency_codex_result("process", "Process exited with code 0") == "event"

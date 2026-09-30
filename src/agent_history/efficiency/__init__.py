"""Efficiency trigger rules shared by transcript parsing and SQL verification.

A result changes the trigger of the *next* model call. This module does not yet
perform ordering, session attribution or incremental parsing.
"""

from __future__ import annotations

import json
import re
from typing import Any

_WAIT = re.compile(
    r'write_stdin\(\{[^}]*chars:\s*""|"chars":\s*""|tools\.wait\(|\bsleep\s+\d|tools\.sleep|setTimeout|wait_agent'
    r"|gh run watch|--watch\b|\buntil\b[^\n]{0,200}\bdo\b|while [^\n]{0,200}sleep|timeout \d+ .*(tail -f|wait)",
    re.S,
)
_STATUS = re.compile(
    r"gh (run|pr) (view|list|checks|status)|gh api [^\n]*(actions/runs|check-runs|pulls)"
    r"|git (fetch|log|status|rev-parse|ls-remote|worktree list)|backlog task (list|view)|list_agents"
    r"|\b(cat|tail|head|sed -n|wc)\b[^\n]{0,160}(state-|report-|outcomes-|\.notified|\.claim|lane|loop\d|wave\d)"
    r"|\bps\b|pgrep|curr_time",
    re.S,
)
_CLOCK = re.compile(r"\s*const r\s*=\s*await tools\.clock__curr_time\(\{\}\);\s*text\(r\)\s*")
_COMMAND = re.compile(r'cmd:\s*"((?:[^"\\]|\\.)*)"')
_CODEX_TOOL = {
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
_CLAUDE_TOOL = {
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


def _object(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    return value if isinstance(value, dict) else {}


def classify_exec(text: str) -> str:
    """Classify shell/code-mode text by the private collector's trigger precedence."""
    if not text or "tools." not in text and "$" not in text and len(text) < 80:
        return "noop"
    if _CLOCK.fullmatch(text):
        return "status"
    if _WAIT.search(text):
        return "wait"
    commands = _COMMAND.findall(text) or [text]
    return "status" if all(_STATUS.search(command) for command in commands) else "work"


def classify_tool_result(agent: str, name: str, arguments: Any, result: Any) -> str:
    """Return the next-call trigger for a completed tool call, without event ordering."""
    args, output = _object(arguments), _object(result)
    if agent == "pi":
        if name == "bash":
            return classify_exec("tools." + (args.get("command") if isinstance(args.get("command"), str) else ""))
        if name == "subagent":
            action = args.get("action")
            if action in ("list", "status"):
                return "status"
            if action == "wait":
                return "event" if output.get("results") else "wait"
            if args.get("workflowScript") is not None or action in ("run", "start", "spawn") or args.get("tasks"):
                return "orchestrate"
        if name in ("watch_start", "wake_at"):
            return "orchestrate"
        return "work"
    if agent == "codex":
        tool = name.rsplit(".", 1)[-1]
        kind = _CODEX_TOOL.get(tool)
        if kind == "wait":
            if tool == "wait_agent":
                return (
                    "event"
                    if output.get("timed_out") is False or re.search(r'"?timed_out"?\s*:\s*false', str(result))
                    else "wait"
                )
            return (
                "event"
                if re.search(r'Process exited with code -?\d+|"?exit_code"?\s*:\s*-?\d+', str(result))
                else "wait"
            )
        if kind:
            return kind
        if tool in ("exec", "exec_command", "js", "run", "shell", "write_stdin"):
            command = args.get("cmd") or args.get("command") or args.get("script") or ""
            if isinstance(arguments, str) and not args:
                command = arguments
            kind = classify_exec(str(command))
            if kind == "wait" and "tools.sleep" not in str(command) and "setTimeout" not in str(command):
                return (
                    "event"
                    if re.search(r'Process exited with code -?\d+|"?exit_code"?\s*:\s*-?\d+', str(result))
                    else "wait"
                )
            return kind
        return "work"
    if agent == "claude":
        kind = _CLAUDE_TOOL.get(name)
        if kind == "wait":
            if name == "TaskOutput":
                task = output.get("task") if isinstance(output.get("task"), dict) else output
                return "wait" if task.get("status") in ("running", "pending") else "event"
            return (
                "wait"
                if output.get("interrupted") or output.get("timedOutAfterMs") or output.get("backgroundTaskId")
                else "event"
            )
        if kind:
            return kind
        if name == "Bash":
            if args.get("run_in_background"):
                return "work"
            kind = classify_exec("tools." + str(args.get("command") or ""))
            command = str(args.get("command") or "")
            if kind == "wait" and re.search(r"^\s*sleep|;\s*sleep|until |while ", command.lower()):
                return "wait"
            return (
                "event"
                if kind == "wait"
                and not any(output.get(key) for key in ("interrupted", "timedOutAfterMs", "backgroundTaskId"))
                else kind
            )
        return "work"
    raise ValueError("unsupported agent")

"""antigravity-* namespaces: Claude-shaped transcripts (written by a converter) parsed by the Claude parser, labelled 'antigravity'.

Fixtures are synthetic and built here; the spool layout is projects/<encoded-cwd>/<conversation>.jsonl.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_history import load
from agent_history.config import NAMESPACE, parse_config
from agent_history.memstore import MemStore, run_file
from agent_history.metrics.efficiency import EfficiencyCollector
from agent_history.parse_claude import ClaudeParser

SID = "22222222-2222-4222-8222-222222222222"
CWD = "/home/tester/repo"
PROJECT = "-home-tester-repo"


def line(record: dict) -> str:
    return json.dumps(record, separators=(",", ":")) + "\n"


def base(kind: str, uuid: str, ts: str, **extra) -> dict:
    return {
        "type": kind,
        "uuid": uuid,
        "parentUuid": None,
        "sessionId": SID,
        "cwd": CWD,
        "timestamp": ts,
        "gitBranch": "main",
        "version": "1.0.0",
        "source": "antigravity",
        **extra,
    }


TRANSCRIPT = "".join(
    [
        line(base("user", "o-1", "2026-10-01T10:00:00.000Z", message={"role": "user", "content": "hello synthetic"})),
        line(
            base(
                "assistant",
                "o-2",
                "2026-10-01T10:00:02.000Z",
                requestId="req_o2",
                message={
                    "id": "msg_o2",
                    "role": "assistant",
                    "model": "synthetic-model",
                    "content": [{"type": "tool_use", "id": "call_1", "name": "bash", "input": {"command": "ls"}}],
                    "usage": {"input_tokens": 10, "output_tokens": 3},
                },
            )
        ),
        line(
            base(
                "user",
                "o-3",
                "2026-10-01T10:00:03.000Z",
                message={
                    "role": "user",
                    "content": [{"type": "tool_result", "tool_use_id": "call_1", "content": "file.txt"}],
                },
            )
        ),
        line(base("system", "o-4", "2026-10-01T10:00:04.000Z", subtype="turn_duration", durationMs=4000)),
    ]
)


def spool(root: Path, namespace: str = "antigravity-personal") -> Path:
    target = root / namespace / "projects" / PROJECT
    target.mkdir(parents=True)
    (target / f"{SID}.jsonl").write_text(TRANSCRIPT)
    return target / f"{SID}.jsonl"


def test_namespace_pattern_and_details():
    assert NAMESPACE.match("antigravity-personal") and not NAMESPACE.match("antigravity_personal")
    assert load.namespace_details("antigravity-personal") == ("antigravity", "personal", None)
    assert load.namespace_details("claude-demo") == ("claude", "demo", None)
    assert load.parser_for("antigravity")[0] is ClaudeParser
    assert load.parser_for("antigravity") == load.parser_for("claude")
    assert parse_config({"sources": {"antigravity-personal": "/nonexistent"}}).sources


def test_inventory_and_listing_accept_antigravity(tmp_path):
    path = spool(tmp_path / "hot")
    assert load.list_namespaces(tmp_path / "hot") == ["antigravity-personal"]
    for strict in (False, True):
        entries = load.inventory(tmp_path / "hot", None, strict=strict)
        ((rel, entry),) = entries.items()
        assert rel == f"antigravity-personal/projects/{PROJECT}/{SID}.jsonl"
        assert (entry.agent, entry.role, entry.profile, entry.path) == ("antigravity", "main", "personal", path)


def test_parser_rows_carry_the_antigravity_label(tmp_path):
    path = spool(tmp_path)
    entry = next(iter(load.inventory(tmp_path, None).values()))
    ctx = load.FileContext(
        str(path), entry.rel_path, entry.namespace, entry.agent, entry.profile, entry.machine, entry.role
    )
    store = MemStore()
    run_file(ClaudeParser, ctx, store, batch_lines=5000)
    sessions = store.rows("session")
    assert [r["session"].agent for r in sessions] == ["antigravity"]
    assert not store.rows("parse_issue")  # the turn_duration marker is a recognised system record
    for table in ("message", "llm_call", "tool_call"):
        rows = store.rows(table)
        assert rows and {r["session"].agent if "session" in r else r["agent"] for r in rows} == {"antigravity"}, table
    assert [r["tool_name"] for r in store.rows("tool_call")] == ["bash"]


def test_efficiency_counts_antigravity_calls(tmp_path):
    spool(tmp_path / "home")
    config = parse_config(
        {
            "sources": {"antigravity-personal": str(tmp_path / "home" / "antigravity-personal")},
            "efficiency": {"baseline_ts": 0, "first_parse_days": 1000},
        }
    )
    families = EfficiencyCollector(config, tmp_path / "state").collect()
    calls = [
        s
        for f in families
        if f.name == "agent_efficiency_llm_calls_total"
        for s in f.samples
        if dict(s.labels)["agent"] == "antigravity"
    ]
    assert calls and sum(s.value for s in calls) == 1
    assert {dict(s.labels)["namespace"] for s in calls} == {"antigravity-personal"}


@pytest.mark.parametrize("name", ["antigravity-personal", "claude-demo"])
def test_efficiency_session_keys_treat_both_as_claude_shaped(name):
    from agent_history.efficiency.parser import efficiency_session_keys

    session, parent = efficiency_session_keys(f"{name}/projects/{PROJECT}/{SID}.jsonl", {})
    assert session is not None and parent is None and SID in session


def test_efficiency_session_keys_use_the_namespace_agent():
    from agent_history.efficiency.parser import build_loop_map, efficiency_session_keys, LoopMap

    base = f"antigravity-toy/projects/{PROJECT}/{SID}"
    assert efficiency_session_keys(f"{base}.jsonl", {}) == (f"antigravity\t{SID}\t", None)
    assert efficiency_session_keys(f"{base}/subagents/agent-a1.jsonl", {}) == (
        f"antigravity\t{SID}\ta1",
        f"antigravity\t{SID}\t",
    )
    assert efficiency_session_keys(f"claude-toy/projects/{PROJECT}/{SID}.jsonl", {})[0] == f"claude\t{SID}\t"
    # An antigravity catalogue row resolves a loop label through its transcript key.
    loops = LoopMap(build_loop_map([("antigravity", SID, "", "r", "c", "n", 1, 100.0, 200.0, "closeout")], [], 1000.0))
    key, parent = efficiency_session_keys(f"{base}.jsonl", {})
    assert loops.label(key, parent, 150.0) != "none"

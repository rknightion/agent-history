"""`agent-history loop <id|closeout path> --usage`: target resolution and the Markdown block's shape."""

import json
import shutil
from pathlib import Path

import pytest

from agent_history import reader

SAMPLE_LEDGER = Path(__file__).parent / "fixtures" / "loop_usage" / "landings.sample.jsonl"

SUMMARY = {"as_of": "2026-10-18T12:00:00Z", "wall_s": "93780", "heuristic_sessions": "0"}
ROWS = [
    {"role": "root", "model": "model-a", "calls": "1200", "cost": "40.5", "unpriced": "0"},
    {"role": "worker", "model": "model-b", "calls": "30", "cost": "", "unpriced": "30"},
    {"role": "worker", "model": "model-a", "calls": "4", "cost": "1.255", "unpriced": "0"},
]


def test_usage_markdown_shape():
    text = reader.usage_markdown(SUMMARY, ROWS, 1)
    assert text.splitlines() == [
        "Usage as of 2026-10-18T12:00:00Z (agent-history catalogue, list prices)",
        "",
        "- Wall time: 1d 2h 3m",
        "- LLM calls: 1,234",
        "- List-price cost: $41.76",
        "- Unpriced calls: 30 (not in the cost total)",
        "",
        "| Role | Model | Calls | Cost |",
        "| --- | --- | ---: | ---: |",
        "| root | model-a | 1,200 | $40.50 |",
        "| worker | model-b | 30 | unpriced |",
        "| worker | model-a | 4 | $1.26 |",
    ]


def test_usage_markdown_states_shared_closeouts_and_heuristic_links():
    summary = {**SUMMARY, "wall_s": "59", "heuristic_sessions": "2"}
    rows = [{**ROWS[0], "role": "a|b"}]
    lines = reader.usage_markdown(summary, rows, 3).splitlines()
    assert "- Wall time: 0h 0m" in lines
    assert "- Launches: 3 share this closeout and are counted as one run" in lines
    assert any(line.startswith("- Heuristic-linked sessions: 2 ") for line in lines)
    assert not any(line.startswith("- Unpriced") for line in lines)
    assert lines[-1] == "| a\\|b | model-a | 1,200 | $40.50 |"
    assert (
        reader.usage_markdown(summary, [], 1).splitlines()[-1]
        == "- Heuristic-linked sessions: 2 (attributed by timing, not by an explicit link)"
    )


ROOT = {
    "peak_context": "412345",
    "compactions": "3",
    "compactions_typed": "1",
    "compactions_untyped": "0",
    "ask_calls": "2",
    "ask_blocked_s": "5400",
    "ask_open": "0",
    "ask_untimed": "0",
}
LANES = [
    {"role": "reviewer", "lanes": "3", "complete": "3"},
    {"role": "worker", "lanes": "8", "complete": "6"},
]
TOOLS = [
    {"kind": "denial", "label": "user-rejected", "n": "5"},
    {"kind": "denial", "label": "automode-blocked", "n": "2"},
    {"kind": "timeout", "label": "", "n": "4"},
    {"kind": "agent", "label": "root:claude", "n": "1"},
    {"kind": "agent", "label": "lane:claude", "n": "11"},
]


def agents(*labels):
    return [{"kind": "agent", "label": label, "n": "1"} for label in labels]


def fake_query(summary=SUMMARY, rows=ROWS, root=ROOT, lanes=LANES, tools=TOOLS, seen=None):
    """Answer each usage query from its own SQL, as the catalogue would."""

    def query(sql, variables):
        if seen is not None:
            seen.append((sql, variables))
        if sql.startswith("SELECT id FROM ah.loop_run WHERE id ="):
            return [{"id": sql.split("=")[1].strip(" ;")}]
        if "report_path = :'path'" in sql:
            return [{"id": "4"}, {"id": "9"}]
        if "AS peak_context" in sql:
            return [root]
        if "FROM ah.lane ln" in sql:
            return lanes
        if "AS kind" in sql:
            return tools
        return [summary] if "AS as_of" in sql else rows

    return query


def detail_block(summary=SUMMARY, rows=ROWS, root=ROOT, lanes=LANES, tools=TOOLS, ledger=None, runs=1):
    detail = {"root": root, "lanes": lanes, "tools": tools, "ledger": ledger}
    return reader.usage_markdown(summary, rows, runs, detail).splitlines()


def test_loop_usage_resolves_a_closeout_path_to_every_launch(monkeypatch, capsys):
    seen = []
    monkeypatch.setattr(reader, "query", fake_query(seen=seen))

    assert reader.main(["loop", "/synthetic/codex/../codex/closeout-synthetic-loop14.md", "--usage"]) == 0
    assert seen[0][1] == {"path": "/synthetic/codex/closeout-synthetic-loop14.md"}
    assert all("ARRAY[4,9]::bigint[]" in sql for sql, _ in seen[1:])
    out = capsys.readouterr().out
    assert out.startswith("Usage as of 2026-10-18T12:00:00Z")
    assert "- Launches: 2 share this closeout" in out


@pytest.mark.parametrize(
    ("target", "message"),
    [
        (
            "/synthetic/codex/closeout-missing-loop1.md",
            "agent-history: no loop run has closeout path /synthetic/codex/closeout-missing-loop1.md",
        ),
        ("42", "agent-history: no loop run with id 42"),
        ("codex/closeout-relative-loop1.md", "agent-history: loop takes a numeric id or an absolute closeout path"),
    ],
)
def test_unknown_loop_exits_non_zero_with_one_line(monkeypatch, target, message):
    monkeypatch.setattr(reader, "query", lambda sql, variables: [])
    with pytest.raises(SystemExit) as raised:
        reader.main(["loop", target, "--usage"])
    assert raised.value.code == message and "\n" not in message


V4 = {
    **SUMMARY,
    "wall_s": "14400",
    "landed": "3",
    "parked": "1",
    "harness": "claude 2.1.4",
    "root_agents": "claude",
}


def test_stamp_names_the_harness_and_what_the_catalogue_does_not_record():
    lines = detail_block(V4)
    assert "- Harness: claude 2.1.4" in lines
    assert "- Plugin and agent-docs versions: not recorded in the catalogue" in lines
    assert "- Landing mode: unknown" in lines
    assert "- Harness: unknown" in detail_block({**V4, "harness": ""})


def test_a_relaunch_across_harnesses_names_both():
    both = {**V4, "harness": "claude 2.1.4, codex 0.9.1", "root_agents": "claude,codex"}
    assert "- Harness: claude 2.1.4, codex 0.9.1" in detail_block(both)


def test_landed_and_parked_come_from_the_closeout_when_there_is_no_ledger():
    lines = detail_block(V4)
    assert "- Landed: 3 tasks (from the closeout)" in lines
    assert "- Parked: 1 task (from the closeout)" in lines
    lines = detail_block({**V4, "landed": "", "parked": ""})
    assert "- Landed: unknown" in lines and "- Parked: unknown" in lines


def test_root_calls_and_calls_per_landed_task():
    lines = detail_block(V4)
    assert "- Root calls: 1,200" in lines
    assert "- Root calls per landed task: 400.0" in lines
    lines = detail_block({**V4, "landed": "0"})
    assert "- Root calls per landed task: unknown (no landed tasks counted)" in lines
    assert "- Root calls per landed task: unknown (no landed tasks counted)" in detail_block({**V4, "landed": ""})


def test_peak_root_context():
    assert "- Peak root context: 412,345 tokens" in detail_block(V4)
    assert "- Peak root context: unknown" in detail_block(V4, root={**ROOT, "peak_context": ""})


def test_compactions_and_how_many_the_owner_typed():
    assert "- Compactions: 3 (typed by the owner: 1)" in detail_block(V4)
    root = {**ROOT, "compactions": "0", "compactions_typed": "0"}
    assert "- Compactions: 0 (typed by the owner: 0)" in detail_block(V4, root=root)
    # Codex and pi record no trigger: the typed count is then unknown, not the manual rows alone.
    root = {**ROOT, "compactions": "4", "compactions_typed": "1", "compactions_untyped": "2"}
    line = "- Compactions: 4 (typed by the owner: unknown, 2 with no trigger recorded)"
    assert line in detail_block(V4, root=root)


def test_root_question_tool_calls_and_blocked_hours():
    assert "- Root question-tool calls: 2 (blocked 1.50 hours)" in detail_block(V4)
    root = {**ROOT, "ask_calls": "0", "ask_blocked_s": "0"}
    assert "- Root question-tool calls: 0 (blocked 0.00 hours)" in detail_block(V4, root=root)


def test_question_tools_on_codex_count_and_pi_records_none():
    codex = {**V4, "root_agents": "codex"}
    assert "- Root question-tool calls: 2 (blocked 1.50 hours)" in detail_block(codex)
    pi = {**V4, "root_agents": "pi"}
    assert "- Root question-tool calls: unknown (no question tool recorded for pi)" in detail_block(pi)
    assert "- Root question-tool calls: unknown (no question tool recorded)" in detail_block({**V4, "root_agents": ""})
    both = {**V4, "root_agents": "claude,pi"}
    assert "- Root question-tool calls: 2 (blocked 1.50 hours, not recorded for pi)" in detail_block(both)


def test_an_open_question_is_open_not_zero_hours():
    root = {**ROOT, "ask_calls": "1", "ask_blocked_s": "0", "ask_open": "1"}
    line = "- Root question-tool calls: 1 (blocked 0.00 hours, 1 still open at closeout)"
    assert line in detail_block(V4, root=root)
    root = {**ROOT, "ask_calls": "2", "ask_untimed": "1"}
    line = "- Root question-tool calls: 2 (blocked 1.50 hours, 1 with no duration recorded)"
    assert line in detail_block(V4, root=root)


def test_lane_bash_timeouts_count_only_timeout_rows():
    assert "- Lane Bash timeouts: 4" in detail_block(V4)
    assert "- Lane Bash timeouts: 0" in detail_block(V4, tools=TOOLS[:2])


def test_timeouts_and_denials_are_not_recorded_for_codex_and_pi_sessions():
    """Only the Claude parser records a timeout or a denial: another harness is unknown, not 0."""
    codex = [r for r in TOOLS if r["kind"] != "agent"] + agents("root:codex", "lane:codex", "lane:pi")
    lines = detail_block(V4, tools=codex)
    assert "- Lane Bash timeouts: unknown (not recorded for codex, pi lanes)" in lines
    assert "- Denials by class: not recorded for codex, pi" in lines
    mixed = [r for r in TOOLS if r["kind"] != "agent"] + agents("root:claude", "lane:claude", "lane:pi")
    lines = detail_block(V4, tools=mixed)
    assert "- Lane Bash timeouts: 4 (not recorded for pi lanes)" in lines
    assert "- Denials by class: recorded for claude sessions only, not for pi" in lines
    lines = detail_block(V4)
    assert not any(line.startswith("- Denials by class") for line in lines)


def test_lane_complete_rate_by_role():
    lines = detail_block(V4)
    at = lines.index("| Lane role | Lanes | Complete | Complete rate |")
    assert lines[at - 1] == "" and lines[at + 1] == "| --- | ---: | ---: | ---: |"
    assert lines[at + 2 : at + 4] == ["| reviewer | 3 | 3 | 100% |", "| worker | 8 | 6 | 75% |"]
    assert "| Lane role | Lanes | Complete | Complete rate |" not in detail_block(V4, lanes=[])


def test_spend_share_by_role():
    lines = detail_block(V4)
    # The worker role has 30 unpriced calls in ROWS: its figure is a floor and the shares are of
    # priced cost only, so they are not offered as complete.
    at = lines.index("| Role | Cost | Share of priced cost |")
    assert lines[at - 1] == "" and lines[at + 1] == "| --- | ---: | ---: |"
    assert lines[at + 2 : at + 4] == ["| root | $40.50 | 97.0% |", "| worker | $1.26 (partial) | 3.0% (partial) |"]
    free = [{**ROWS[0], "cost": "", "unpriced": "1200"}]
    lines = detail_block(V4, rows=free)
    assert "| root | unpriced | unpriced |" in lines
    assert not any(line.startswith("| root |") and ("$0.00" in line or "0.0%" in line) for line in lines)
    # Fully priced roles keep plain figures under the plain heading.
    lines = detail_block(V4, rows=[ROWS[0], {**ROWS[2], "unpriced": "0"}])
    assert "| Role | Cost | Share of cost |" in lines
    assert "| worker | $1.26 | 3.0% |" in lines


def test_denials_by_class():
    lines = detail_block(V4)
    at = lines.index("| Denial class | Denials |")
    assert lines[at - 1] == "" and lines[at + 1] == "| --- | ---: |"
    assert lines[at + 2 : at + 4] == ["| user-rejected | 5 |", "| automode-blocked | 2 |"]
    assert "| Denial class | Denials |" not in detail_block(V4, tools=TOOLS[2:])


def test_the_new_numbers_sit_between_the_old_bullets_and_the_model_table():
    lines = detail_block(V4)
    assert lines[2:5] == ["- Wall time: 4h 0m", "- LLM calls: 1,234", "- List-price cost: $41.76"]
    assert lines.index("- Harness: claude 2.1.4") == 6 and lines.index("| Role | Model | Calls | Cost |") > 15
    assert lines.index("| Role | Cost | Share of priced cost |") > lines.index("| root | model-a | 1,200 | $40.50 |")


EVENT_KEYS = ("v", "ts", "event", "outcome", "pr", "head", "issues", "merge_sha", "main", "deploy", "fix_forward")


def tool_shape(event):
    """What the landing tool never writes. `fix_forward` is set only on a `ready` (outcome `go`) or a
    `refused` event and is null on `merged` and `verified`; a `merged` event has no outcome."""
    if not isinstance(event, dict):
        return []
    problems = []
    if event.get("event") in ("merged", "verified") and event.get("fix_forward") is not None:
        problems.append("fix_forward on " + event["event"])
    if event.get("event") == "merged" and event.get("outcome") is not None:
        problems.append("outcome on merged")
    if event.get("fix_forward") is not None and event.get("event") not in ("ready", "refused"):
        problems.append("fix_forward on " + str(event.get("event")))
    return problems


def ev(event, pr=None, head=None, issues=(), **fields):
    """An event as the tool writes it: the whole schema, with only that event's fields set."""
    row = dict.fromkeys(EVENT_KEYS)
    row |= {"v": 1, "ts": "2026-11-02T09:00:00Z", "event": event, "pr": pr, "head": head, "issues": list(issues)}
    row |= {"slot_seconds": None, **fields}
    assert not tool_shape(row), tool_shape(row)
    return row


def ledger_text(*events):
    return "".join(json.dumps(event) + "\n" for event in events)


def lines_of(text):
    return [json.loads(line) for line in text.splitlines() if line.strip().startswith("{")]


def ledger_beside_closeout(tmp_path, name="landings.jsonl", text=None):
    closeout = tmp_path / "codex" / "closeout-2026-11-02-loop7.md"
    run = closeout.parent / "loop7"
    run.mkdir(parents=True)
    if text is None:
        shutil.copy(SAMPLE_LEDGER, run / name)
    else:
        # Every ledger a test writes is held to the tool's shape.
        assert not [p for event in lines_of(text) for p in tool_shape(event)]
        (run / name).write_text(text)
    return str(closeout)


def reader_event(event):
    return {key: event.get(key) for key in reader.LEDGER_KEPT}


def with_events(*events, summary=V4):
    ledger = {"too_large": False, "unreadable": 0, "events": [reader_event(e) for e in events]}
    return detail_block(summary, ledger=ledger)


@pytest.mark.parametrize("path", sorted(SAMPLE_LEDGER.parent.glob("*.jsonl")), ids=lambda p: p.name)
def test_fixture_ledgers_have_the_shape_the_landing_tool_writes(path):
    events = lines_of(path.read_text())
    assert events and not [p for event in events for p in tool_shape(event)]
    assert any(e["event"] == "ready" and e["fix_forward"] for e in events)
    assert all(e["outcome"] is None for e in events if e["event"] == "merged")


def test_the_shape_check_rejects_what_the_tool_does_not_write():
    assert tool_shape({"event": "merged", "fix_forward": "reason", "outcome": "merged"}) == [
        "fix_forward on merged",
        "outcome on merged",
        "fix_forward on merged",
    ]
    assert tool_shape({"event": "verified", "fix_forward": "reason"})[0] == "fix_forward on verified"
    assert tool_shape({"event": "ready", "outcome": "go", "fix_forward": "reason"}) == []
    assert tool_shape({"event": "refused", "outcome": "held", "fix_forward": "reason"}) == []
    with pytest.raises(AssertionError):
        ev("merged", 1, "a", outcome="merged")
    with pytest.raises(AssertionError):
        ev("verified", 1, "a", outcome="landed", fix_forward="reason")


def test_a_ledger_beside_the_closeout_adds_landings_and_their_rows(tmp_path):
    ledger = reader.read_ledger(ledger_beside_closeout(tmp_path))
    lines = detail_block(V4, ledger=ledger)
    # Landed counts the issues of landings that merged: 101, 102 and 103, not the abandoned 104.
    assert "- Landed: 3 tasks (from the ledger)" in lines
    assert "- Landing mode: pull request (from the ledger)" in lines
    assert "- Landings per hour: 0.50 (2 landings in 4h 0m)" in lines
    assert "- Fix-forward landings: 1" in lines
    at = lines.index("| Landing | Outcome | Slot time |")
    assert lines[at - 1] == "" and lines[at + 1] == "| --- | --- | ---: |"
    assert lines[at + 2 :] == [
        "| PR 11 (1111111) | landed | 14m 35s |",
        "| PR 12 (2222222) | main-red | 19m 00s |",
        "| PR 13 (3333333) | abandoned | 40m 00s |",
    ]


def merged_landing(pr, head, issues, **fields):
    return [
        ev("ready", pr, head, issues, outcome="go", **fields),
        ev("merged", pr, head, issues, merge_sha="m" * 40),
    ]


def test_ledger_issues_replace_the_closeout_count():
    events = [*merged_landing(1, "a", ["X-1", "X-2"]), ev("refused", 2, "b", ["X-3"], outcome="held")]
    lines = with_events(*events, summary={**V4, "landed": "9"})
    assert "- Landed: 2 tasks (from the ledger)" in lines
    assert "- Root calls per landed task: 600.0" in lines
    # A ledger with no landing falls back to the closeout's count.
    assert "- Landed: 9 tasks (from the closeout)" in with_events(events[-1], summary={**V4, "landed": "9"})


def test_a_direct_mode_landing_is_keyed_by_its_head():
    events = [
        ev("ready", None, "0123456789abcdef0123", outcome="go"),
        ev("verified", None, "0123456789abcdef0123", ["X-1"], outcome="landed", main="green", slot_seconds=45),
    ]
    lines = with_events(*events)
    assert "- Landing mode: direct (from the ledger)" in lines
    assert lines[-1] == "| direct 0123456789ab | landed | 45s |"
    assert "- Landed: 1 task (from the ledger)" in lines


def test_ledger_lines_that_are_not_events_are_counted_and_cells_are_escaped(tmp_path):
    text = (
        ledger_text(ev("verified", 5, "h", [1], outcome="a|b\nc", slot_seconds=75))
        + "not json\n[1,2]\n\n"
        + ledger_text(ev("closeout-check", outcome="ok", missing=[]))
    )
    ledger = reader.read_ledger(ledger_beside_closeout(tmp_path, text=text))
    assert ledger["unreadable"] == 2 and len(ledger["events"]) == 1
    lines = detail_block(V4, ledger=ledger)
    assert "- Landing ledger lines that were not events: 2" in lines
    assert "| PR 5 (h) | a\\|b c | 1m 15s |" in lines


def test_read_ledger_only_reads_the_named_file_beside_a_closeout(tmp_path):
    assert reader.read_ledger("") is None
    assert reader.read_ledger(str(tmp_path / "codex" / "notes.md")) is None
    # No ledger beside the closeout, a ledger for another loop, a directory in its place.
    closeout = ledger_beside_closeout(tmp_path)
    other = str(Path(closeout).with_name("closeout-2026-11-03-loop8.md"))
    assert reader.read_ledger(other) is None
    (Path(closeout).parent / "loop9" / "landings.jsonl").mkdir(parents=True)
    assert reader.read_ledger(str(Path(closeout).with_name("closeout-x-loop9.md"))) is None
    # A link is not followed.
    (Path(closeout).parent / "loop10").mkdir()
    (Path(closeout).parent / "loop10" / "landings.jsonl").symlink_to(SAMPLE_LEDGER)
    assert reader.read_ledger(str(Path(closeout).with_name("closeout-x-loop10.md"))) is None


def test_an_oversize_ledger_is_not_read(tmp_path, monkeypatch):
    closeout = ledger_beside_closeout(tmp_path)
    monkeypatch.setattr(reader, "MAX_LEDGER_BYTES", 10)
    ledger = reader.read_ledger(closeout)
    assert ledger == {"too_large": True, "events": [], "unreadable": 0}
    lines = detail_block(V4, ledger=ledger)
    assert "- Landing ledger: too large to read" in lines
    assert "- Landed: 3 tasks (from the closeout)" in lines
    assert not any(line.startswith("| Landing |") for line in lines)


def test_loop_usage_reads_the_ledger_beside_the_closeout_it_resolves(monkeypatch, capsys, tmp_path):
    closeout = ledger_beside_closeout(tmp_path)
    summary = {**V4, "report_path": closeout}
    monkeypatch.setattr(reader, "query", fake_query(summary=summary))
    assert reader.main(["loop", closeout, "--usage"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert "- Landings per hour: 0.50 (2 landings in 4h 0m)" in out
    assert "- Landed: 3 tasks (from the ledger)" in out
    assert "- Fix-forward landings: 1" in out
    assert "| PR 12 (2222222) | main-red | 19m 00s |" in out
    # The ledger is read on this machine only: without the file the block has no landing rows.
    summary = {**V4, "report_path": str(tmp_path / "elsewhere" / "closeout-2026-11-02-loop7.md")}
    monkeypatch.setattr(reader, "query", fake_query(summary=summary))
    assert reader.main(["loop", "7", "--usage"]) == 0
    out = capsys.readouterr().out
    assert "Landings per hour" not in out and "| Landing |" not in out


def test_queries_carry_every_number_the_block_prints():
    ids = "ARRAY[4]::bigint[]"
    root = reader.USAGE_ROOT_SQL.format(ids=ids)
    for needle in ("context_tokens", "ah.compaction", "'manual'", "'AskUserQuestion'", "duration_ms"):
        assert needle in root
    tools = reader.USAGE_TOOLS_SQL.format(ids=ids)
    for needle in ("timed_out", "denial_kind", "role <> 'root'"):
        assert needle in tools
    assert "return_status = 'complete'" in reader.USAGE_LANES_SQL
    summary = reader.USAGE_SUMMARY_SQL.format(ids=ids)
    for needle in ("tasks_landed", "parks_total", "report_path", "cli_version_last"):
        assert needle in summary


def test_landed_issues_come_from_the_merged_or_verified_event_only():
    lines = with_events(
        ev("refused", 1, "a", ["X-8"], outcome="held"),
        ev("ready", 1, "a", ["X-1", "X-9"], outcome="go"),
        ev("merged", 1, "a", ["X-1"], merge_sha="m"),
    )
    assert "- Landed: 1 task (from the ledger)" in lines


def test_an_issue_number_and_its_hash_form_are_one_issue():
    lines = with_events(
        ev("merged", 1, "a", [5, "#5", " #5 ", "6"]),
        ev("merged", 2, "b", ["#6", None, True, ""]),
    )
    assert "- Landed: 2 tasks (from the ledger)" in lines


def test_a_merged_landing_that_names_no_issue_does_not_zero_the_count():
    lines = with_events(ev("merged", 1, "a", []))
    assert "- Landed: 3 tasks (from the closeout)" in lines
    assert "- Landings per hour: 0.25 (1 landing in 4h 0m)" in lines
    # One landing that names its issues does not hide another that does not.
    lines = with_events(ev("merged", 1, "a", ["X-1"]), ev("merged", 2, "b", []))
    assert "- Landed: 3 tasks (from the closeout)" in lines
    assert "- Landings per hour: 0.50 (2 landings in 4h 0m)" in lines


def test_one_landing_is_singular():
    lines = with_events(ev("merged", 1, "a", ["X-1"]))
    assert "- Landings per hour: 0.25 (1 landing in 4h 0m)" in lines


def test_one_landing_is_one_row_whatever_head_its_events_carry():
    """The landing tool keys a landing by its pull request. A failed verify writes an error event
    with no head; a refusal carries the head it was asked about. Neither makes another landing."""
    lines = with_events(
        *merged_landing(12, "aaaaaaaaaa", ["X-1"]),
        ev("error", 12, None, outcome="error"),
    )
    rows = [line for line in lines if line.startswith("| PR ")]
    assert rows == ["| PR 12 (aaaaaaa) | error | - |"]
    assert "- Landings per hour: 0.25 (1 landing in 4h 0m)" in lines


def fix_forward_attempt(flag):
    """One pull request: a stale-head refusal, then ready, merged and verified on a new head."""
    return [
        ev("refused", 12, "oldoldold", ["X-1"], outcome="stale-head", fix_forward=flag),
        ev("ready", 12, "newnewnew", ["X-1"], outcome="go", fix_forward=flag),
        ev("merged", 12, "newnewnew", ["X-1"], merge_sha="m"),
        ev("verified", 12, "newnewnew", ["X-1"], outcome="landed", main="green", slot_seconds=9),
    ]


def test_a_fix_forward_landing_is_counted_once_from_its_ready_event():
    lines = with_events(*fix_forward_attempt("main is red"))
    assert "- Fix-forward landings: 1" in lines
    assert [line for line in lines if line.startswith("| PR ")] == ["| PR 12 (newnewn) | landed | 9s |"]
    assert "- Landings per hour: 0.25 (1 landing in 4h 0m)" in lines


def test_the_same_landing_without_the_flag_prints_no_fix_forward_line():
    lines = with_events(*fix_forward_attempt(None))
    assert not any(line.startswith("- Fix-forward") for line in lines)
    assert [line for line in lines if line.startswith("| PR ")] == ["| PR 12 (newnewn) | landed | 9s |"]


def test_an_abandoned_fix_forward_attempt_is_not_counted():
    lines = with_events(
        ev("ready", 13, "ccccccccc", ["X-2"], outcome="go", fix_forward="main is red"),
        ev("abandoned", 13, "ccccccccc", ["X-2"], outcome="abandoned", slot_seconds=2400),
        ev("refused", 14, "ddddddddd", ["X-3"], outcome="held", fix_forward="main is red"),
    )
    assert not any(line.startswith("- Fix-forward") for line in lines)


def test_only_the_last_go_decides_whether_a_landing_is_fix_forward():
    lines = with_events(
        ev("ready", 12, "a", ["X-1"], outcome="go", fix_forward="main is red"),
        ev("abandoned", 12, "a", ["X-1"], outcome="abandoned", slot_seconds=1),
        ev("ready", 12, "b", ["X-1"], outcome="go"),
        ev("merged", 12, "b", ["X-1"], merge_sha="m"),
    )
    assert not any(line.startswith("- Fix-forward") for line in lines)


def test_a_last_event_without_an_outcome_shows_the_event_name():
    lines = with_events(ev("merged", 3, "dddddddd", ["X-1"]))
    assert "| PR 3 (ddddddd) | merged | - |" in lines


def test_a_slot_time_that_is_not_a_finite_number_is_not_shown(tmp_path):
    text = "".join(
        '{"event":"verified","outcome":"landed","pr":%d,"head":"h%d","issues":[%d],"slot_seconds":%s}\n' % (n, n, n, v)
        for n, v in enumerate(["NaN", "Infinity", "-Infinity", '"90"', "true", "null", "1e999", "90"], 1)
    )
    ledger = reader.read_ledger(ledger_beside_closeout(tmp_path, text=text))
    lines = detail_block(V4, ledger=ledger)
    slots = [line.strip("|").split("|")[-1].strip() for line in lines if line.startswith("| PR ")]
    assert slots == ["-"] * 7 + ["1m 30s"]

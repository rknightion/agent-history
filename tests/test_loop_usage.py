"""`agent-history loop <id|closeout path> --usage`: target resolution and the Markdown block's shape."""

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
}
LANES = [
    {"role": "reviewer", "lanes": "3", "complete": "3"},
    {"role": "worker", "lanes": "8", "complete": "6"},
]
TOOLS = [
    {"kind": "denial", "label": "user-rejected", "n": "5"},
    {"kind": "denial", "label": "automode-blocked", "n": "2"},
    {"kind": "timeout", "label": "", "n": "4"},
]


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


V4 = {**SUMMARY, "wall_s": "14400", "landed": "3", "parked": "1", "harness": "claude", "harness_version": "2.1.4"}


def test_stamp_names_the_harness_and_what_the_catalogue_does_not_record():
    lines = detail_block(V4)
    assert "- Harness: claude 2.1.4" in lines
    assert "- Plugin and agent-docs versions: not recorded in the catalogue" in lines
    assert "- Landing mode: unknown" in lines
    unknown = {**V4, "harness": "", "harness_version": ""}
    assert "- Harness: unknown" in detail_block(unknown)


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
    assert "- Compactions: 3 (1 typed by the owner)" in detail_block(V4)
    root = {**ROOT, "compactions": "0", "compactions_typed": "0"}
    assert "- Compactions: 0 (0 typed by the owner)" in detail_block(V4, root=root)
    # Codex and pi record no trigger: those are not counted as typed or as automatic.
    root = {**ROOT, "compactions": "4", "compactions_typed": "1", "compactions_untyped": "2"}
    assert "- Compactions: 4 (1 typed by the owner, 2 with no trigger recorded)" in detail_block(V4, root=root)


def test_root_question_tool_calls_and_blocked_hours():
    assert "- Root question-tool calls: 2 (blocked 1.50 hours)" in detail_block(V4)
    root = {**ROOT, "ask_calls": "0", "ask_blocked_s": "0"}
    assert "- Root question-tool calls: 0 (blocked 0.00 hours)" in detail_block(V4, root=root)


def test_lane_bash_timeouts_count_only_timeout_rows():
    assert "- Lane Bash timeouts: 4" in detail_block(V4)
    assert "- Lane Bash timeouts: 0" in detail_block(V4, tools=TOOLS[:2])


def test_lane_complete_rate_by_role():
    lines = detail_block(V4)
    at = lines.index("| Lane role | Lanes | Complete | Complete rate |")
    assert lines[at - 1] == "" and lines[at + 1] == "| --- | ---: | ---: | ---: |"
    assert lines[at + 2 : at + 4] == ["| reviewer | 3 | 3 | 100% |", "| worker | 8 | 6 | 75% |"]
    assert "| Lane role | Lanes | Complete | Complete rate |" not in detail_block(V4, lanes=[])


def test_spend_share_by_role():
    lines = detail_block(V4)
    at = lines.index("| Role | Cost | Share of cost |")
    assert lines[at - 1] == "" and lines[at + 1] == "| --- | ---: | ---: |"
    assert lines[at + 2 : at + 4] == ["| root | $40.50 | 97.0% |", "| worker | $1.26 | 3.0% |"]
    free = [{**ROWS[0], "cost": "", "unpriced": "1200"}]
    assert "| root | $0.00 | n/a |" in detail_block(V4, rows=free)


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
    assert lines.index("| Role | Cost | Share of cost |") > lines.index("| root | model-a | 1,200 | $40.50 |")


def ledger_beside_closeout(tmp_path, name="landings.jsonl", text=None):
    closeout = tmp_path / "codex" / "closeout-2026-11-02-loop7.md"
    run = closeout.parent / "loop7"
    run.mkdir(parents=True)
    if text is None:
        shutil.copy(SAMPLE_LEDGER, run / name)
    else:
        (run / name).write_text(text)
    return str(closeout)


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
        "| PR 11 | landed | 14m 35s |",
        "| PR 12 | main-red | 19m 00s |",
        "| PR 13 | abandoned | 40m 00s |",
    ]


def test_ledger_issues_replace_the_closeout_count():
    events = [
        {"event": "merged", "outcome": "merged", "pr": 1, "head": "a", "issues": ["X-1", "X-2"]},
        {"event": "refused", "outcome": "held", "pr": 2, "head": "b", "issues": ["X-3"]},
    ]
    ledger = {"too_large": False, "unreadable": 0, "events": [reader_event(e) for e in events]}
    lines = detail_block({**V4, "landed": "9"}, ledger=ledger)
    assert "- Landed: 2 tasks (from the ledger)" in lines
    assert "- Root calls per landed task: 600.0" in lines
    # A ledger with no landing falls back to the closeout's count.
    ledger["events"] = ledger["events"][1:]
    assert "- Landed: 9 tasks (from the closeout)" in detail_block({**V4, "landed": "9"}, ledger=ledger)


def reader_event(event):
    return {key: None for key in reader.LEDGER_KEPT} | event


def test_a_direct_mode_landing_is_keyed_by_its_head():
    events = [
        reader_event({"event": "ready", "outcome": "go", "head": "0123456789abcdef0123", "issues": ["X-1"]}),
        reader_event({"event": "verified", "outcome": "landed", "head": "0123456789abcdef0123", "slot_seconds": 45}),
    ]
    lines = detail_block(V4, ledger={"too_large": False, "unreadable": 0, "events": events})
    assert "- Landing mode: direct (from the ledger)" in lines
    assert lines[-1] == "| direct 0123456789ab | landed | 45s |"
    assert "- Landed: 1 task (from the ledger)" in lines


def test_ledger_lines_that_are_not_events_are_counted_and_cells_are_escaped(tmp_path):
    text = (
        '{"event":"merged","outcome":"a|b\\nc","pr":5,"head":"h","issues":[1]}\n'
        "not json\n[1,2]\n\n"
        '{"event":"closeout-check","outcome":"ok","pr":null,"head":null}\n'
    )
    ledger = reader.read_ledger(ledger_beside_closeout(tmp_path, text=text))
    assert ledger["unreadable"] == 2 and len(ledger["events"]) == 1
    lines = detail_block(V4, ledger=ledger)
    assert "- Landing ledger lines that were not events: 2" in lines
    assert "| PR 5 | a\\|b c | - |" in lines


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
    assert "| PR 12 | main-red | 19m 00s |" in out
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

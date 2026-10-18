"""`agent-history loop <id|closeout path> --usage`: target resolution and the Markdown block's shape."""

import pytest

from agent_history import reader

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


def test_loop_usage_resolves_a_closeout_path_to_every_launch(monkeypatch, capsys):
    seen = []

    def query(sql, variables):
        seen.append((sql, variables))
        if "report_path" in sql and "FROM ah.loop_run WHERE" in sql:
            return [{"id": "4"}, {"id": "9"}]
        return [SUMMARY] if "as_of" in sql else ROWS

    monkeypatch.setattr(reader, "query", query)
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

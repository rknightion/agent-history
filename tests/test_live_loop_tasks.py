"""Lane brief header grammar and the `loops --live` reader command, without a database."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from agent_history import cli
from agent_history.live_loop_tasks import LANE_HEADER_PATTERN, TITLE_MAX, parse_lane_header

MIGRATION = Path(__file__).resolve().parents[1] / "src/agent_history/sql/migrations/043_live_loop_tasks.sql"
HEADER = "Lane: w1 · Task: ABC-0123 (Fix the widget) · Tier: routine"

# (brief, expected) pairs shared with the database parity test in test_live_loop_tasks_pg.py.
CASES = [
    (HEADER, ("ABC-0123", "Fix the widget")),
    (HEADER + "\n\nBody text naming DEF-0456 (another task).", ("ABC-0123", "Fix the widget")),
    (HEADER.replace("routine", "guarded"), ("ABC-0123", "Fix the widget")),
    (HEADER + "\r\nBody", ("ABC-0123", "Fix the widget")),
    ("Lane: r2 · Task: ABC-0123.2 (Split (part two) of it) · Tier: routine", ("ABC-0123.2", "Split (part two) of it")),
    ("Lane: w3 · Task: abc-7 (Lower-case prefix) · Tier: guarded", ("abc-7", "Lower-case prefix")),
    ("Lane: w1 · Task: ABC-0123 (Title with · a dot) · Tier: routine", ("ABC-0123", "Title with · a dot")),
    ("Lane: w1 · Task: ABC-0123 (" + "x" * TITLE_MAX + ") · Tier: routine", ("ABC-0123", "x" * TITLE_MAX)),
    # Not the header: the task is never guessed from other text.
    ("Lane: w1 · Task: ABC-0123 (" + "x" * (TITLE_MAX + 1) + ") · Tier: routine", None),
    ("", None),
    ("Scout the test layout and report.", None),
    ("Context first.\n" + HEADER, None),
    (" " + HEADER, None),
    (HEADER + " ", None),
    (HEADER.replace("routine", "urgent"), None),
    (HEADER.replace(" · ", " | "), None),
    (HEADER.replace("·", "-"), None),
    ("Lane: w1 · Task: loop12-w1 (Alias, not a tracker id) · Tier: routine", None),
    ("Lane: w1 · Task: ABC-0123 () · Tier: routine", None),
    ("Lane: w1 · Task: ABC-0123 Fix the widget · Tier: routine", None),
    ("Lane:  w1 · Task: ABC-0123 (Fix the widget) · Tier: routine", None),
    ("Lane: · Task: ABC-0123 (Fix the widget) · Tier: routine", None),
    ("lane: w1 · task: ABC-0123 (Fix the widget) · tier: routine", None),
    ("Lane: w1 · Task: ABC-0123 (Fix the widget) · Tier: routine · Extra: yes", None),
]


@pytest.mark.parametrize(("brief", "expected"), CASES)
def test_parse_lane_header(brief, expected):
    assert parse_lane_header(brief) == expected


def test_migration_applies_the_reference_pattern_verbatim():
    # Same pattern text in both places; test_live_loop_tasks_pg.py checks the two engines agree on CASES.
    assert LANE_HEADER_PATTERN.startswith("^Lane: ")
    sql = MIGRATION.read_text()
    assert "'" + LANE_HEADER_PATTERN + "'" in sql
    assert f"<= {TITLE_MAX}" in sql


DOCUMENT = {
    "loops": [
        {
            "loop": 7,
            "launched_at": "2026-10-19T09:00:00.000000Z",
            "status": "waiting",
            "waiting_until": "2026-10-19T12:00:00.000000Z",
            "observed_at": "2026-10-19T10:00:00.000000Z",
            "active_lanes": 2,
            "tasks": [
                {"id": "ABC-0123", "title": "Fix the widget"},
                {"id": "ABC-0124", "title": "Untrusted \x1b[2Jtitle\ttext"},
            ],
        }
    ]
}


def run_live(monkeypatch, tmp_path, *argv):
    config = tmp_path / "config.toml"
    config.write_text('reader_dsn="dbname=synthetic"\n')
    for name in ("AGENT_HISTORY_READER_DSN", "PGSERVICE"):
        monkeypatch.delenv(name, raising=False)
    observed = []

    def process(cmd, **kwargs):
        observed.append((cmd, kwargs))
        out = "live\n" + '"' + json.dumps(DOCUMENT).replace('"', '""') + '"\n'
        return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")

    monkeypatch.setattr(subprocess, "run", process)
    return cli.main(["--config", str(config), *argv]), observed


def test_loops_live_prints_the_function_document_as_json(monkeypatch, capsys, tmp_path):
    status, observed = run_live(
        monkeypatch, tmp_path, "--format", "json", "loops", "--live", "--repo", "example-owner/example-repo"
    )
    assert status == 0
    assert json.loads(capsys.readouterr().out) == DOCUMENT
    cmd, kwargs = observed[0]
    assert "SELECT ah.live_loop_tasks(:'repo')" in kwargs["input"]
    assert "repo=example-owner/example-repo" in cmd
    assert kwargs["env"]["PGDATABASE"] == "synthetic"


def test_loops_live_aligned_marks_an_unknown_loop_number(monkeypatch, capsys, tmp_path):
    loop = {**DOCUMENT["loops"][0], "loop": None, "status": "running", "waiting_until": None, "tasks": []}
    monkeypatch.setitem(DOCUMENT, "loops", [loop])
    status, _ = run_live(monkeypatch, tmp_path, "loops", "--live", "--repo", "example-owner/example-repo")
    assert status == 0
    assert capsys.readouterr().out == "loop?\trunning\t2026-10-19T09:00:00.000000Z\tactive lanes 2\n"


def test_loops_live_aligned_lists_each_loop_and_its_tasks(monkeypatch, capsys, tmp_path):
    status, _ = run_live(monkeypatch, tmp_path, "loops", "--live", "--repo", "example-owner/example-repo")
    assert status == 0
    assert capsys.readouterr().out.splitlines() == [
        "loop7\twaiting\t2026-10-19T09:00:00.000000Z\tactive lanes 2\twaiting until 2026-10-19T12:00:00.000000Z",
        "\tABC-0123\tFix the widget",
        "\tABC-0124\tUntrusted  [2Jtitle text",
    ]


@pytest.mark.parametrize("argv", [["loops", "--live"], ["loops", "--live", "--repo", "not a slug"]])
def test_loops_live_needs_an_owner_repo_slug(monkeypatch, tmp_path, argv):
    config = tmp_path / "config.toml"
    config.write_text('reader_dsn="dbname=synthetic"\n')
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("no query without a valid --repo"))
    with pytest.raises(SystemExit, match="owner/repo"):
        cli.main(["--config", str(config), *argv])


ROLE_SCRIPT = Path(__file__).resolve().parents[1] / "src/agent_history/sql/live_loop_tasks_role.sql"
# psql's part of the role script: -v repo=owner/repo becomes a session setting the plain SQL below
# reads. test_live_loop_tasks_pg.py runs that SQL with the setting made the same way.
PSQL_PREAMBLE = (
    "\\if :{?repo}\nSELECT set_config('ah_loop_tasks.repo', :'repo', false) AS ah_loop_tasks_repo \\gset\n\\endif\n"
)


def test_role_script_takes_the_repo_only_from_its_psql_preamble():
    text = ROLE_SCRIPT.read_text()
    head, marker, body = text.partition(PSQL_PREAMBLE)
    assert marker and all(line.startswith("--") or not line for line in head.splitlines())
    assert "\\" not in body.replace("'\\password ah_loop_tasks'", "") and ":'" not in body

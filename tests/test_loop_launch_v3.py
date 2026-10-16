"""Launch grammar and the compatibility paths used by the catalogue post-pass."""

import hashlib

import pytest

from agent_history import loops
from agent_history.loop_launch import parse_launch, report_target

CWD = "/tmp/synthetic-project"
AT = "2026-10-04T12:00:00Z"
CLOSEOUT = CWD + "/codex/closeout-synthetic-loop14.md"
CHARTER = CWD + "/codex/charter-synthetic-loop14.md"
PROMPT = "You are the root. Read codex/charter-synthetic-loop14.md. Write codex/closeout-synthetic-loop14.md."


@pytest.mark.parametrize("at", ["2020-01-01T00:00:00Z", AT])
def test_v3_recognition_is_shape_based_and_freezes_compatibility_paths(at):
    hit = loops._launch(PROMPT + "\nTime budget: 2h", CWD, at)
    assert hit is not None
    assert (hit["report"], hit["loop"], hit["mode"], hit["budget"]) == (CLOSEOUT, 14, "v3", 7200)
    assert hit["launch_sha256"] == hashlib.sha256((PROMPT + "\nTime budget: 2h").encode()).hexdigest()
    assert loops._report_meta(hit["report"]) == {
        "naming": "loop",
        "repo_slug": "synthetic-project",
        "goal_path": CHARTER,
        "campaign_slug": "synthetic",
    }
    v2 = loops._launch("You are the root. Write codex/report-synthetic-loop14.md.", CWD, at)
    assert (v2["mode"], v2["report"]) == ("v2", CWD + "/codex/report-synthetic-loop14.md")
    assert loops._report_meta(v2["report"])["goal_path"] == CWD + "/codex/goal-synthetic-loop14.md"


def test_v3_tokens_share_resolution_boundaries_and_ambiguity_rules():
    text = PROMPT + " Also write `" + CLOSEOUT + "`."
    hit = parse_launch(text, CWD + "/subdirectory", AT, None)
    assert hit is not None and hit["report"] == CLOSEOUT and hit["mode"] == "v3"
    assert report_target(text, CWD + "/subdirectory", "v3") == {"report": CLOSEOUT, "loop": 14}
    assert parse_launch(PROMPT + " And codex/closeout-other-loop14.md", CWD, AT, None) is None
    assert parse_launch("You are the root. codex/closeout-synthetic-loop14.md.backup", CWD, AT, None) is None
    assert parse_launch("Please review codex/closeout-synthetic-loop14.md", CWD, AT, None) is None
    assert report_target("codex/report-synthetic-loop14.md", CWD, "v3") is None
    assert report_target("codex/report-synthetic-loop14.md", CWD, "v2")["loop"] == 14


@pytest.mark.parametrize(
    "reports,closeouts",
    [
        (["synthetic-loop14"], ["notes-loop1"]),
        (["synthetic-loop14"], ["notes-loop1", "other-loop2"]),
        (["synthetic-loop14", "second-loop15"], ["notes-loop1"]),
    ],
)
def test_report_tokens_keep_v2_recognition_and_ambiguity_with_incidental_closeouts(reports, closeouts):
    text = "You are the root. Write " + " or ".join(f"codex/report-{name}.md" for name in reports)
    text += ". Background notes: " + " and ".join(f"codex/closeout-{name}.md" for name in closeouts) + "."
    parsed = parse_launch(text, CWD, AT, "1970-01-01T00:00:00Z")
    catalogue = loops._launch(text, CWD, AT)
    if len(reports) > 1:
        assert parsed is None and catalogue is None
    else:
        for hit in (parsed, catalogue):
            assert hit is not None
            assert (hit["mode"], hit["report"], hit["loop"]) == ("v2", CWD + "/codex/report-synthetic-loop14.md", 14)
        assert loops._report_meta(catalogue["report"])["goal_path"] == CWD + "/codex/goal-synthetic-loop14.md"


def test_file_backed_parser_accepts_both_shapes_without_a_date_cutover(tmp_path):
    directory = tmp_path / "codex"
    directory.mkdir()
    path = directory / "launch-synthetic-loop14.txt"
    for prefix, mode in (("closeout", "v3"), ("report", "v2")):
        content = f"You are the campaign root. Write codex/{prefix}-synthetic-loop14.md."
        path.write_text(content)
        hit = parse_launch(f"`{path}`", "/tmp/elsewhere", AT, "1970-01-01T00:00:00Z")
        assert hit is not None
        assert hit["mode"] == mode
        assert hit["report"] == str(directory / f"{prefix}-synthetic-loop14.md")
        assert hit["launch_sha256"] == hashlib.sha256(content.encode()).hexdigest()


@pytest.mark.parametrize("agent", ["claude", "codex", "pi"])
def test_v3_recognition_through_real_transcript_parsers(tmp_path, agent):
    from agent_history import load
    from agent_history.memstore import MemStore, run_file
    from agent_history.model import FileContext
    from test_loop_launch_v3_pg import transcript

    path = transcript(tmp_path, agent)
    context = FileContext(
        str(path),
        agent + "-test/" + path.relative_to(tmp_path).as_posix(),
        agent + "-test",
        agent,
        "test",
        None,
        "main",
    )
    store = MemStore()
    parser, _ = load.parser_for(agent)
    run_file(parser, context, store)
    (prompt,) = [row for row in store.rows("message") if row["message_class"] == "human_prompt"]
    hit = loops._launch(prompt["text"], CWD, AT)
    assert hit is not None and (hit["mode"], hit["report"]) == ("v3", CLOSEOUT)
    if agent == "codex":
        assert any(row["path"] == CLOSEOUT and row["op"] == "create" for row in store.rows("file_touch"))


def test_legacy_wave_grammar_is_preserved():
    text = "You are the campaign root. Write codex/report-synthetic-wave3.md."
    legacy = parse_launch(text, CWD, "1960-01-01T00:00:00Z", "1970-01-01T00:00:00Z")
    assert (legacy["mode"], legacy["loop"]) == ("legacy", 3)
    assert parse_launch(text, CWD, AT, "1970-01-01T00:00:00Z") is None

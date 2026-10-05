"""Synthetic protocol shapes, independent of local agent homes."""

from agent_history import load, loops
from agent_history.parse_pi import PiArtifactParser, lineage
from agent_history.model import FileContext, LinePos


def test_new_run_directory_lineage_and_inventory():
    root = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    run = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
    nested = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
    parts = ("sessions", "slug", f"timestamp_{root}", run, "session.jsonl")
    assert load.file_role("pi", parts) == "subagent"
    assert lineage("/".join(parts)) == {
        "root": root,
        "depth": 1,
        "path": f"{root}/{run}",
        "task": run,
    }
    assert lineage(f"sessions/slug/timestamp_{root}/{run}/session/{nested}/run-0/session.jsonl") == {
        "root": root,
        "depth": 2,
        "path": f"{root}/{run}/{nested}/run-0",
        "task": f"{nested}/run-0",
    }
    assert load.file_role("pi", ("sessions", "slug", "root", "junk", "file.jsonl")) is None


def test_path_launch_has_protocol_target_without_local_file_reads():
    hit = loops._launch("/tmp/synthetic/codex/launch-2026-10-04-loop14.txt", None, "2026-10-04T12:00:00Z")
    assert hit["report"] == "/tmp/synthetic/codex/report-2026-10-04-loop14.md"
    assert hit["loop"] == 14


def test_artifact_records_low_and_custom_agent_roles():
    ctx = FileContext(
        "/tmp/artifact.jsonl",
        "pi-test/sessions/slug/subagent-artifacts/artifact.jsonl",
        "pi-test",
        "pi",
        "test",
        None,
        "pi_artifact",
    )
    parser = PiArtifactParser(ctx, {})
    for name in ("lane-worker-low", "ops-probe", "triager", "custom-reviewer"):
        rows = list(
            parser.line(
                {
                    "version": 1,
                    "runId": "synthetic-run",
                    "agent": name,
                    "message": {"responseId": "synthetic-response"},
                },
                LinePos(0, 10, 1),
            )
        )
        assert rows[0].agent_type == name

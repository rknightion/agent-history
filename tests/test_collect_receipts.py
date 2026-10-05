"""The collector's receipt step runs from `main`, including without a database (--dry-run)."""

import json
import subprocess

import pytest

from agent_history import collect_git, collect_receipts, loops


@pytest.mark.parametrize("state_source", ["regular", "file-link", "directory-link"])
def test_main_dry_run_counts_receipts_of_configured_repositories(tmp_path, monkeypatch, capsys, state_source):
    repo = tmp_path / "checkout"
    (repo / "codex").mkdir(parents=True)
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    (repo / "codex/report-synthetic-loop1.md.notified").write_text("request req-1\n")
    (repo / "codex/goal-synthetic-loop1.md.started").write_text("example/repo#loop1#" + "a" * 64 + "\n")
    state = repo / "codex/state-synthetic-loop1.jsonl"
    state.write_text(state_log())
    if state_source == "file-link":
        outside = tmp_path / "outside-state"
        state.rename(outside)
        state.symlink_to(outside)
    elif state_source == "directory-link":
        outside = tmp_path / "outside-directory"
        (repo / "codex").rename(outside)
        (repo / "codex").symlink_to(outside, target_is_directory=True)
    config = tmp_path / "config.toml"
    config.write_text(
        f'[git]\nrepos = ["{repo}"]\n[collector]\nmachine = "synthetic-mac"\nlock_file = "{tmp_path / "lock"}"\n'
    )
    # Git, GitHub and home collection are other steps with their own tests.
    monkeypatch.setattr(collect_git.Collector, "repositories", lambda self: None)
    monkeypatch.setattr(collect_git.Collector, "homes", lambda self, machine, hostname: None)
    assert collect_git.main(["--dry-run", "--config", str(config)]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["machine"] == "synthetic-mac"
    if state_source == "regular":
        assert summary["errors"] == []
    else:
        assert len(summary["errors"]) == 1
        assert summary["errors"][0]["step"] == "loop_state"
    assert summary["tables"]["loop_receipt"] == {"rows": 2}
    assert summary["tables"]["loop_state"] == {"rows": 1 if state_source == "regular" else 0}


def test_mtime_keeps_microsecond_precision():
    from agent_history import collect_receipts

    # 1_800_000_000.123457 s: a float of nanoseconds / 1e9 rounds the last microsecond here.
    assert collect_receipts._utc(1_800_000_000_123_457_999).microsecond == 123457


def test_a_fifo_named_like_a_receipt_or_target_is_skipped_not_waited_on(tmp_path):
    import os

    from agent_history import collect_receipts

    repo = tmp_path / "checkout"
    (repo / "codex").mkdir(parents=True)
    os.mkfifo(repo / "codex/report-synthetic-loop1.md.notified")
    (repo / "codex/report-synthetic-loop2.md.notified").write_text("request req-2\n")
    os.mkfifo(repo / "codex/report-synthetic-loop2.md")  # a target that is not a regular file
    errors = []
    rows = list(collect_receipts.receipts(repo, "synthetic-mac", errors))
    assert [(r["path"].rsplit("/", 1)[1], r["target_exists"], r["target_sha256"]) for r in rows] == [
        ("report-synthetic-loop2.md", True, None)
    ]
    assert [e["error"] for e in errors] == ["OSError"]


def state_log(*events):
    records = [
        {
            "ev": "open",
            "goal_sha256": "a" * 64,
            "tier": "guarded",
            "root": "llm",
            "root_model": "synthetic",
            "envelope": ["TASK-1"],
        },
        *events,
    ]
    return "".join(
        json.dumps({"v": 1, "seq": n, "ts": "2026-10-04T12:00:00Z", "by": "root", **event}) + "\n"
        for n, event in enumerate(records, 1)
    )


def test_state_collection_preserves_complete_bytes_and_only_exact_names(tmp_path, monkeypatch):
    repo = tmp_path / "checkout"
    (repo / "codex").mkdir(parents=True)
    content = state_log({"ev": "judgement", "text": "Keep the recorded judgement verbatim."})
    (repo / "codex/state-synthetic-loop1.jsonl").write_text(content)
    for name in ("state-synthetic-wave1.jsonl", "state-synthetic-loop1.jsonl.tmp", "state-synthetic-loopx.jsonl"):
        (repo / "codex" / name).write_text(content)
    monkeypatch.setattr(collect_receipts, "_origin", lambda _: "example/project")
    rows = list(collect_receipts.states(repo, "synthetic-mac"))
    assert len(rows) == 1
    assert rows[0]["content"] == content
    assert rows[0]["loop"] == "loop1" and rows[0]["repo_origin"] == "example/project"
    assert rows[0]["state_mtime"].tzinfo is not None


def test_state_collection_reports_oversize_fifo_and_concurrent_rewrite(tmp_path, monkeypatch):
    import os

    repo = tmp_path / "checkout"
    (repo / "codex").mkdir(parents=True)
    path = repo / "codex/state-synthetic-loop1.jsonl"
    path.write_text(state_log())
    os.mkfifo(repo / "codex/state-synthetic-loop2.jsonl")
    monkeypatch.setattr(collect_receipts, "MAX_STATE_BYTES", 8)
    errors = []
    assert list(collect_receipts.states(repo, "synthetic-mac", errors)) == []
    assert [e["error"] for e in errors] == ["ValueError", "OSError"]
    monkeypatch.setattr(collect_receipts, "MAX_STATE_BYTES", 4096)
    original = collect_receipts._stat

    def changed(path):
        path.write_text(state_log({"ev": "judgement", "text": "changed"}))
        return original(path)

    monkeypatch.setattr(collect_receipts, "_stat", changed)
    errors = []
    assert list(collect_receipts.states(repo, "synthetic-mac", errors)) == []
    assert [e["error"] for e in errors] == ["Changed", "OSError"]


def test_state_parser_preserves_known_empty_and_skips_torn_non_events():
    parsed = loops.state_progress(state_log() + '{"ev":"accept"\n')
    assert parsed is not None and parsed[0] == "a" * 64 and parsed[2] == []
    assert loops.state_progress("") is None
    assert loops.state_progress(state_log().replace('"v": 1', '"v": true')) is None
    assert loops.state_progress(state_log().replace('"seq": 1', '"seq": 2')) is None
    assert loops.state_progress(state_log().replace('"ts": "2026-10-04T12:00:00Z"', '"ts": "invalid"')) is None
    assert (
        loops.state_progress(
            state_log({"ev": "accept", "task": "TASK-1", "lane": "one", "accepted": "true", "reason": "not a boolean"})
        )
        is None
    )
    assert loops.state_progress(state_log({"ev": "land", "task": "TASK-1"})) is None
    assert loops.state_progress(state_log() + '{"v":1,"v":1}\n') is None

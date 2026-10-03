"""The collector's receipt step runs from `main`, including without a database (--dry-run)."""

import json
import subprocess

from agent_history import collect_git


def test_main_dry_run_counts_receipts_of_configured_repositories(tmp_path, monkeypatch, capsys):
    repo = tmp_path / "checkout"
    (repo / "codex").mkdir(parents=True)
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    (repo / "codex/report-synthetic-loop1.md.notified").write_text("request req-1\n")
    (repo / "codex/goal-synthetic-loop1.md.started").write_text("example/repo#loop1#" + "a" * 64 + "\n")
    config = tmp_path / "config.toml"
    config.write_text(
        f'[git]\nrepos = ["{repo}"]\n[collector]\nmachine = "synthetic-mac"\nlock_file = "{tmp_path / "lock"}"\n'
    )
    # Git, GitHub and home collection are other steps with their own tests.
    monkeypatch.setattr(collect_git.Collector, "repositories", lambda self: None)
    monkeypatch.setattr(collect_git.Collector, "homes", lambda self, machine, hostname: None)
    assert collect_git.main(["--dry-run", "--config", str(config)]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["machine"] == "synthetic-mac" and summary["errors"] == []
    assert summary["tables"]["loop_receipt"] == {"rows": 2}


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

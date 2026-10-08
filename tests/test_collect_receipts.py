"""The collector's receipt step runs from `main`, including without a database (--dry-run)."""

import json
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from agent_history import collect_git, collect_receipts, loops


@pytest.mark.parametrize("state_source", ["regular", "file-link", "directory-link"])
def test_main_dry_run_counts_receipts_of_configured_repositories(tmp_path, monkeypatch, capsys, state_source):
    repo = tmp_path / "checkout"
    (repo / "codex").mkdir(parents=True)
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    (repo / "codex/report-synthetic-loop1.md.notified").write_text("request req-1\n")
    # wave-notify's current completion receipt; the older `.notified` one stays collected.
    (repo / "codex/report-synthetic-loop2.md.posted").write_text("sha256:" + "c" * 64 + ' receiver {"id": "r"}\n')
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
    assert summary["tables"]["loop_receipt"] == {"rows": 3}
    assert summary["tables"]["loop_state"] == {"rows": 1 if state_source == "regular" else 0}


@pytest.mark.parametrize("outcome", ["normal", "locked", "mkdir", "open", "machine", "connect", "close", "interrupt"])
@pytest.mark.parametrize("seconds, interval", [(0.0, 0.0), (120.0, 0.0), (120.0, 60.0)])
def test_main_restores_callers_alarm(tmp_path, monkeypatch, outcome, seconds, interval):
    config = tmp_path / "config.toml"
    config.write_text(f'[git]\nrepos = []\n[collector]\nhomes = {{}}\nlock_file = "{tmp_path / "lock"}"\n')
    monkeypatch.setattr(collect_git.Collector, "repositories", lambda self: None)
    monkeypatch.setattr(collect_git.Collector, "homes", lambda self, machine, hostname: None)

    def fail(*args, **kwargs):
        raise OSError("synthetic edge failure")

    def machine():
        remaining, repeat = signal.getitimer(signal.ITIMER_REAL)
        assert 590 < remaining <= 600 and repeat == 0
        assert signal.getsignal(signal.SIGALRM) is not previous_handler
        return "synthetic-mac", "synthetic-host"

    monkeypatch.setattr(collect_git, "machine_name", machine)
    if outcome == "locked":

        def locked(*args):
            raise BlockingIOError()

        monkeypatch.setattr(collect_git.fcntl, "flock", locked)
    elif outcome == "mkdir":
        monkeypatch.setattr(Path, "mkdir", fail)
    elif outcome == "open":
        monkeypatch.setattr(collect_git, "open", fail, raising=False)
    elif outcome == "machine":
        monkeypatch.setattr(collect_git, "machine_name", fail)
    elif outcome == "connect":
        monkeypatch.setattr(collect_git, "connect", fail)
    elif outcome == "close":

        class Connection:
            close = fail

        monkeypatch.setattr(collect_git, "connect", lambda *args: Connection())
    elif outcome == "interrupt":

        def interrupted():
            raise KeyboardInterrupt()

        monkeypatch.setattr(collect_git, "machine_name", interrupted)

    original_handler = signal.getsignal(signal.SIGALRM)
    original_timer = signal.getitimer(signal.ITIMER_REAL)

    def previous_handler(signum, frame):
        pytest.fail("the caller's long alarm must not expire during this bounded test")

    signal.signal(signal.SIGALRM, previous_handler)
    signal.setitimer(signal.ITIMER_REAL, seconds, interval)
    started = time.monotonic()
    try:
        argv = ["--config", str(config)]
        if outcome not in ("connect", "close"):
            argv.append("--dry-run")
        if outcome in ("mkdir", "open", "machine", "close", "interrupt"):
            with pytest.raises(KeyboardInterrupt if outcome == "interrupt" else OSError):
                collect_git.main(argv)
        else:
            assert collect_git.main(argv) == (1 if outcome == "connect" else 0)
        elapsed = time.monotonic() - started
        assert signal.getsignal(signal.SIGALRM) is previous_handler
        remaining, repeat = signal.getitimer(signal.ITIMER_REAL)
        assert remaining == pytest.approx(max(0, seconds - elapsed), abs=0.1)
        assert repeat == interval
    finally:
        # Only this test's own synthetic timer is cleaned up, after the assertions above.
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, original_handler)
        signal.setitimer(signal.ITIMER_REAL, *original_timer)


@pytest.mark.parametrize("interval", [0.0, 60.0])
def test_collector_alarm_delivers_overdue_callers_timer(interval):
    fired = []
    original_handler = signal.getsignal(signal.SIGALRM)
    original_timer = signal.getitimer(signal.ITIMER_REAL)

    def previous_handler(signum, frame):
        fired.append(signum)

    signal.signal(signal.SIGALRM, previous_handler)
    signal.setitimer(signal.ITIMER_REAL, 0.01, interval)
    try:
        with collect_git._collector_alarm():
            time.sleep(0.03)
            assert fired == []  # the caller's timer is suspended only inside this scope
        # Let the promptly rearmed overdue signal reach Python, without waiting its old duration.
        until = time.monotonic() + 0.5
        while not fired and time.monotonic() < until:
            time.sleep(0.001)
        assert fired == [signal.SIGALRM]
        assert signal.getsignal(signal.SIGALRM) is previous_handler
        remaining, repeat = signal.getitimer(signal.ITIMER_REAL)
        assert repeat == interval
        assert (0 < remaining <= interval) if interval else remaining == 0
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, original_handler)
        signal.setitimer(signal.ITIMER_REAL, *original_timer)


def test_collector_cli_dry_run_and_active_hard_deadline(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text(f'[git]\nrepos = []\n[collector]\nhomes = {{}}\nlock_file = "{tmp_path / "lock"}"\n')
    cli = Path(sys.executable).with_name("agent-history-collect")
    result = subprocess.run(
        [str(cli), "--dry-run", "--config", str(config)], capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["ok"] is True
    # Exercise the same installed console script with only its process edge replaced.
    # Deliver SIGALRM now, rather than wait ten minutes or shorten the production deadline.
    script = """
import json, os, runpy, signal, subprocess, sys
original_run = subprocess.run

def process_edge(argv, *args, **kwargs):
    if argv[0] == "/usr/sbin/scutil":
        remaining, interval = signal.getitimer(signal.ITIMER_REAL)
        print(json.dumps({"active_remaining": remaining, "interval": interval}), flush=True)
        os.kill(os.getpid(), signal.SIGALRM)
        raise AssertionError("hard deadline must exit, not return")
    return original_run(argv, *args, **kwargs)

subprocess.run = process_edge
cli, config = sys.argv[1:]
sys.argv = [cli, "--dry-run", "--config", config]
runpy.run_path(cli, run_name="__main__")
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(cli), str(config)], capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 124, result.stderr
    active, expired = map(json.loads, result.stdout.splitlines())
    assert 590 < active["active_remaining"] <= 600 and active["interval"] == 0
    assert expired == {"ok": False, "error": "runtime_limit", "limit_s": 600}


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

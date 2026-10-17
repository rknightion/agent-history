"""git_commits reads metadata, statistics and Task trailers from one fixed collection snapshot.

These are real-git regressions: the ref moves, or git's own clock crosses the relative cutoff,
between the separate `git log` queries of one pass.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

from agent_history import collect_git as C

SLUG = "github.com/example/widget"
DAY = 86400


def _run(repo: Path, *args: str, committed_at: int | None = None) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test" + "@" + "example.com",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test" + "@" + "example.com",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
    }
    if committed_at is not None:
        env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = f"@{committed_at} +0000"
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, env=env, timeout=30)
    if result.returncode != 0:
        raise RuntimeError(f"git {args} failed: {result.stderr}")
    return result.stdout


def _commit(repo: Path, message: str, committed_at: int | None = None) -> str:
    _run(repo, "-c", "commit.gpgsign=false", "commit", "--allow-empty", "-q", "-m", message, committed_at=committed_at)
    return _run(repo, "rev-parse", "HEAD").strip()


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "widget"
    repo.mkdir()
    _run(repo, "init", "-q", "-b", "main")
    return repo


def _info(repo: Path) -> dict:
    return {"path": repo, "ref": "main", "slug": SLUG, "emails": set()}


def _after_first_log(monkeypatch, action):
    """Run `action` once, straight after the first `git log` of the pass returns."""
    real = C.git
    seen = {"logs": 0}

    def git(repo, *args, **kwargs):
        out = real(repo, *args, **kwargs)
        if args and args[0] == "log":
            seen["logs"] += 1
            if seen["logs"] == 1:
                action()
        return out

    monkeypatch.setattr(C, "git", git)
    return seen


def test_ref_moving_between_queries_keeps_one_snapshot(tmp_path, monkeypatch):
    """A fetch rewinds the branch and lands new work mid-pass; every row still describes the first tip."""
    repo = _repo(tmp_path)
    first = _commit(repo, "first\n\nTask: TASK-1001")
    second = _commit(repo, "second\n\nTask: TASK-1002")

    def move():
        _run(repo, "update-ref", "refs/heads/main", first)
        _commit(repo, "replacement\n\nTask: TASK-1003")

    seen = _after_first_log(monkeypatch, move)
    rows = C.git_commits(_info(repo), 1)

    assert seen["logs"] == 3
    assert {row["sha"]: row["task_ids"] for row in rows} == {first: ["TASK-1001"], second: ["TASK-1002"]}


def test_relative_cutoff_crossing_between_queries_keeps_one_window(tmp_path, monkeypatch):
    """git's clock passes the window edge after the metadata query; the commit stays in every query."""
    repo = _repo(tmp_path)
    now = int(time.time())
    edge = _commit(repo, "edge\n\nTask: TASK-2001", committed_at=now - DAY + 60)
    fresh = _commit(repo, "fresh\n\nTask: TASK-2002", committed_at=now - 10)

    monkeypatch.setattr(C, "GIT_ENV", {**C.GIT_ENV, "GIT_TEST_DATE_NOW": str(now)})

    def cross():
        C.GIT_ENV["GIT_TEST_DATE_NOW"] = str(now + 600)

    _after_first_log(monkeypatch, cross)
    rows = C.git_commits(_info(repo), 1)

    assert {row["sha"]: row["task_ids"] for row in rows} == {edge: ["TASK-2001"], fresh: ["TASK-2002"]}


@pytest.mark.parametrize("dropped", ["trailers", "numstat"])
def test_missing_trailer_or_stat_evidence_fails_closed(tmp_path, monkeypatch, dropped):
    """A query that loses a listed commit is an error, never a known-empty array or zero statistics."""
    repo = _repo(tmp_path)
    _commit(repo, "landed\n\nTask: TASK-3001")
    real = C.git

    def git(repo_path, *args, **kwargs):
        out = real(repo_path, *args, **kwargs)
        is_trailers = any("trailers:key=Task" in a for a in args)
        is_numstat = "--numstat" in args
        if (
            args
            and args[0] == "log"
            and ((dropped == "trailers" and is_trailers) or (dropped == "numstat" and is_numstat))
        ):
            return ""
        return out

    monkeypatch.setattr(C, "git", git)
    with pytest.raises(C.SnapshotMismatch):
        C.git_commits(_info(repo), 1)


def test_failed_snapshot_read_writes_nothing_and_records_the_repo_error(tmp_path, monkeypatch):
    """The repo pass rolls back: no git_commit rows are written and stored history is left alone."""
    repo = _repo(tmp_path)
    _commit(repo, "landed\n\nTask: TASK-4001")
    real = C.git

    def git(repo_path, *args, **kwargs):
        if args and args[0] == "log" and any("trailers:key=Task" in a for a in args):
            return ""
        return real(repo_path, *args, **kwargs)

    monkeypatch.setattr(C, "git", git)
    collector = C.Collector(None, True, time.monotonic() + 30)
    writes = []
    monkeypatch.setattr(collector, "write", lambda table, cols, key, rows, *a: writes.append(table))
    monkeypatch.setattr(collector, "_reconcile_off_branch", lambda *a: writes.append("reconcile"))
    monkeypatch.setattr(collector, "_git_commit_files", lambda *a: None)
    monkeypatch.setattr(collector, "_backlog", lambda *a: None)
    monkeypatch.setattr(C, "ci_runs", lambda slug: [])
    collector.rescan_days = 1

    collector.one_repo({**_info(repo), "fetched": True}, {})

    assert "git_commit" not in writes and "reconcile" not in writes
    assert collector.errors == [{"repo": SLUG, "step": "git_commit", "error": "SnapshotMismatch"}]

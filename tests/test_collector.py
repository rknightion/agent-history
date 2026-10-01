"""Synthetic collector parser, git and database contracts, ported from the standalone collector."""

from __future__ import annotations
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
import pytest
from agent_history import collect_git as C


def test_revert_detection():
    body = 'Revert "x"\n\nThis reverts commit 0123456789abcdef0123456789abcdef01234567.\n'
    assert C.reverts_sha(body) == "0123456789abcdef0123456789abcdef01234567"
    assert C.reverts_sha("This reverts commit abc1234") == "abc1234"
    assert C.reverts_sha("reverts nothing in particular") is None


def test_numstat_keeps_sha_markers_and_counts_binary_files():
    text = (
        "\x1eaaa\n\n3\t1\tsrc/a.py\n-\t-\timg.png\n10\t0\told => new\n"
        "\x1ebbb\n"  # merge / empty commit: no numstat
        "\x1eccc\n\n0\t7\tgone.txt\n"
    )
    assert C.parse_numstat(text) == {"aaa": [3, 13, 1], "bbb": [0, 0, 0], "ccc": [1, 0, 7]}


# --------------------------------------------------------------------------------------------
# git_commit_file
# --------------------------------------------------------------------------------------------


def test_parse_commit_files_pins_the_raw_plus_numstat_z_grammar():
    """`-z` terminates each commit's %H with NUL, not the usual blank line: a diff-less commit
    (here 'aaa', an empty/merge-with-no-first-parent-diff commit) is just `<sha>\\0` with the next
    record's \\x1e following immediately; a commit with entries is `<sha>\\0\\n<raw><numstat>`.
    """
    text = (
        "\x1eaaa\0"
        "\x1ebbb\0\n"
        ":100644 100644 111 222 M\0keep.txt\0"
        ":000000 100644 000 333 A\0img.bin\0"
        ":100644 100644 444 555 R100\0old.txt\0new.txt\0"
        ":100644 000000 666 000 D\0gone.txt\0"
        "1\t0\tkeep.txt\0"
        "-\t-\timg.bin\0"
        "0\t0\t\0old.txt\0new.txt\0"
        "0\t3\tgone.txt\0"
    )
    assert C.parse_commit_files("repo", text) == [
        {
            "repo_slug": "repo",
            "sha": "bbb",
            "path": "keep.txt",
            "change": "M",
            "old_path": None,
            "insertions": 1,
            "deletions": 0,
        },
        {
            "repo_slug": "repo",
            "sha": "bbb",
            "path": "img.bin",
            "change": "A",
            "old_path": None,
            "insertions": None,
            "deletions": None,
        },
        {
            "repo_slug": "repo",
            "sha": "bbb",
            "path": "new.txt",
            "change": "R",
            "old_path": "old.txt",
            "insertions": 0,
            "deletions": 0,
        },
        {
            "repo_slug": "repo",
            "sha": "bbb",
            "path": "gone.txt",
            "change": "D",
            "old_path": None,
            "insertions": 0,
            "deletions": 3,
        },
    ]


def _git(repo: Path, *args: str) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "" + "test" + "@" + "example.com" + "",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "" + "test" + "@" + "example.com" + "",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
    }
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, env=env, timeout=30)
    if result.returncode != 0:
        raise RuntimeError(f"git {args} failed: {result.stderr}")
    return result.stdout


def _init_repo(path: Path) -> None:
    path.mkdir(exist_ok=True)
    _git(path, "init", "-q", "-b", "main")


def _commit(path: Path, message: str, allow_empty: bool = False) -> str:
    args = ["-c", "commit.gpgsign=false", "commit", "-q", "-m", message]
    if allow_empty:
        args.append("--allow-empty")
    _git(path, *args)
    return _git(path, "rev-parse", "HEAD").strip()


def _name_status(repo: Path) -> dict[str, list[tuple[str, str | None, str]]]:
    """`git log -M --name-status -z` parsed to the same (change, old_path, path) shape, for comparison."""
    out = _git(repo, "log", "main", "-M", "--name-status", "-z", "--format=%x1e%H")
    result: dict[str, list[tuple[str, str | None, str]]] = {}
    for record in out.split("\x1e")[1:]:
        nul = record.find("\0")
        sha, rest = record[:nul], record[nul + 1 :]
        if rest.startswith("\n"):
            rest = rest[1:]
        tokens = [t for t in rest.split("\0") if t]
        entries: list[tuple[str, str | None, str]] = []
        i = 0
        while i < len(tokens):
            change = tokens[i][:1]
            i += 1
            if change in ("R", "C"):
                entries.append((change, tokens[i], tokens[i + 1]))
                i += 2
            else:
                entries.append((change, None, tokens[i]))
                i += 1
        result[sha] = entries
    return result


def test_git_commit_files_matches_git_log_name_status(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "keep.txt").write_text("a\nb\nc\n")
    (repo / "torename.txt").write_text("x\ny\n")
    (repo / "todelete.txt").write_text("z\n")
    _git(repo, "add", "keep.txt", "torename.txt", "todelete.txt")
    _commit(repo, "init")

    (repo / "keep.txt").write_text("a\nb\nc\nd\n")
    _git(repo, "mv", "torename.txt", "renamed.txt")
    _git(repo, "rm", "-q", "todelete.txt")
    (repo / "img.bin").write_bytes(b"\x00\x01\x02binary")
    (repo / "newfile.txt").write_text("new content\n")
    _git(repo, "add", "keep.txt", "renamed.txt", "img.bin", "newfile.txt")
    sha2 = _commit(repo, "multi change")

    info = {"path": repo, "ref": "main", "slug": "test/repo"}
    rows = C.git_commit_files(info, 30)

    expected = _name_status(repo)
    got = {(r["sha"], r["change"], r["old_path"], r["path"]) for r in rows}
    want = {(sha, change, old, path) for sha, entries in expected.items() for change, old, path in entries}
    assert got == want

    by_path = {r["path"]: r for r in rows if r["sha"] == sha2}
    assert by_path["img.bin"]["insertions"] is None and by_path["img.bin"]["deletions"] is None
    assert by_path["keep.txt"] == {
        "repo_slug": "test/repo",
        "sha": sha2,
        "path": "keep.txt",
        "change": "M",
        "old_path": None,
        "insertions": 1,
        "deletions": 0,
    }
    assert by_path["renamed.txt"]["old_path"] == "torename.txt" and by_path["renamed.txt"]["change"] == "R"
    assert by_path["todelete.txt"] == {
        "repo_slug": "test/repo",
        "sha": sha2,
        "path": "todelete.txt",
        "change": "D",
        "old_path": None,
        "insertions": 0,
        "deletions": 1,
    }


def test_git_commit_files_merge_uses_first_parent_only_and_never_duplicates(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "f.txt").write_text("base\n")
    _git(repo, "add", "f.txt")
    _commit(repo, "base")
    _commit(repo, "empty", allow_empty=True)  # a diff-less commit must not crash the parser
    _git(repo, "checkout", "-qb", "feature")
    (repo / "f.txt").write_text("base\nfeature-change\n")
    _git(repo, "add", "f.txt")
    _commit(repo, "feature")
    _git(repo, "checkout", "-q", "main")
    (repo / "other.txt").write_text("base\nmain-change\n")
    _git(repo, "add", "other.txt")
    _commit(repo, "main-change")
    _git(repo, "-c", "commit.gpgsign=false", "merge", "-q", "--no-ff", "feature", "-m", "merge feature")
    merge_sha = _git(repo, "rev-parse", "HEAD").strip()

    info = {"path": repo, "ref": "main", "slug": "test/merge-repo"}
    rows = C.git_commit_files(info, 30)

    merge_rows = [r for r in rows if r["sha"] == merge_sha]
    assert merge_rows == [
        {
            "repo_slug": "test/merge-repo",
            "sha": merge_sha,
            "path": "f.txt",
            "change": "M",
            "old_path": None,
            "insertions": 1,
            "deletions": 0,
        }
    ]
    keys = [(r["sha"], r["path"]) for r in rows]
    assert len(keys) == len(set(keys))  # no file duplicated across the merge's two parents


def test_git_commit_files_keeps_repos_isolated(tmp_path):
    repo_a, repo_b = tmp_path / "a", tmp_path / "b"
    _init_repo(repo_a)
    (repo_a / "a.txt").write_text("hello\n")
    _git(repo_a, "add", "a.txt")
    sha_a = _commit(repo_a, "commit a")
    _init_repo(repo_b)
    (repo_b / "b.txt").write_text("world\n")
    _git(repo_b, "add", "b.txt")
    sha_b = _commit(repo_b, "commit b")

    rows_a = C.git_commit_files({"path": repo_a, "ref": "main", "slug": "github.com/example/repo-a"}, 30)
    rows_b = C.git_commit_files({"path": repo_b, "ref": "main", "slug": "github.com/example/repo-b"}, 30)

    assert {(r["repo_slug"], r["sha"], r["path"]) for r in rows_a} == {("github.com/example/repo-a", sha_a, "a.txt")}
    assert {(r["repo_slug"], r["sha"], r["path"]) for r in rows_b} == {("github.com/example/repo-b", sha_b, "b.txt")}


DSN = os.environ.get("AGENT_HISTORY_TEST_DSN", "")


@pytest.mark.skipif(
    not DSN or "agent_history_test" not in DSN, reason="AGENT_HISTORY_TEST_DSN (a *_test database) not set"
)
def test_git_commit_file_upsert_is_idempotent():
    """The exact upsert the collector runs: an unchanged second run must write 0 rows (migration 003)."""
    pytest.importorskip("psycopg")
    from agent_history import load  # noqa: E402  (deferred: only this test needs a live database)

    conn = load.connect(DSN)
    load.apply_schema(conn, force=True)
    conn.commit()
    slug = "test/idempotent-repo"
    rows = [
        {
            "repo_slug": slug,
            "sha": "a" * 40,
            "path": "keep.txt",
            "change": "M",
            "old_path": None,
            "insertions": 1,
            "deletions": 0,
        },
        {
            "repo_slug": slug,
            "sha": "a" * 40,
            "path": "renamed.txt",
            "change": "R",
            "old_path": "old.txt",
            "insertions": 0,
            "deletions": 0,
        },
        {
            "repo_slug": slug,
            "sha": "a" * 40,
            "path": "img.bin",
            "change": "A",
            "old_path": None,
            "insertions": None,
            "deletions": None,
        },
    ]
    try:
        conn.execute("DELETE FROM ah.git_commit_file WHERE repo_slug = %s", (slug,))
        conn.commit()
        with conn.transaction(), conn.cursor() as cur:
            first = C.upsert(cur, "git_commit_file", C.GIT_FILE_COLS, ["repo_slug", "sha", "path"], rows, touch=False)
        assert first == (3, 0)
        with conn.transaction(), conn.cursor() as cur:
            second = C.upsert(cur, "git_commit_file", C.GIT_FILE_COLS, ["repo_slug", "sha", "path"], rows, touch=False)
        assert second == (0, 0)  # unchanged rows: the second run writes nothing
    finally:
        conn.execute("DELETE FROM ah.git_commit_file WHERE repo_slug = %s", (slug,))
        conn.commit()
        conn.close()


TASK = """---
id: SYN-91
title: 'Close the ''gap'' in the parser'
status: In Progress
assignee: []
created_date: '2026-09-25 15:02'
updated_date: '2026-07-01'
labels:
  - agent-history
  - "parser"
priority: high
project: camden
references:
  - >-
    /opt/example/long/path.md
dependencies: [SYN-0090, 'SYN-0089']
description: >-
  folded text
  continues here
---

## Description
id: not-frontmatter
"""


def test_frontmatter_parsing():
    fm = C.parse_frontmatter(TASK)
    assert fm["id"] == "SYN-91"
    assert fm["title"] == "Close the 'gap' in the parser"
    assert fm["labels"] == ["agent-history", "parser"]
    assert fm["assignee"] == []
    assert fm["dependencies"] == ["SYN-0090", "SYN-0089"]
    assert fm["description"] == "folded text continues here"
    assert "## Description" not in json.dumps(fm)
    assert C.parse_frontmatter("no fence here") is None
    assert C.parse_frontmatter("---\nid: X-1\n") is None  # unterminated


def test_task_row_normalises_key_and_london_dates():
    row = C.task_row(C.parse_frontmatter(TASK), pad=4, archived=False)
    assert row["task_key"] == "SYN-0091"
    assert row["status"] == "In Progress" and row["priority"] == "high" and row["project"] == "camden"
    # Naive dates are Europe/London: 15:02 BST is 14:02 UTC; a bare date is local midnight.
    assert row["created_at"].astimezone(timezone.utc) == datetime(2026, 9, 25, 14, 2, tzinfo=timezone.utc)
    assert row["updated_at"].astimezone(timezone.utc) == datetime(2026, 6, 30, 23, 0, tzinfo=timezone.utc)
    winter = C.london_ts("2026-01-10 09:30")
    assert winter.astimezone(timezone.utc) == datetime(2026, 1, 10, 9, 30, tzinfo=timezone.utc)
    assert C.london_ts("2026-09-25T10:00:00Z") == datetime(2026, 9, 25, 10, 0, tzinfo=timezone.utc)
    assert C.task_row(C.parse_frontmatter(TASK), pad=4, archived=True)["status"] == "Archived"


def test_updated_falls_back_to_created():
    fm = C.parse_frontmatter("---\nid: lab-7\ntitle: t\nstatus: To Do\ncreated_date: '2026-09-01 10:00'\n---\n")
    row = C.task_row(fm, pad=None, archived=False)
    assert row["task_key"] == "LAB-7" and row["updated_at"] == row["created_at"] and row["labels"] == []


@pytest.mark.parametrize(
    "config,pad",
    [
        ('project_name: "x"\nzero_padded_ids: 4\ntask_prefix: "lab"\n', 4),
        ('project_name: "Backlog.md"\ntask_prefix: "back"\n', None),  # no padding configured
        ("zero_padded_ids: 0\n", None),
        ("zero_padded_ids: nonsense\n", None),
    ],
)
def test_zero_pad_detection(config, pad):
    assert C.zero_pad(C.parse_config(config)) == pad


@pytest.mark.parametrize(
    "raw,pad,key",
    [
        ("syn-0091", 4, "SYN-0091"),
        ("SYN-91", 4, "SYN-0091"),
        ("BACK-42", None, "BACK-42"),
        ("SYN-12.01", 4, "SYN-0012.01"),
        ("SYN-12345", 4, "SYN-12345"),
        ("not a task id", 4, None),
        (None, 4, None),
    ],
)
def test_task_key(raw, pad, key):
    assert C.task_key(raw, pad) == key


def test_skill_and_namespace_names(monkeypatch):
    assert C.plugin_skill_name("plugin@market", "read") == "plugin:read"
    assert C.normalise_skill(" example/ ") == "example"
    monkeypatch.setattr(C, "HOME_NAMESPACES", {"~/.claude": "claude-lab"})
    assert C.home_namespace("~/.claude", "camden") == "claude-lab"
    assert C.home_namespace("~/.codex-lab", "camden") == "codex-lab"


def test_permission_line_keeps_structure_never_text():
    secret = "curl -H 'Authorization: Bearer synthetic-value' https://example.test"
    line = json.dumps(
        {
            "ts": "2026-09-19T22:02:44+00:00",
            "tool": "Bash",
            "cwd": "/opt/example",
            "session": "s",
            "mode": "auto",
            "subagent": True,
            "agent_type": "general-purpose",
            "description": "fetch the thing",
            "target": f"cd /tmp && FOO=1 timeout 30 {secret}",
        }
    )
    row = C.permission_row(line + "\n")
    assert row["ts"] == datetime(2026, 9, 19, 22, 2, 44, tzinfo=timezone.utc)
    assert row["tool_name"] == "Bash" and row["cmd_verb"] == "curl"
    assert row["reason"] == "classifier:auto" and row["is_subagent"] is True
    assert len(row["line_hash"]) == 64 and row["line_hash"] == C.permission_row(line)["line_hash"]
    assert "Bearer" not in json.dumps(row, default=str) and "example.test" not in json.dumps(row, default=str)
    web = C.permission_row(
        json.dumps(
            {
                "ts": "2026-09-20T10:00:00Z",
                "tool": "WebFetch",
                "mode": "auto",
                "subagent": False,
                "target": "https://example.test",
            }
        )
    )
    assert web["cmd_verb"] is None and web["is_subagent"] is False
    assert C.permission_row("not json") is None and C.permission_row("\n") is None

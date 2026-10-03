"""collect_git: commits of a configured checkout into ah.git_commit, owner matched, no emails stored.

Skipped unless AGENT_HISTORY_TEST_DSN names a *_test database.
"""

from __future__ import annotations

import os
import subprocess

import pytest
from test_loader_pg import DSN, clean, conn  # noqa: F401  (fixtures)

pytestmark = pytest.mark.skipif(
    not DSN or "agent_history_test" not in DSN, reason="AGENT_HISTORY_TEST_DSN (a *_test database) not set"
)

from agent_history.collect_git import collect, repo_slug  # noqa: E402
from agent_history.config import parse_config  # noqa: E402

OWNER = "owner@example.com"


def git(repo, *args, email=OWNER):
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "A",
        "GIT_AUTHOR_EMAIL": email,
        "GIT_COMMITTER_NAME": "A",
        "GIT_COMMITTER_EMAIL": email,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, env=env)


@pytest.fixture
def repo(tmp_path, clean):  # noqa: F811
    clean.execute("TRUNCATE ah.git_commit, ah.git_commit_file")  # collector tables survive `clean`
    clean.commit()
    path = tmp_path / "widget"
    path.mkdir()
    git(path, "init", "-q", "-b", "main")
    git(path, "remote", "add", "origin", "git@github.com:example-org/widget.git")
    (path / "a.txt").write_text("one\n")
    git(path, "add", "a.txt")
    git(path, "commit", "-q", "-m", "add a")
    (path / "a.txt").write_text("one\ntwo\n")
    git(path, "commit", "-q", "-am", "extend a", email="someone@example.net")
    return path


@pytest.mark.parametrize(
    "remote, slug",
    [
        ("git@github.com:example-org/widget.git", "github.com/example-org/widget"),
        ("https://github.com/Example-Org/Widget", "github.com/example-org/widget"),
        ("ssh://git@git.example.net:2222/team/tools.git", "git.example.net/team/tools"),
        ("nonsense", None),
    ],
)
def test_repo_slug(remote, slug):
    assert repo_slug(remote) == slug


def test_commits_are_ingested_with_owner_flag_and_no_email(clean, repo):  # noqa: F811
    config = parse_config({"identities": {"owner_emails": [OWNER]}, "git": {"repos": [str(repo)]}})
    result = collect(clean, config)
    assert result["repos"] == 1 and result["commits"] == 2
    rows = clean.execute(
        "SELECT subject, author_is_owner, files_changed, insertions FROM ah.git_commit "
        "WHERE repo_slug = 'github.com/example-org/widget' ORDER BY committed_at, subject"
    ).fetchall()
    assert sorted(rows) == [("add a", True, 1, 1), ("extend a", False, 1, 1)]
    dump = " ".join(str(v) for row in clean.execute("SELECT * FROM ah.git_commit").fetchall() for v in row)
    assert "@" not in dump
    assert clean.execute("SELECT count(*) FROM ah.git_commit_file").fetchone()[0] == 2
    assert collect(clean, config)["commits"] == 2  # idempotent upsert
    assert clean.execute("SELECT count(*) FROM ah.git_commit").fetchone()[0] == 2
    clean.rollback()


def test_owner_allowlist_skips_other_owners(clean, repo):  # noqa: F811
    config = parse_config({"identities": {"git_owners": ["github.com/someone-else"]}, "git": {"repos": [str(repo)]}})
    result = collect(clean, config)
    assert result["repos"] == 0 and result["skipped"] == ["widget: owner not in [identities] git_owners"]
    clean.rollback()


def test_done_events_are_collected_idempotently_and_survive_rescans(clean, repo):  # noqa: F811
    import time

    from agent_history.collect_git import Collector

    clean.execute("DELETE FROM ah.backlog_done_event WHERE repo_slug = 'github.com/example-org/widget'")
    clean.execute("DELETE FROM ah.backlog_done_scan WHERE repo_slug = 'github.com/example-org/widget'")
    clean.commit()
    tasks = repo / "backlog" / "tasks"
    tasks.mkdir(parents=True)
    task = tasks / "ex-0001 - First.md"
    task.write_text("---\nid: EX-0001\ntitle: t\nstatus: To Do\n---\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "add task")
    task.write_text("---\nid: EX-0001\ntitle: t\nstatus: Done\n---\n")
    git(repo, "commit", "-q", "-am", "close task")
    info = {"path": repo, "ref": "main", "slug": "github.com/example-org/widget", "fetched": False}
    try:
        for _ in range(2):
            Collector(clean, False, time.monotonic() + 60)._backlog_done(info, 4)
        rows = clean.execute(
            "SELECT task_key, from_status FROM ah.backlog_done_event WHERE repo_slug = 'github.com/example-org/widget'"
        ).fetchall()
        assert rows == [("EX-0001", "To Do")]
    finally:
        clean.rollback()
        clean.execute("DELETE FROM ah.backlog_done_event WHERE repo_slug = 'github.com/example-org/widget'")
        clean.execute("DELETE FROM ah.backlog_done_scan WHERE repo_slug = 'github.com/example-org/widget'")
        clean.commit()


def test_repo_with_no_done_events_is_widened_once(clean, repo, monkeypatch):  # noqa: F811
    import time

    from agent_history import collect_git
    from agent_history.collect_git import RESCAN_DAYS, Collector

    slug = "github.com/example-org/widget"
    clean.execute("DELETE FROM ah.backlog_done_scan WHERE repo_slug = %s", (slug,))
    clean.execute("DELETE FROM ah.backlog_done_event WHERE repo_slug = %s", (slug,))
    clean.execute(
        "INSERT INTO ah.git_commit (repo_slug, sha, context, committed_at, author_is_owner, subject, parent_count, "
        "files_changed, insertions, deletions, on_default) "
        "VALUES (%s, %s, 'default', now() - interval '100 days', true, 's', 1, 0, 0, 0, true)",
        (slug, "f" * 40),
    )
    clean.commit()
    windows = []
    monkeypatch.setattr(collect_git, "backlog_done_events", lambda info, days, pad: windows.append(days) or [])
    info = {"path": repo, "ref": "main", "slug": slug, "fetched": False}
    try:
        for _ in range(2):
            Collector(clean, False, time.monotonic() + 60)._backlog_done(info, 4)
        assert windows[0] > RESCAN_DAYS and windows[1] == RESCAN_DAYS  # no flips, yet only the first run widens
    finally:
        clean.rollback()
        clean.execute("DELETE FROM ah.backlog_done_scan WHERE repo_slug = %s", (slug,))
        clean.execute("DELETE FROM ah.git_commit WHERE repo_slug = %s", (slug,))
        clean.commit()


def test_done_events_for_commits_left_off_the_default_branch_are_removed(clean, repo):  # noqa: F811
    import time

    from agent_history.collect_git import Collector

    slug = "github.com/example-org/widget"
    git(repo, "checkout", "-q", "-b", "side")
    (repo / "side.txt").write_text("x\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "side commit")
    side = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    git(repo, "checkout", "-q", "main")
    rows = [("EX-0001", side), ("EX-0002", "9" * 40)]  # a commit this clone has but main lacks; an unknown sha
    clean.execute("DELETE FROM ah.backlog_done_scan WHERE repo_slug = %s", (slug,))
    for task, sha in rows:
        clean.execute(
            "INSERT INTO ah.backlog_done_event (repo_slug, task_key, sha, done_at) VALUES (%s, %s, %s, now())",
            (slug, task, sha),
        )
    clean.commit()
    info = {"path": repo, "ref": "main", "slug": slug, "fetched": True}
    try:
        Collector(clean, False, time.monotonic() + 60)._backlog_done(info, 4)
        kept = clean.execute(
            "SELECT task_key FROM ah.backlog_done_event WHERE repo_slug = %s ORDER BY task_key", (slug,)
        ).fetchall()
        assert kept == [("EX-0002",)]  # unknown shas are never removed on a guess
    finally:
        clean.rollback()
        for task, sha in rows:
            clean.execute(
                "DELETE FROM ah.backlog_done_event WHERE repo_slug = %s AND task_key = %s AND sha = %s",
                (slug, task, sha),
            )
        clean.execute("DELETE FROM ah.backlog_done_scan WHERE repo_slug = %s", (slug,))
        clean.commit()

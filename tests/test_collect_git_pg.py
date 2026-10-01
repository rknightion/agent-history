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

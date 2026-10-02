"""A failed Actions query is a collection failure, never a CI observation."""

import os
import subprocess
import time

import pytest

from agent_history import collect_git as C


@pytest.mark.parametrize("slug", [None, "", "local/repo", "example.com/operator", "example.com/operator/repo/extra"])
def test_malformed_slugs_are_not_allowlisted(slug, monkeypatch):
    monkeypatch.setattr(C, "ALLOWED_OWNERS", {("example.com", "operator")})
    assert C.allowlisted(slug) is False


OWNERS = {("github.com", "example-org"), ("git.example.net", "team")}
# Userinfo with a password is assembled at run time so no scanner sees a credential-shaped literal.
WITH_PASSWORD = "https://" + "user" + ":" + "synthetic" + "@github.com/example-org/widget.git"


@pytest.mark.parametrize(
    "remote, slug",
    [
        ("https://github.com/example-org/widget", "github.com/example-org/widget"),
        ("https://GitHub.com/Example-Org/Widget.git", "github.com/example-org/widget"),
        ("https://github.com/example-org/widget.git/", "github.com/example-org/widget"),
        ("https://github.com/example-org/.github", "github.com/example-org/.github"),
        (WITH_PASSWORD, "github.com/example-org/widget"),
        ("http://git.example.net:3000/team/tools", "git.example.net/team/tools"),
        ("ssh://git@github.com/example-org/widget.git", "github.com/example-org/widget"),
        ("ssh://git@git.example.net:2222/team/tools.git", "git.example.net/team/tools"),
        ("git@github.com:example-org/widget.git", "github.com/example-org/widget"),
        ("github.com:example-org/widget", "github.com/example-org/widget"),
        ("git@github.com:example-org/widget.git\n", "github.com/example-org/widget"),
    ],
)
def test_owner_remote_of_each_url_form_is_accepted(remote, slug, monkeypatch):
    monkeypatch.setattr(C, "ALLOWED_OWNERS", OWNERS)
    assert C.repo_slug(remote) == slug
    assert C.allowlisted(C.repo_slug(remote)) is True


@pytest.mark.parametrize(
    "remote",
    [
        # look-alike host
        "https://github.com.example.net/example-org/widget",
        "https://notgithub.com/example-org/widget",
        "ssh://git@github.com.example.net/example-org/widget",
        "git@github.com.example.net:example-org/widget",
        "git@" + "notgithub.com:example-org/widget",  # assembled: the literal has the shape of an email address
        "https://github.com./example-org/widget",
        # look-alike path
        "https://example.net/github.com/example-org/widget",
        "ssh://git@example.net/github.com/example-org/widget",
        "example.net:github.com/example-org/widget",
        # userinfo trick: the host is what follows the userinfo
        "https://github.com@example.net/example-org/widget",
        "https://github.com:443@example.net/example-org/widget",
        "ssh://github.com@example.net/example-org/widget",
        "github.com@example.net:example-org/widget",
        # authority confusion: a client ends the authority at '#', '?' or a backslash
        "https://example.net#@github.com/example-org/widget",
        "https://example.net?@github.com/example-org/widget",
        "https://example.net\\@github.com/example-org/widget",
        "https://example.net/@github.com/example-org/widget",
        "https://a@example.net@github.com/example-org/widget",
        "a@example.net@github.com:example-org/widget",
        # not a network remote, or not exactly host/owner/name
        "file://github.com/example-org/widget",
        "ftp://github.com/example-org/widget",
        "https://github.com/example-org/widget?ref=x",
        "https://github.com/example-org/widget#x",
        "https://github.com/example-org/..",
        "https://github.com/example-org/widget/extra",
        "https://github.com//example-org/widget",
        "https://github.com/example-org%2Fwidget/x",
        "https://github.com/example-org/wid get",
        "https://github.com:port/example-org/widget",
        "git@github.com:/example-org/widget",
        # a different owner on the right host
        "https://github.com/other-org/widget",
        "git@github.com:example-org-evil/widget",
    ],
)
def test_lookalike_remote_is_rejected(remote, monkeypatch):
    monkeypatch.setattr(C, "ALLOWED_OWNERS", OWNERS)
    assert C.allowlisted(C.repo_slug(remote)) is False


def test_non_ascii_owner_is_not_folded_onto_an_allowlisted_one(monkeypatch):
    monkeypatch.setattr(C, "ALLOWED_OWNERS", {("github.com", "kit")})
    assert C.allowlisted(C.repo_slug("https://github.com/kit/widget")) is True
    # U+212A KELVIN SIGN lower-cases to an ASCII k; the forge would see a different owner.
    assert C.repo_slug("https://github.com/\u212ait/widget") is None
    assert C.repo_slug("git@github.com:\u212ait/widget") is None


def checkout(path, remote):
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
    identity = ["-c", "user.name=A", "-c", "user.email=owner@example.com", "-c", "commit.gpgsign=false"]
    path.mkdir()
    for args in (
        ["init", "-q", "-b", "main"],
        ["remote", "add", "origin", remote],
        [*identity, "commit", "-q", "--allow-empty", "-m", "first"],
    ):
        subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True, env=env)
    return path


@pytest.fixture
def offline(monkeypatch):
    """Owner allowlist in place, and no network: `git fetch` fails at once, as it does offline."""
    monkeypatch.setattr(C, "ALLOWED_OWNERS", OWNERS)
    real = subprocess.run

    def run(argv, **kwargs):
        if "fetch" in argv:
            return subprocess.CompletedProcess(argv, 128, b"", b"")
        return real(argv, **kwargs)

    monkeypatch.setattr(C.subprocess, "run", run)


@pytest.mark.parametrize(
    "remote",
    [
        "https://github.com/example-org/widget.git",
        "ssh://git@github.com/example-org/widget.git",
        "git@github.com:example-org/widget.git",
    ],
)
def test_repository_with_owner_remote_is_collected(tmp_path, offline, remote):
    info, reason = C.classify_repo(checkout(tmp_path / "widget", remote), set())
    assert reason is None
    assert (info["slug"], info["branch"]) == ("github.com/example-org/widget", "main")


@pytest.mark.parametrize(
    "remote, reason",
    [
        ("https://github.com.example.net/example-org/widget", "not_allowlisted"),
        ("https://notgithub.com/example-org/widget", "not_allowlisted"),
        ("https://github.com@example.net/example-org/widget", "not_allowlisted"),
        ("https://example.net/github.com/example-org/widget", "no_origin"),
        ("https://example.net#@github.com/example-org/widget", "no_origin"),
        ("file://github.com/example-org/widget", "no_origin"),
    ],
)
def test_repository_with_lookalike_remote_is_skipped(tmp_path, offline, remote, reason):
    assert C.classify_repo(checkout(tmp_path / "widget", remote), set()) == (None, reason)


def test_fork_and_worktree_rules_are_unchanged(tmp_path, offline):
    github = checkout(tmp_path / "widget", "git@github.com:example-org/widget.git")
    assert C.classify_repo(github, {"github.com/example-org/widget"}) == (None, "fork")
    assert C.classify_repo(github, None) == (None, "fork_status_unknown")
    # Fork status is a GitHub question: another allowlisted host is collected without it.
    other = checkout(tmp_path / "tools", "ssh://git@git.example.net:2222/team/tools.git")
    info, reason = C.classify_repo(other, None)
    assert reason is None and info["slug"] == "git.example.net/team/tools"
    subprocess.run(
        ["git", "-C", str(github), "worktree", "add", "-q", str(tmp_path / "linked"), "-b", "side"],
        check=True,
        capture_output=True,
    )
    assert C.classify_repo(tmp_path / "linked", set()) == (None, "worktree")
    assert C.classify_repo(tmp_path, set()) == (None, "not_git")


class Connection:
    def rollback(self):
        pass


@pytest.mark.parametrize("connection", [Connection(), None])
def test_actions_failure_is_attributable_and_does_not_stop_other_repositories(monkeypatch, connection):
    bad = "github.com/example/missing"
    good = "github.com/example/available"
    writes = []
    collector = C.Collector(connection, True, time.monotonic() + 30)
    monkeypatch.setattr(C, "git_commits", lambda *a: [])
    monkeypatch.setattr(collector, "_git_commit_files", lambda *a: None)
    monkeypatch.setattr(collector, "_backlog", lambda *a: None)
    monkeypatch.setattr(collector, "write", lambda table, cols, keys, rows, *a: writes.append((table, rows)))

    def run(argv, **kwargs):
        if "example/missing" in argv:
            return subprocess.CompletedProcess(argv, 1, "", "HTTP 404: Not Found (private request details)")
        return subprocess.CompletedProcess(
            argv,
            0,
            '[{"databaseId":7,"headSha":"abc","workflowName":"test","status":"completed",'
            '"conclusion":"success","createdAt":"2026-01-01T00:00:00Z"}]',
            "",
        )

    monkeypatch.setattr(C.subprocess, "run", run)
    for slug in (bad, good):
        collector.one_repo({"slug": slug}, {})
    assert collector.errors == [
        {"repo": bad, "step": "ci_run", "error": "CICollectionError", "reason": "http_404", "exit": 1}
    ]
    ci = [rows for table, rows in writes if table == "ci_run"]
    assert len(ci) == 1
    assert ci[0][0]["repo_slug"] == good
    assert ci[0][0]["conclusion"] == "success"

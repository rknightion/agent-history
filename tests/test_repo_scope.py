"""Synthetic exact-repository admission and explicit read-branch contracts."""

import subprocess
import time

import pytest

from agent_history import collect_git as C
from agent_history.config import ConfigError, parse_config
from test_collector import _commit, _git, _init_repo


@pytest.fixture(autouse=True)
def collector_globals(monkeypatch):
    for key in (
        "REPOSITORIES",
        "ALLOWED_OWNERS",
        "ALLOWED_REPOS",
        "EXTRA_BRANCHES",
        "GITHUB_OWNERS",
        "OWNER_IDENTITIES",
        "CONTEXT",
        "REPO_CONTEXTS",
        "HOMES",
        "HOME_NAMESPACES",
        "MACHINE",
        "LOCK_FILE",
    ):
        monkeypatch.setattr(C, key, getattr(C, key, None), raising=False)


def test_exact_repo_allowlist_does_not_admit_sibling(tmp_path, monkeypatch):
    config = parse_config({"identities": {"git_repos": ["GitHub.com/Example-Team/Widget/"]}})
    monkeypatch.setattr(C, "ALLOWED_OWNERS", set())
    monkeypatch.setattr(C, "ALLOWED_REPOS", set(), raising=False)
    C.configure(config)
    for name, reason in (("widget", "no_default_branch"), ("other", "not_allowlisted")):
        repo = tmp_path / name
        _init_repo(repo)
        _git(repo, "remote", "add", "origin", f"https://github.com/example-team/{name}.git")
        assert C.classify_repo(repo, set()) == (None, reason)


def test_exact_github_fork_check_never_enumerates_owner(monkeypatch):
    C.configure(parse_config({"identities": {"git_repos": ["github.com/example-team/widget"]}}))
    calls = []

    def view(name, args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, '{"isFork": false}')

    monkeypatch.setattr(C, "_outbound_run", view)
    assert C.github_forks() == set()
    assert calls == [["gh", "repo", "view", "example-team/widget", "--json", "isFork"]]
    monkeypatch.setattr(C, "_outbound_run", lambda *args, **kwargs: subprocess.CompletedProcess([], 1, ""))
    assert C.github_forks() is None


@pytest.mark.parametrize(
    "git",
    [
        {"extra_branches": {"github.com/example/widget": ["--all"]}},
        {"extra_branches": {"github.com/example/widget": ["main..other"]}},
        {"extra_branches": {"github.com/example/widget": "next"}},
        {"repo_aliases": {"old/widget": "new/widget", "new/widget": "old/widget"}},
        {"repo_aliases": {"old/widget": "new/widget", "new/widget": "final/widget"}},
    ],
)
def test_invalid_branch_and_alias_config_is_rejected(git):
    with pytest.raises(ConfigError):
        parse_config({"git": git})


def test_extra_branch_collects_done_and_commits_without_checkout(tmp_path, monkeypatch):
    repo = tmp_path / "widget"
    _init_repo(repo)
    task = repo / "backlog/tasks/ex-0001 - Widget.md"
    task.parent.mkdir(parents=True)
    (repo / "backlog/config.yml").write_text("task_prefix: EX\nzero_padded_ids: 4\n")
    task.write_text("---\nid: EX-0001\nstatus: To Do\nupdated_date: 2030-01-01\n---\n")
    _git(repo, "add", "backlog")
    base = _commit(repo, "base")
    _git(repo, "checkout", "-qb", "staging")
    task.write_text("---\nid: EX-0001\nstatus: Done\nupdated_date: 2030-01-02\n---\n")
    _git(repo, "add", "backlog")
    landed = _commit(repo, "close task\n\nTask: EX-0001")
    _git(repo, "checkout", "-q", "main")
    slug = "git.example.org/example/widget"
    config = parse_config(
        {"identities": {"git_owners": ["git.example.org/example"]}, "git": {"extra_branches": {slug: ["staging"]}}}
    )
    C.configure(config)
    # Real git fetches, only the transport URL is replaced with a disposable local source.
    origin = tmp_path / "origin.git"
    _git(tmp_path, "clone", "--bare", str(repo), str(origin))
    _git(repo, "remote", "add", "origin", f"https://{slug}.git")
    outbound = C._outbound_run

    def local_fetch(name, args, **kwargs):
        args = [str(origin) if arg == "origin" else arg for arg in args]
        return outbound(name, args, **kwargs)

    monkeypatch.setattr(C, "_outbound_run", local_fetch)
    monkeypatch.setattr(C, "REPOSITORIES", [repo])
    info, reason = C.classify_repo(repo, set())
    assert reason is None
    assert _git(repo, "branch", "--show-current").strip() == "main"
    rows = C.git_commits(info, 180)
    assert {r["sha"]: r["on_default"] for r in rows} == {base: True, landed: False}
    assert set(info["refs"]) == {"refs/remotes/origin/main", "refs/remotes/origin/staging"}
    assert {r["sha"] for r in C.git_commit_files(info, 180)} == {base, landed}
    assert [(r["task_key"], r["sha"]) for r in C.backlog_done_events(info, 180, 4)] == [("EX-0001", landed)]
    prefix, tasks, _ = C.backlog(info)
    assert prefix["prefix"] == "EX" and tasks[0]["status"] == "Done"
    collector = C.Collector(None, True, time.monotonic() + 30)
    collector.repositories()
    assert collector.errors == []
    assert collector.counts["git_commit"]["rows"] == 2
    assert collector.counts["backlog_done_event"]["rows"] == 1
    # Reading the same history from both configured branches never emits duplicate keys.
    _git(repo, "update-ref", "refs/remotes/origin/main", landed)
    assert len(C.git_commits(info, 180)) == 2
    assert len(C.backlog_done_events(info, 180, 4)) == 1
    # A missing explicitly requested branch must not trigger destructive reconciliation.
    config.git_extra_branches[slug] = ("missing",)
    C.configure(config)
    with pytest.raises(RuntimeError):
        C.classify_repo(repo, set())

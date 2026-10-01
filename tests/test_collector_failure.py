"""A failed Actions query is a collection failure, never a CI observation."""

import subprocess
import time

import pytest

from agent_history import collect_git as C


@pytest.mark.parametrize("slug", [None, "", "local/repo", "example.com/operator", "example.com/operator/repo/extra"])
def test_malformed_slugs_are_not_allowlisted(slug, monkeypatch):
    monkeypatch.setattr(C, "ALLOWED_OWNERS", {("example.com", "operator")})
    assert C.allowlisted(slug) is False


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

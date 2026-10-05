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
    clean.execute("TRUNCATE ah.git_commit, ah.git_commit_file, ah.collector_mutation_audit")  # survive `clean`
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


@pytest.fixture
def off_default_repo(clean, repo):  # noqa: F811
    import time

    from agent_history.collect_git import Collector

    slug = "git.example.net/team/tools"
    git(repo, "checkout", "-q", "-b", "side")
    (repo / "side.txt").write_text("side\n")
    git(repo, "add", "side.txt")
    git(repo, "commit", "-q", "-m", "side commit")
    side = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    info = {"path": repo, "ref": "side", "slug": slug, "fetched": False, "emails": {OWNER}}
    collector = Collector(clean, False, time.monotonic() + 60)
    collector.one_repo(info, {})
    assert not collector.errors
    git(repo, "checkout", "-q", "main")
    git(repo, "update-ref", "refs/remotes/origin/main", "main")
    info.update(ref="origin/main", fetched=True)
    tip = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "origin/main"], capture_output=True, text=True, check=True
    ).stdout.strip()
    unknown = "9" * 40
    clean.execute("DELETE FROM ah.backlog_done_event WHERE repo_slug = %s", (slug,))
    clean.execute("DELETE FROM ah.backlog_done_scan WHERE repo_slug = %s", (slug,))
    clean.execute("INSERT INTO ah.backlog_done_scan VALUES (%s, now())", (slug,))
    for task, sha in [("EX-0001", side), ("EX-0002", side), ("EX-0003", unknown), ("EX-0004", tip)]:
        clean.execute(
            "INSERT INTO ah.backlog_done_event (repo_slug, task_key, sha, done_at) VALUES (%s, %s, %s, now())",
            (slug, task, sha),
        )
    clean.execute(
        "INSERT INTO ah.git_commit (repo_slug, sha, context, committed_at, subject, on_default) "
        "VALUES (%s, %s, 'default', now(), 'unknown', true)",
        (slug, unknown),
    )
    clean.commit()
    try:
        yield info, side, tip, unknown
    finally:
        clean.rollback()
        clean.execute("DELETE FROM ah.backlog_done_event WHERE repo_slug = %s", (slug,))
        clean.execute("DELETE FROM ah.backlog_done_scan WHERE repo_slug = %s", (slug,))
        clean.commit()


def reconcile_off_default(db, info, table, dry_run=False):
    import time

    from agent_history.collect_git import Collector

    collector = Collector(db, dry_run, time.monotonic() + 60)
    if table == "git_commit":
        collector.one_repo(info, {})
        return collector
    collector._backlog_done(info, 4)
    return collector


@pytest.fixture
def mutation_guard(clean):  # noqa: F811
    """A real BEFORE trigger verifies the audit is already visible when mutation starts."""
    from psycopg import sql

    installed = []

    def install(table, fail=False):
        clean.execute(
            """
            CREATE OR REPLACE FUNCTION pg_temp.require_collector_audit() RETURNS trigger AS $$
            DECLARE expected_key jsonb;
            BEGIN
                IF TG_OP = 'UPDATE' THEN
                    IF NEW.on_default IS DISTINCT FROM false OR OLD.on_default IS NOT DISTINCT FROM false THEN
                        RETURN NEW;
                    END IF;
                END IF;
                expected_key := jsonb_build_object('repo_slug', OLD.repo_slug, 'sha', OLD.sha);
                IF TG_TABLE_NAME = 'backlog_done_event' THEN
                    expected_key := expected_key || jsonb_build_object('task_key', to_jsonb(OLD)->>'task_key');
                END IF;
                IF NOT EXISTS (SELECT 1 FROM ah.collector_mutation_audit
                               WHERE table_name = TG_TABLE_NAME AND row_key = expected_key) THEN
                    RAISE EXCEPTION 'synthetic mutation started without prior audit';
                END IF;
                IF TG_ARGV[0] = 'fail' THEN
                    RAISE EXCEPTION 'synthetic mutation failure after prior audit';
                END IF;
                IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql
            """
        )
        operation = "UPDATE" if table == "git_commit" else "DELETE"
        clean.execute(
            sql.SQL(
                "CREATE TRIGGER require_audit BEFORE {} ON {} FOR EACH ROW "
                "EXECUTE FUNCTION pg_temp.require_collector_audit({})"
            ).format(sql.SQL(operation), sql.Identifier("ah", table), sql.Literal("fail" if fail else "pass"))
        )
        installed.append(table)
        clean.commit()

    yield install
    clean.rollback()
    for table in installed:
        clean.execute(sql.SQL("DROP TRIGGER require_audit ON {}").format(sql.Identifier("ah", table)))
    clean.commit()


@pytest.mark.parametrize("table, operation", [("git_commit", "off_default"), ("backlog_done_event", "delete")])
def test_off_branch_mutations_append_attributable_audit_before_mutating(
    clean,  # noqa: F811
    off_default_repo,
    mutation_guard,
    table,
    operation,
):
    info, side, tip, unknown = off_default_repo
    slug = info["slug"]
    before_files = clean.execute(
        "SELECT * FROM ah.git_commit_file WHERE repo_slug = %s ORDER BY sha, path", (slug,)
    ).fetchall()
    clean.commit()
    mutation_guard(table)
    collector = reconcile_off_default(clean, info, table)
    assert not collector.errors
    audits = clean.execute(
        "SELECT table_name, operation, row_key, repo_slug, ref, ref_sha, deleted_at "
        "FROM ah.collector_mutation_audit ORDER BY row_key->>'task_key'"
    ).fetchall()
    keys = (
        [{"repo_slug": slug, "sha": side}]
        if table == "git_commit"
        else [{"repo_slug": slug, "task_key": task, "sha": side} for task in ("EX-0001", "EX-0002")]
    )
    assert len(audits) == len(keys)
    for audit, key in zip(audits, keys, strict=True):
        assert audit[:6] == (table, operation, key, slug, "origin/main", tip)
        assert audit[6] is not None
    if table == "git_commit":
        assert clean.execute(
            "SELECT on_default FROM ah.git_commit WHERE repo_slug = %s AND sha = %s", (slug, side)
        ).fetchone() == (False,)
        assert clean.execute(
            "SELECT on_default FROM ah.git_commit WHERE repo_slug = %s AND sha = %s", (slug, unknown)
        ).fetchone() == (True,)
        assert clean.execute("SELECT count(*) FROM ah.git_commit WHERE repo_slug = %s", (slug,)).fetchone() == (4,)
    else:
        assert clean.execute(
            "SELECT task_key FROM ah.backlog_done_event WHERE repo_slug = %s ORDER BY task_key", (slug,)
        ).fetchall() == [("EX-0003",), ("EX-0004",)]
    assert (
        clean.execute("SELECT * FROM ah.git_commit_file WHERE repo_slug = %s ORDER BY sha, path", (slug,)).fetchall()
        == before_files
    )
    clean.commit()
    collector = reconcile_off_default(clean, info, table)
    assert not collector.errors
    assert clean.execute("SELECT count(*) FROM ah.collector_mutation_audit").fetchone() == (len(keys),)
    clean.rollback()


@pytest.mark.parametrize("table", ["git_commit", "backlog_done_event"])
@pytest.mark.parametrize("failure_site", ["audit", "mutation"])
def test_off_branch_audit_and_mutation_roll_back_together(clean, off_default_repo, mutation_guard, table, failure_site):  # noqa: F811
    import psycopg

    info, side, _, _ = off_default_repo
    if failure_site == "mutation":
        mutation_guard(table, fail=True)
    else:
        clean.execute(
            """
            CREATE OR REPLACE FUNCTION pg_temp.reject_collector_audit() RETURNS trigger AS $$
            BEGIN RAISE EXCEPTION 'synthetic audit failure'; END;
            $$ LANGUAGE plpgsql;
            CREATE TRIGGER reject_audit BEFORE INSERT ON ah.collector_mutation_audit
            FOR EACH ROW EXECUTE FUNCTION pg_temp.reject_collector_audit();
            """
        )
        clean.commit()
    try:
        if table == "git_commit":
            collector = reconcile_off_default(clean, info, table)
            assert collector.errors == [{"repo": info["slug"], "step": "git_commit", "error": "RaiseException"}]
            assert "off_default" not in collector.counts.get(table, {})
        else:
            message = (
                "synthetic audit failure" if failure_site == "audit" else "synthetic mutation failure after prior audit"
            )
            with pytest.raises(psycopg.errors.RaiseException, match=message):
                reconcile_off_default(clean, info, table)
        clean.rollback()
        assert clean.execute("SELECT count(*) FROM ah.collector_mutation_audit").fetchone() == (0,)
        assert clean.execute(
            "SELECT on_default FROM ah.git_commit WHERE repo_slug = %s AND sha = %s", (info["slug"], side)
        ).fetchone() == (True,)
        assert clean.execute(
            "SELECT count(*) FROM ah.backlog_done_event WHERE repo_slug = %s AND sha = %s", (info["slug"], side)
        ).fetchone() == (2,)
    finally:
        clean.rollback()
        if failure_site == "audit":
            clean.execute("DROP TRIGGER reject_audit ON ah.collector_mutation_audit")
            clean.commit()


@pytest.mark.parametrize("table", ["git_commit", "backlog_done_event"])
@pytest.mark.parametrize("dry_run, fetched", [(False, False), (True, True)])
def test_off_branch_audit_requires_successful_fetch_and_non_dry_run(clean, off_default_repo, table, dry_run, fetched):  # noqa: F811
    info, side, _, _ = off_default_repo
    info["fetched"] = fetched
    collector = reconcile_off_default(clean, info, table, dry_run=dry_run)
    assert not collector.errors
    assert clean.execute("SELECT count(*) FROM ah.collector_mutation_audit").fetchone() == (0,)
    assert clean.execute(
        "SELECT on_default FROM ah.git_commit WHERE repo_slug = %s AND sha = %s", (info["slug"], side)
    ).fetchone() == (True,)
    assert clean.execute(
        "SELECT count(*) FROM ah.backlog_done_event WHERE repo_slug = %s AND sha = %s", (info["slug"], side)
    ).fetchone() == (2,)
    clean.rollback()

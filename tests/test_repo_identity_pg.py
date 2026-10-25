"""Explicit transfer identity is reader metadata, never a rewrite or scope grant."""

import json
from datetime import timedelta

import pytest
from test_loader_pg import DSN, clean, conn  # noqa: F401
from test_loop_progress_pg import add_done, add_scan, collector_rows, populated, tasks_done  # noqa: F401

from agent_history import collect_git, loops
from agent_history.config import parse_config

pytestmark = pytest.mark.skipif(
    not DSN or "agent_history_test" not in DSN, reason="AGENT_HISTORY_TEST_DSN (a *_test database) not set"
)


def test_transfer_counts_old_and_new_history_once(clean, collector_rows, tmp_path):  # noqa: F811
    populated(clean, tmp_path)
    old, new = "github.com/example/project", "github.com/new-team/project"
    # The writer's published map is retained metadata, shared by refreshes and readers.
    clean.execute(
        "INSERT INTO ah.meta (key,value) VALUES ('git_repo_aliases', %s)",
        (json.dumps({"example/project": "new-team/project"}),),
    )
    clean.execute("UPDATE ah.loops SET repo='new-team/project'")
    launch = clean.execute("SELECT launch_ts FROM ah.loops").fetchone()[0]
    try:
        add_scan(clean, collector_rows, old)
        add_done(clean, collector_rows, old, "EX-0001", "a" * 40, launch + timedelta(minutes=1))
        add_done(clean, collector_rows, new, "EX-0001", "a" * 40, launch + timedelta(minutes=1))
        add_done(clean, collector_rows, new, "EX-0002", "b" * 40, launch + timedelta(minutes=2))
        add_done(clean, collector_rows, "github.com/new-team/other", "EX-0003", "c" * 40, launch + timedelta(minutes=3))
        loops._refresh_tasks_done(clean)
        assert tasks_done(clean) == 2
        clean.execute("UPDATE ah.loops SET repo='example/project'")
        loops._refresh_tasks_done(clean)
        assert tasks_done(clean) == 2
        assert clean.execute("SELECT count(*) FROM ah.backlog_done_event WHERE repo_slug = %s", (old,)).fetchone() == (
            1,
        )
    finally:
        clean.rollback()
        clean.execute("DELETE FROM ah.meta WHERE key='git_repo_aliases'")
        clean.commit()


def test_writer_publishes_additive_aliases_without_history_or_grant_changes(clean):  # noqa: F811
    before = clean.execute(
        "SELECT grantee,table_name,privilege_type FROM information_schema.role_table_grants "
        "WHERE table_schema='ah' ORDER BY 1,2,3"
    ).fetchall()
    config = parse_config({"git": {"repo_aliases": {"example/project": "new-team/project"}}})
    try:
        collect_git.publish_repo_aliases(clean, config)
        collect_git.publish_repo_aliases(clean, parse_config({}))  # an unconfigured copy cannot erase history
        assert clean.execute("SELECT ah.canonical_repo('github.com/Example/Project')").fetchone() == (
            "new-team/project",
        )
        assert clean.execute("SELECT ah.canonical_repo('new-team/project')").fetchone() == ("new-team/project",)
        assert clean.execute("SELECT ah.canonical_repo('github.com/new-team/other')").fetchone() == ("new-team/other",)
        with pytest.raises(ValueError, match="conflicting retained"):
            collect_git.publish_repo_aliases(
                clean, parse_config({"git": {"repo_aliases": {"example/project": "other/project"}}})
            )
        assert (
            clean.execute(
                "SELECT grantee,table_name,privilege_type FROM information_schema.role_table_grants "
                "WHERE table_schema='ah' ORDER BY 1,2,3"
            ).fetchall()
            == before
        )
    finally:
        clean.rollback()
        clean.execute("DELETE FROM ah.meta WHERE key='git_repo_aliases'")
        clean.commit()

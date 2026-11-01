"""A loop row is attributed to a repository whenever its own checkout or root session names one.

Exact identity (receipts, collected charter/closeout origins) still wins. When it yields no
repository, ah.loops.repo falls back to the checkout holding the report path, then to the root
session's repository, so a live loop is never unattributed while its checkout is known.
"""

import os
from datetime import timedelta

import pytest

from agent_history import load
from test_loop_identity_pg import conn  # noqa: F401 - real disposable catalogue fixture
from test_loop_identity_v3 import AT, DIGEST
from test_loop_identity_v3_pg import clean, observe, refresh  # noqa: F401
from test_loop_launch_v3_pg import transcript

DSN = os.environ.get("AGENT_HISTORY_TEST_DSN", "")
pytestmark = pytest.mark.skipif(not DSN or "agent_history_test" not in DSN, reason="scratch database not set")

CHECKOUT = "/home/user/repos/project"
REPORT_IN_CHECKOUT = CHECKOUT + "/codex/closeout-synthetic-loop14.md"


def repo_of(db):
    return db.execute("SELECT repo FROM ah.loops").fetchone()[0]


def add_repo(db, root, slug):
    return db.execute(
        "INSERT INTO ah.repo(local_root, slug) VALUES (%s, %s) ON CONFLICT (local_root) DO UPDATE "
        "SET slug = EXCLUDED.slug RETURNING id",
        (root, slug),
    ).fetchone()[0]


@pytest.fixture
def launched(clean, tmp_path):  # noqa: F811
    transcript(tmp_path, "claude")
    refresh(clean, {"claude-test": tmp_path})
    return clean


def test_unattributed_run_stays_null_without_any_checkout(launched):
    refresh(launched)
    assert repo_of(launched) is None


def test_root_session_repository_attributes_the_run(launched):
    repo_id = add_repo(launched, "/home/user/repos/elsewhere", "github.com/Example/Project")
    launched.execute("UPDATE ah.session SET repo_id = %s", (repo_id,))
    launched.commit()
    refresh(launched)
    assert repo_of(launched) == "example/project"


def test_report_path_checkout_attributes_a_run_whose_session_has_no_repository(launched):
    add_repo(launched, CHECKOUT, "github.com/example/project")
    launched.execute("UPDATE ah.loop_run SET report_path = %s", (REPORT_IN_CHECKOUT,))
    launched.commit()
    assert launched.execute("SELECT repo_id FROM ah.session").fetchone()[0] is None
    refresh(launched)
    assert repo_of(launched) == "example/project"


def test_report_path_checkout_wins_over_the_root_session_repository(launched):
    add_repo(launched, CHECKOUT, "github.com/example/project")
    other = add_repo(launched, "/home/user/repos/other", "github.com/example/other")
    launched.execute("UPDATE ah.session SET repo_id = %s", (other,))
    launched.execute("UPDATE ah.loop_run SET report_path = %s", (REPORT_IN_CHECKOUT,))
    launched.commit()
    refresh(launched)
    assert repo_of(launched) == "example/project"


def test_local_only_checkouts_are_not_a_repository(launched):
    repo_id = add_repo(launched, "/home/user/repos/scratch", "local/scratch")
    launched.execute("UPDATE ah.session SET repo_id = %s", (repo_id,))
    launched.commit()
    refresh(launched)
    assert repo_of(launched) is None


def test_exact_origin_wins_over_the_fallback(launched):
    repo_id = add_repo(launched, "/home/user/repos/elsewhere", "github.com/example/wrong")
    launched.execute("UPDATE ah.session SET repo_id = %s", (repo_id,))
    launched.commit()
    observe(launched, origin="example/project", when=AT + timedelta(minutes=1))
    refresh(launched)
    assert launched.execute("SELECT repo, loop, goal_sha256 FROM ah.loops").fetchone() == (
        "example/project",
        "loop14",
        DIGEST,
    )


def test_finished_unattributed_row_is_backfilled_by_the_normal_refresh(launched):
    observe(launched, origin="example/project")
    refresh(launched)
    launched.execute("UPDATE ah.loops SET repo = NULL")
    launched.execute("DELETE FROM ah.loop_run_metadata")
    repo_id = add_repo(launched, "/home/user/repos/elsewhere", "github.com/example/backfilled")
    launched.execute("UPDATE ah.session SET repo_id = %s", (repo_id,))
    launched.commit()
    load.post_passes(launched)
    launched.commit()
    assert repo_of(launched) == "example/backfilled"

"""Loop lifecycle and identity from wave-notify receipts, against a disposable catalogue.

Receipt rows are synthetic (`/work/example/repo`); the collector tests use real files in a
temporary repository. Skipped unless the AGENT_HISTORY_TEST_* DSNs name a *_test database.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
import types
import uuid
from datetime import datetime, timedelta, timezone

import pytest

DSN = os.environ.get("AGENT_HISTORY_TEST_DSN", "")
ADMIN_DSN = os.environ.get("AGENT_HISTORY_TEST_ADMIN_DSN", "")
READER_DSN = os.environ.get("AGENT_HISTORY_TEST_READER_DSN", "")
pytestmark = pytest.mark.skipif(
    not all("agent_history_test" in dsn for dsn in (DSN, ADMIN_DSN, READER_DSN)),
    reason="disposable AGENT_HISTORY_TEST_* DSNs not set",
)
psycopg = pytest.importorskip("psycopg")
from psycopg import sql  # noqa: E402

from agent_history import collect_git, load, loops  # noqa: E402

REPO = "/work/example/repo"
GOAL = "a" * 64
OTHER = "b" * 64


@pytest.fixture(scope="module")
def conn():
    connection = load.connect(DSN)
    load.apply_schema(connection, force=True)
    connection.commit()
    yield connection
    connection.close()


@pytest.fixture
def clean(conn):
    conn.execute("TRUNCATE " + ", ".join(f"ah.{table}" for table in load.DATA_TABLES) + " RESTART IDENTITY CASCADE")
    conn.execute("DELETE FROM ah.loop_receipt")
    conn.execute("DELETE FROM ah.meta WHERE key = 'loops_receipt_identity_seen'")
    conn.commit()
    yield conn
    conn.rollback()
    conn.execute("DELETE FROM ah.loop_receipt")
    conn.execute("DELETE FROM ah.meta WHERE key = 'loops_receipt_identity_seen'")
    conn.commit()


class World:
    """Index synthetic root launches (one session file each), then add receipts."""

    def __init__(self, conn, tmp_path):
        self.conn, self.tmp_path = conn, tmp_path
        self.base = datetime.now(timezone.utc) - timedelta(hours=3)
        self.counter = 0

    def launch(self, number=1, *, at=0, cwd=REPO, extra="", name="synthetic"):
        """Launch of report-<name>-loop<number>.md `at` seconds after base, in its own root session."""
        self.counter += 1
        source = self.tmp_path / "sessions"
        project = source / "projects" / "synthetic-project"
        project.mkdir(parents=True, exist_ok=True)
        session = str(uuid.UUID(int=self.counter))
        line = {
            "type": "user",
            "uuid": f"launch-{self.counter}",
            "sessionId": session,
            "timestamp": (self.base + timedelta(seconds=at)).isoformat(),
            "cwd": cwd,
            "message": {
                "role": "user",
                "content": f"You are the root. Write codex/report-{name}-loop{number}.md.\n{extra}",
            },
        }
        (project / f"{session}.jsonl").write_text(json.dumps(line, separators=(",", ":")) + "\n")
        stats = load.refresh(self.conn, sources={"claude-test": source}, textfile=None, log=lambda *_: None)
        assert stats.errors == 0
        self.conn.execute("UPDATE ah.session SET last_event_at = now()")
        self.conn.commit()
        return session

    def report(self, number=1, cwd=REPO, name="synthetic"):
        return f"{cwd}/codex/report-{name}-loop{number}.md"

    def goal(self, number=1, cwd=REPO, name="synthetic"):
        return f"{cwd}/codex/goal-{name}-loop{number}.md"

    def receipt(self, kind, path, content, *, at=60, machine="mac-a", origin=None, exists=True, sha=None, line1=None):
        self.conn.execute(
            "INSERT INTO ah.loop_receipt (machine, path, kind, content, receipt_mtime, target_exists, "
            "target_sha256, target_line1, repo_origin) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT (machine, kind, path) DO UPDATE SET content = EXCLUDED.content, "
            "receipt_mtime = EXCLUDED.receipt_mtime",
            (machine, path, kind, content, self.base + timedelta(seconds=at), exists, sha, line1, origin),
        )
        self.conn.commit()
        return self.base + timedelta(seconds=at)

    def notified(self, number=1, *, content=None, kind="notified", **kw):
        """A v2 receipt whose target is a well-formed report for `number` unless overridden.

        `kind="posted"` is the receiver receipt wave-notify writes in place of `.notified`.
        """
        header = kw.pop("line1", f"# Loop: example/repo loop{number} · Goal: {GOAL}")
        sha = kw.pop("sha", "c" * 64)
        if content is None:
            content = (
                f"sha256:{sha} request req-1\n"
                if kind == "notified"
                else f'sha256:{sha} receiver {{"id": "example/repo#loop{number}#{GOAL}", "revision": "1"}}\n'
            )
        cwd = kw.pop("cwd", REPO)
        return self.receipt(kind, self.report(number, cwd), content, sha=kw.pop("target_sha", sha), line1=header, **kw)

    def refresh(self):
        load.post_passes(self.conn)
        self.conn.commit()

    def loop(self, number=1):
        return self.conn.execute(
            "SELECT l.status, l.end_ts, r.end_evidence FROM ah.loops l JOIN ah.loop_run r USING (launch_uid) "
            "WHERE r.report_path = %s ORDER BY r.launch_ts DESC LIMIT 1",
            (self.report(number),),
        ).fetchone()

    def identity(self, number=1):
        return self.conn.execute(
            "SELECT l.repo, l.loop, l.goal_sha256 FROM ah.loops l JOIN ah.loop_run r USING (launch_uid) "
            "WHERE r.report_path = %s ORDER BY r.launch_ts DESC LIMIT 1",
            (self.report(number),),
        ).fetchone()


@pytest.fixture
def world(clean, tmp_path):
    return World(clean, tmp_path)


# -- finish rule ---------------------------------------------------------------------------------


def test_no_receipt_is_running(world):
    world.launch()
    world.refresh()
    assert world.loop() == ("running", None, "root_last_event")


def test_receipt_after_launch_finishes_at_its_mtime(world):
    world.launch()
    when = world.notified(at=60)
    world.refresh()
    assert world.loop() == ("finished", when, "completion_receipt")


def test_report_write_alone_no_longer_finishes(world):
    # The root writing its report is not a delivered notification.
    root = world.launch()
    world.conn.execute(
        "INSERT INTO ah.artifact (agent, event_uid, session_id, ts, kind, action, path, evidence_type, "
        "source_id, byte_offset) SELECT 'codex', 'synthetic-report', id, now(), 'file', 'update', %s, 'tool', 0, 0 "
        "FROM ah.session WHERE session_uid = %s",
        (world.report(), root),
    )
    world.conn.execute("INSERT INTO ah.dirty_session (session_id) SELECT id FROM ah.session")
    world.conn.commit()
    world.refresh()
    assert world.loop()[0] == "running"


def test_receipt_before_launch_is_running(world):
    world.launch(at=120)
    world.notified(at=60)
    world.refresh()
    assert world.loop()[0] == "running"


def test_a_later_launch_of_the_same_report_path_bounds_the_earlier_one(world):
    world.launch(1, at=0)
    world.launch(1, at=100)  # a relaunch of the same report path, in another root
    world.notified(1, at=200)
    world.refresh()
    first, second = world.conn.execute(
        "SELECT l.status FROM ah.loops l JOIN ah.loop_run r USING (launch_uid) ORDER BY r.launch_ts"
    ).fetchall()
    assert (first, second) == (("running",), ("finished",))


def test_parallel_campaigns_in_one_directory_finish_independently(world):
    world.launch(1, at=0, name="campa")
    world.launch(7, at=100, name="campb")
    when = world.receipt("notified", world.report(1, name="campa"), "request r\n", at=200, exists=False)
    world.refresh()
    assert world.conn.execute(
        "SELECT l.status, l.end_ts FROM ah.loops l JOIN ah.loop_run r USING (launch_uid) "
        "WHERE r.report_path LIKE '%campa%'"
    ).fetchone() == ("finished", when)
    assert world.conn.execute(
        "SELECT l.status FROM ah.loops l JOIN ah.loop_run r USING (launch_uid) WHERE r.report_path LIKE '%campb%'"
    ).fetchone() == ("running",)


def test_digest_receipt_whose_report_is_missing_does_not_finish(world):
    world.launch()
    world.receipt("notified", world.report(), f"sha256:{'c' * 64} request r\n", exists=False)
    world.refresh()
    assert world.loop()[0] == "running"


def test_lane_session_after_receipt_is_running(world):
    root = world.launch()
    world.notified(at=60)
    world.conn.execute(
        "INSERT INTO ah.session (agent, session_uid, namespace, root_session_id, first_event_at) "
        "SELECT 'pi', 'synthetic-late-lane', 'pi-test', id, %s FROM ah.session WHERE session_uid = %s",
        (world.base + timedelta(seconds=120), root),
    )
    world.conn.commit()
    world.refresh()
    assert world.loop()[0] == "running"


@pytest.mark.parametrize(
    "case, kw",
    [
        ("other loop number", {"line1": f"# Loop: example/repo loop2 · Goal: {GOAL}"}),
        ("sha mismatch", {"target_sha": "d" * 64}),
        ("no header", {"line1": "not a loop header"}),
        ("malformed content", {"content": "delivered\n"}),
    ],
)
def test_receipt_that_does_not_match_its_report_is_running(world, case, kw):
    world.launch()
    world.notified(**kw)
    world.refresh()
    assert world.loop()[0] == "running", case


def test_posted_receipt_finishes_and_identifies_like_a_notified_one(world):
    world.launch()
    when = world.notified(kind="posted", origin="example/repo")
    world.refresh()
    assert world.loop() == ("finished", when, "completion_receipt")
    assert world.identity() == ("example/repo", "loop1", GOAL)


@pytest.mark.parametrize(
    "case, kw",
    [
        ("sha mismatch", {"target_sha": "d" * 64}),
        ("other loop number", {"line1": f"# Loop: example/repo loop2 · Goal: {GOAL}"}),
        ("report missing", {"exists": False}),
        # A receiver receipt always carries a digest: it never counts on the exact path alone.
        ("no digest", {"content": 'receiver {"id": "r", "revision": "1"}\n'}),
        ("notified wording", {"content": f"sha256:{'c' * 64} request req-1\n"}),
    ],
)
def test_posted_receipt_that_does_not_match_its_report_is_running(world, case, kw):
    world.launch()
    world.notified(kind="posted", **kw)
    world.refresh()
    assert world.loop()[0] == "running", case


def test_posted_and_notified_receipts_together_take_the_earliest(world):
    world.launch()
    when = world.notified(at=100, origin="example/repo")
    world.notified(kind="posted", at=200, origin="example/repo")
    world.refresh()
    assert world.loop() == ("finished", when, "completion_receipt")
    assert world.identity() == ("example/repo", "loop1", GOAL)


def test_legacy_receipt_counts_on_exact_path_alone(world):
    world.launch()
    when = world.receipt("notified", world.report(), "request req-legacy\n", exists=False)
    world.refresh()
    assert world.loop() == ("finished", when, "completion_receipt")


def test_a_receipt_for_another_path_does_not_finish_the_loop(world):
    world.launch()
    world.receipt("notified", world.report() + ".tmp", "request req-legacy\n")
    world.receipt("notified", world.report(2), "request req-legacy\n")
    world.receipt("started", world.report(), "request req-legacy\n")
    world.refresh()
    assert world.loop()[0] == "running"


def test_two_machines_take_the_earliest_receipt(world):
    world.launch()
    world.notified(at=300, machine="mac-a")
    when = world.notified(at=100, machine="mac-b")
    world.refresh()
    assert world.loop() == ("finished", when, "completion_receipt")


def test_finish_survives_analytics_reapply_and_rebuild(world):
    world.launch()
    when = world.notified()
    world.refresh()
    load.apply_schema(world.conn, force=True)
    world.conn.commit()
    assert world.conn.execute("SELECT count(*) FROM ah.loop_receipt").fetchone() == (1,)
    assert (
        load.rebuild(
            world.conn, sources={"claude-test": world.tmp_path / "sessions"}, textfile=None, log=lambda *_: None
        ).errors
        == 0
    )
    world.conn.execute("UPDATE ah.session SET last_event_at = now()")
    world.conn.commit()
    world.refresh()
    assert world.conn.execute("SELECT count(*) FROM ah.loop_receipt").fetchone() == (1,)
    assert world.loop() == ("finished", when, "completion_receipt")


def test_receipt_cost_scales_with_receipts(world):
    """Ten times the loops and receipts costs about ten times the work, not a hundred."""

    def build(count, offset):
        root = world.launch(900)  # a real root session to own the synthetic launches
        owner = world.conn.execute("SELECT id FROM ah.session WHERE session_uid = %s", (root,)).fetchone()[0]
        world.conn.execute(
            "INSERT INTO ah.loop_run (launch_uid, root_session_id, status, loop_number, naming, report_path, "
            "launch_ts) SELECT 'scale-' || %s || '-' || n, %s, 'resolved', n, 'loop', "
            "'/work/scale/' || (n %% 7) || '/codex/report-scale-loop' || n || '.md', %s::timestamptz + n * interval '1 second' "
            "FROM generate_series(1, %s) n",
            (offset, owner, world.base, count),
        )
        world.conn.execute(
            "INSERT INTO ah.loop_receipt (machine, path, kind, content, receipt_mtime, target_exists) "
            "SELECT 'mac-a', report_path, 'notified', 'request r', launch_ts + interval '1 second', false "
            "FROM ah.loop_run WHERE launch_uid LIKE 'scale-' || %s || '-%%'",
            (offset,),
        )
        world.conn.commit()
        # Autovacuum analyses a live catalogue; a fresh test table has no statistics to plan with.
        world.conn.autocommit = True
        world.conn.execute("ANALYZE ah.loop_run")
        world.conn.execute("ANALYZE ah.loop_receipt")
        world.conn.autocommit = False

    def timed():
        started = time.perf_counter()
        loops._refresh_completion_receipts(world.conn)
        world.conn.commit()
        return time.perf_counter() - started

    build(150, "a")
    small = timed()
    assert world.conn.execute(
        "SELECT count(*) FROM ah.loop_run WHERE end_evidence = 'completion_receipt'"
    ).fetchone() == (150,)
    world.conn.execute("DELETE FROM ah.loop_run WHERE launch_uid LIKE 'scale-%'")
    world.conn.execute("DELETE FROM ah.loop_receipt")
    world.conn.commit()
    build(1500, "b")
    large = timed()
    assert world.conn.execute(
        "SELECT count(*) FROM ah.loop_run WHERE end_evidence = 'completion_receipt'"
    ).fetchone() == (1500,)
    # Generous bound: quadratic work would be near 100x; allow noise on a small baseline.
    assert large < max(small, 0.02) * 40
    # Finished launches are not read again: nothing is left to finish.
    assert loops._refresh_completion_receipts(world.conn) == []
    world.conn.commit()


# -- identity from start receipts -----------------------------------------------------------------


def started(world, number=1, *, content=None, origin="example/repo", **kw):
    content = content if content is not None else f"example/repo#loop{number}#{GOAL}\n"
    return world.receipt("started", world.goal(number), content, origin=origin, **kw)


def test_started_receipt_with_matching_origin_fills_identity(world):
    world.launch()
    started(world)
    world.refresh()
    assert world.identity() == ("example/repo", "loop1", GOAL)


def test_identity_origin_comparison_ignores_case(world):
    world.launch()
    started(world, content=f"Example/Repo#loop1#{GOAL}\n", origin="example/repo")
    world.refresh()
    assert world.identity() == ("Example/Repo", "loop1", GOAL)


@pytest.mark.parametrize(
    "case, kw",
    [
        ("different origin", {"origin": "example/other"}),
        ("no origin", {"origin": None}),
        ("malformed content", {"content": "example/repo loop1 " + GOAL}),
        ("upper-case digest", {"content": f"example/repo#loop1#{GOAL.upper()}\n"}),
    ],
)
def test_receipt_that_cannot_be_verified_leaves_identity_null(world, case, kw):
    world.launch()
    started(world, **kw)
    world.refresh()
    assert world.identity() == (None, None, None), case


def test_receipt_naming_another_loop_is_not_evidence_about_this_launch(world):
    world.launch()
    started(world, content=f"example/repo#loop2#{OTHER}\n")
    world.refresh()
    assert world.identity() == (None, "loop1", None)


def test_receipt_naming_another_loop_keeps_an_exact_launch_identity(world):
    world.launch(extra=f"# Loop: example/repo loop1 · Goal: {GOAL}")
    world.refresh()
    assert world.identity() == ("example/repo", "loop1", GOAL)
    started(world, content=f"example/repo#loop2#{OTHER}\n")
    world.refresh()
    assert world.identity() == ("example/repo", "loop1", GOAL)


def test_relaunch_never_gives_the_first_launch_the_newer_identity(world):
    world.launch(1, at=0)
    world.launch(1, at=500)  # same goal path; the goal changed, so its receipt overwrote the first
    started(world, content=f"example/repo#loop1#{OTHER}\n", at=510)
    world.refresh()
    rows = world.conn.execute(
        "SELECT l.repo, l.goal_sha256 FROM ah.loops l JOIN ah.loop_run r USING (launch_uid) ORDER BY r.launch_ts"
    ).fetchall()
    assert rows == [(None, None), ("example/repo", OTHER)]


def test_started_receipt_older_than_the_launch_is_not_applied(world):
    world.launch(1, at=1000)
    started(world, at=0)
    world.refresh()
    assert world.identity() == (None, "loop1", None)


def test_started_receipt_within_the_skew_allowance_before_the_launch_applies(world):
    world.launch(1, at=1000)
    started(world, at=1000 - 60)
    world.refresh()
    assert world.identity() == ("example/repo", "loop1", GOAL)


def test_receipt_inside_two_launch_windows_is_ambiguous_for_both(world):
    world.launch(1, at=0)
    world.launch(1, at=100)
    started(world, at=50)  # within the skew allowance of the second launch, after the first
    world.refresh()
    rows = world.conn.execute(
        "SELECT l.repo, l.loop, l.goal_sha256 FROM ah.loops l JOIN ah.loop_run r USING (launch_uid) ORDER BY r.launch_ts"
    ).fetchall()
    assert rows == [(None, None, None), (None, None, None)]


def test_machines_that_disagree_leave_identity_null(world):
    world.launch()
    started(world, machine="mac-a")
    started(world, machine="mac-b", content=f"example/repo#loop1#{OTHER}\n")
    world.refresh()
    assert world.identity() == (None, None, None)


def test_machines_that_agree_fill_identity(world):
    world.launch()
    started(world, machine="mac-a")
    started(world, machine="mac-b")
    world.refresh()
    assert world.identity() == ("example/repo", "loop1", GOAL)


def test_two_loops_of_one_repository_keep_their_own_identities(world):
    world.launch(1, at=0)
    world.launch(2, at=5)
    started(world, 1)
    world.receipt("started", world.goal(2), f"example/repo#loop2#{OTHER}\n", origin="example/repo")
    world.refresh()
    assert world.identity(1) == ("example/repo", "loop1", GOAL)
    assert world.identity(2) == ("example/repo", "loop2", OTHER)


def test_receipt_disagreeing_with_an_explicit_launch_identity_is_null(world):
    world.launch(extra=f"# Loop: example/repo loop1 · Goal: {OTHER}")
    started(world)
    world.refresh()
    assert world.identity() == (None, None, None)


def test_receipt_agreeing_with_an_explicit_launch_identity_keeps_it(world):
    world.launch(extra=f"# Loop: example/repo loop1 · Goal: {GOAL}")
    started(world)
    world.refresh()
    assert world.identity() == ("example/repo", "loop1", GOAL)


def test_identity_without_any_receipt_is_unchanged(world):
    world.launch()
    world.refresh()
    assert world.identity() == (None, "loop1", None)


def test_receipt_for_an_already_finished_loop_still_fills_identity(world):
    world.launch()
    world.notified()
    world.refresh()
    assert world.loop()[0] == "finished"
    assert world.identity() == (None, "loop1", None)
    started(world)
    world.refresh()
    assert world.identity() == ("example/repo", "loop1", GOAL)


# -- identity from a valid completion receipt -------------------------------------------------------


def header(number=1, goal=GOAL):
    return f"# Loop: example/repo loop{number} · Goal: {goal}"


def test_finished_loop_takes_identity_from_the_receipt_origin_and_header(world):
    world.launch()
    world.notified(origin="example/repo")
    world.refresh()
    assert world.loop()[0] == "finished"
    assert world.identity() == ("example/repo", "loop1", GOAL)


def test_repo_comes_from_the_origin_never_from_the_header_name(world):
    world.launch()
    world.notified(origin="example/actual", line1=f"# Loop: example/named loop1 · Goal: {GOAL}")
    world.refresh()
    assert world.identity() == ("example/actual", "loop1", GOAL)


@pytest.mark.parametrize(
    "case, kw",
    [
        ("header names another loop", {"line1": header(2), "content": "request r\n"}),
        ("no origin", {"origin": None}),
        ("legacy header without a goal digest", {"line1": "# Loop: example/repo loop1", "content": "request r\n"}),
        ("no header at all", {"line1": None, "content": "request r\n"}),
        # A legacy receipt is not bound to the header bytes, which may have been rewritten since.
        ("legacy receipt beside a well-formed header", {"content": "request r\n"}),
    ],
)
def test_receipt_that_cannot_vouch_for_identity_contributes_nothing(world, case, kw):
    world.launch()
    kw = {"origin": "example/repo", **kw}
    world.notified(**kw)
    world.refresh()
    assert world.loop()[0] == "finished", case
    assert world.identity() == (None, "loop1", None), case


def test_machines_disagreeing_on_the_report_header_leave_identity_null(world):
    world.launch()
    world.notified(origin="example/repo", machine="mac-a")
    world.notified(origin="example/repo", machine="mac-b", line1=header(1, OTHER), at=70)
    world.refresh()
    assert world.identity() == (None, None, None)


def test_machines_disagreeing_on_the_origin_leave_identity_null(world):
    world.launch()
    world.notified(origin="example/repo", machine="mac-a")
    world.notified(origin="example/fork", machine="mac-b", at=70)
    world.refresh()
    assert world.identity() == (None, None, None)


def test_started_identity_wins_when_the_header_agrees_and_nulls_when_it_disagrees(world):
    world.launch()
    started(world)
    world.notified(origin="example/repo")
    world.refresh()
    assert world.identity() == ("example/repo", "loop1", GOAL)
    world.notified(origin="example/repo", line1=header(1, OTHER), machine="mac-b", at=70)
    world.conn.execute("DELETE FROM ah.loop_receipt WHERE machine = 'mac-a' AND kind = 'notified'")
    world.conn.commit()
    world.conn.execute("INSERT INTO ah.dirty_session (session_id) SELECT id FROM ah.session")
    world.conn.commit()
    world.refresh()
    assert world.identity() == (None, None, None)


def test_running_loop_gets_no_identity_from_a_receipt_header(world):
    world.launch(at=120)
    world.notified(at=60, origin="example/repo")  # before the launch: does not finish it
    world.refresh()
    assert world.loop()[0] == "running"
    assert world.identity() == (None, "loop1", None)


def test_receipt_identity_survives_a_dirty_re_tag(world):
    root = world.launch()
    world.notified(origin="example/repo")
    world.refresh()
    world.conn.execute(
        "INSERT INTO ah.dirty_session (session_id) SELECT id FROM ah.session WHERE session_uid = %s", (root,)
    )
    world.conn.commit()
    world.refresh()
    assert world.identity() == ("example/repo", "loop1", GOAL)


# -- collector ------------------------------------------------------------------------------------


def make_repo(tmp_path, origin="https://git.example.test/example/repo.git"):
    repo = tmp_path / "checkout"
    (repo / "codex").mkdir(parents=True)
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
    subprocess.run(["git", "-C", str(repo), "init", "-q", "-b", "main"], check=True, env=env)
    if origin:
        subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", origin], check=True, env=env)
    return repo


def run_collector(conn, repos, machine="mac-a"):
    from agent_history import collect_receipts

    collector = collect_git.Collector(conn, False, time.monotonic() + 60)
    collect_receipts.collect(collector, repos, machine)
    return collector


def test_collector_ships_exact_receipts_and_metadata_only(clean, tmp_path):
    repo = make_repo(tmp_path)
    body = "SECRET-BODY-MARKER"
    report = repo / "codex/report-synthetic-loop1.md"
    report.write_text(f"# Loop: example/repo loop1 · Goal: {GOAL}\n{body}\n")
    goal = repo / "codex/goal-synthetic-loop1.md"
    goal.write_text(f"{body} goal\n")
    (repo / "codex/report-synthetic-loop1.md.notified").write_text("request req-1\n")
    posted = f'sha256:{"c" * 64} receiver {{"id": "r", "revision": "1"}}\n'
    (repo / "codex/report-synthetic-loop1.md.posted").write_text(posted)
    (repo / "codex/goal-synthetic-loop1.md.started").write_text(f"example/repo#loop1#{GOAL}\n")
    (repo / "codex/report-synthetic-loop1.md.tmp.notified").write_text("request req-tmp\n")
    (repo / "codex/report-synthetic-loop1.md.tmp.posted").write_text(posted)
    (repo / "codex/goal-synthetic-loop1.md.tmp.started").write_text("request req-tmp\n")
    (repo / "codex/other.md.notified").write_text("request req-other\n")
    collector = run_collector(clean, [repo])
    assert collector.errors == []
    rows = clean.execute(
        "SELECT machine, kind, path, content, target_exists, target_sha256 IS NOT NULL, target_line1, repo_origin "
        "FROM ah.loop_receipt ORDER BY kind"
    ).fetchall()
    assert rows == [
        (
            "mac-a",
            "notified",
            str(report),
            "request req-1\n",
            True,
            True,
            f"# Loop: example/repo loop1 · Goal: {GOAL}",
            "example/repo",
        ),
        (
            "mac-a",
            "posted",
            str(report),
            posted,
            True,
            True,
            f"# Loop: example/repo loop1 · Goal: {GOAL}",
            "example/repo",
        ),
        ("mac-a", "started", str(goal), f"example/repo#loop1#{GOAL}\n", True, True, None, "example/repo"),
    ]
    dump = clean.execute("SELECT row_to_json(r)::text FROM ah.loop_receipt r").fetchall()
    assert body not in json.dumps(dump)
    # A second pass changes nothing.
    assert run_collector(clean, [repo]).counts["loop_receipt"].get("updated", 0) == 0


def test_collector_bounds_the_first_line_and_keeps_only_a_report_header(clean, tmp_path):
    repo = make_repo(tmp_path)
    huge = b"x" * (3 * 1024 * 1024)  # one enormous line: no header, nothing to store, still hashed whole
    (repo / "codex/report-synthetic-loop1.md").write_bytes(huge)
    (repo / "codex/report-synthetic-loop1.md.notified").write_text("request req-1\n")
    run_collector(clean, [repo])
    assert clean.execute("SELECT target_exists, target_sha256, target_line1 FROM ah.loop_receipt").fetchone() == (
        True,
        hashlib.sha256(huge).hexdigest(),
        None,
    )
    (repo / "codex/report-synthetic-loop1.md").write_text("some prose, not a header\nbody\n")
    run_collector(clean, [repo])
    assert clean.execute("SELECT target_line1 FROM ah.loop_receipt").fetchone() == (None,)


def test_unreadable_target_keeps_the_receipt_with_unknown_target_metadata(clean, tmp_path):
    repo = make_repo(tmp_path)
    report = repo / "codex/report-synthetic-loop1.md"
    report.write_text(header() + "\n")
    (repo / "codex/report-synthetic-loop1.md.notified").write_text("request req-1\n")
    report.chmod(0)
    try:
        collector = run_collector(clean, [repo])
    finally:
        report.chmod(0o600)
    assert collector.errors == []
    assert clean.execute(
        "SELECT content, target_exists, target_sha256, target_line1 FROM ah.loop_receipt"
    ).fetchone() == (
        "request req-1\n",
        True,
        None,
        None,
    )


def test_collector_without_origin_or_target(clean, tmp_path):
    repo = make_repo(tmp_path, origin=None)
    (repo / "codex/report-synthetic-loop1.md.notified").write_text("request req-1\n")
    run_collector(clean, [repo])
    assert clean.execute(
        "SELECT target_exists, target_sha256, target_line1, repo_origin FROM ah.loop_receipt"
    ).fetchone() == (False, None, None, None)


def test_changed_receipt_is_replaced_not_duplicated(clean, tmp_path):
    repo = make_repo(tmp_path)
    receipt = repo / "codex/report-synthetic-loop1.md.notified"
    receipt.write_text("request req-1\n")
    run_collector(clean, [repo])
    receipt.write_text("request req-2\n")
    run_collector(clean, [repo])
    assert clean.execute("SELECT content FROM ah.loop_receipt").fetchall() == [("request req-2\n",)]


def test_file_changing_during_collection_is_skipped_not_mixed(clean, tmp_path, monkeypatch):
    from agent_history import collect_receipts

    repo = make_repo(tmp_path)
    (repo / "codex/report-synthetic-loop1.md").write_text(f"# Loop: example/repo loop1 · Goal: {GOAL}\n")
    (repo / "codex/report-synthetic-loop1.md.notified").write_text("request req-1\n")
    (repo / "codex/report-synthetic-loop2.md.notified").write_text("request req-2\n")
    real = collect_receipts._stat

    def racing(path):  # the first receipt is rewritten between its read and its closing stat
        st = real(path)
        if str(path).endswith("loop1.md.notified"):
            return types.SimpleNamespace(
                st_dev=st.st_dev,
                st_ino=st.st_ino,
                st_size=st.st_size,
                st_mtime_ns=st.st_mtime_ns + 1,
                st_ctime_ns=st.st_ctime_ns,
            )
        return st

    monkeypatch.setattr(collect_receipts, "_stat", racing)
    collector = run_collector(clean, [repo])
    stored = {row[0] for row in clean.execute("SELECT path FROM ah.loop_receipt")}
    assert stored == {str(repo / "codex/report-synthetic-loop2.md")}
    assert collector.errors and collector.errors[0]["step"] == "loop_receipt"


def test_root_that_pings_before_renaming_the_report_has_no_receipt(world, tmp_path):
    repo = make_repo(tmp_path)
    world.launch(cwd=str(repo))
    (repo / "codex/report-synthetic-loop1.md.tmp").write_text("draft\n")
    run_collector(world.conn, [repo])
    world.refresh()
    assert world.conn.execute("SELECT status FROM ah.loops").fetchone() == ("running",)
    (repo / "codex/report-synthetic-loop1.md.tmp").rename(repo / "codex/report-synthetic-loop1.md")
    (repo / "codex/report-synthetic-loop1.md.notified").write_text("request req-1\n")
    run_collector(world.conn, [repo])
    world.refresh()
    assert world.conn.execute("SELECT status FROM ah.loops").fetchone() == ("finished",)


# -- grants and survival ----------------------------------------------------------------------


@pytest.fixture
def ingest_role(clean):
    """Re-apply the real migration with the dedicated collector role present."""
    with psycopg.connect(ADMIN_DSN, autocommit=True) as admin:
        admin.execute("CREATE ROLE ah_ingest LOGIN")
        admin.execute("GRANT USAGE ON SCHEMA ah TO ah_ingest")
        password = psycopg.conninfo.conninfo_to_dict(ADMIN_DSN).get("password")
        if password:
            admin.execute(sql.SQL("ALTER ROLE ah_ingest PASSWORD {}").format(sql.Literal(password)))
    clean.execute("DROP TABLE ah.loop_receipt")
    clean.execute("DELETE FROM ah.meta WHERE key = 'migration:025_loop_receipts.sql'")
    clean.commit()
    load.apply_schema(clean, force=True)
    clean.commit()
    yield psycopg.conninfo.make_conninfo(ADMIN_DSN, user="ah_ingest")
    with psycopg.connect(ADMIN_DSN, autocommit=True) as admin:
        admin.execute("DROP OWNED BY ah_ingest")
        admin.execute("DROP ROLE ah_ingest")


def test_collector_role_upserts_with_only_the_migration_grants(ingest_role, clean, tmp_path):
    repo = make_repo(tmp_path)
    receipt = repo / "codex/report-synthetic-loop1.md.notified"
    receipt.write_text("request req-1\n")
    from agent_history import collect_receipts

    writer = collect_git.connect(ingest_role)
    try:
        for content in ("request req-1\n", "request req-2\n"):
            receipt.write_text(content)
            collector = collect_git.Collector(writer, False, time.monotonic() + 60)
            collect_receipts.collect(collector, [repo], "mac-a")
            assert collector.errors == []
    finally:
        writer.close()
    assert clean.execute("SELECT content FROM ah.loop_receipt").fetchall() == [("request req-2\n",)]
    privileges = clean.execute(
        "SELECT has_table_privilege('ah_ingest', 'ah.loop_receipt', p) FROM unnest(ARRAY['SELECT','INSERT','UPDATE',"
        "'DELETE','TRUNCATE']) p"
    ).fetchall()
    assert privileges == [(True,), (True,), (True,), (False,), (False,)]
    assert clean.execute("SELECT has_table_privilege('ah_reader', 'ah.loop_receipt', 'SELECT')").fetchone() == (True,)
    assert clean.execute("SELECT has_table_privilege('ah_reader', 'ah.loop_receipt', 'INSERT')").fetchone() == (False,)

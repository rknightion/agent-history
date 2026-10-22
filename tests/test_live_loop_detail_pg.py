"""ah.live_loop_detail against real parser, Writer and refresh rows plus collector metadata.

Lanes come from real Claude Code transcripts (the same dispatch and return evidence that
ah.live_loop_tasks reads); charter first-wave IDs and commits are collector rows written directly,
as the collector would. The document must carry exactly the frozen keys and nothing else.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from datetime import datetime, timedelta, timezone

import pytest

psycopg = pytest.importorskip("psycopg")

from psycopg import errors  # noqa: E402

import test_live_loop_tasks_pg as tasks_pg  # noqa: E402
from agent_history import load  # noqa: E402
from psycopg.conninfo import make_conninfo  # noqa: E402
from test_live_loop_tasks_pg import CWD, OTHER_REPO, REPO, Claude, header, identify, iso, login, refresh, write  # noqa: E402

# Shared fixtures: the scratch catalogue, the admin connection and the shipped narrow role.
db = tasks_pg.db
admin_dsn = tasks_pg.admin_dsn
role_dsn = tasks_pg.role_dsn

DSN = os.environ.get("AGENT_HISTORY_TEST_DSN", "")
pytestmark = pytest.mark.skipif(not DSN or "agent_history_test" not in DSN, reason="scratch database not set")
SLUG = "github.com/" + REPO
STAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$")
TASK_ID = re.compile(r"^[A-Z][A-Z0-9]{0,15}-\d{1,6}(\.\d{1,4}){0,3}$")
TOP_KEYS = {"schema", "loops"}
LOOP_KEYS = {
    "loop",
    "status",
    "launched_at",
    "waiting_until",
    "observed_at",
    "last_activity_at",
    "lanes",
    "tasks_landed",
    "commits",
    "tasks",
    "events",
    "landings",
}
LANE_KEYS = {"total", "running", "returned", "blocked"}
TASK_KEYS = {"id", "title", "planned", "state"}
EVENT_KEYS = {"at", "lane", "role", "task_id", "kind", "status"}
LANDING_KEYS = {"sha", "subject", "at", "task_ids"}


@pytest.fixture
def conn(db):
    db.execute("TRUNCATE " + ", ".join(f"ah.{t}" for t in load.DATA_TABLES) + " RESTART IDENTITY CASCADE")
    # Collector tables survive the derived-data truncate; clear the ones these tests write.
    db.execute("DELETE FROM ah.git_commit")
    db.execute("DELETE FROM ah.loop_run_metadata")
    db.execute("DELETE FROM ah.meta WHERE key = 'rebuild_in_progress'")
    db.commit()
    yield db
    db.rollback()
    db.execute("DELETE FROM ah.meta WHERE key = 'rebuild_in_progress'")
    db.commit()


class Lanes(Claude):
    """A Claude root whose lanes choose their role and hand back any lane-return status."""

    def dispatch(self, call: str, brief: str, role: str = "agent-workflows:worker") -> None:
        agent = "a" + uuid.uuid4().hex[:16]
        self.agents[call] = agent
        tool = {"description": "lane", "prompt": brief, "subagent_type": role, "run_in_background": True}
        self.assistant([{"type": "tool_use", "id": call, "name": "Agent", "input": tool}])
        self.user(
            [{"type": "tool_result", "tool_use_id": call, "content": "Async agent launched successfully."}],
            toolUseResult={"isAsync": True, "status": "async_launched", "agentId": agent},
        )

    def hand_back(self, call: str, brief: str, lane: str, status: str, task: str | None = None) -> None:
        agent, at = self.agents[call], self.at + timedelta(seconds=1)
        self.at += timedelta(seconds=3)
        base = {"sessionId": self.root, "agentId": agent, "cwd": CWD, "isSidechain": True}
        handed = {"v": 3, "lane": lane, "status": status, "tail": "private tail", "worktree": "/tmp/wt"}
        if task is not None:
            handed["task"] = task
        block = "Done.\n```lane-return\n" + json.dumps(handed) + "\n```"
        records = [
            {
                **base,
                "type": "user",
                "uuid": str(uuid.uuid4()),
                "timestamp": iso(at),
                "message": {"role": "user", "content": brief},
            },
            {
                **base,
                "type": "assistant",
                "uuid": str(uuid.uuid4()),
                "timestamp": iso(at + timedelta(seconds=1)),
                "requestId": "req_lane_" + agent,
                "message": {
                    "id": "msg_lane_" + agent,
                    "type": "message",
                    "role": "assistant",
                    "model": "synthetic-model",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "hand_" + agent,
                            "name": "SubagentHandback",
                            "input": {"message": block},
                        }
                    ],
                    "stop_reason": "tool_use",
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                },
            },
        ]
        write(self.source / "projects" / "synthetic" / self.root / "subagents" / f"agent-{agent}.jsonl", records)


def lanes_loop(source, n: int, start: datetime) -> Lanes:
    root = Lanes(source, str(uuid.uuid4()), start)
    root.user(f"You are the root. Read codex/charter-synthetic-loop{n}.md. Write codex/closeout-synthetic-loop{n}.md.")
    return root


def charter(
    conn,
    n: int,
    ids: list[str] | None,
    machine: str = "machine-a",
    mtime: datetime | None = None,
    origin: str | None = REPO,
) -> str:
    """Point loop<n>'s run at a charter path and record a collector row for it."""
    path = f"{CWD}/codex/charter-synthetic-loop{n}.md"
    conn.execute(
        "UPDATE ah.loop_run SET goal_path = %s WHERE launch_uid IN (SELECT launch_uid FROM ah.loops WHERE loop = %s)",
        (path, f"loop{n}"),
    )
    conn.execute(
        "INSERT INTO ah.loop_run_metadata (machine, path, kind, sha256, file_mtime, repo_origin, task_ids) "
        "VALUES (%s, %s, 'charter', %s, %s, %s, %s)",
        (machine, path, "0" * 64, mtime or datetime.now(timezone.utc), origin, ids),
    )
    conn.commit()
    return path


def commit(
    conn,
    sha: str,
    at: datetime,
    task_ids: list[str] | None,
    *,
    slug: str = SLUG,
    on_default: bool = True,
    subject: str = "feat: synthetic change",
    context: str = "personal",
) -> None:
    conn.execute(
        "INSERT INTO ah.git_commit (repo_slug, sha, context, committed_at, subject, on_default, task_ids) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (slug, sha, context, at, subject, on_default, task_ids),
    )
    conn.commit()


def launch_of(conn, n: int) -> datetime:
    at = conn.execute(
        "SELECT launch_ts FROM ah.loops WHERE loop = %s AND status IN ('running','waiting','paused')", (f"loop{n}",)
    ).fetchone()[0]
    conn.commit()
    return at


def detail(conn, repo: str = REPO) -> dict:
    document = conn.execute("SELECT ah.live_loop_detail(%s)", (repo,)).fetchone()[0]
    conn.commit()
    return document


def assert_shape(document: dict) -> None:
    """Every key the seam names, and no other key anywhere."""
    assert set(document) == TOP_KEYS and document["schema"] == 1
    assert len(document["loops"]) <= 20
    for loop in document["loops"]:
        assert set(loop) == LOOP_KEYS
        assert isinstance(loop["loop"], int) and loop["status"] in ("running", "waiting", "paused")
        for key in ("launched_at", "observed_at"):
            assert STAMP.match(loop[key]), key
        for key in ("waiting_until", "last_activity_at"):
            assert loop[key] is None or STAMP.match(loop[key]), key
        assert set(loop["lanes"]) == LANE_KEYS
        assert all(isinstance(v, int) and v >= 0 for v in loop["lanes"].values())
        assert loop["lanes"]["total"] == loop["lanes"]["running"] + loop["lanes"]["returned"]
        assert loop["lanes"]["blocked"] <= loop["lanes"]["returned"]
        assert len(loop["tasks"]) <= 200 and len(loop["events"]) <= 20 and len(loop["landings"]) <= 10
        for task in loop["tasks"]:
            assert set(task) == TASK_KEYS and TASK_ID.match(task["id"])
            assert task["state"] in ("landed", "running", "blocked", "returned", "planned")
            assert isinstance(task["planned"], bool) and (task["title"] is None or isinstance(task["title"], str))
        for event in loop["events"]:
            assert set(event) == EVENT_KEYS and STAMP.match(event["at"])
            assert event["kind"] in ("dispatched", "returned")
            assert event["task_id"] is None or TASK_ID.match(event["task_id"])
            if event["kind"] == "dispatched":
                assert event["status"] is None
        assert [e["at"] for e in loop["events"]] == sorted((e["at"] for e in loop["events"]), reverse=True)
        for landing in loop["landings"]:
            assert set(landing) == LANDING_KEYS and re.fullmatch(r"[0-9a-f]{7}", landing["sha"])
            assert STAMP.match(landing["at"]) and landing["task_ids"] == sorted(landing["task_ids"])
            assert len(landing["subject"]) <= 200 and "\n" not in landing["subject"]
        assert [x["at"] for x in loop["landings"]] == sorted((x["at"] for x in loop["landings"]), reverse=True)


def states(loop: dict) -> dict[str, tuple]:
    return {t["id"]: (t["planned"], t["state"], t["title"]) for t in loop["tasks"]}


# --- document shape and task state -----------------------------------------------------------------


def test_document_keys_states_events_and_landings(conn, tmp_path):
    start = datetime.now(timezone.utc) - timedelta(minutes=30)
    loop = lanes_loop(tmp_path, 7, start)
    # APP-0002: planned and dispatched, then landed by a commit, which outranks its live lane.
    loop.dispatch("toolu_l2", header("L02", "APP-0002", "Landed task"))
    # APP-0003: an earlier blocked worker return, then a live lane: running.
    b3 = header("L03", "APP-0003", "Running again")
    loop.dispatch("toolu_l3a", b3)
    loop.hand_back("toolu_l3a", b3, "L03", "blocked", "APP-0003")
    loop.dispatch("toolu_l3b", header("L03b", "APP-0003", "Running again"))
    # APP-0004 blocked, APP-0005 partial (both count as blocked lanes), APP-0006 complete.
    for call, lane, task, status in (
        ("toolu_l4", "L04", "APP-0004", "blocked"),
        ("toolu_l5", "L05", "APP-0005", "partial"),
        ("toolu_l6", "L06", "APP-0006", "complete"),
    ):
        brief = header(lane, task, f"Task {task}")
        loop.dispatch(call, brief, "agent-workflows:senior" if task == "APP-0006" else "agent-workflows:worker")
        loop.hand_back(call, brief, lane, status, task)
    # APP-0007: complete, then a later blocked return; the latest worker return decides.
    b7 = header("L07", "APP-0007", "Latest return wins")
    loop.dispatch("toolu_l7a", b7)
    loop.hand_back("toolu_l7a", b7, "L07", "complete", "APP-0007")
    loop.dispatch("toolu_l7b", b7)
    loop.hand_back("toolu_l7b", b7, "L07", "blocked", "APP-0007")
    # APP-0008: only a reviewer returned for it, blocked: not a worker or senior return.
    b8 = header("R08", "APP-0008", "Reviewed only")
    loop.dispatch("toolu_r8", b8, "agent-workflows:reviewer")
    loop.hand_back("toolu_r8", b8, "R08", "blocked", "APP-0008")
    # The lane-return's own task outranks the brief header for a returned lane.
    b9 = header("L09", "APP-0009", "Header task")
    loop.dispatch("toolu_l9", b9)
    loop.hand_back("toolu_l9", b9, "L09", "complete", "APP-0010")
    # APP-0011: a worker return that is neither blocked nor complete still counts as returned.
    b11 = header("L11", "APP-0011", "Failed return")
    loop.dispatch("toolu_l11", b11)
    loop.hand_back("toolu_l11", b11, "L11", "failed", "APP-0011")
    # A header-less live lane counts as a lane but names no task.
    for scout in ("toolu_s1", "toolu_s2", "toolu_s3"):
        loop.dispatch(scout, "Scout the layout and report.", "agent-workflows:scout")
    loop.assistant([{"type": "text", "text": "Waiting on lanes."}], stop="end_turn")
    loop.save()
    refresh(conn, {"claude-test": tmp_path})
    identify(conn, 7, REPO, "waiting")
    conn.execute(
        "UPDATE ah.loops SET waiting_until = '2030-01-02T03:04:05Z', tasks_landed = 1, commits = 2 WHERE loop = 'loop7'"
    )
    conn.commit()
    charter(conn, 7, ["APP-0001", "APP-0002", "APP-0004"])
    launched = launch_of(conn, 7)
    commit(conn, "a" * 40, launched + timedelta(minutes=10), ["APP-0002", "ZZZ-1"], subject="feat: land\nbody line")
    commit(conn, "b" * 40, launched + timedelta(minutes=11), ["APP-0099"])  # no loop task: not a landing
    commit(conn, "c" * 40, launched + timedelta(minutes=12), ["APP-0006"], on_default=False)
    commit(conn, "d" * 40, launched - timedelta(minutes=1), ["APP-0004"])  # before launch
    commit(conn, "e" * 40, launched + timedelta(minutes=13), ["APP-0005"], slug="github.com/" + OTHER_REPO)
    commit(conn, "f" * 40, launched + timedelta(minutes=14), ["APP-0003"], subject="x" * 300)

    document = detail(conn)
    assert_shape(document)
    [only] = document["loops"]
    assert (only["loop"], only["status"], only["waiting_until"]) == (7, "waiting", "2030-01-02T03:04:05.000000Z")
    assert (only["tasks_landed"], only["commits"]) == (1, 2)
    # 14 lanes: l2, l3b and three scouts are live; nine have returned, l3a, l4, l5, l7b and r8 blocked.
    assert only["lanes"] == {"total": 14, "running": 5, "returned": 9, "blocked": 5}
    assert [t["id"] for t in only["tasks"]][:3] == ["APP-0001", "APP-0002", "APP-0004"]
    assert states(only) == {
        "APP-0001": (True, "planned", None),
        "APP-0002": (True, "landed", "Landed task"),
        "APP-0004": (True, "blocked", "Task APP-0004"),
        "APP-0003": (False, "landed", "Running again"),
        "APP-0005": (False, "blocked", "Task APP-0005"),
        "APP-0006": (False, "returned", "Task APP-0006"),
        "APP-0007": (False, "blocked", "Latest return wins"),
        "APP-0008": (False, "planned", "Reviewed only"),
        "APP-0009": (False, "planned", "Header task"),
        "APP-0010": (False, "returned", None),
        "APP-0011": (False, "returned", "Failed return"),
    }
    assert [t["id"] for t in only["tasks"]][3:] == [
        "APP-0003",
        "APP-0005",
        "APP-0006",
        "APP-0007",
        "APP-0008",
        "APP-0009",
        "APP-0010",
        "APP-0011",
    ]
    assert [(x["sha"], x["task_ids"]) for x in only["landings"]] == [
        ("fffffff", ["APP-0003"]),
        ("aaaaaaa", ["APP-0002", "ZZZ-1"]),
    ]
    assert only["landings"][1]["subject"] == "feat: land"
    assert only["landings"][0]["subject"] == "x" * 200
    # 13 dispatches and 8 returns: the oldest event (l2's dispatch) falls off the newest 20.
    assert len(only["events"]) == 20
    assert ("dispatched", "L02") not in {(e["kind"], e["lane"]) for e in only["events"]}
    newest = only["events"][0]
    # The header-less scout has no lane transcript yet, so nothing names its lane or role.
    assert (newest["kind"], newest["lane"], newest["role"], newest["task_id"], newest["status"]) == (
        "dispatched",
        None,
        None,
        None,
        None,
    )
    returned = [e for e in only["events"] if e["kind"] == "returned"]
    assert {(e["lane"], e["role"], e["task_id"], e["status"]) for e in returned} >= {
        ("L09", "worker", "APP-0010", "complete"),
        ("L06", "senior", "APP-0006", "complete"),
        ("R08", "reviewer", "APP-0008", "blocked"),
    }
    text = json.dumps(document)
    for leaked in ("private tail", "/tmp/wt", "toolu_", "agent-workflows", CWD, "body line"):
        assert leaked not in text


def test_running_state_and_no_charter(conn, tmp_path):
    loop = lanes_loop(tmp_path, 7, datetime.now(timezone.utc) - timedelta(minutes=30))
    loop.dispatch("toolu_w1", header("w1", "APP-0001", "Live"))
    loop.save()
    refresh(conn, {"claude-test": tmp_path})
    identify(conn, 7, REPO)
    document = detail(conn)
    assert_shape(document)
    [only] = document["loops"]
    assert states(only) == {"APP-0001": (False, "running", "Live")}
    assert only["lanes"] == {"total": 1, "running": 1, "returned": 0, "blocked": 0}
    assert [(e["kind"], e["lane"], e["task_id"], e["status"]) for e in only["events"]] == [
        ("dispatched", "w1", "APP-0001", None)
    ]
    assert only["landings"] == []
    assert detail(conn, "example-owner/absent-repo") == {"schema": 1, "loops": []}


def test_charter_row_prefers_the_newest_collected_ids(conn, tmp_path):
    loop = lanes_loop(tmp_path, 7, datetime.now(timezone.utc) - timedelta(minutes=30))
    loop.save()
    refresh(conn, {"claude-test": tmp_path})
    identify(conn, 7, REPO)
    now = datetime.now(timezone.utc)
    # The newer copy came from a collector that does not record task IDs yet: the older ids win.
    charter(conn, 7, ["APP-0002", "APP-0001"], machine="machine-a", mtime=now - timedelta(minutes=5))
    charter(conn, 7, None, machine="machine-b", mtime=now)
    [only] = detail(conn)["loops"]
    assert [(t["id"], t["planned"], t["state"]) for t in only["tasks"]] == [
        ("APP-0002", True, "planned"),
        ("APP-0001", True, "planned"),
    ]
    # Among copies that carry ids, the newest file wins, even when it names none.
    charter(conn, 7, ["APP-0003"], machine="machine-c", mtime=now - timedelta(minutes=1))
    assert [t["id"] for t in detail(conn)["loops"][0]["tasks"]] == ["APP-0003"]
    charter(conn, 7, [], machine="machine-d", mtime=now - timedelta(seconds=30))
    assert detail(conn)["loops"][0]["tasks"] == []
    # A newer copy of the same path from another repository, or with no origin, is never used;
    # a github.com/ prefixed origin for this repository is.
    charter(conn, 7, ["ZZZ-0001"], machine="machine-e", mtime=now - timedelta(seconds=20), origin=OTHER_REPO)
    charter(conn, 7, ["ZZZ-0002"], machine="machine-f", mtime=now - timedelta(seconds=15), origin=None)
    assert detail(conn)["loops"][0]["tasks"] == []
    charter(conn, 7, ["APP-0004"], machine="machine-g", mtime=now - timedelta(seconds=10), origin="github.com/" + REPO)
    assert [t["id"] for t in detail(conn)["loops"][0]["tasks"]] == ["APP-0004"]


def test_overlapping_loops_attribute_landings_by_task_and_window(conn, tmp_path):
    start = datetime.now(timezone.utc) - timedelta(minutes=60)
    first = lanes_loop(tmp_path, 7, start)
    first.dispatch("toolu_a1", header("a1", "APP-0101", "First loop task"))
    first.save()
    second = lanes_loop(tmp_path, 8, start + timedelta(minutes=5))
    second.dispatch("toolu_b1", header("b1", "APP-0201", "Second loop task"))
    second.save()
    # loop 5 then loop 6 in one root: loop 5's window ends at loop 6's launch.
    same_root = lanes_loop(tmp_path, 5, start + timedelta(minutes=1))
    same_root.dispatch("toolu_c1", header("c1", "APP-0301", "Earlier run on the root"))
    same_root.user("You are the root. Read codex/charter-synthetic-loop6.md. Write codex/closeout-synthetic-loop6.md.")
    same_root.dispatch("toolu_c2", header("c2", "APP-0302", "Later run on the root"))
    same_root.save()
    refresh(conn, {"claude-test": tmp_path})
    for n in (7, 8, 6, 5):
        identify(conn, n, REPO)  # loop 5 is reopened with no recorded end
    t7, t8, t6 = launch_of(conn, 7), launch_of(conn, 8), launch_of(conn, 6)
    assert t7 < t8 and launch_of(conn, 5) < t6
    commit(conn, "1" * 40, t8 + timedelta(minutes=1), ["APP-0101"])  # inside both windows, loop 7's task
    commit(conn, "2" * 40, t8 + timedelta(minutes=2), ["APP-0201", "APP-0999"])  # loop 8's task
    commit(conn, "3" * 40, t8 + timedelta(minutes=3), ["APP-0101", "APP-0201"], slug=REPO)  # unprefixed slug
    commit(conn, "4" * 40, t6 + timedelta(minutes=1), ["APP-0301", "APP-0302"])  # after loop 5's window
    commit(conn, "5" * 40, t6 - timedelta(seconds=1), ["APP-0301"])  # inside loop 5's window
    commit(
        conn, "1" * 40, t8 + timedelta(minutes=1), ["APP-0101"], slug=REPO
    )  # the same commit under the unprefixed slug
    document = detail(conn)
    assert_shape(document)
    loops = {x["loop"]: x for x in document["loops"]}
    assert [x["loop"] for x in document["loops"]] == sorted(loops, key=lambda n: loops[n]["launched_at"])
    assert [(x["sha"], x["task_ids"]) for x in loops[7]["landings"]] == [
        ("3333333", ["APP-0101", "APP-0201"]),
        ("1111111", ["APP-0101"]),
    ]
    assert [(x["sha"], x["task_ids"]) for x in loops[8]["landings"]] == [
        ("3333333", ["APP-0101", "APP-0201"]),
        ("2222222", ["APP-0201", "APP-0999"]),
    ]
    assert [x["sha"] for x in loops[5]["landings"]] == ["5555555"]
    assert [x["sha"] for x in loops[6]["landings"]] == ["4444444"]
    assert states(loops[7])["APP-0101"][1] == "landed"
    assert states(loops[8])["APP-0201"][1] == "landed"
    assert "APP-0201" not in states(loops[7]) and "APP-0101" not in states(loops[8])
    assert states(loops[5]) == {"APP-0301": (False, "landed", "Earlier run on the root")}
    assert states(loops[6]) == {"APP-0302": (False, "landed", "Later run on the root")}


# --- guards and privileges -------------------------------------------------------------------------


@pytest.mark.parametrize("repo", [None, "", "owner", "owner/repo/extra", "owner/re po", "owner/repo'--"])
def test_repo_must_be_an_owner_repo_slug(conn, repo):
    with pytest.raises(errors.InvalidParameterValue) as raised:
        conn.execute("SELECT ah.live_loop_detail(%s)", (repo,))
    assert raised.value.sqlstate == "22023"
    conn.rollback()


def test_refuses_during_a_rebuild(conn):
    conn.execute("INSERT INTO ah.meta VALUES ('rebuild_in_progress', 'synthetic')")
    conn.commit()
    with pytest.raises(errors.ObjectNotInPrerequisiteState) as raised:
        conn.execute("SELECT ah.live_loop_detail(%s)", (REPO,))
    assert raised.value.sqlstate == "55000"
    conn.rollback()


def test_function_is_definer_pinned_and_not_public(conn):
    row = conn.execute(
        "SELECT p.prosecdef, p.provolatile, p.proconfig, "
        "has_function_privilege('public', p.oid, 'EXECUTE') OR EXISTS ("
        "  SELECT 1 FROM aclexplode(p.proacl) a WHERE a.grantee = 0 AND a.privilege_type = 'EXECUTE') "
        "FROM pg_proc p WHERE p.oid = 'ah.live_loop_detail(text)'::regprocedure"
    ).fetchone()
    conn.commit()
    assert row == (True, "s", ["search_path=pg_catalog, pg_temp"], False)


def test_scoped_role_reads_its_repo_only(conn, tmp_path, role_dsn, admin_dsn):
    for n in (7, 9):
        loop = lanes_loop(tmp_path, n, datetime.now(timezone.utc) - timedelta(minutes=30))
        loop.dispatch(f"toolu_{n}", header("w1", f"APP-0{n}00", "Synthetic task"))
        loop.save()
    refresh(conn, {"claude-test": tmp_path})
    identify(conn, 7, REPO)
    identify(conn, 9, OTHER_REPO)
    expected = detail(conn)
    with psycopg.connect(role_dsn) as narrow:
        assert narrow.execute("SELECT ah.live_loop_detail(%s)", (REPO.upper(),)).fetchone()[0] == expected
        narrow.rollback()
        with pytest.raises(errors.InsufficientPrivilege) as refused:
            narrow.execute("SELECT ah.live_loop_detail(%s)", (OTHER_REPO,))
        assert refused.value.sqlstate == "42501"
    with psycopg.connect(admin_dsn, autocommit=True) as admin:
        admin.execute("CREATE ROLE synthetic_member")
        admin.execute("GRANT ah_loop_tasks TO synthetic_member")
    login("synthetic_member")
    with psycopg.connect(make_conninfo(admin_dsn, user="synthetic_member")) as member:
        member.execute("SET ROLE ah_loop_tasks")
        with pytest.raises(errors.InsufficientPrivilege):
            member.execute("SELECT ah.live_loop_detail(%s)", (REPO,))
    assert conn.execute(
        "SELECT has_function_privilege('synthetic_bystander', 'ah.live_loop_detail(text)', 'EXECUTE')"
    ).fetchone() == (False,)
    conn.commit()
    reader = os.environ.get("AGENT_HISTORY_TEST_READER_DSN", "")
    if "agent_history_test" in reader:
        with psycopg.connect(reader) as plain:
            assert [
                x["loop"] for x in plain.execute("SELECT ah.live_loop_detail(%s)", (OTHER_REPO,)).fetchone()[0]["loops"]
            ] == [9]


def test_migration_grants_an_existing_role_and_survives_reapplication(conn, role_dsn):
    # As on a catalogue whose narrow role was provisioned before the indexer applied 044.
    conn.execute("DROP FUNCTION ah.live_loop_detail(text)")
    conn.execute("DELETE FROM ah.meta WHERE key = 'migration:044_live_loop_detail.sql'")
    conn.commit()
    with psycopg.connect(role_dsn) as narrow, pytest.raises(errors.UndefinedFunction):
        narrow.execute("SELECT ah.live_loop_detail(%s)", (REPO,))
    load.apply_schema(conn, force=True)  # replays 044 and re-applies the analytics files
    conn.commit()
    load.apply_schema(conn, force=True)
    conn.commit()
    assert conn.execute(
        "SELECT has_function_privilege('synthetic_bystander', 'ah.live_loop_detail(text)', 'EXECUTE')"
    ).fetchone() == (False,)
    conn.commit()
    with psycopg.connect(role_dsn) as narrow:
        assert narrow.execute("SELECT ah.live_loop_detail(%s)", (REPO,)).fetchone()[0] == {"schema": 1, "loops": []}


def test_collector_upsert_keeps_null_and_empty_task_ids_apart(conn, tmp_path):
    from agent_history import collect_git, collect_loop_metadata

    rows = [
        {
            "machine": "machine-a",
            "path": f"{CWD}/codex/{name}",
            "kind": kind,
            "sha256": "0" * 64,
            "line1": None,
            "file_mtime": datetime.now(timezone.utc),
            "repo_origin": REPO,
            "landed_count": None,
            "parked_count": None,
            "task_ids": ids,
        }
        for name, kind, ids in (
            ("charter-a-loop1.md", "charter", ["APP-0002", "APP-0001"]),
            ("charter-b-loop2.md", "charter", []),
            ("closeout-a-loop1.md", "closeout", None),
        )
    ]
    for _ in range(2):  # an unchanged second pass updates nothing
        with conn.cursor() as cur:
            collect_git.upsert(cur, "loop_run_metadata", collect_loop_metadata.COLS, collect_loop_metadata.KEY, rows)
        conn.commit()
    assert conn.execute("SELECT kind, task_ids FROM ah.loop_run_metadata ORDER BY path").fetchall() == [
        ("charter", ["APP-0002", "APP-0001"]),
        ("charter", []),
        ("closeout", None),
    ]
    conn.commit()

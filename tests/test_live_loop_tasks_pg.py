"""ah.live_loop_tasks and its narrow role against real parser, Writer and refresh rows.

Each harness's root dispatches lanes the way it really records them; the function must link every
root brief to its dispatch and to the return the catalogue already holds, read the task only from
the brief's header line, and expose nothing else. The role test runs the shipped role script.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

psycopg = pytest.importorskip("psycopg")

from psycopg import errors, sql  # noqa: E402
from psycopg.conninfo import conninfo_to_dict, make_conninfo  # noqa: E402

from agent_history import load  # noqa: E402
from test_live_loop_tasks import CASES  # noqa: E402

DSN = os.environ.get("AGENT_HISTORY_TEST_DSN", "")
ADMIN_DSN = os.environ.get("AGENT_HISTORY_TEST_ADMIN_DSN", "")
pytestmark = pytest.mark.skipif(not DSN or "agent_history_test" not in DSN, reason="scratch database not set")
ROLE_SCRIPT = Path(load.SQL_DIR) / "live_loop_tasks_role.sql"
MIGRATION = "043_live_loop_tasks.sql"
CWD = "/tmp/synthetic-project"
REPO = "example-owner/example-repo"
OTHER_REPO = "example-owner/other-repo"


def prompt(n: int) -> str:
    return f"You are the root. Read codex/charter-synthetic-loop{n}.md. Write codex/closeout-synthetic-loop{n}.md."


def header(lane: str, task: str, title: str, tier: str = "routine") -> str:
    return f"Lane: {lane} · Task: {task} ({title}) · Tier: {tier}\n\nSynthetic lane body."


@pytest.fixture(scope="module")
def db():
    connection = load.connect(DSN)
    load.apply_schema(connection, force=True)
    connection.commit()
    yield connection
    connection.close()


@pytest.fixture
def conn(db):
    db.execute("TRUNCATE " + ", ".join(f"ah.{t}" for t in load.DATA_TABLES) + " RESTART IDENTITY CASCADE")
    db.execute("DELETE FROM ah.meta WHERE key = 'rebuild_in_progress'")
    db.commit()
    yield db
    db.rollback()
    db.execute("DELETE FROM ah.meta WHERE key = 'rebuild_in_progress'")
    db.commit()


def iso(at: datetime) -> str:
    return at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def write(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records))


def refresh(conn, sources: dict[str, Path]) -> None:
    assert load.refresh(conn, sources=sources, textfile=None, log=lambda *_: None).errors == 0
    conn.commit()


def identify(conn, n: int, repo: str, status: str = "running") -> None:
    """Give loop<n> the identity and lifecycle the projection would hold for it after the last refresh."""
    end = "now()" if status in ("finished", "stale") else "NULL"
    conn.execute(
        f"UPDATE ah.loops l SET repo = %s, loop = %s, status = %s, end_ts = {end} "
        "FROM ah.loop_run r WHERE r.launch_uid = l.launch_uid AND r.loop_number = %s",
        (repo, f"loop{n}", status, n),
    )
    conn.commit()


def live(conn, repo: str = REPO) -> dict:
    document = conn.execute("SELECT ah.live_loop_tasks(%s)", (repo,)).fetchone()[0]
    conn.commit()
    return document


def simple(document: dict) -> list[tuple]:
    return [(x["loop"], x["status"], x["active_lanes"], x["tasks"]) for x in document["loops"]]


# --- Claude Code -----------------------------------------------------------------------------------


class Claude:
    def __init__(self, source: Path, root: str, at: datetime) -> None:
        self.source, self.root, self.at, self.records = source, root, at, []
        self.agents: dict[str, str] = {}

    def tick(self) -> str:
        self.at += timedelta(seconds=5)
        return iso(self.at)

    def user(self, content, **extra) -> None:
        self.records.append(
            {
                "type": "user",
                "uuid": str(uuid.uuid4()),
                "sessionId": self.root,
                "timestamp": self.tick(),
                "cwd": CWD,
                "isSidechain": False,
                "message": {"role": "user", "content": content},
                **extra,
            }
        )

    def assistant(self, content, stop="tool_use") -> None:
        uid = str(uuid.uuid4())
        self.records.append(
            {
                "type": "assistant",
                "uuid": uid,
                "sessionId": self.root,
                "timestamp": self.tick(),
                "cwd": CWD,
                "isSidechain": False,
                "requestId": "req_" + uid,
                "message": {
                    "id": "msg_" + uid,
                    "type": "message",
                    "role": "assistant",
                    "model": "synthetic-model",
                    "content": content,
                    "stop_reason": stop,
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                },
            }
        )

    def dispatch(self, call: str, brief: str) -> None:
        agent = "a" + uuid.uuid4().hex[:16]
        self.agents[call] = agent
        tool = {"description": "lane", "prompt": brief, "subagent_type": "worker", "run_in_background": True}
        self.assistant([{"type": "tool_use", "id": call, "name": "Agent", "input": tool}])
        self.user(
            [{"type": "tool_result", "tool_use_id": call, "content": "Async agent launched successfully."}],
            toolUseResult={"isAsync": True, "status": "async_launched", "agentId": agent},
        )

    def notify(self, call: str) -> None:
        self.user(
            f"<task-notification>\n<task-id>{self.agents[call]}</task-id>\n<tool-use-id>{call}</tool-use-id>\n"
            '<status>completed</status>\n<summary>Agent "lane" completed</summary>\n</task-notification>',
            origin={"kind": "task-notification"},
            promptSource="system",
        )

    def lane_returns(self, call: str, brief: str, lane: str) -> None:
        """The lane's own transcript hands back a lane-return; the root has not been notified."""
        agent, at = self.agents[call], self.at + timedelta(seconds=1)
        base = {"sessionId": self.root, "agentId": agent, "cwd": CWD, "isSidechain": True}
        block = "Done.\n```lane-return\n" + json.dumps({"v": 3, "lane": lane, "status": "complete"}) + "\n```"
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

    def save(self) -> None:
        write(self.source / "projects" / "synthetic" / f"{self.root}.jsonl", self.records)


def claude_loop(source: Path, n: int, start: datetime) -> Claude:
    root = Claude(source, str(uuid.uuid4()), start)
    root.user(prompt(n))
    return root


def test_claude_live_returned_unheadered_and_excluded_loops(conn, tmp_path):
    start = datetime.now(timezone.utc) - timedelta(minutes=30)
    loop7 = claude_loop(tmp_path, 7, start)
    loop7.dispatch("toolu_w1", header("w1", "ABC-0123", "Fix the widget"))
    loop7.dispatch("toolu_w2", header("w2", "ABC-0124", "Returned via notification"))
    loop7.dispatch("toolu_w3", header("w3", "ABC-0125", "Returned in its own transcript"))
    loop7.dispatch(
        "toolu_s1", "Scout the test layout and report.\nLane: s1 · Task: ABC-0777 (Not a header) · Tier: routine"
    )
    loop7.dispatch("toolu_r1", header("r1", "ABC-0123", "Fix the widget", "guarded"))
    loop7.dispatch("toolu_w4", header("w4", "ABC-0126", "Second live task"))
    loop7.notify("toolu_w2")
    loop7.lane_returns("toolu_w3", header("w3", "ABC-0125", "Returned in its own transcript"), "w3")
    loop7.assistant([{"type": "text", "text": "Waiting on lanes."}], stop="end_turn")
    loop7.save()
    finished = claude_loop(tmp_path, 6, start)
    finished.dispatch("toolu_f1", header("f1", "ABC-0999", "Belongs to a finished loop"))
    finished.save()
    stale = claude_loop(tmp_path, 5, start)
    stale.dispatch("toolu_t1", header("t1", "ABC-0998", "Belongs to a stale loop"))
    stale.save()
    other = claude_loop(tmp_path, 9, start)
    other.dispatch("toolu_o1", header("o1", "ABC-0555", "Another repository"))
    other.save()
    refresh(conn, {"claude-test": tmp_path})
    # The real links the function relies on, rather than rows written for it.
    assert conn.execute(
        "SELECT sp.spawn_uid, sp.completion_status, ln.return_status FROM ah.subagent_spawn sp "
        "LEFT JOIN ah.lane ln ON ln.session_id = sp.child_session_id "
        "WHERE sp.spawn_uid IN ('toolu_w2', 'toolu_w3') ORDER BY 1"
    ).fetchall() == [("toolu_w2", "completed", None), ("toolu_w3", None, "complete")]
    identify(conn, 7, REPO, "waiting")
    conn.execute("UPDATE ah.loops SET waiting_until = '2030-01-02T03:04:05Z' WHERE loop = 'loop7'")
    conn.commit()
    identify(conn, 6, REPO, "finished")
    identify(conn, 5, REPO, "stale")
    identify(conn, 9, OTHER_REPO)

    document = live(conn)
    launched, observed = conn.execute("SELECT launch_ts, observed_at FROM ah.loops WHERE loop = 'loop7'").fetchone()
    conn.commit()
    assert document == {
        "loops": [
            {
                "loop": 7,
                "launched_at": launched.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                "status": "waiting",
                "waiting_until": "2030-01-02T03:04:05.000000Z",
                "observed_at": observed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                # w1, r1, w4 and the header-less scout; w2 and w3 have returned.
                "active_lanes": 4,
                "tasks": [
                    {"id": "ABC-0123", "title": "Fix the widget"},
                    {"id": "ABC-0126", "title": "Second live task"},
                ],
            }
        ]
    }
    assert simple(live(conn, OTHER_REPO.upper())) == [
        (9, "running", 1, [{"id": "ABC-0555", "title": "Another repository"}])
    ]
    assert live(conn, "example-owner/absent-repo") == {"loops": []}


def test_claude_lane_stays_active_until_its_completion_is_recorded(conn, tmp_path):
    loop = claude_loop(tmp_path, 7, datetime.now(timezone.utc) - timedelta(minutes=30))
    loop.dispatch("toolu_w1", header("w1", "ABC-0123", "Fix the widget"))
    loop.save()
    refresh(conn, {"claude-test": tmp_path})
    identify(conn, 7, REPO)
    assert simple(live(conn)) == [(7, "running", 1, [{"id": "ABC-0123", "title": "Fix the widget"}])]
    loop.notify("toolu_w1")
    loop.save()
    refresh(conn, {"claude-test": tmp_path})
    identify(conn, 7, REPO)
    assert simple(live(conn)) == [(7, "running", 0, [])]


def test_briefs_before_a_relaunch_in_the_same_root_belong_to_the_earlier_run(conn, tmp_path):
    loop = claude_loop(tmp_path, 6, datetime.now(timezone.utc) - timedelta(minutes=30))
    loop.dispatch("toolu_old", header("w1", "ABC-0601", "Dispatched by the previous run"))
    loop.user(prompt(7))
    loop.dispatch("toolu_new", header("w1", "ABC-0701", "Dispatched by this run"))
    loop.save()
    refresh(conn, {"claude-test": tmp_path})
    statuses = conn.execute("SELECT l.status FROM ah.loops l ORDER BY l.launch_ts").fetchall()
    conn.commit()
    assert statuses == [("finished",), ("running",)]  # the relaunch ended the earlier run
    identify(conn, 7, REPO)
    assert simple(live(conn)) == [(7, "running", 1, [{"id": "ABC-0701", "title": "Dispatched by this run"}])]


def test_an_exactly_linked_relaunch_root_dispatches_for_the_same_run(conn, tmp_path):
    start = datetime.now(timezone.utc) - timedelta(minutes=30)
    loop = claude_loop(tmp_path, 7, start)
    loop.dispatch("toolu_w1", header("w1", "ABC-0123", "Fix the widget"))
    loop.save()
    relaunch = Claude(tmp_path, str(uuid.uuid4()), start + timedelta(minutes=5))
    relaunch.user("Resume the synthetic run from its state.")
    relaunch.dispatch("toolu_w2", header("w2", "ABC-0124", "Dispatched after the relaunch"))
    relaunch.save()
    unlinked = Claude(tmp_path, str(uuid.uuid4()), start + timedelta(minutes=5))
    unlinked.user("An unrelated session.")
    unlinked.dispatch("toolu_x1", header("x1", "ABC-0888", "Not part of the run"))
    unlinked.save()
    refresh(conn, {"claude-test": tmp_path})
    # The relaunch link itself is the projection's (test_loop_identity_pg covers how it is proved).
    conn.execute(
        "UPDATE ah.session SET loop_run_id = (SELECT id FROM ah.loop_run), loop_link_method = 'relaunch' "
        "WHERE session_uid = %s AND agent_id = ''",
        (relaunch.root,),
    )
    identify(conn, 7, REPO)
    assert simple(live(conn)) == [
        (
            7,
            "running",
            2,
            [
                {"id": "ABC-0123", "title": "Fix the widget"},
                {"id": "ABC-0124", "title": "Dispatched after the relaunch"},
            ],
        )
    ]


# --- Codex -----------------------------------------------------------------------------------------


def test_codex_spawn_links_by_dispatch_line_and_activity_completion(conn, tmp_path):
    at = datetime.now(timezone.utc) - timedelta(minutes=30)
    root = str(uuid.uuid4())

    def rec(kind, payload, seconds):
        return {"type": kind, "timestamp": iso(at + timedelta(seconds=seconds)), "payload": payload}

    def spawn(call, task, brief, seconds):
        arguments = json.dumps({"task_name": task, "message": brief, "agent_type": "worker"})
        return [
            rec(
                "response_item",
                {"type": "function_call", "call_id": call, "name": "spawn_agent", "arguments": arguments},
                seconds,
            ),
            rec(
                "response_item",
                {"type": "function_call_output", "call_id": call, "output": json.dumps({"task_name": task})},
                seconds,
            ),
        ]

    records = [
        rec("session_meta", {"id": root, "source": "cli", "cwd": CWD}, 0),
        rec("event_msg", {"type": "user_message", "message": prompt(7)}, 1),
        *spawn("call_c1", "lane_c1", header("c1", "ABC-0201", "Live Codex lane"), 2),
        *spawn("call_c2", "lane_c2", header("c2", "ABC-0202", "Finished Codex lane"), 3),
        *spawn("call_c3", "lane_c3", "No header on this brief.", 4),
        rec(
            "event_msg",
            {
                "type": "sub_agent_activity",
                "agent_path": "/root/lane_c2",
                "agent_thread_id": str(uuid.uuid4()),
                "kind": "completed",
                "occurred_at_ms": int((at + timedelta(seconds=9)).timestamp() * 1000),
            },
            9,
        ),
    ]
    write(tmp_path / "sessions" / "2026" / "10" / "19" / f"rollout-{root}.jsonl", records)
    refresh(conn, {"codex-test": tmp_path})
    identify(conn, 7, REPO)
    assert simple(live(conn)) == [(7, "running", 2, [{"id": "ABC-0201", "title": "Live Codex lane"}])]


# --- pi --------------------------------------------------------------------------------------------


def test_pi_async_foreground_and_parallel_dispatches(conn, tmp_path):
    at = datetime.now(timezone.utc) - timedelta(minutes=30)
    root = str(uuid.uuid4())
    records: list[dict] = []

    def add(record, seconds):
        stamp = iso(at + timedelta(seconds=seconds))
        record = {"id": uuid.uuid4().hex[:8], "timestamp": stamp, **record}
        if "message" in record:
            record["message"]["timestamp"] = stamp
        records.append(record)

    def call(call_id, arguments, seconds):
        add(
            {
                "type": "message",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "toolCall", "id": call_id, "name": "subagent", "arguments": arguments}],
                    "model": "synthetic-model",
                    "responseId": "resp_" + call_id,
                    "stopReason": "toolUse",
                    "usage": {"input": 1, "output": 1, "cacheRead": 0, "cacheWrite": 0},
                },
            },
            seconds,
        )

    def result(call_id, details, seconds):
        add(
            {
                "type": "message",
                "message": {
                    "role": "toolResult",
                    "toolCallId": call_id,
                    "toolName": "subagent",
                    "content": [{"type": "text", "text": "ok"}],
                    "isError": False,
                    "details": details,
                },
            },
            seconds,
        )

    add({"type": "session", "id": root, "cwd": CWD, "version": 3}, 0)
    add({"type": "message", "message": {"role": "user", "content": prompt(7)}}, 1)
    runs = {name: str(uuid.uuid4()) for name in ("pa", "pb")}
    for seconds, name, task in ((2, "pa", "ABC-0301"), (3, "pb", "ABC-0302")):
        call("call_" + name, {"agent": "worker", "task": header(name, task, "Async " + name), "async": True}, seconds)
        result("call_" + name, {"mode": "single", "runId": runs[name], "asyncId": runs[name], "results": []}, seconds)
    call("call_pc", {"agent": "worker", "task": header("pc", "ABC-0303", "Foreground and returned")}, 4)
    result("call_pc", {"mode": "single", "results": []}, 5)
    tasks = [
        {"agent": "worker", "task": header("pd", "ABC-0304", "Parallel one")},
        {"agent": "worker", "task": header("pe", "ABC-0305", "Parallel two")},
    ]
    call("call_pd", {"tasks": tasks}, 6)
    add(
        {
            "type": "custom_message",
            "customType": "subagent-notify",
            "content": "Background task completed: **worker**\n\nSynthetic result.\n"
            f"Retention-managed async directory: /tmp/synthetic/async-subagent-runs/{runs['pb']}",
            "display": True,
        },
        7,
    )
    write(tmp_path / "sessions" / "synthetic" / f"2026-10-19T00-00-00-000Z_{root}.jsonl", records)
    refresh(conn, {"pi-test": tmp_path})
    identify(conn, 7, REPO)
    assert simple(live(conn)) == [
        (
            7,
            "running",
            3,
            [
                {"id": "ABC-0301", "title": "Async pa"},
                {"id": "ABC-0304", "title": "Parallel one"},
                {"id": "ABC-0305", "title": "Parallel two"},
            ],
        )
    ]


# --- grammar parity, guards and the narrow role ---------------------------------------------------


def test_sql_header_grammar_matches_the_reference(conn):
    for brief, expected in CASES:
        row = conn.execute("SELECT task_id, title FROM ah.lane_brief_task(%s)", (brief,)).fetchone()
        assert row == expected, brief
    conn.commit()


@pytest.mark.parametrize("repo", [None, "", "owner", "owner/repo/extra", "owner/re po", "owner/repo'--"])
def test_repo_must_be_an_owner_repo_slug(conn, repo):
    with pytest.raises(errors.InvalidParameterValue):
        conn.execute("SELECT ah.live_loop_tasks(%s)", (repo,))
    conn.rollback()


def test_refuses_during_a_rebuild(conn):
    conn.execute("INSERT INTO ah.meta VALUES ('rebuild_in_progress', 'synthetic')")
    conn.commit()
    with pytest.raises(errors.ObjectNotInPrerequisiteState):
        conn.execute("SELECT ah.live_loop_tasks(%s)", (REPO,))
    conn.rollback()


def run_role_script(repo: str | None = None) -> None:
    """Run the shipped role script as psql would with or without -v repo=<repo>."""
    from test_live_loop_tasks import PSQL_PREAMBLE

    _, marker, body = ROLE_SCRIPT.read_text().partition(PSQL_PREAMBLE)
    with psycopg.connect(ADMIN_DSN, autocommit=True) as admin:
        if repo is not None:
            admin.execute("SELECT set_config('ah_loop_tasks.repo', %s, false)", (repo,))
        admin.execute(body if marker else ROLE_SCRIPT.read_text())


def login(role: str) -> None:
    password = conninfo_to_dict(ADMIN_DSN).get("password")
    with psycopg.connect(ADMIN_DSN, autocommit=True) as admin:
        admin.execute(sql.SQL("ALTER ROLE {} LOGIN").format(sql.Identifier(role)))
        if password:
            admin.execute(sql.SQL("ALTER ROLE {} PASSWORD {}").format(sql.Identifier(role), sql.Literal(password)))


def drop_roles(*roles: str) -> None:
    with psycopg.connect(ADMIN_DSN, autocommit=True) as admin:
        if admin.execute("SELECT to_regclass('ah.live_loop_tasks_scope')").fetchone()[0]:
            admin.execute("DELETE FROM ah.live_loop_tasks_scope WHERE role_name = ANY(%s)", (list(roles),))
        for role in roles:
            if admin.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone():
                admin.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(role)))
                admin.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))


@pytest.fixture
def admin_dsn(conn):
    if "agent_history_test" not in ADMIN_DSN:
        pytest.skip("AGENT_HISTORY_TEST_ADMIN_DSN not set")
    yield ADMIN_DSN
    drop_roles("ah_loop_tasks", "synthetic_bystander", "synthetic_member")


@pytest.fixture
def role_dsn(admin_dsn):
    run_role_script("Example-Owner/Example-Repo")  # stored and compared case-insensitively
    run_role_script(REPO)  # re-running the shipped script is harmless
    run_role_script()  # without -v repo it grants nothing new
    login("ah_loop_tasks")
    with psycopg.connect(admin_dsn, autocommit=True) as admin:
        admin.execute("CREATE ROLE synthetic_bystander")
    return make_conninfo(admin_dsn, user="ah_loop_tasks")


def test_narrow_role_runs_the_function_and_reads_nothing_else(conn, tmp_path, role_dsn):
    loop = claude_loop(tmp_path, 7, datetime.now(timezone.utc) - timedelta(minutes=30))
    loop.dispatch("toolu_w1", header("w1", "ABC-0123", "Fix the widget"))
    loop.save()
    refresh(conn, {"claude-test": tmp_path})
    identify(conn, 7, REPO)
    expected = live(conn)
    assert simple(expected) == [(7, "running", 1, [{"id": "ABC-0123", "title": "Fix the widget"}])]
    with psycopg.connect(role_dsn) as narrow:
        assert narrow.execute("SELECT ah.live_loop_tasks(%s)", (REPO,)).fetchone()[0] == expected
        assert narrow.execute("SHOW default_transaction_read_only").fetchone() == ("on",)
        narrow.rollback()
        for statement in (
            "SELECT * FROM ah.loops",
            "SELECT text FROM ah.message",
            "SELECT * FROM ah.lane_brief_task('x')",
            "SELECT * FROM ah.recent_loops(NULL, 1)",
            "SELECT * FROM ah.live_loop_tasks_scope",
            "SELECT ah.live_loop_tasks('example-owner/other-repo')",
        ):
            with pytest.raises(errors.InsufficientPrivilege):
                narrow.execute(statement)
            narrow.rollback()
    reachable = conn.execute(
        "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = 'ah' AND c.relkind IN ('r', 'v', 'm', 'p', 'f') "
        "AND has_table_privilege('ah_loop_tasks', c.oid, 'SELECT, INSERT, UPDATE, DELETE')"
    ).fetchall()
    assert reachable == []
    assert conn.execute(
        "SELECT has_function_privilege('synthetic_bystander', 'ah.live_loop_tasks(text)', 'EXECUTE')"
    ).fetchone() == (False,)
    conn.commit()


def test_migration_grants_an_existing_role_and_survives_reapplication(conn, role_dsn):
    # As on a catalogue where the role was provisioned before the indexer applied 043: a fresh
    # function, created by the owner, granted to the existing role and to nobody else.
    conn.execute("DROP FUNCTION ah.live_loop_tasks(text)")
    conn.execute("DROP FUNCTION ah.lane_brief_task(text)")
    conn.execute("DELETE FROM ah.meta WHERE key = %s", ("migration:" + MIGRATION,))
    conn.commit()
    with psycopg.connect(role_dsn) as narrow, pytest.raises(errors.UndefinedFunction):
        narrow.execute("SELECT ah.live_loop_tasks(%s)", (REPO,))
    load.apply_schema(conn, force=True)  # replays 043 and re-applies the analytics files
    conn.commit()
    load.apply_schema(conn, force=True)
    conn.commit()
    assert conn.execute(
        "SELECT has_function_privilege('synthetic_bystander', 'ah.live_loop_tasks(text)', 'EXECUTE'), "
        "has_function_privilege('synthetic_bystander', 'ah.lane_brief_task(text)', 'EXECUTE'), "
        "p.prosecdef, pg_get_userbyid(p.proowner) = current_user, p.proconfig "
        "FROM pg_proc p WHERE p.oid = 'ah.live_loop_tasks(text)'::regprocedure"
    ).fetchone() == (False, False, True, True, ["search_path=pg_catalog, pg_temp"])
    conn.commit()
    with psycopg.connect(role_dsn) as narrow:
        assert narrow.execute("SELECT ah.live_loop_tasks(%s)", (REPO,)).fetchone()[0] == {"loops": []}


def test_role_script_refuses_a_role_that_can_read_a_column(admin_dsn):
    with psycopg.connect(admin_dsn, autocommit=True) as admin:
        admin.execute("GRANT SELECT (text) ON ah.message TO PUBLIC")
    try:
        with pytest.raises(errors.RaiseException, match="message"):
            run_role_script(REPO)
    finally:
        with psycopg.connect(admin_dsn, autocommit=True) as admin:
            admin.execute("REVOKE SELECT (text) ON ah.message FROM PUBLIC")


def scope_rows(conn) -> list[tuple]:
    rows = conn.execute("SELECT role_name::text, repo FROM ah.live_loop_tasks_scope ORDER BY 1, 2").fetchall()
    conn.commit()
    return rows


def test_role_script_scope_comes_only_from_the_repo_variable(conn, admin_dsn):
    run_role_script()
    login("ah_loop_tasks")
    assert scope_rows(conn) == []
    with psycopg.connect(make_conninfo(admin_dsn, user="ah_loop_tasks")) as narrow:
        with pytest.raises(errors.InsufficientPrivilege):
            narrow.execute("SELECT ah.live_loop_tasks(%s)", (REPO,))
    with pytest.raises(errors.RaiseException, match="owner/repo"):
        run_role_script("not a slug")
    run_role_script(REPO)
    run_role_script(REPO.upper())
    assert scope_rows(conn) == [("ah_loop_tasks", REPO)]


def test_scoped_role_gets_only_its_repo_and_readers_are_unscoped(conn, tmp_path, role_dsn):
    for n in (7, 9):
        loop = claude_loop(tmp_path, n, datetime.now(timezone.utc) - timedelta(minutes=30))
        loop.dispatch(f"toolu_{n}", header("w1", f"ABC-0{n}00", "Synthetic task"))
        loop.save()
    refresh(conn, {"claude-test": tmp_path})
    identify(conn, 7, REPO)
    identify(conn, 9, OTHER_REPO)
    assert scope_rows(conn) == [("ah_loop_tasks", REPO)]
    with psycopg.connect(role_dsn) as narrow:
        assert simple(narrow.execute("SELECT ah.live_loop_tasks(%s)", (REPO.upper(),)).fetchone()[0]) == [
            (7, "running", 1, [{"id": "ABC-0700", "title": "Synthetic task"}])
        ]
        narrow.rollback()
        with pytest.raises(errors.InsufficientPrivilege) as refused:
            narrow.execute("SELECT ah.live_loop_tasks(%s)", (OTHER_REPO,))
        assert refused.value.sqlstate == "42501"
    reader = os.environ.get("AGENT_HISTORY_TEST_READER_DSN", "")
    if "agent_history_test" in reader:
        with psycopg.connect(reader) as plain:
            assert simple(plain.execute("SELECT ah.live_loop_tasks(%s)", (OTHER_REPO,)).fetchone()[0]) == [
                (9, "running", 1, [{"id": "ABC-0900", "title": "Synthetic task"}])
            ]


def test_scope_follows_the_login_role_not_set_role(conn, role_dsn, admin_dsn):
    # A login that can become ah_loop_tasks holds its EXECUTE privilege but not its repository scope.
    with psycopg.connect(admin_dsn, autocommit=True) as admin:
        admin.execute("CREATE ROLE synthetic_member")
        admin.execute("GRANT ah_loop_tasks TO synthetic_member")
    login("synthetic_member")
    with psycopg.connect(make_conninfo(admin_dsn, user="synthetic_member")) as member:
        for become in ("", "SET ROLE ah_loop_tasks"):
            if become:
                member.execute(become)
            with pytest.raises(errors.InsufficientPrivilege):
                member.execute("SELECT ah.live_loop_tasks(%s)", (REPO,))
            member.rollback()
    with psycopg.connect(role_dsn) as narrow:
        assert narrow.execute("SELECT ah.live_loop_tasks(%s)", (REPO,)).fetchone()[0] == {"loops": []}

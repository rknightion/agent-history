"""Real catalogue -> exporter -> Prometheus proof text and local OTLP receiver."""

import os
import time
from datetime import datetime, timedelta, timezone

import psycopg
import pytest

from agent_history import telemetry
from agent_history.metrics import otlp, otlp_parity
from agent_history.metrics.catalogue import CatalogueCollector
from agent_history.metrics.collection import Collection, State
from agent_history.metrics.loop_health import PREFIX, QUERY
from agent_history.metrics.self import SelfCollector
from test_loop_identity_pg import TEST_DB, clean, conn  # noqa: F401 - disposable database fixtures
from test_otlp_bridge import receiver  # noqa: F401 - loopback OTLP, never a real endpoint

DSN = os.environ.get("AGENT_HISTORY_TEST_DSN", "")
pytestmark = pytest.mark.skipif(TEST_DB != "agent_history_test", reason="exact disposable catalogue required")
AT = datetime(2030, 1, 1, tzinfo=timezone.utc)


def seed(connection, *, key="first", at=AT, tasks=2, cost=1.25, status="finished"):
    source = connection.execute(
        "INSERT INTO ah.source_file (rel_path,namespace,agent,profile,file_role,tier) "
        "VALUES (%s,'pi-test','pi','test','main','hot') RETURNING id",
        (key,),
    ).fetchone()[0]
    root = connection.execute(
        "INSERT INTO ah.session (agent,session_uid,namespace,profile,is_stub) "
        "VALUES ('pi',%s,'pi-test','test',false) RETURNING id",
        (key,),
    ).fetchone()[0]
    run = connection.execute(
        "INSERT INTO ah.loop_run (launch_uid,root_session_id,launch_ts,end_ts,report_path) "
        "VALUES (%s,%s,%s,%s,%s) RETURNING id",
        (key, root, at, at + timedelta(seconds=600), key),
    ).fetchone()[0]
    connection.execute(
        "INSERT INTO ah.loops (launch_uid,repo,status,launch_ts,end_ts,observed_at,tasks_done,priced_cost_usd) "
        "VALUES (%s,'example/project',%s,%s,%s,%s,%s,%s)",
        (key, status, at, at + timedelta(seconds=600), at, tasks, cost),
    )
    return root, run, source


def message(connection, root, source, key, text, seconds=100, cls="assistant_text"):
    connection.execute(
        "INSERT INTO ah.message (agent,event_uid,session_id,namespace,profile,ts,role,message_class,text,"
        "content_sha256,source_id,byte_offset,byte_length) "
        "VALUES ('pi',%s,%s,'pi-test','test',%s,'assistant',%s,%s,'synthetic',%s,0,0)",
        (key, root, AT + timedelta(seconds=seconds), cls, text, source),
    )


def call(connection, root, source, key, start, end, *, reason=None):
    connection.execute(
        "INSERT INTO ah.llm_call (agent,response_id,session_id,ts,source_id,byte_offset,stop_reason) "
        "VALUES ('pi',%s,%s,%s,%s,0,%s)",
        (key, root, AT + timedelta(seconds=end), source, reason),
    )
    connection.execute(
        "INSERT INTO ah.request_progress (session_id,request_uid,source_id,byte_offset,namespace,started_at,"
        "written_at,state,producer_version,provenance,ended_at,response_id) "
        "VALUES (%s,%s,%s,0,'pi-test',%s,%s,'ended',1,'loopPiProgress.v1',%s,%s)",
        (
            root,
            key,
            source,
            AT + timedelta(seconds=start),
            AT + timedelta(seconds=end),
            AT + timedelta(seconds=end),
            key,
        ),
    )


def test_exporter_latest_completed_health_and_otlp(clean, tmp_path, receiver):  # noqa: F811
    root, run, source = seed(clean)
    for i, text in enumerate(
        ("Owner question: which option?", "Parked pending review. For-ratification: proceed.", "Working.", "Finished.")
    ):
        message(clean, root, source, f"text-{i}", text)
    message(clean, root, source, "reasoning", "owner question parked for-ratification", cls="reasoning")
    message(clean, root, source, "brief", "owner question parked for-ratification", cls="subagent_brief")
    message(clean, root, source, "later", "owner question parked for-ratification", seconds=601)
    call(clean, root, source, "clipped", -30, 60)
    call(clean, root, source, "second", 180, 300)
    call(clean, root, source, "compact", 300, 400, reason="compaction")
    for i, status in enumerate(("complete", "failed", None)):
        child = clean.execute(
            "INSERT INTO ah.session (agent,session_uid,loop_run_id,loop_link_method) "
            "VALUES ('pi',%s,%s,'lineage') RETURNING id",
            (f"child-{i}", run),
        ).fetchone()[0]
        clean.execute(
            "INSERT INTO ah.lane (loop_run_id,session_id,link_method,return_status) VALUES (%s,%s,'lineage',%s)",
            (run, child, status),
        )
        message(clean, child, source, f"child-text-{i}", "Owner question parked for-ratification")
        call(clean, child, source, f"child-call-{i}", 1, 500)
    relaunch = clean.execute(
        "INSERT INTO ah.session (agent,session_uid,loop_run_id,loop_link_method) "
        "VALUES ('pi','relaunch',%s,'relaunch') RETURNING id",
        (run,),
    ).fetchone()[0]
    message(clean, relaunch, source, "relaunch-text", "Continuing.")
    call(clean, relaunch, source, "relaunch-call", 400, 460)
    # A later stale loop is not a completion and cannot replace the observation.
    seed(clean, key="stale", at=AT + timedelta(hours=1), status="stale")
    clean.execute("INSERT INTO ah.parse_issue (source_id,byte_offset,kind) VALUES (%s,0,'json_error')", (source,))
    clean.commit()

    telemetry.setup("agent-history-index")
    bridge = otlp.Bridge.create()
    collection = Collection([CatalogueCollector(DSN)], State(tmp_path), refresh=0, bridge=bridge)
    text = collection.metrics()
    decoded = receiver.flush()
    parity = otlp_parity.compare(text, decoded, rejected=bridge.rejected)
    assert parity["ok"], parity
    expected = {
        "root_model_time_share": 0.4,
        "root_model_calls": 3,
        "root_model_timed_calls": 3,
        "root_messages": 5,
        "owner_questions_per_1k_root_messages": 200,
        "owner_parks_per_1k_root_messages": 200,
        "ratification_rulings": 1,
        "lane_complete_rate": 1 / 3,
        "tasks_done": 2,
        "estimated_cost_usd": 1.25,
    }
    for field, value in expected.items():
        points = decoded[PREFIX + field]["points"]
        assert len(points) == 1
        assert next(iter(points.values())) == pytest.approx(value)
        assert dict(next(iter(points))) == {"repo": "example/project", "harness": "pi"}

    # Lost timing is unknown, never a zero or a partial full-loop share. The real exporter
    # and the OTLP callback must retire the previous known gauge without a restart.
    clean.execute("DELETE FROM ah.request_progress WHERE request_uid = 'second'")
    clean.commit()
    text = collection.metrics()
    assert PREFIX + "root_model_time_share{" not in text
    assert PREFIX + "root_model_time_share" not in receiver.flush()
    assert PREFIX + 'root_model_timed_calls{harness="pi",repo="example/project"} 2' in text
    # A newer completed loop with missing evidence must replace every old field, not select
    # the latest known value separately per family. No messages/lanes/cost means unknown.
    seed(clean, key="newest", at=AT + timedelta(hours=2), tasks=None, cost=None)
    clean.commit()
    text = collection.metrics()
    for field in (
        "tasks_done",
        "estimated_cost_usd",
        "lane_complete_rate",
        "owner_questions_per_1k_root_messages",
        "ratification_rulings",
    ):
        assert PREFIX + field + "{" not in text
    clean.execute("INSERT INTO ah.meta (key,value) VALUES ('rebuild_in_progress','synthetic')")
    clean.commit()
    assert all(not family.samples for family in CatalogueCollector(DSN).collect() if family.name.startswith(PREFIX))
    clean.execute("DELETE FROM ah.meta WHERE key='rebuild_in_progress'")
    clean.commit()


@pytest.mark.parametrize("fault", ["permission", "lock"])
def test_health_fault_isolation_and_recovery(clean, tmp_path, receiver, fault):  # noqa: F811
    """A real health-query failure retires only health; its 60s budget stays local."""
    reader = os.environ.get("AGENT_HISTORY_TEST_READER_DSN", "")
    admin_dsn = os.environ.get("AGENT_HISTORY_TEST_ADMIN_DSN", "")
    if not reader or not admin_dsn:
        pytest.skip("disposable reader and admin required")
    _, _, source = seed(clean)
    clean.execute("INSERT INTO ah.parse_issue (source_id,byte_offset,kind) VALUES (%s,0,'json_error')", (source,))
    clean.execute("DELETE FROM ah.refresh_log")
    clean.execute("DELETE FROM ah.embed_failure")
    clean.execute(
        "INSERT INTO ah.refresh_log (refresh_id,kind,finished_at,ok) VALUES (1,'refresh',to_timestamp(100),true)"
    )
    clean.commit()
    telemetry.setup("agent-history-index")
    bridge = otlp.Bridge.create()
    health = SelfCollector()
    collector = CatalogueCollector(reader)
    collection = Collection([collector, health], State(tmp_path), refresh=0, bridge=bridge)

    def collect():
        text = collection.metrics()
        decoded = receiver.flush()
        parity = otlp_parity.compare(text, decoded, rejected=bridge.rejected)
        assert parity["ok"], parity
        return text, decoded

    before, _ = collect()
    assert PREFIX + 'tasks_done{harness="pi",repo="example/project"} 2' in before
    clean.execute("UPDATE ah.refresh_log SET finished_at=to_timestamp(200)")
    clean.execute("UPDATE ah.loops SET tasks_done=3")
    clean.commit()
    with psycopg.connect(admin_dsn, autocommit=True) as admin:
        if fault == "permission":
            admin.execute("REVOKE SELECT ON ah.loop_run FROM ah_reader")
        else:
            admin.execute("BEGIN")
            admin.execute("LOCK TABLE ah.loop_run IN ACCESS EXCLUSIVE MODE")
        try:
            started = time.monotonic()
            failed, decoded = collect()
            elapsed = time.monotonic() - started
            assert elapsed < (70 if fault == "lock" else 10)
            if fault == "lock":
                assert elapsed >= 59  # Health retains its intended 60s, not the ordinary 5s.
            assert PREFIX not in failed
            assert not any(name.startswith(PREFIX) for name in decoded)
            assert "agent_history_last_success_timestamp_seconds 200" in failed
            assert "agent_history_sources{" in failed
            assert "agent_history_embed_vectors 0" in failed
            assert health.success["catalogue_loop_health"] == 0
            assert health.errors["catalogue_loop_health"] == 1
            assert "agent_sessions_metrics_collection_success 0" in failed
            assert 'agent_sessions_metrics_section_success{section="catalogue_loop_health"} 0' in failed
            assert "permission denied" not in failed
            assert "statement timeout" not in failed
        finally:
            if fault == "permission":
                admin.execute("GRANT SELECT ON ah.loop_run TO ah_reader")
            else:
                admin.execute("ROLLBACK")

    recovered, _ = collect()
    assert PREFIX + 'tasks_done{harness="pi",repo="example/project"} 3' in recovered
    assert health.success["catalogue_loop_health"] == 1
    assert health.errors["catalogue_loop_health"] == 1
    assert not health.collection_failed
    # A later embedding fault must still use 5s after the health transaction used 60s.
    if fault == "lock":
        with psycopg.connect(admin_dsn, autocommit=True) as admin:
            admin.execute("BEGIN")
            admin.execute("LOCK TABLE ah.embed_failure IN ACCESS EXCLUSIVE MODE")
            try:
                started = time.monotonic()
                failed, decoded = collect()
                assert 4 <= time.monotonic() - started < 10
                assert "agent_history_embed_failed_inputs" not in failed
                assert "agent_history_embed_failed_inputs" not in decoded
                assert PREFIX + 'tasks_done{harness="pi",repo="example/project"} 3' in failed
                assert health.success["catalogue_embed_failures"] == 0
                assert health.success["catalogue_loop_health"] == 1
            finally:
                admin.execute("ROLLBACK")
        final, _ = collect()
        assert "agent_history_embed_failed_inputs 0" in final
        assert not health.collection_failed


def test_health_query_is_read_only_and_clips_before_capping(clean):  # noqa: F811
    root, _, source = seed(clean)
    call(clean, root, source, "one", -100, 500)
    call(clean, root, source, "two", 0, 500)
    clean.commit()
    with clean.transaction():
        clean.execute("SET TRANSACTION READ ONLY")
        row = clean.execute(QUERY).fetchone()
    assert row[3] == 1  # SUM overlaps, not interval union; clamp only the share.

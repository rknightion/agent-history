"""Catalogue section failures through real database permissions and lock timeouts."""

import os
import time

import psycopg
import pytest

from agent_history import telemetry
from agent_history.metrics import otlp, otlp_parity
from agent_history.metrics.catalogue import CatalogueCollector
from agent_history.metrics.collection import Collection, State
from agent_history.metrics.self import SelfCollector
from test_loop_links_pg import conn  # noqa: F401 - disposable schema fixture
from test_otlp_bridge import receiver  # noqa: F401 - loopback OTLP receiver fixture

READER = os.environ.get("AGENT_HISTORY_TEST_READER_DSN", "")
ADMIN = os.environ.get("AGENT_HISTORY_TEST_ADMIN_DSN", "")
pytestmark = pytest.mark.skipif(
    not all("agent_history_test" in dsn for dsn in (READER, ADMIN)), reason="scratch database not set"
)


@pytest.mark.parametrize(
    "table,metric,section,fault",
    [
        ("refresh_log", "agent_history_last_success_timestamp_seconds", "refresh", "permission"),
        ("session", "agent_history_unresolved_links", "links", "permission"),
        ("embed_failure", "agent_history_embed_failed_inputs", "embed_failures", "lock"),
    ],
)
def test_catalogue_section_fault_and_recovery(conn, tmp_path, receiver, table, metric, section, fault):  # noqa: F811
    conn.execute("DELETE FROM ah.refresh_log")
    conn.execute("DELETE FROM ah.embed_failure")
    conn.execute(
        "INSERT INTO ah.refresh_log (refresh_id, kind, finished_at, ok) VALUES (1, 'refresh', to_timestamp(100), true)"
    )
    conn.execute("INSERT INTO ah.embed_failure (model, input_sha256) VALUES ('synthetic', 'a')")
    source = conn.execute(
        "INSERT INTO ah.source_file (rel_path, namespace, agent, profile, file_role, tier) "
        "VALUES ('synthetic/metrics.jsonl','synthetic','pi','local','main','hot') RETURNING id"
    ).fetchone()[0]
    conn.execute("INSERT INTO ah.parse_issue (source_id, byte_offset, kind) VALUES (%s, 0, 'json_error')", (source,))
    conn.commit()
    telemetry.setup("agent-history-index")
    bridge = otlp.Bridge.create()
    health = SelfCollector()
    collector = CatalogueCollector(READER)
    collection = Collection([collector, health], State(tmp_path), refresh=0, bridge=bridge)

    def collect():
        text = collection.metrics()
        decoded = receiver.flush()
        report = otlp_parity.compare(text, decoded, rejected=bridge.rejected)
        assert report["ok"], report
        return text, decoded

    before, _ = collect()
    assert metric + "{" in before or metric + " " in before
    assert "agent_history_embed_failed_inputs 1" in before
    conn.execute("UPDATE ah.refresh_log SET finished_at = to_timestamp(200)")
    conn.execute("INSERT INTO ah.embed_failure (model, input_sha256) VALUES ('synthetic', 'b')")
    conn.commit()
    with psycopg.connect(ADMIN, autocommit=True) as admin:
        if fault == "permission":
            admin.execute(f"REVOKE SELECT ON ah.{table} FROM ah_reader")
        else:
            admin.execute("BEGIN")
            admin.execute(f"LOCK TABLE ah.{table} IN ACCESS EXCLUSIVE MODE")
        try:
            start = time.monotonic()
            failed, decoded = collect()
            elapsed = time.monotonic() - start
            assert elapsed < 10
            assert metric not in failed  # Neither stale samples nor a fabricated zero.
            assert metric not in decoded
            assert "agent_history_sources{" in failed
            assert "agent_history_embed_vectors 0" in failed  # Query after the fault still works.
            assert health.success[f"catalogue_{section}"] == 0
            assert health.errors[f"catalogue_{section}"] == 1
            assert health.collection_failed
            assert "agent_sessions_metrics_collection_success 0" in failed
            assert f'agent_sessions_metrics_section_success{{section="catalogue_{section}"}} 0' in failed
            if table != "refresh_log":
                assert "agent_history_last_success_timestamp_seconds 200" in failed
            if table != "embed_failure":
                assert "agent_history_embed_failed_inputs 2" in failed
            # Error evidence has fixed section labels, never database error text or object names.
            assert "permission denied" not in failed
            assert "statement timeout" not in failed
            if fault == "lock":
                assert elapsed >= 4
        finally:
            if fault == "permission":
                admin.execute(f"GRANT SELECT ON ah.{table} TO ah_reader")
            else:
                admin.execute("ROLLBACK")
    recovered, _ = collect()
    assert metric + "{" in recovered or metric + " " in recovered
    assert "agent_history_embed_failed_inputs 2" in recovered
    assert health.success[f"catalogue_{section}"] == 1
    assert health.errors[f"catalogue_{section}"] == 1
    assert not health.collection_failed

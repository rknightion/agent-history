"""Catalogue section failures through real database permissions and lock timeouts."""

import time

import pytest
from scratch_catalogue import scratch_catalogue, scratch_conn, table_fault  # noqa: F401 - shared guarded fixtures

from agent_history import load, telemetry
from agent_history.metrics import otlp, otlp_parity
from agent_history.metrics.catalogue import CatalogueCollector
from agent_history.metrics.collection import Collection, State
from agent_history.metrics.self import SelfCollector
from test_otlp_bridge import receiver  # noqa: F401 - loopback OTLP receiver fixture


@pytest.fixture
def conn(scratch_conn):  # noqa: F811 - imported fixture
    scratch_conn.execute(
        "TRUNCATE " + ", ".join(f"ah.{table}" for table in load.DATA_TABLES) + " RESTART IDENTITY CASCADE"
    )
    scratch_conn.execute("DELETE FROM ah.meta WHERE key LIKE 'loops_links_projection_v%%'")
    scratch_conn.commit()
    yield scratch_conn
    scratch_conn.rollback()


@pytest.mark.parametrize(
    "table,metric,section,fault",
    [
        ("refresh_log", "agent_history_last_success_timestamp_seconds", "refresh", "permission"),
        ("session", "agent_history_unresolved_links", "links", "permission"),
        ("embed_failure", "agent_history_embed_failed_inputs", "embed_failures", "lock"),
    ],
)
def test_catalogue_section_fault_and_recovery(
    conn,
    scratch_catalogue,  # noqa: F811 - imported fixture
    tmp_path,
    receiver,  # noqa: F811 - imported fixture
    table,
    metric,
    section,
    fault,
):
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
    collector = CatalogueCollector(scratch_catalogue.reader)
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
    vectors = conn.execute("SELECT count(*) FROM ah.embedding").fetchone()[0]
    conn.commit()
    with table_fault(scratch_catalogue, table, fault):
        start = time.monotonic()
        failed, decoded = collect()
        elapsed = time.monotonic() - start
        assert elapsed < 10
        assert metric not in failed  # Neither stale samples nor a fabricated zero.
        assert metric not in decoded
        assert "agent_history_sources{" in failed
        assert (
            f"agent_history_embed_vectors {vectors}\n" in failed
        )  # Independently queried current cache, never assumed empty.
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
    recovered, _ = collect()
    assert metric + "{" in recovered or metric + " " in recovered
    assert "agent_history_embed_failed_inputs 2" in recovered
    assert health.success[f"catalogue_{section}"] == 1
    assert health.errors[f"catalogue_{section}"] == 1
    assert not health.collection_failed

"""The real catalogue collector against a disposable database: parity and outbound spans."""

import os

import pytest

pytest.importorskip("opentelemetry.sdk")

from agent_history import load, telemetry
from agent_history.metrics import otlp, otlp_parity
from agent_history.metrics.catalogue import CatalogueCollector
from agent_history.metrics.self import SelfCollector
from agent_history.metrics.server import MetricServer, State
from test_otlp_bridge import receiver  # noqa: F401  (the fixture)

DSN = os.environ.get("AGENT_HISTORY_TEST_DSN", "")
pytestmark = pytest.mark.skipif("agent_history_test" not in DSN, reason="disposable catalogue required")


def test_real_catalogue_collection_agrees_and_its_database_calls_are_spans(tmp_path, receiver):  # noqa: F811
    with load.connect(DSN) as conn:
        load.apply_schema(conn, force=True)
        conn.commit()
    telemetry.setup("agent-history-exporter")
    bridge = otlp.Bridge.create()
    server = MetricServer(
        ("127.0.0.1", 0), [CatalogueCollector(DSN), SelfCollector()], State(tmp_path), 0, bridge=bridge
    )
    try:
        text = server.metrics()
        decoded = receiver.flush()
    finally:
        server.server_close()
    report = otlp_parity.compare(text, decoded, rejected=bridge.rejected)
    assert report["ok"], {k: v for k, v in report.items() if v}
    assert {"agent_history_sources", "agent_history_rows", "agent_history_dirty_sessions"} <= set(decoded)
    spans = receiver.spans()
    collector = next(
        s
        for s in spans
        if s.name == "exporter.collector"
        and any(a.key == "agent_history.collector" and a.value.string_value == "catalogue" for a in s.attributes)
    )
    database = [s for s in spans if s.name.startswith("db.")]
    assert {"db.connect", "db.query"} <= {s.name for s in database}
    # Every catalogue database call happens inside the collector span, and none carries SQL text.
    assert all(s.trace_id == collector.trace_id for s in database)
    assert not any("SELECT" in str(s) or DSN in str(s) for s in spans)

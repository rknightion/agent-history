"""The Prometheus exposition is a public contract: a downstream comparator reads these bytes."""

from pathlib import Path

from agent_history.metrics.server import MetricServer, State
from synthetic_metrics import Stub

GOLDEN = Path(__file__).parent / "fixtures" / "metrics" / "exposition-golden.txt"


def render(tmp_path, bridge=None) -> str:
    """Three collections through the real MetricServer: initial, unchanged, then a reset/omission/retirement."""
    collector = Stub()
    server = MetricServer(("127.0.0.1", 0), [collector], State(tmp_path), refresh=0, bridge=bridge)
    try:
        chunks = []
        for step in (0, 1, 2):
            collector.step = step
            chunks.append(f"# step {step}\n" + server.metrics())
        return "".join(chunks)
    finally:
        server.server_close()


def test_exposition_is_byte_identical_to_the_pre_bridge_capture(tmp_path):
    assert render(tmp_path).encode() == GOLDEN.read_bytes()

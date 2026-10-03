"""The promtool alert fixtures use the series the exporter really exposes for a failed embed run."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from agent_history.metrics.catalogue import RunCollector
from agent_history.metrics.server import State, exposition

ROOT = Path(__file__).resolve().parents[1]


def build_rules():
    spec = importlib.util.spec_from_file_location("build_rules", ROOT / "grafana" / "build_rules.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "snapshot",
    [
        pytest.param(lambda rules: rules.failed_snapshot("billing_quota"), id="402-billing_quota"),
        pytest.param(lambda rules: rules.failed_snapshot("rate_limit"), id="429-rate_limit"),
        pytest.param(lambda rules: rules.SUCCESS_SNAPSHOT, id="success"),
    ],
)
def test_fixture_snapshot_matches_exporter_exposition(tmp_path, snapshot):
    lines = snapshot(build_rules())
    (tmp_path / "agent-history-embed.prom").write_text("\n".join(lines) + "\n")
    output = exposition(list(RunCollector(tmp_path).collect()), State(tmp_path / "exporter-state"))
    exposed = [line for line in output.splitlines() if line and not line.startswith("#")]
    assert sorted(exposed) == sorted(lines)

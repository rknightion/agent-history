"""The promtool alert fixtures use the series the metric collection really publishes."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from agent_history.metrics.catalogue import RunCollector
from agent_history.metrics.collection import State, exposition
from agent_history.metrics.otlp import SPECS

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
def test_fixture_snapshot_matches_collection_exposition(tmp_path, snapshot):
    lines = snapshot(build_rules())
    (tmp_path / "agent-history-embed.prom").write_text("\n".join(lines) + "\n")
    output = exposition(list(RunCollector(tmp_path).collect()), State(tmp_path / "collection-state"))
    exposed = [line for line in output.splitlines() if line and not line.startswith("#")]
    assert sorted(exposed) == sorted(lines)


def test_stored_names_follow_the_otlp_unit_translation():
    # Grafana Cloud appends `_ratio` to a gauge whose unit is "1"; every other family a rule reads keeps
    # its name. A unit change in SPECS must change the selector the rules use, or the rule reads nothing.
    for family, stored in build_rules().STORED.items():
        spec = SPECS[family]
        expected = family + "_ratio" if spec.kind == "G" and spec.unit == "1" else family
        assert stored == expected, family

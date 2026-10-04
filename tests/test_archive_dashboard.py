"""The archive dashboard queries only series the deployment publishes, under the labels they really carry."""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest

from agent_history.metrics.otlp import HISTOGRAMS, SPECS

ROOT = Path(__file__).resolve().parents[1]
# A metric selector: a name followed by braces, where the braces may contain ${variable} references.
SELECTOR = re.compile(r"([a-zA-Z_:][a-zA-Z0-9_:]*)\{((?:\$\{[^}]*\}|[^{}])*)\}")
AGENT_HISTORY_FAMILY = re.compile(r"\bagent_(?:history|sessions|efficiency)_[a-zA-Z0-9_]+")
INDEX_JOB = 'job="agent-history-index"'
WORKER_JOBS = {
    'job="agent-history-index"',
    'job="agent-history-embed"',
    'job="agent-history-journal-sync"',
    'job=~"agent-history-index|agent-history-embed|agent-history-journal-sync"',
}
# Series that come from outside the indexer's collection, by name, with the job matchers they may use.
EXTERNAL = {
    "traces_spanmetrics_calls_total": WORKER_JOBS,
    # A native histogram: a `_bucket` selector on it reads nothing.
    "traces_spanmetrics_latency": WORKER_JOBS,
    "gen_ai_client_operation_duration_seconds_bucket": {'job="agent-history-embed"'},
    "gen_ai_client_token_usage_sum": {'job="agent-history-embed"'},
    "node_systemd_unit_state": {'job="agent-sessions"'},
}
# OTLP series carry only job, service_name and service_version plus their own attributes.
FORBIDDEN = ("instance=", "component=", "exported_instance=", "env=")
UNIT_SUFFIX = {"s": "seconds", "ms": "milliseconds", "By": "bytes", "%": "percent", "min": "minutes"}


def generator():
    spec = importlib.util.spec_from_file_location(
        "build_archive_dashboard", ROOT / "grafana" / "build_archive_dashboard.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve their module while the class is created
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def dashboard():
    return generator().build()


def stored(name: str) -> str:
    """The name Grafana Cloud stores an OTLP family under (docs/otel-design.md, backend identity).

    The unit word is appended unless the name already holds it as a token, then `_total` for a counter.
    """
    spec = SPECS[name]
    counter = spec.kind == "C" and name.endswith("_total")
    stem = name.removesuffix("_total") if counter else name
    suffix = "ratio" if spec.kind == "G" and spec.unit == "1" else UNIT_SUFFIX.get(spec.unit)
    if suffix and suffix not in stem.split("_"):
        stem += "_" + suffix
    return stem + "_total" if counter else stem


STORED = {stored(name): name for name in SPECS}
STORED.update({f"{name}{part}_total": name for name in HISTOGRAMS for part in ("_bucket", "_sum", "_count")})


def queries(dashboard):
    """(panel id, datasource group, query text) for every panel query and query variable."""
    for element in dashboard["spec"]["elements"].values():
        for query in element["spec"]["data"]["spec"]["queries"]:
            data = query["spec"]["query"]
            text = data["spec"]["query"] if data["group"] == "tempo" else data["spec"]["expr"]
            yield element["spec"]["id"], data["group"], text
    for variable in dashboard["spec"]["variables"]:
        if variable["kind"] == "QueryVariable":
            yield None, "prometheus", variable["spec"]["query"]["spec"]["query"]


def test_resource_is_the_gitsync_dashboard(dashboard):
    assert dashboard["apiVersion"] == "dashboard.grafana.app/v2"
    assert dashboard["kind"] == "Dashboard"
    assert dashboard["metadata"] == {
        "name": "agent-session-archive",
        "annotations": {"grafana.app/folder": "REPLACE_WITH_FOLDER_UID"},
    }


def test_panel_ids_are_unique_and_every_panel_is_laid_out(dashboard):
    elements = dashboard["spec"]["elements"]
    ids = [element["spec"]["id"] for element in elements.values()]
    assert len(ids) == len(set(ids))
    assert 148 not in ids  # retired with the v1.2 efficiency wave
    laid_out = [
        item["spec"]["element"]["name"]
        for tab in dashboard["spec"]["layout"]["spec"]["tabs"]
        for item in tab["spec"]["layout"]["spec"]["items"]
    ]
    assert sorted(laid_out) == sorted(elements)


def test_tabs_keep_their_order(dashboard):
    titles = [tab["spec"]["title"] for tab in dashboard["spec"]["layout"]["spec"]["tabs"]]
    assert titles == [
        "Overview",
        "Storage & archive",
        "Reliability",
        "Agent efficiency",
        "Pipeline",
        "Embedding provider",
    ]


def test_every_query_selects_an_agent_history_source(dashboard):
    checked = 0
    for panel_id, group, text in queries(dashboard):
        if group == "loki":
            assert re.search(r'service_name=~?"agent-history-', text), (panel_id, text)
            continue
        if group == "tempo":
            assert re.search(r'resource\.service\.name=~?"agent-history-', text), (panel_id, text)
            continue
        selectors = SELECTOR.findall(text)
        assert selectors, (panel_id, text)
        for name, body in selectors:
            checked += 1
            jobs = set(re.findall(r'job=~?"[^"]*"', body))
            allowed = EXTERNAL.get(name, {INDEX_JOB})
            assert len(jobs) == 1 and jobs <= allowed, (panel_id, name, body)
            if name.startswith("node_systemd_"):
                continue  # node exporter series keep their scrape labels
            for label in FORBIDDEN:
                assert label not in body, (panel_id, name, body)
    assert checked > 150


def test_every_agent_history_metric_is_a_published_family(dashboard):
    # A typo, a retired family or a missing OTLP unit suffix renders an empty panel.
    for panel_id, group, text in queries(dashboard):
        if group != "prometheus":
            continue
        for name, _ in SELECTOR.findall(text):
            assert name in STORED or name in EXTERNAL, (panel_id, name)
        for name in AGENT_HISTORY_FAMILY.findall(text):
            assert name in STORED, (panel_id, name)


def test_efficiency_queries_follow_the_loop_label_where_the_family_carries_it(dashboard):
    loop_filter = 'loop=~"${loop:pipe}"'
    checked = 0
    for panel_id, group, text in queries(dashboard):
        if panel_id is None or group != "prometheus":
            continue
        for name, body in SELECTOR.findall(text):
            if not name.startswith("agent_efficiency_"):
                continue
            family = STORED[name]
            labelled = family in HISTOGRAMS or "loop" in SPECS[family].attributes
            if labelled:
                checked += 1
                assert loop_filter in body, (panel_id, name, body)
            else:
                assert "loop" not in body, (panel_id, name, body)
    assert checked > 50


def test_loop_variable_defaults_to_all_and_keeps_unlabelled_history(dashboard):
    loop = next(v for v in dashboard["spec"]["variables"] if v["spec"]["name"] == "loop")
    spec = loop["spec"]
    assert loop["kind"] == "QueryVariable"
    assert spec["includeAll"] and spec["multi"]
    assert spec["allValue"] == ".*"  # .+ would drop series from before the label existed
    assert spec["current"] == {"text": "All", "value": "$__all"}
    assert spec["query"]["spec"]["query"].startswith("label_values(agent_efficiency_")


def element(dashboard, panel_id):
    return next(e["spec"] for e in dashboard["spec"]["elements"].values() if e["spec"]["id"] == panel_id)


def test_stalled_loop_companion_panel_carries_the_alert_thresholds(dashboard):
    companion = element(dashboard, 167)
    assert companion["vizConfig"]["group"] == "timeseries"
    expr = companion["data"]["spec"]["queries"][0]["spec"]["query"]["spec"]["expr"]
    assert "agent_efficiency_root_no_lane_seconds" in expr and "by(namespace)" in expr.replace(" ", "")
    steps = companion["vizConfig"]["spec"]["fieldConfig"]["defaults"]["thresholds"]["steps"]
    assert [step["value"] for step in steps if step["value"] is not None] == [900, 1200]


def test_protocol_poll_share_stats_compare_v2_1_against_everything_else(dashboard):
    exprs = [
        query["spec"]["query"]["spec"]["expr"]
        for panel_id in (162, 163)
        for query in element(dashboard, panel_id)["data"]["spec"]["queries"]
    ]
    assert 'protocol="v2.1"' in exprs[0] and 'protocol!="v2.1"' in exprs[1]


def test_agent_changes_annotation_reads_the_grafana_tag(dashboard):
    (annotation,) = [a for a in dashboard["spec"]["annotations"] if a["spec"]["name"] == "Agent changes"]
    spec = annotation["spec"]
    assert spec["enable"]
    # Dashboard v2 strict decoding rejects spec.datasource; the datasource lives on the DataQuery.
    assert "datasource" not in spec
    query = spec["query"]
    assert (query["kind"], query["group"], query["datasource"]) == ("DataQuery", "grafana", {"name": "-- Grafana --"})
    assert query["spec"]["tags"] == ["agent-change"] and query["spec"]["type"] == "tags"
    assert not query["spec"]["matchAny"]

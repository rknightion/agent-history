#!/usr/bin/env python3
"""Generate the agent session archive Grafana dashboard (Dashboard v2, GitSync resource shape).

Edit this file, then run `python3 grafana/build_archive_dashboard.py`; `--check` fails on drift.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "dashboards" / "agent-session-archive.json"
FOLDER = "REPLACE_WITH_FOLDER_UID"

DATASOURCE = "${datasource}"
# Every agent-history family is exported over OTLP by the indexer (service.name agent-history-index),
# which Grafana Cloud maps to the Prometheus job label. OTLP series carry no instance or component label.
SELECTOR = 'job="agent-history-index"'
# The archive and rrsync systemd units still come from the Fleet pipeline's unix exporter.
SYSTEMD_SELECTOR = 'job="agent-sessions",instance="camden"'
# The workers' traces and logs, and the series derived from them, carry the worker's own service name.
LOKI = "grafanacloud-logs"
TEMPO = "grafanacloud-traces"
EMBED_SELECTOR = 'job="agent-history-embed"'
JOURNAL_SELECTOR = 'job="agent-history-journal-sync"'
WORKER_JOBS = 'job=~"agent-history-index|agent-history-embed|agent-history-journal-sync"'
WORKER_STREAMS = '{service_name=~"agent-history-(index|embed|journal-sync)"}'
# Tempo's span metrics: the latency series is a native histogram (no _bucket suffix).
CALLS = "traces_spanmetrics_calls_total"
LATENCY = "traces_spanmetrics_latency"
SPAN_ERROR = 'status_code="STATUS_CODE_ERROR"'
PASSES = f'{WORKER_JOBS},span_name=~"index.pass|postpass.pass|embed.pass|journal_sync.pass"'
# Each worker passes every few minutes and its spans and GenAI requests arrive in bursts, so a
# $__rate_interval window is mostly empty; worker rates and quantiles use this rolling window.
BURST = "30m"


def prom_query(expr: str, legend: str = "", *, instant: bool = False, ref_id: str = "A") -> dict[str, Any]:
    return {
        "kind": "PanelQuery",
        "spec": {
            "hidden": False,
            "query": {
                "datasource": {"name": DATASOURCE},
                "group": "prometheus",
                "kind": "DataQuery",
                "spec": {
                    "editorMode": "code",
                    "expr": expr,
                    "instant": instant,
                    "legendFormat": legend,
                    "range": not instant,
                },
                "version": "v0",
            },
            "refId": ref_id,
        },
    }


def loki_query(expr: str, legend: str = "", *, instant: bool = False, ref_id: str = "A") -> dict[str, Any]:
    return {
        "kind": "PanelQuery",
        "spec": {
            "hidden": False,
            "query": {
                "datasource": {"name": LOKI},
                "group": "loki",
                "kind": "DataQuery",
                "spec": {
                    "editorMode": "code",
                    "expr": expr,
                    "instant": instant,
                    "legendFormat": legend,
                    "queryType": "instant" if instant else "range",
                    "range": not instant,
                },
                "version": "v0",
            },
            "refId": ref_id,
        },
    }


def tempo_query(traceql: str, legend: str = "", *, instant: bool = False, ref_id: str = "A") -> dict[str, Any]:
    return {
        "kind": "PanelQuery",
        "spec": {
            "hidden": False,
            "query": {
                "datasource": {"name": TEMPO},
                "group": "tempo",
                "kind": "DataQuery",
                "spec": {"limit": 20, "query": traceql, "queryType": "traceql", "tableType": "traces"},
                "version": "v0",
            },
            "refId": ref_id,
        },
    }


QUERIES = {"prometheus": prom_query, "loki": loki_query, "tempo": tempo_query}


def thresholds(warning: float | None = None, critical: float | None = None, *, inverse: bool = False) -> dict[str, Any]:
    base = "green" if not inverse else "red"
    steps: list[dict[str, Any]] = [{"color": base, "value": None}]
    if warning is not None:
        steps.append({"color": "orange" if not inverse else "yellow", "value": warning})
    if critical is not None:
        steps.append({"color": "red" if not inverse else "green", "value": critical})
    return {"mode": "absolute", "steps": steps}


def viz_config(
    kind: str,
    unit: str,
    *,
    warning: float | None = None,
    critical: float | None = None,
    inverse: bool = False,
    decimals: int | None = None,
    stack: bool = False,
) -> dict[str, Any]:
    defaults: dict[str, Any] = {
        "color": {"mode": "thresholds" if kind in ("stat", "gauge") else "palette-classic"},
        "thresholds": thresholds(warning, critical, inverse=inverse),
        "unit": unit,
    }
    if decimals is not None:
        defaults["decimals"] = decimals
    if kind == "stat":
        options = {
            "colorMode": "background_solid",
            "graphMode": "area",
            "justifyMode": "auto",
            "orientation": "auto",
            "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
            "showPercentChange": False,
            "textMode": "auto",
            "wideLayout": True,
        }
    elif kind == "bargauge":
        options = {
            "displayMode": "gradient",
            "minVizHeight": 10,
            "minVizWidth": 0,
            "namePlacement": "auto",
            "orientation": "horizontal",
            "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
            "showUnfilled": True,
            "sizing": "auto",
            "valueMode": "color",
        }
    elif kind == "logs":
        options = {
            "dedupStrategy": "none",
            "enableLogDetails": True,
            "prettifyLogMessage": False,
            "showCommonLabels": False,
            "showLabels": True,
            "showTime": True,
            "sortOrder": "Descending",
            "wrapLogMessage": True,
        }
    elif kind == "table":
        defaults["custom"] = {"align": "auto", "cellOptions": {"type": "auto"}, "filterable": True, "inspect": False}
        options = {"cellHeight": "sm", "showHeader": True}
    elif kind == "gauge":
        options = {
            "minVizHeight": 75,
            "minVizWidth": 75,
            "orientation": "auto",
            "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
            "showThresholdLabels": False,
            "showThresholdMarkers": True,
            "sizing": "auto",
        }
    else:
        defaults["custom"] = {
            "axisBorderShow": False,
            "axisColorMode": "text",
            "axisLabel": "",
            "axisPlacement": "auto",
            "barAlignment": 0,
            "drawStyle": "line",
            "fillOpacity": 100 if stack else 18,
            "gradientMode": "opacity",
            "hideFrom": {"legend": False, "tooltip": False, "viz": False},
            "insertNulls": False,
            "lineInterpolation": "smooth",
            "lineWidth": 2,
            "pointSize": 4,
            "scaleDistribution": {"type": "linear"},
            "showPoints": "auto",
            "spanNulls": True,
            "stacking": {"group": "A", "mode": "percent" if stack else "none"},
            "thresholdsStyle": {"mode": "line" if warning is not None else "off"},
        }
        options = {
            "legend": {
                "calcs": ["lastNotNull", "max"],
                "displayMode": "table",
                "placement": "bottom",
                "showLegend": True,
                "sortBy": "Last *",
                "sortDesc": True,
            },
            "tooltip": {"hideZeros": False, "mode": "multi", "sort": "desc"},
        }
    return {
        "group": kind,
        "kind": "VizConfig",
        "spec": {
            "fieldConfig": {"defaults": defaults, "overrides": []},
            "options": options,
        },
        "version": "13.3.0",
    }


@dataclass
class Panel:
    name: str
    panel_id: int
    title: str
    expressions: list[tuple[str, str]]
    description: str
    kind: str = "timeseries"
    unit: str = "short"
    warning: float | None = None
    critical: float | None = None
    inverse: bool = False
    decimals: int | None = None
    instant: bool = False
    width: int = 12
    height: int = 8
    stack: bool = False
    source: str = "prometheus"

    def element(self) -> dict[str, Any]:
        queries = [
            QUERIES[self.source](expression, legend, instant=self.instant, ref_id=chr(65 + index))
            for index, (expression, legend) in enumerate(self.expressions)
        ]
        return {
            "kind": "Panel",
            "spec": {
                "data": {
                    "kind": "QueryGroup",
                    "spec": {"queries": queries, "queryOptions": {}, "transformations": []},
                },
                "description": self.description,
                "id": self.panel_id,
                "links": [],
                "title": self.title,
                "vizConfig": viz_config(
                    self.kind,
                    self.unit,
                    warning=self.warning,
                    critical=self.critical,
                    inverse=self.inverse,
                    decimals=self.decimals,
                    stack=self.stack,
                ),
            },
        }


@dataclass
class Dashboard:
    elements: dict[str, Any] = field(default_factory=dict)
    tabs: list[dict[str, Any]] = field(default_factory=list)

    def add_tab(self, title: str, panels: list[Panel]) -> None:
        items: list[dict[str, Any]] = []
        x = 0
        y = 0
        row_height = 0
        for panel in panels:
            if x + panel.width > 24:
                x = 0
                y += row_height
                row_height = 0
            self.elements[panel.name] = panel.element()
            items.append(
                {
                    "kind": "GridLayoutItem",
                    "spec": {
                        "element": {"kind": "ElementReference", "name": panel.name},
                        "height": panel.height,
                        "width": panel.width,
                        "x": x,
                        "y": y,
                    },
                }
            )
            x += panel.width
            row_height = max(row_height, panel.height)
        self.tabs.append(
            {
                "kind": "TabsLayoutTab",
                "spec": {
                    "layout": {"kind": "GridLayout", "spec": {"items": items}},
                    "title": title,
                },
            }
        )


def stat(
    name: str,
    panel_id: int,
    title: str,
    expr: str,
    description: str,
    unit: str = "short",
    warning: float | None = None,
    critical: float | None = None,
    *,
    inverse: bool = False,
    decimals: int | None = None,
    source: str = "prometheus",
) -> Panel:
    return Panel(
        name,
        panel_id,
        title,
        [(expr, title)],
        description,
        "stat",
        unit,
        warning,
        critical,
        inverse,
        decimals,
        True,
        4,
        4,
        source=source,
    )


def chart(
    name: str,
    panel_id: int,
    title: str,
    expressions: list[tuple[str, str]],
    description: str,
    unit: str = "short",
    *,
    width: int = 12,
    height: int = 8,
    stack: bool = False,
    warning: float | None = None,
    critical: float | None = None,
    source: str = "prometheus",
) -> Panel:
    return Panel(
        name,
        panel_id,
        title,
        expressions,
        description,
        unit=unit,
        width=width,
        height=height,
        stack=stack,
        warning=warning,
        critical=critical,
        source=source,
    )


def logs(name: str, panel_id: int, title: str, expr: str, description: str) -> Panel:
    return Panel(name, panel_id, title, [(expr, "")], description, "logs", width=24, height=10, source="loki")


def traces(name: str, panel_id: int, title: str, traceql: str, description: str) -> Panel:
    return Panel(name, panel_id, title, [(traceql, "")], description, "table", width=24, height=8, source="tempo")


def bar(
    name: str,
    panel_id: int,
    title: str,
    expr: str,
    legend: str,
    description: str,
    unit: str = "short",
    *,
    width: int = 12,
) -> Panel:
    return Panel(
        name, panel_id, title, [(expr, legend)], description, "bargauge", unit, instant=True, width=width, height=8
    )


def gauge(
    name: str,
    panel_id: int,
    title: str,
    expr: str,
    legend: str,
    description: str,
    unit: str = "short",
    warning: float | None = None,
    critical: float | None = None,
    *,
    width: int = 12,
) -> Panel:
    return Panel(
        name,
        panel_id,
        title,
        [(expr, legend)],
        description,
        "gauge",
        unit,
        warning,
        critical,
        instant=True,
        width=width,
        height=8,
    )


def variables() -> list[dict[str, Any]]:
    datasource = {
        "kind": "DatasourceVariable",
        "spec": {
            "allowCustomValue": True,
            "current": {"text": "Grafana Cloud Prometheus", "value": "grafanacloud-prom"},
            "hide": "dontHide",
            "includeAll": False,
            "label": "Data source",
            "multi": False,
            "name": "datasource",
            "options": [],
            "pluginId": "prometheus",
            "refresh": "onDashboardLoad",
            "regex": "(?!grafanacloud-usage|grafanacloud-ml-metrics).+",
            "skipUrlSync": False,
        },
    }
    query_variables = []
    for name, label, query, all_value, refresh in (
        (
            "namespace",
            "Namespace",
            'label_values(agent_sessions_storage_files{job="agent-history-index"}, namespace)',
            ".+",
            "onDashboardLoad",
        ),
        (
            "model",
            "Model",
            'label_values(agent_efficiency_model_calls_total{job="agent-history-index"}, model)',
            ".+",
            "onDashboardLoad",
        ),
        (
            "agent",
            "Agent",
            'label_values(agent_efficiency_spawns_total{job="agent-history-index"}, agent)',
            ".+",
            "onDashboardLoad",
        ),
        (
            "role",
            "Role",
            'label_values(agent_efficiency_active_threads{job="agent-history-index"}, role)',
            ".+",
            "onDashboardLoad",
        ),
        # Fan-out loop ("<repo>/<loop|wave><n>", or none) on the Agent efficiency tab. allValue .* keeps
        # series from before the label existed; the list follows the time range and namespace.
        (
            "loop",
            "Loop",
            'label_values(agent_efficiency_llm_calls_total{job="agent-history-index",namespace=~"${namespace:regex}"}, loop)',
            ".*",
            "onTimeRangeChanged",
        ),
    ):
        query_variables.append(
            {
                "kind": "QueryVariable",
                "spec": {
                    "allValue": all_value,
                    "allowCustomValue": True,
                    "current": {"text": "All", "value": "$__all"},
                    "hide": "dontHide",
                    "includeAll": True,
                    "label": label,
                    "multi": True,
                    "name": name,
                    "options": [],
                    "query": {
                        "datasource": {"name": DATASOURCE},
                        "group": "prometheus",
                        "kind": "DataQuery",
                        "spec": {"query": query, "refId": name},
                        "version": "v0",
                    },
                    "refresh": refresh,
                    "regex": "",
                    "regexApplyTo": "value",
                    "skipUrlSync": False,
                    "sort": "alphabeticalAsc",
                },
            }
        )
    window = {
        "kind": "CustomVariable",
        "spec": {
            "allowCustomValue": False,
            "current": {"text": "24 hours", "value": "24h"},
            "hide": "dontHide",
            "includeAll": False,
            "label": "Activity window",
            "multi": False,
            "name": "activity_window",
            "options": [
                {"selected": False, "text": "1 hour", "value": "1h"},
                {"selected": True, "text": "24 hours", "value": "24h"},
                {"selected": False, "text": "7 days", "value": "7d"},
                {"selected": False, "text": "30 days", "value": "30d"},
            ],
            "query": "1 hour : 1h, 24 hours : 24h, 7 days : 7d, 30 days : 30d",
            "skipUrlSync": False,
        },
    }
    return [datasource, *query_variables, window]


def build() -> dict[str, Any]:
    d = Dashboard()
    ns = 'namespace=~"${namespace:regex}"'
    model = 'model=~"${model:regex}"'
    agent_v = 'agent=~"${agent:regex}"'
    # Agent efficiency series carry `loop` (collector v1.4). allValue .* also matches series that predate the label.
    EFF = f'{SELECTOR},loop=~"${{loop:pipe}}"'
    role_v = 'role=~"${role:regex}"'

    d.add_tab(
        "Overview",
        [
            stat(
                "health",
                1,
                "Collector healthy",
                f"min(agent_sessions_metrics_collection_success_ratio{{{SELECTOR}}})",
                "All collection sections succeeded on the latest run.",
                "bool",
                1,
                1,
                inverse=True,
            ),
            stat(
                "hot_size",
                2,
                "Hot JSONL",
                f'sum(agent_sessions_storage_bytes{{{SELECTOR},tier="hot",{ns}}})',
                "Current hot-tier JSONL bytes for selected namespaces.",
                "bytes",
            ),
            stat(
                "cold_size",
                3,
                "Cold JSONL",
                f'sum(agent_sessions_storage_bytes{{{SELECTOR},tier="cold",{ns}}})',
                "Permanent main-tree JSONL bytes on Synology.",
                "bytes",
            ),
            stat(
                "archive_age",
                5,
                "Archive age",
                f"time() - agent_sessions_archive_receipt_timestamp_seconds{{{SELECTOR}}}",
                "Seconds since the latest successful cold archive receipt.",
                "s",
                90000,
                129600,
            ),
            chart(
                "tier_growth",
                7,
                "Hot and cold JSONL growth",
                [(f"sum by(tier) (agent_sessions_storage_bytes{{{SELECTOR},{ns}}})", "{{tier}}")],
                "Main-tree JSONL bytes by storage tier.",
                "bytes",
            ),
            chart(
                "namespace_growth",
                8,
                "JSONL by namespace",
                [
                    (
                        f"sum by(namespace,tier) (agent_sessions_storage_bytes{{{SELECTOR},{ns}}})",
                        "{{tier}} / {{namespace}}",
                    )
                ],
                "Storage distribution across profile namespaces.",
                "bytes",
            ),
        ],
    )

    d.add_tab(
        "Storage & archive",
        [
            stat(
                "nfs",
                20,
                "Cold NFS mounted",
                f"agent_sessions_cold_nfs_mounted_ratio{{{SELECTOR}}}",
                "Cold authority resolves to an NFS mount.",
                "bool",
                1,
                1,
                inverse=True,
            ),
            stat(
                "pending_files",
                21,
                "Files awaiting archive",
                f"agent_sessions_archive_pending_files{{{SELECTOR}}}",
                "Hot files absent from or larger than the current cold copy.",
                "short",
                1,
                100,
            ),
            stat(
                "pending_bytes",
                22,
                "Bytes awaiting archive",
                f"agent_sessions_archive_pending_bytes{{{SELECTOR}}}",
                "Hot bytes not represented in the current cold copy.",
                "bytes",
                1,
            ),
            stat(
                "receipts",
                23,
                "Archive receipts",
                f"agent_sessions_archive_receipts{{{SELECTOR}}}",
                "Retained successful archive receipts.",
                "short",
            ),
            stat(
                "versions_size",
                24,
                "Version history",
                f"agent_sessions_archive_version_bytes{{{SELECTOR}}}",
                "Storage occupied by superseded cold JSONL versions.",
                "bytes",
            ),
            stat(
                "eligible",
                25,
                "Past hot retention",
                f"agent_sessions_hot_retention_eligible_files{{{SELECTOR}}}",
                "Hot files older than 90 days; should clear after a successful archive run.",
                "short",
                1,
                100,
            ),
            chart(
                "storage_files",
                26,
                "Main-tree files by namespace",
                [
                    (
                        f"sum by(namespace,tier) (agent_sessions_storage_files{{{SELECTOR},{ns}}})",
                        "{{tier}} / {{namespace}}",
                    )
                ],
                "JSONL file count by tier and namespace.",
            ),
            chart(
                "storage_bytes",
                27,
                "Main-tree bytes by namespace",
                [
                    (
                        f"sum by(namespace,tier) (agent_sessions_storage_bytes{{{SELECTOR},{ns}}})",
                        "{{tier}} / {{namespace}}",
                    )
                ],
                "JSONL byte count by tier and namespace.",
                "bytes",
            ),
            chart(
                "newest_age",
                28,
                "Newest transcript age",
                [
                    (
                        f"time() - max by(namespace,tier) (agent_sessions_storage_newest_mtime_seconds{{{SELECTOR},{ns}}})",
                        "{{tier}} / {{namespace}}",
                    )
                ],
                "Age of the newest JSONL modification by namespace.",
                "s",
            ),
            chart(
                "oldest_age",
                29,
                "Oldest retained transcript age",
                [
                    (
                        f"time() - min by(namespace,tier) (agent_sessions_storage_oldest_mtime_seconds{{{SELECTOR},{ns}}})",
                        "{{tier}} / {{namespace}}",
                    )
                ],
                "Age of the oldest main-tree JSONL by namespace.",
                "s",
            ),
            chart(
                "filesystem_use",
                30,
                "Filesystem utilisation",
                [
                    (
                        f'100 * sum by(tier) (agent_sessions_filesystem_bytes{{{SELECTOR},kind="used"}}) / sum by(tier) (agent_sessions_filesystem_bytes{{{SELECTOR},kind="total"}})',
                        "{{tier}}",
                    )
                ],
                "Filesystem capacity used by the underlying hot and cold volumes.",
                "percent",
            ),
            chart(
                "filesystem_free",
                31,
                "Filesystem available bytes",
                [(f'sum by(tier) (agent_sessions_filesystem_bytes{{{SELECTOR},kind="available"}})', "{{tier}}")],
                "Bytes available to an unprivileged writer on each backing filesystem.",
                "bytes",
            ),
            chart(
                "version_history",
                32,
                "Cold version history growth",
                [
                    (f"agent_sessions_archive_version_bytes{{{SELECTOR}}}", "bytes"),
                    (f"agent_sessions_archive_version_files{{{SELECTOR}}}", "files"),
                ],
                "Superseded file history retained permanently on Synology.",
            ),
            chart(
                "receipt_snapshot",
                33,
                "Archive tier inventory",
                [
                    (f"agent_sessions_archive_bytes{{{SELECTOR}}}", "bytes / {{tier}}"),
                    (f"agent_sessions_archive_files{{{SELECTOR}}}", "files / {{tier}}"),
                ],
                "File and byte totals by archive tier.",
            ),
            # Archive receipt, version history and capacity detail. New ids start at 260.
            stat(
                "receipt_jsonl_files",
                260,
                "Receipt JSONL files",
                f"agent_sessions_archive_receipt_jsonl_files{{{SELECTOR}}}",
                "JSONL file count recorded by the latest archive receipt.",
                "short",
            ),
            stat(
                "receipt_jsonl_bytes",
                261,
                "Receipt JSONL bytes",
                f"agent_sessions_archive_receipt_jsonl_bytes{{{SELECTOR}}}",
                "JSONL byte count recorded by the latest archive receipt.",
                "bytes",
            ),
            stat(
                "version_snapshots",
                262,
                "Version snapshots",
                f"agent_sessions_archive_version_snapshots{{{SELECTOR}}}",
                "Version snapshot directories retained on the cold tier.",
                "short",
            ),
            stat(
                "version_newest_age",
                263,
                "Newest version age",
                f"time() - agent_sessions_archive_version_newest_mtime_seconds{{{SELECTOR}}}",
                "Seconds since the newest superseded JSONL version was written; it moves only when an archive run replaces a changed cold file.",
                "s",
            ),
            stat(
                "eligible_bytes",
                264,
                "Bytes past hot retention",
                f"agent_sessions_hot_retention_eligible_bytes{{{SELECTOR}}}",
                "Hot bytes older than 90 days; should clear after a successful archive run.",
                "bytes",
                1,
            ),
            stat(
                "archive_tiers_up",
                265,
                "Archive tiers mounted",
                f"sum(agent_sessions_archive_available_ratio{{{SELECTOR}}})",
                "Archive tiers (hot, cold, incoming, conflicts) mounted on the latest collection; a tier this deployment does not use stays at 0 on the availability chart.",
                "short",
            ),
            chart(
                "archive_available",
                266,
                "Archive tier availability",
                [(f"agent_sessions_archive_available_ratio{{{SELECTOR}}}", "{{tier}}")],
                "Whether each archive tier is mounted (1) or missing (0).",
            ),
            chart(
                "root_available",
                267,
                "Storage root availability",
                [(f"agent_sessions_storage_root_available_ratio{{{SELECTOR}}}", "{{tier}}")],
                "Whether each configured session storage root is an available real directory (1) or not (0).",
            ),
            chart(
                "inode_use",
                268,
                "Filesystem inode utilisation",
                [
                    (
                        f'100 * (1 - sum by(tier) (agent_sessions_filesystem_inodes{{{SELECTOR},kind="free"}}) / (sum by(tier) (agent_sessions_filesystem_inodes{{{SELECTOR},kind="total"}}) > 0))',
                        "{{tier}}",
                    )
                ],
                "Share of inodes used on the filesystems backing the session tiers; a filesystem that reports no inode counts, such as an NFS mount, is left out.",
                "percent",
                width=24,
            ),
        ],
    )

    d.add_tab(
        "Reliability",
        [
            stat(
                "collector_freshness",
                60,
                "Collector freshness",
                f"time() - agent_sessions_metrics_last_success_timestamp_seconds{{{SELECTOR}}}",
                "Alert companion: seconds since all collector sections succeeded.",
                "s",
                600,
                900,
            ),
            stat(
                "cold_mount_health",
                67,
                "Cold NFS health",
                f"agent_sessions_cold_nfs_mounted_ratio{{{SELECTOR}}}",
                "Alert companion: cold authority must remain on NFS.",
                "bool",
                1,
                1,
                inverse=True,
            ),
            stat(
                "archive_freshness",
                65,
                "Archive freshness",
                f"time() - agent_sessions_archive_receipt_timestamp_seconds{{{SELECTOR}}}",
                "Alert companion: daily archive receipt age.",
                "s",
                90000,
                129600,
            ),
            chart(
                "section_health",
                61,
                "Collector section health",
                [(f"agent_sessions_metrics_section_success_ratio{{{SELECTOR}}}", "{{section}}")],
                "Latest success result for the indexer's metrics collection sections.",
            ),
            chart(
                "section_duration",
                62,
                "Collector section duration",
                [(f"agent_sessions_metrics_section_duration_seconds{{{SELECTOR}}}", "{{section}}")],
                "Per-section runtime; storage includes NFS metadata traversal.",
                "s",
            ),
            chart(
                "unit_results",
                63,
                "Systemd unit failures",
                [(f'node_systemd_unit_state{{{SYSTEMD_SELECTOR},state="failed"}}', "{{name}}")],
                "Failure state for the archive and receiver units from Alloy.",
            ),
            chart(
                "unit_state",
                64,
                "Systemd unit states",
                [(f"node_systemd_unit_state{{{SYSTEMD_SELECTOR}}}", "{{name}} / {{state}}")],
                "Current archive and receiver unit states from Alloy.",
            ),
            chart(
                "hot_pressure",
                70,
                "Hot filesystem pressure",
                [
                    (
                        f'100 * sum(agent_sessions_filesystem_bytes{{{SELECTOR},tier="hot",kind="used"}}) / sum(agent_sessions_filesystem_bytes{{{SELECTOR},tier="hot",kind="total"}})',
                        "hot used",
                    )
                ],
                "Alert companion: utilisation of the filesystem backing `/opt`.",
                "percent",
            ),
            chart(
                "cold_pressure",
                71,
                "Cold filesystem pressure",
                [
                    (
                        f'100 * sum(agent_sessions_filesystem_bytes{{{SELECTOR},tier="cold",kind="used"}}) / sum(agent_sessions_filesystem_bytes{{{SELECTOR},tier="cold",kind="total"}})',
                        "cold used",
                    )
                ],
                "Utilisation of the Synology volume backing the permanent archive.",
                "percent",
            ),
            chart(
                "collector_failures",
                74,
                "Collector failure increase",
                [(f"increase(agent_sessions_metrics_collection_failures_total{{{SELECTOR}}}[$__range])", "failures")],
                "Failed full collection attempts during the dashboard time range.",
            ),
            chart(
                "collection_runtime",
                75,
                "Full collection runtime",
                [(f"agent_sessions_metrics_collection_duration_seconds{{{SELECTOR}}}", "duration")],
                "Total metrics collection runtime.",
                "s",
            ),
            chart(
                "timer_health",
                77,
                "Archive timer activation state",
                [
                    (
                        f'node_systemd_unit_state{{{SYSTEMD_SELECTOR},name="agent-session-archive.timer",state="active"}}',
                        "{{name}}",
                    )
                ],
                "Alert companion: the archive timer must remain active.",
            ),
            stat(
                "scrape_up",
                78,
                "Metrics export live",
                f"clamp_max(count(agent_sessions_metrics_last_success_timestamp_seconds{{{SELECTOR}}}), 1) or vector(0)",
                "Whether the indexer's OTLP metrics are arriving. OTLP has no scrape health series, so this is series presence.",
                "bool",
                1,
                1,
                inverse=True,
            ),
            # Collection run, per-collector and build detail. New ids start at 280.
            chart(
                "collection_runs",
                280,
                "Collection runs per minute",
                [(f"60 * rate(agent_sessions_metrics_collection_runs_total{{{SELECTOR}}}[$__rate_interval])", "runs")],
                "Completed metrics collection runs per minute; a flat zero means the collection loop has stopped.",
                "short",
            ),
            chart(
                "section_last_success",
                281,
                "Section last success age",
                [
                    (
                        f"time() - agent_sessions_metrics_section_last_success_timestamp_seconds{{{SELECTOR}}}",
                        "{{section}}",
                    )
                ],
                "Seconds since each collection section last succeeded.",
                "s",
            ),
            chart(
                "collector_duration",
                282,
                "Collector duration",
                [(f"agent_history_exporter_collection_duration_seconds{{{SELECTOR}}}", "{{collector}}")],
                "Latest runtime of each collector.",
                "s",
            ),
            chart(
                "collector_errors",
                283,
                "Collector errors",
                [
                    (
                        f"sum by(collector) (increase(agent_history_exporter_collection_errors_total{{{SELECTOR}}}[$__rate_interval]))",
                        "{{collector}}",
                    )
                ],
                "Alert companion: collector failures per interval; a rise fires the section failing alert even when the section reports success.",
            ),
            chart(
                "source_status",
                284,
                "Catalogue source files by status",
                [(f"agent_history_sources{{{SELECTOR}}}", "{{status}}")],
                "Source transcript files in the catalogue by indexing status; error and partial should stay at or near zero.",
            ),
            chart(
                "build_info",
                285,
                "Collector version",
                [(f"max by(version) (agent_sessions_metrics_build_info_ratio{{{SELECTOR}}})", "{{version}}")],
                "The running indexer version; a change in series marks a deployment.",
            ),
        ],
    )

    d.add_tab(
        "Agent efficiency",
        [
            stat(
                "eff_poll_share_stat",
                120,
                "Poll share (root)",
                f'100 * sum(increase(agent_efficiency_llm_calls_total{{{EFF},role="root",trigger=~"wait|status|noop",{ns},{agent_v}}}[$__range])) / sum(increase(agent_efficiency_llm_calls_total{{{EFF},role="root",{ns},{agent_v}}}[$__range]))',
                "Share of root LLM calls whose trigger is wait, status or noop. Status includes ordinary checks such as git or Backlog; this is broader than idle waiting. Pi push-event handling is event. This counts calls, not wall time; good is under 20%.",
                "percent",
                20,
                35,
            ),
            stat(
                "eff_wasted_tokens_stat",
                121,
                "Wasted context tokens (uncached)",
                f'sum(increase(agent_efficiency_input_tokens_total{{{EFF},role="root",trigger=~"wait|status|noop",cache="miss",{ns},{agent_v}}}[$__range]))',
                "Uncached root input tokens spent on poll/status/noop calls over the range; good is close to zero.",
                "short",
            ),
            stat(
                "eff_root_calls_stat",
                122,
                "Root LLM calls",
                f'sum(increase(agent_efficiency_llm_calls_total{{{EFF},role="root",{ns},{agent_v}}}[$__range]))',
                "Root-thread model calls over the selected range.",
                "short",
            ),
            stat(
                "eff_spawns_stat",
                123,
                "Agents spawned",
                f"sum(increase(agent_efficiency_spawns_total{{{EFF},{ns},{agent_v}}}[$__range]))",
                "Subagents spawned over the selected range.",
                "short",
            ),
            stat(
                "eff_calls_per_spawn_stat",
                124,
                "Root calls per spawn",
                f'sum(increase(agent_efficiency_llm_calls_total{{{EFF},role="root",{ns},{agent_v}}}[$__range])) / sum(increase(agent_efficiency_spawns_total{{{EFF},{ns},{agent_v}}}[$__range]))',
                "Root model calls needed per spawned agent; lower means less orchestration overhead per delegation.",
                "short",
                decimals=1,
            ),
            stat(
                "eff_turn_errors_stat",
                125,
                "Turn errors",
                f"sum(increase(agent_efficiency_turn_errors_total{{{EFF},{ns},{agent_v}}}[$__range]))",
                "Turns ending in error, abort, or upstream failure over the range; good is zero.",
                "short",
                1,
                5,
            ),
            chart(
                "eff_poll_share_ts",
                126,
                "Poll share over time",
                [
                    (
                        f'100 * sum(rate(agent_efficiency_llm_calls_total{{{EFF},role="root",trigger=~"wait|status|noop",{ns},{agent_v}}}[$__rate_interval])) / sum(rate(agent_efficiency_llm_calls_total{{{EFF},role="root",{ns},{agent_v}}}[$__rate_interval]))',
                        "poll share",
                    )
                ],
                "Rolling share of root calls triggered by wait, status or noop. Status includes ordinary checks, so this is broader than idle waiting. Pi push-event handling is event; good stays under 20%.",
                "percent",
            ),
            chart(
                "eff_active_threads_role",
                127,
                "Active threads by role",
                [(f"sum by(role) (agent_efficiency_active_threads{{{EFF},{ns},{agent_v},{role_v}}})", "{{role}}")],
                "Threads with a model call in the last 10 minutes, split root vs worker.",
            ),
            chart(
                "eff_worker_root_ratio",
                128,
                "Worker/root active-thread ratio",
                [
                    (
                        f'sum(agent_efficiency_active_threads{{{EFF},role="worker",{ns},{agent_v}}}) / sum(agent_efficiency_active_threads{{{EFF},role="root",{ns},{agent_v}}})',
                        "worker/root",
                    )
                ],
                "Active workers per active root; good is at or above 3.",
            ),
            chart(
                "eff_active_roots_idle",
                129,
                "Active roots with idle workers",
                [(f"sum(agent_efficiency_active_roots_with_idle_workers{{{EFF},{ns},{agent_v}}})", "idle roots")],
                "Active roots with zero active workers in the last 10 minutes; good is close to zero.",
            ),
            chart(
                "eff_root_time_state",
                130,
                "Root time by state",
                [
                    (
                        f'sum by(state) (rate(agent_efficiency_time_seconds_total{{{EFF},role="root",state!~"idle|gap",{ns},{agent_v}}}[$__rate_interval]))',
                        "{{state}}",
                    )
                ],
                "Share of root wall time by activity state, excluding idle and gap; good is dominated by model/tool_work over tool_wait/tool_status/tool_noop.",
                "percentunit",
                stack=True,
            ),
            bar(
                "eff_poll_calls_target",
                131,
                "Poll calls by target",
                f"sum by(target) (increase(agent_efficiency_poll_calls_total{{{EFF},{ns},{agent_v},{role_v}}}[$__range]))",
                "{{target}}",
                "Poll/wait tool calls over the range broken out by what they waited on.",
                "short",
            ),
            bar(
                "eff_poll_seconds_target",
                132,
                "Poll seconds by target",
                f"sum by(target) (increase(agent_efficiency_poll_seconds_total{{{EFF},{ns},{agent_v},{role_v}}}[$__range]))",
                "{{target}}",
                "Wall seconds blocked in poll/wait tool calls over the range, by target; good is dominated by ci/gate rather than sleep.",
                "s",
            ),
            chart(
                "eff_wasted_tokens_cache",
                133,
                "Wasted context tokens by cache",
                [
                    (
                        f'sum by(cache) (rate(agent_efficiency_input_tokens_total{{{EFF},role="root",trigger=~"wait|status|noop",{ns},{agent_v}}}[$__rate_interval]))',
                        "{{cache}}",
                    )
                ],
                "Root poll/status/noop input-token burn rate split cached vs uncached; good is small and mostly hit.",
                "short",
            ),
            chart(
                "eff_model_latency",
                134,
                "Model latency by model and role",
                [
                    (
                        f'sum by(model,role) (rate(agent_efficiency_model_seconds_total{{{EFF},role=~"root|worker",{ns},{agent_v},{model}}}[$__rate_interval])) / sum by(model,role) (rate(agent_efficiency_model_calls_total{{{EFF},role=~"root|worker",{ns},{agent_v},{model}}}[$__rate_interval]))',
                        "{{model}} / {{role}}",
                    )
                ],
                "Average model call latency by model, split root vs worker; watch for per-model regressions.",
                "s",
            ),
            chart(
                "eff_context_tokens_pct",
                135,
                "Context tokens per call (p50/p90) by role",
                [
                    (
                        f'avg by(role) (agent_efficiency_context_tokens{{{EFF},quantile="0.5",{ns},{agent_v}}})',
                        "p50 / {{role}}",
                    ),
                    (
                        f'avg by(role) (agent_efficiency_context_tokens{{{EFF},quantile="0.9",{ns},{agent_v}}})',
                        "p90 / {{role}}",
                    ),
                ],
                "Input tokens per model call, median and 90th percentile, by role; good is p90 not far above p50.",
                "short",
            ),
            chart(
                "eff_compactions",
                136,
                "Compaction rate",
                [
                    (
                        f"sum by(role) (rate(agent_efficiency_compactions_total{{{EFF},{ns},{agent_v}}}[$__rate_interval]))",
                        "{{role}}",
                    )
                ],
                "Context compactions per second by role; good is near zero outside very long threads.",
                "short",
            ),
            chart(
                "eff_turn_errors_kind",
                137,
                "Turn errors by kind",
                [
                    (
                        f"sum by(kind) (increase(agent_efficiency_turn_errors_total{{{EFF},{ns},{agent_v}}}[$__rate_interval]))",
                        "{{kind}}",
                    )
                ],
                "Turn errors per interval by kind; good is zero across all kinds.",
                "short",
            ),
            # v1.2 additions (efficiency-seam.md "v1.2 additions"). Routing group.
            stat(
                "eff_fork_all_stat",
                140,
                "Full-history forks",
                f'sum(increase(agent_efficiency_spawns_by_route_total{{{EFF},fork="all",{ns},{agent_v}}}[$__range]))',
                "Spawns that requested full conversation history via fork=all; good is zero across the range.",
                "short",
                1,
                5,
            ),
            bar(
                "eff_spawns_route_bar",
                141,
                "Spawns by model and effort",
                f"sum by(spawn_model,effort) (increase(agent_efficiency_spawns_by_route_total{{{EFF},{ns},{agent_v}}}[$__range]))",
                "{{spawn_model}} / {{effort}}",
                "Spawned agents over the range grouped by requested model and reasoning effort; watch for routes outside the agreed contract combinations.",
            ),
            bar(
                "eff_spawns_agent_type_bar",
                142,
                "Spawns by agent type",
                f"sum by(agent_type) (increase(agent_efficiency_spawns_by_route_total{{{EFF},{ns},{agent_v}}}[$__range]))",
                "{{agent_type}}",
                "Spawned agents over the range grouped by the recorded requested type. Pi async launches use the explicit agent argument; no type is inferred from lane labels.",
            ),
            chart(
                "eff_luna_gate_share",
                143,
                "Luna max vs high effort share (GATE trial)",
                [
                    (
                        f'sum by(effort) (rate(agent_efficiency_spawns_by_route_total{{{EFF},spawn_model=~".*luna.*",effort=~"max|high",agent_type="gate-runner",{ns}}}[$__rate_interval]))',
                        "{{effort}}",
                    )
                ],
                "Share of Luna GATE-lane spawns using max vs high reasoning effort during the trial; watch for high effort's share growing beyond the agreed trial allocation.",
                "percentunit",
                stack=True,
            ),
            chart(
                "eff_spawn_errors_kind",
                144,
                "Spawn errors by kind",
                [
                    (
                        f"sum by(kind) (rate(agent_efficiency_spawn_errors_total{{{EFF},{ns},{agent_v}}}[$__rate_interval]))",
                        "{{kind}}",
                    )
                ],
                "Failed spawn attempts per second by failure kind; good is at or near zero across all kinds.",
                "short",
            ),
            # Quota group.
            gauge(
                "eff_quota_used_gauge",
                145,
                "Codex rate-limit used %",
                f"agent_efficiency_rate_limit_used_percent{{{SELECTOR},{ns}}}",
                "{{namespace}} / {{window}}",
                "Latest Codex rate-limit consumption per namespace and window; account-wide, so not filtered by loop. Good stays under 70%.",
                "percent",
                70,
                85,
            ),
            stat(
                "eff_quota_reset_stat",
                146,
                "Time to nearest quota reset",
                f"min(agent_efficiency_rate_limit_resets_at_seconds{{{SELECTOR},{ns}}} - time())",
                "Seconds until the soonest Codex rate-limit window reset across selected namespaces; account-wide, so not filtered by loop. A low value ahead of a busy period is expected.",
                "s",
            ),
            chart(
                "eff_quota_used_ts",
                147,
                "Codex quota used % over time",
                [(f"agent_efficiency_rate_limit_used_percent{{{SELECTOR},{ns}}}", "{{namespace}} / {{window}}")],
                "Codex rate-limit usage trend per namespace and window; account-wide, so not filtered by loop. Good stays clear of the 85% alert threshold.",
                "percent",
            ),
            # Delivery group.
            stat(
                "eff_ci_red_rate_stat",
                149,
                "CI red rate",
                f'100 * sum(increase(agent_efficiency_ci_waits_total{{{EFF},outcome="failure",{ns},{agent_v}}}[$__range])) / sum(increase(agent_efficiency_ci_waits_total{{{EFF},{ns},{agent_v}}}[$__range]))',
                "Share of watched CI waits that ended in failure over the range; good stays low and trending down.",
                "percent",
                20,
                40,
            ),
            stat(
                "eff_findings_per_lane_stat",
                150,
                "Critical+major findings per spawned lane",
                f'sum(increase(agent_efficiency_coderabbit_findings_total{{{EFF},severity=~"critical|major",{ns},{agent_v}}}[$__range])) / sum(increase(agent_efficiency_spawns_total{{{EFF},{ns},{agent_v}}}[$__range]))',
                "Critical and major CodeRabbit findings per agent spawned over the range; good trends toward zero.",
                "short",
                decimals=2,
            ),
            chart(
                "eff_pushes_outcome",
                151,
                "Pushes by outcome",
                [
                    (
                        f"sum by(outcome) (rate(agent_efficiency_git_pushes_total{{{EFF},{ns},{agent_v}}}[$__rate_interval]))",
                        "{{outcome}}",
                    )
                ],
                "git push attempts per second by exit outcome; good is almost entirely success.",
            ),
            chart(
                "eff_ci_waits_outcome",
                152,
                "CI waits by outcome",
                [
                    (
                        f"sum by(outcome) (rate(agent_efficiency_ci_waits_total{{{EFF},{ns},{agent_v}}}[$__rate_interval]))",
                        "{{outcome}}",
                    )
                ],
                "Watched CI run terminations per second by outcome; good is dominated by success over failure/cancelled.",
            ),
            chart(
                "eff_gate_runs_rate",
                153,
                "Gate run pass/fail rate",
                [
                    (
                        f'sum by(outcome) (rate(agent_efficiency_gate_runs_total{{{EFF},outcome=~"success|failure",{ns},{agent_v}}}[$__rate_interval]))',
                        "{{outcome}}",
                    )
                ],
                "Share of gate command runs (just check/test/ci, make check/test) passing vs failing; good stays dominated by success.",
                "percentunit",
                stack=True,
            ),
            chart(
                "eff_coderabbit_findings_severity",
                154,
                "CodeRabbit findings by severity",
                [
                    (
                        f"sum by(severity) (rate(agent_efficiency_coderabbit_findings_total{{{EFF},{ns},{agent_v}}}[$__rate_interval]))",
                        "{{severity}}",
                    )
                ],
                "CodeRabbit findings per second by severity; good is dominated by minor/trivial/info over critical/major.",
            ),
            chart(
                "eff_coderabbit_reviews_outcome",
                155,
                "CodeRabbit reviews by outcome",
                [
                    (
                        f"sum by(outcome) (rate(agent_efficiency_coderabbit_reviews_total{{{EFF},{ns},{agent_v}}}[$__rate_interval]))",
                        "{{outcome}}",
                    )
                ],
                "CodeRabbit review runs per second by outcome; watch for a rising rate_limited share.",
            ),
            # Friction group.
            chart(
                "eff_context_fill_role",
                156,
                "Context fill ratio p50/p90 by role",
                [
                    (
                        f'avg by(role) (agent_efficiency_context_fill_ratio{{{EFF},quantile="0.5",{ns},{agent_v},{role_v}}})',
                        "p50 / {{role}}",
                    ),
                    (
                        f'avg by(role) (agent_efficiency_context_fill_ratio{{{EFF},quantile="0.9",{ns},{agent_v},{role_v}}})',
                        "p90 / {{role}}",
                    ),
                ],
                "Input tokens as a share of the model's context window, median and 90th percentile, by role; good stays clear of the 0.8 threshold.",
                "percentunit",
                warning=0.8,
            ),
            chart(
                "eff_tool_failure_rate_class",
                157,
                "Tool failure rate by class",
                [
                    (
                        f"sum by(class) (rate(agent_efficiency_tool_failures_total{{{EFF},{ns},{agent_v},{role_v}}}[$__rate_interval])) / sum by(class) (rate(agent_efficiency_tool_calls_total{{{EFF},{ns},{agent_v},{role_v}}}[$__rate_interval]))",
                        "{{class}}",
                    )
                ],
                "Tool calls ending in failure divided by tool calls issued, by class; good stays near zero.",
                "percentunit",
            ),
            chart(
                "eff_interventions_kind",
                158,
                "Interventions by kind",
                [
                    (
                        f"sum by(kind) (rate(agent_efficiency_interventions_total{{{EFF},{ns},{agent_v}}}[$__rate_interval]))",
                        "{{kind}}",
                    )
                ],
                "Human messages into a running thread and user-triggered aborts per second, by kind; good stays low for unattended runs.",
            ),
            bar(
                "eff_lane_duration_buckets",
                159,
                "Lane duration histogram buckets",
                f"sum by(le) (increase(agent_efficiency_lane_seconds_bucket_total{{{EFF},{ns},{agent_v}}}[$__range]))",
                "{{le}}",
                "Worker lane lifetimes over the range bucketed by upper bound in seconds; good has most lanes in the lower buckets.",
            ),
            chart(
                "eff_first_spawn_latency",
                160,
                "First-spawn latency p50/p90",
                [
                    (
                        f"histogram_quantile(0.5, sum by(le) (increase(agent_efficiency_first_spawn_seconds_bucket_total{{{EFF},{ns},{agent_v}}}[6h])))",
                        "p50",
                    ),
                    (
                        f"histogram_quantile(0.9, sum by(le) (increase(agent_efficiency_first_spawn_seconds_bucket_total{{{EFF},{ns},{agent_v}}}[6h])))",
                        "p90",
                    ),
                ],
                "Time from a root thread starting to its first spawn, median and 90th percentile; good is short and stable.",
                "s",
            ),
            # v1.3 additions (efficiency-seam.md "v1.3 additions"). Protocol group. id 148 was
            # deleted in v1.2 and stays retired; new ids start at 161.
            chart(
                "eff_protocol_poll_share_ts",
                161,
                "Poll share by protocol",
                [
                    (
                        f'100 * sum by(protocol) (rate(agent_efficiency_root_llm_calls_by_protocol_total{{{EFF},poll="true",{ns},{agent_v}}}[$__rate_interval])) / sum by(protocol) (rate(agent_efficiency_root_llm_calls_by_protocol_total{{{EFF},{ns},{agent_v}}}[$__rate_interval]))',
                        "{{protocol}}",
                    )
                ],
                "Rolling share of root LLM calls spent polling, split by the contract protocol declared in the thread's first human prompt; good stays under 20% for every protocol.",
                "percent",
            ),
            stat(
                "eff_protocol_poll_share_v21_stat",
                162,
                "Poll share (v2.1)",
                f'100 * sum(increase(agent_efficiency_root_llm_calls_by_protocol_total{{{EFF},protocol="v2.1",poll="true",{ns},{agent_v}}}[$__range])) / sum(increase(agent_efficiency_root_llm_calls_by_protocol_total{{{EFF},protocol="v2.1",{ns},{agent_v}}}[$__range]))',
                "Poll share of root LLM calls from threads whose first prompt declared Contract: loop-v2.1, over the selected range.",
                "percent",
                20,
                35,
            ),
            stat(
                "eff_protocol_poll_share_other_stat",
                163,
                "Poll share (pre-v2.1 / other)",
                f'100 * sum(increase(agent_efficiency_root_llm_calls_by_protocol_total{{{EFF},protocol!="v2.1",poll="true",{ns},{agent_v}}}[$__range])) / sum(increase(agent_efficiency_root_llm_calls_by_protocol_total{{{EFF},protocol!="v2.1",{ns},{agent_v}}}[$__range]))',
                "Poll share of root LLM calls from every other declared protocol (v2.0, other, or none), over the selected range; compare against the v2.1 stat to see whether the newer contract actually cut polling.",
                "percent",
                20,
                35,
            ),
            bar(
                "eff_protocol_time_state_share_bar",
                164,
                "Time-by-state share by protocol",
                f"sum by(protocol,state) (increase(agent_efficiency_root_time_seconds_by_protocol_total{{{EFF},{ns},{agent_v}}}[$__range])) / on(protocol) group_left sum by(protocol) (increase(agent_efficiency_root_time_seconds_by_protocol_total{{{EFF},{ns},{agent_v}}}[$__range]))",
                "{{protocol}} / {{state}}",
                "Share of root wall time spent in each state over the selected range, broken out by declared protocol; good is dominated by model/tool_work over tool_wait/tool_status/tool_noop for every protocol.",
                "percentunit",
            ),
            # Loop stalls group.
            stat(
                "eff_roots_without_lanes_stat",
                165,
                "Roots with no lane in flight now",
                f"sum(agent_efficiency_roots_without_lanes{{{EFF},{ns},{agent_v}}})",
                "Live spawning roots with zero children in flight right now; good is close to zero.",
                "short",
            ),
            stat(
                "eff_root_no_lane_seconds_stat",
                166,
                "Longest no-lane stretch now",
                f"max(agent_efficiency_root_no_lane_seconds{{{EFF},{ns},{agent_v}}})",
                "Alert companion: the longest a live spawning root has currently gone with zero children in flight; AgentLoopRootStalled fires above 1200s.",
                "s",
                900,
                1200,
            ),
            chart(
                "eff_root_no_lane_seconds_ts",
                167,
                "No-lane stretch by namespace",
                [
                    (
                        f"max by(namespace) (agent_efficiency_root_no_lane_seconds{{{EFF},{ns},{agent_v}}})",
                        "{{namespace}}",
                    )
                ],
                "Alert companion: per-namespace max no-lane stretch over time, with a threshold line at the 1200s AgentLoopRootStalled trigger so the climb toward the alert is visible.",
                "s",
                warning=900,
                critical=1200,
            ),
            # Parser state and loop labelling group (account-wide, so not filtered by loop where the series has
            # no loop label). New ids start at 168.
            stat(
                "eff_malformed_stat",
                168,
                "Malformed records",
                f"sum(increase(agent_efficiency_malformed_records_total{{{SELECTOR},{ns},{agent_v}}}[$__range]))",
                "Complete transcript records the efficiency parser could not read over the range; each is skipped. Good is zero.",
                "short",
                1,
                10,
            ),
            stat(
                "eff_tracked_files_stat",
                169,
                "Tracked transcript files",
                f"agent_efficiency_tracked_files{{{SELECTOR}}}",
                "Transcript files held in the incremental efficiency parser state.",
                "short",
            ),
            stat(
                "eff_baseline_stat",
                170,
                "Efficiency baseline",
                f"agent_efficiency_baseline_timestamp_seconds{{{SELECTOR}}} * 1000",
                "Efficiency events before this time are never counted.",
                "dateTimeFromNow",
            ),
            stat(
                "eff_loop_map_loops_stat",
                171,
                "Loops in loop map",
                f"agent_efficiency_loop_map_loops{{{SELECTOR}}}",
                "Distinct loops in the catalogue loop map used for the loop label; zero when no map is available.",
                "short",
            ),
            stat(
                "eff_loop_map_age_stat",
                172,
                "Loop map age",
                f"agent_efficiency_loop_map_age_seconds{{{SELECTOR}}}",
                "Age of the catalogue loop map used for the loop label; absent while every thread reads none.",
                "s",
                1800,
                3600,
            ),
            stat(
                "eff_loop_labels_stat",
                173,
                "Loop labels carried",
                f"agent_efficiency_loop_labels{{{SELECTOR}}}",
                "Loop label values currently carried by the efficiency series, excluding none; a loop retires after its retention window with no counted event.",
                "short",
            ),
            chart(
                "eff_output_tokens_role",
                174,
                "Output tokens by role",
                [
                    (
                        f"sum by(role) (rate(agent_efficiency_output_tokens_total{{{EFF},{ns},{agent_v},{role_v}}}[$__rate_interval]))",
                        "{{role}}",
                    )
                ],
                "Model output tokens per second by role.",
                "short",
            ),
            bar(
                "eff_wait_requests_tool",
                175,
                "Wait requests by tool",
                f"sum by(tool) (increase(agent_efficiency_wait_requests_total{{{EFF},{ns},{agent_v},{role_v}}}[$__range]))",
                "{{tool}}",
                "Wait or sleep tool requests over the range by tool.",
                "short",
            ),
            bar(
                "eff_wait_timeout_tool",
                176,
                "Mean requested wait timeout by tool",
                f"sum by(tool) (increase(agent_efficiency_wait_timeout_ms_milliseconds_total{{{EFF},{ns},{agent_v},{role_v}}}[$__range])) / sum by(tool) (increase(agent_efficiency_wait_requests_total{{{EFF},{ns},{agent_v},{role_v}}}[$__range]))",
                "{{tool}}",
                "Average timeout requested per wait call over the range, by tool; long timeouts on a root block it from reacting.",
                "ms",
            ),
        ],
    )

    # Worker passes from Tempo span metrics, worker events from Loki and errored traces from Tempo.
    pass_errors = (
        f"(sum by(span_name) (rate({CALLS}{{{PASSES},{SPAN_ERROR}}}[{BURST}])) or 0 * sum by(span_name) "
        f"(rate({CALLS}{{{PASSES}}}[{BURST}]))) / sum by(span_name) (rate({CALLS}{{{PASSES}}}[{BURST}]))"
    )
    journal_pass = f'{JOURNAL_SELECTOR},span_name="journal_sync.pass"'
    journal_read = f'{JOURNAL_SELECTOR},span_name="journal.read"'
    d.add_tab(
        "Pipeline",
        [
            stat(
                "pipe_error_ratio",
                200,
                "Pass error ratio",
                f"(sum(increase({CALLS}{{{PASSES},{SPAN_ERROR}}}[$__range])) or vector(0)) / sum(increase({CALLS}{{{PASSES}}}[$__range]))",
                "Share of index, postpass, embed and journal sync passes whose span ended in error over the range.",
                "percentunit",
                0.01,
                0.05,
            ),
            stat(
                "pipe_failed_passes",
                201,
                "Failed passes",
                f'sum(count_over_time({WORKER_STREAMS} | event_name="worker.pass.failed" [$__range])) or vector(0)',
                "worker.pass.failed events across the workers over the range, from their logs.",
                "short",
                1,
                5,
                source="loki",
            ),
            stat(
                "pipe_index_p95",
                202,
                "Index pass p95",
                f'histogram_quantile(0.95, sum(rate({LATENCY}{{{SELECTOR},span_name="index.pass"}}[$__range])))',
                "95th percentile index pass duration over the range.",
                "s",
            ),
            stat(
                "pipe_embed_p95",
                203,
                "Embed pass p95",
                f'histogram_quantile(0.95, sum(rate({LATENCY}{{{EMBED_SELECTOR},span_name="embed.pass"}}[$__range])))',
                "95th percentile embed pass duration over the range.",
                "s",
            ),
            stat(
                "pipe_journal_age",
                204,
                "Journal sync last pass age",
                f"time() - max_over_time((timestamp(increase({CALLS}{{{journal_pass}}}[5m]) > 0))[24h:1m])",
                "Seconds since a journal sync pass last completed, to within five minutes; empty after a day with no pass.",
                "s",
                900,
                1800,
            ),
            stat(
                "pipe_outbound_failures",
                205,
                "Failed outbound calls",
                f'sum(count_over_time({WORKER_STREAMS} | event_name="outbound.call.failed" [$__range])) or vector(0)',
                "outbound.call.failed events across the workers over the range, from their logs.",
                "short",
                1,
                5,
                source="loki",
            ),
            chart(
                "pipe_pass_rate",
                206,
                "Passes per minute by pass type",
                [(f"60 * sum by(span_name) (rate({CALLS}{{{PASSES}}}[{BURST}]))", "{{span_name}}")],
                "Index, postpass, embed and journal sync passes per minute from Tempo span metrics, averaged over a rolling 30 minutes.",
                "short",
            ),
            chart(
                "pipe_pass_errors",
                207,
                "Pass error ratio by pass type",
                [(pass_errors, "{{span_name}}")],
                "Share of each pass type whose span ended in error.",
                "percentunit",
            ),
            chart(
                "pipe_pass_duration",
                208,
                "Pass duration p50/p95 by pass type",
                [
                    (
                        f"histogram_quantile(0.5, sum by(span_name) (rate({LATENCY}{{{PASSES}}}[{BURST}])))",
                        "p50 / {{span_name}}",
                    ),
                    (
                        f"histogram_quantile(0.95, sum by(span_name) (rate({LATENCY}{{{PASSES}}}[{BURST}])))",
                        "p95 / {{span_name}}",
                    ),
                ],
                "Pass span duration, median and 95th percentile, by pass type.",
                "s",
            ),
            chart(
                "pipe_pass_events",
                209,
                "Worker pass events by service and outcome",
                [
                    (
                        f'sum by(service_name, agent_history_outcome) (count_over_time({WORKER_STREAMS} | event_name=~"worker.pass.(completed|failed|skipped)" [$__auto]))',
                        "{{service_name}} / {{agent_history_outcome}}",
                    )
                ],
                "worker.pass.completed, failed and skipped events per interval from the workers' logs.",
                "short",
                source="loki",
            ),
            logs(
                "pipe_failed_logs",
                210,
                "Recent pass failures",
                f'{WORKER_STREAMS} | event_name=~"worker.pass.failed|outbound.call.failed"',
                "Failed passes and the failed outbound calls inside them. Open a record's trace_id field to jump to the trace in Tempo.",
            ),
            traces(
                "pipe_error_traces",
                211,
                "Errored pass traces",
                '{resource.service.name=~"agent-history-.+" && name=~"index.pass|postpass.pass|embed.pass|journal_sync.pass" && status=error}',
                "Pass traces whose pass span ended in error; open a trace ID to see the failing span.",
            ),
            chart(
                "pipe_journal_activity",
                212,
                "Journal sync passes and reads per minute",
                [
                    (f"60 * sum(rate({CALLS}{{{journal_pass}}}[{BURST}]))", "passes"),
                    (f"60 * sum(rate({CALLS}{{{journal_read}}}[{BURST}]))", "journal reads"),
                ],
                "Journal sync passes and the journal reads they make, per minute, averaged over a rolling 30 minutes.",
                "short",
            ),
            chart(
                "pipe_journal_read_latency",
                213,
                "Journal read latency p50/p95",
                [
                    (f"histogram_quantile(0.5, sum(rate({LATENCY}{{{journal_read}}}[{BURST}])))", "p50"),
                    (f"histogram_quantile(0.95, sum(rate({LATENCY}{{{journal_read}}}[{BURST}])))", "p95"),
                ],
                "Duration of the journal read inside each journal sync pass.",
                "s",
            ),
        ],
    )

    # The embedder's GenAI client metrics, its HTTP attempt spans and its run and GC snapshots.
    model_g = 'gen_ai_operation_name="embeddings"'
    attempt = f'{EMBED_SELECTOR},span_name="embedding.http_attempt"'
    attempt_errors = (
        f"(sum(rate({CALLS}{{{attempt},{SPAN_ERROR}}}[{BURST}])) or 0 * sum(rate({CALLS}{{{attempt}}}"
        f"[{BURST}]))) / sum(rate({CALLS}{{{attempt}}}[{BURST}]))"
    )
    d.add_tab(
        "Embedding provider",
        [
            stat(
                "emb_request_p95",
                230,
                "Request p95",
                f"histogram_quantile(0.95, sum by(le) (increase(gen_ai_client_operation_duration_seconds_bucket{{{EMBED_SELECTOR},{model_g}}}[$__range])))",
                "95th percentile embeddings request duration over the range.",
                "s",
            ),
            stat(
                "emb_tokens",
                231,
                "Tokens embedded",
                f"sum(increase(gen_ai_client_token_usage_sum{{{EMBED_SELECTOR},{model_g}}}[$__range]))",
                "Input tokens sent to the embeddings provider over the range.",
                "short",
            ),
            stat(
                "emb_attempt_errors",
                232,
                "HTTP attempt error ratio",
                f"(sum(increase({CALLS}{{{attempt},{SPAN_ERROR}}}[$__range])) or vector(0)) / sum(increase({CALLS}{{{attempt}}}[$__range]))",
                "Share of embeddings HTTP attempts whose span ended in error over the range; retries make some errors harmless.",
                "percentunit",
                0.05,
                0.2,
            ),
            stat(
                "emb_run_duration",
                233,
                "Last run duration",
                f"agent_history_embed_run_duration_seconds{{{SELECTOR}}}",
                "Duration of the latest embed run.",
                "s",
            ),
            stat(
                "emb_run_items",
                234,
                "Last run items",
                f"agent_history_embed_run_items{{{SELECTOR}}}",
                "Items embedded by the latest embed run.",
                "short",
            ),
            stat(
                "emb_gc_age",
                235,
                "GC last run age",
                f"time() - agent_history_embed_gc_last_run_timestamp_seconds{{{SELECTOR}}}",
                "Seconds since the embedding garbage collection last ran.",
                "s",
            ),
            chart(
                "emb_request_latency",
                236,
                "Request latency p50/p95 by model",
                [
                    (
                        f"histogram_quantile(0.5, sum by(le,gen_ai_request_model) (rate(gen_ai_client_operation_duration_seconds_bucket{{{EMBED_SELECTOR},{model_g}}}[{BURST}])))",
                        "p50 / {{gen_ai_request_model}}",
                    ),
                    (
                        f"histogram_quantile(0.95, sum by(le,gen_ai_request_model) (rate(gen_ai_client_operation_duration_seconds_bucket{{{EMBED_SELECTOR},{model_g}}}[{BURST}])))",
                        "p95 / {{gen_ai_request_model}}",
                    ),
                ],
                "Embeddings request duration from the GenAI client metrics, median and 95th percentile, by model.",
                "s",
            ),
            chart(
                "emb_token_rate",
                237,
                "Tokens per minute by model",
                [
                    (
                        f"60 * sum by(gen_ai_request_model,gen_ai_token_type) (rate(gen_ai_client_token_usage_sum{{{EMBED_SELECTOR},{model_g}}}[{BURST}]))",
                        "{{gen_ai_request_model}} / {{gen_ai_token_type}}",
                    )
                ],
                "Tokens sent to the embeddings provider per minute, by model and token type, averaged over a rolling 30 minutes.",
                "short",
            ),
            chart(
                "emb_attempt_rate",
                238,
                "HTTP attempts per minute by status",
                [(f"60 * sum by(status_code) (rate({CALLS}{{{attempt}}}[{BURST}]))", "{{status_code}}")],
                "Embeddings HTTP attempts per minute, retries included, by span status, averaged over a rolling 30 minutes.",
                "short",
            ),
            chart(
                "emb_attempt_error_ratio",
                239,
                "HTTP attempt error ratio",
                [(attempt_errors, "error ratio")],
                "Share of embeddings HTTP attempts whose span ended in error.",
                "percentunit",
            ),
            chart(
                "emb_attempt_latency",
                240,
                "HTTP attempt latency p50/p95",
                [
                    (f"histogram_quantile(0.5, sum(rate({LATENCY}{{{attempt}}}[{BURST}])))", "p50"),
                    (f"histogram_quantile(0.95, sum(rate({LATENCY}{{{attempt}}}[{BURST}])))", "p95"),
                ],
                "Duration of each embeddings HTTP attempt from Tempo span metrics.",
                "s",
            ),
            chart(
                "emb_run",
                241,
                "Embed run duration and items",
                [
                    (f"agent_history_embed_run_duration_seconds{{{SELECTOR}}}", "duration (s)"),
                    (f"agent_history_embed_run_items{{{SELECTOR}}}", "items"),
                ],
                "Duration and item count of each embed run as the latest snapshot changes.",
            ),
            chart(
                "emb_gc_vectors",
                242,
                "GC vectors eligible and deleted",
                [
                    (f"agent_history_embed_gc_eligible{{{SELECTOR}}}", "eligible"),
                    (f"agent_history_embed_gc_deleted{{{SELECTOR}}}", "deleted"),
                ],
                "Vectors the latest embedding garbage collection found eligible and deleted.",
            ),
            chart(
                "emb_gc_state",
                243,
                "GC dry run and skip reason",
                [
                    (f"agent_history_embed_gc_dry_run_ratio{{{SELECTOR}}}", "dry run"),
                    (f"agent_history_embed_gc_skipped_ratio{{{SELECTOR}}}", "skipped / {{reason}}"),
                ],
                "Whether the latest garbage collection was a dry run, and its skip reason (none when it ran).",
            ),
        ],
    )

    metadata = {"name": "agent-session-archive", "annotations": {"grafana.app/folder": FOLDER}}
    return {
        "apiVersion": "dashboard.grafana.app/v2",
        "kind": "Dashboard",
        "metadata": metadata,
        "spec": {
            "annotations": [
                {
                    "kind": "AnnotationQuery",
                    "spec": {
                        "builtIn": False,
                        "enable": True,
                        "hide": False,
                        "iconColor": "gray",
                        "name": "Agent changes",
                        "query": {
                            "kind": "DataQuery",
                            "group": "grafana",
                            "version": "v0",
                            "datasource": {"name": "-- Grafana --"},
                            "spec": {
                                "limit": 100,
                                "matchAny": False,
                                "tags": ["agent-change"],
                                "type": "tags",
                            },
                        },
                    },
                },
            ],
            "cursorSync": "Crosshair",
            "description": "Operational health, capacity, archive durability, worker pipeline, embedding provider and agent efficiency across hot and cold agent session history.",
            "editable": True,
            "elements": d.elements,
            "layout": {"kind": "TabsLayout", "spec": {"tabs": d.tabs}},
            "links": [],
            "liveNow": False,
            "preload": False,
            "tags": ["agents", "archive", "backup", "camden", "efficiency"],
            "timeSettings": {
                "autoRefresh": "1m",
                "autoRefreshIntervals": ["1m", "5m", "15m", "1h"],
                "fiscalYearStartMonth": 0,
                "from": "now-24h",
                "hideTimepicker": False,
                "timezone": "browser",
                "to": "now",
            },
            "title": "Chat archive \u00b7 Session Archive",
            "variables": variables(),
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    content = json.dumps(build(), indent=2, sort_keys=True) + "\n"
    if args.check:
        if not OUT.exists() or OUT.read_text(encoding="utf-8") != content:
            raise SystemExit(f"generated dashboard drift: {OUT.relative_to(ROOT)}")
    else:
        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_text(content, encoding="utf-8")


if __name__ == "__main__":
    main()

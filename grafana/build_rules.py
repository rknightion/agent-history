#!/usr/bin/env python3
"""Generate the agent-history Grafana-managed alert rules and their promtool fixtures.

Edit this file, then run `just gen-alerts`; `just alerts-check` fails on drift.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "alerts" / "grafana-managed"
FOLDER = "REPLACE_WITH_FOLDER_UID"
PROM = "grafanacloud-prom"
# The indexer publishes every agent-history family over OTLP as service.name agent-history-index;
# Grafana Cloud maps service.name to the job label.
JOB = 'job="agent-history-index"'
# Grafana Cloud's OTLP translation appends a unit suffix to some names: a gauge with unit "1" gains
# `_ratio`. Keys are the collection's family names (metrics/otlp.py SPECS), values the stored names.
# tests/test_alert_rules.py derives these from SPECS so a unit change cannot silently break a rule.
STORED = {
    "agent_history_embed_run_success": "agent_history_embed_run_success_ratio",
    "agent_history_embed_last_failure_reason": "agent_history_embed_last_failure_reason_ratio",
    "agent_history_embed_last_success_timestamp_seconds": "agent_history_embed_last_success_timestamp_seconds",
    "agent_history_embed_pending_messages": "agent_history_embed_pending_messages",
    "agent_sessions_metrics_collection_runs_total": "agent_sessions_metrics_collection_runs_total",
    "agent_sessions_metrics_last_success_timestamp_seconds": "agent_sessions_metrics_last_success_timestamp_seconds",
    "agent_sessions_metrics_section_success": "agent_sessions_metrics_section_success_ratio",
    "agent_sessions_metrics_collection_duration_seconds": "agent_sessions_metrics_collection_duration_seconds",
    "agent_history_exporter_collection_errors_total": "agent_history_exporter_collection_errors_total",
    "agent_history_last_success_timestamp_seconds": "agent_history_last_success_timestamp_seconds",
    "agent_history_run_success": "agent_history_run_success_ratio",
    "agent_history_lag_bytes": "agent_history_lag_bytes",
    "agent_history_parse_issues_total": "agent_history_parse_issues_total",
    "agent_sessions_archive_receipt_timestamp_seconds": "agent_sessions_archive_receipt_timestamp_seconds",
    "agent_sessions_cold_nfs_mounted": "agent_sessions_cold_nfs_mounted_ratio",
    "agent_sessions_filesystem_bytes": "agent_sessions_filesystem_bytes",
    "agent_sessions_hot_retention_eligible_files": "agent_sessions_hot_retention_eligible_files",
    "agent_efficiency_root_no_lane_seconds": "agent_efficiency_root_no_lane_seconds",
}
# The archive service and timer states come from the deployment's node exporter systemd collector
# (job agent-sessions), not from the indexer, so they are not OTLP families.
SYSTEMD_JOB = 'job="agent-sessions"'
ARCHIVE_SERVICE = "agent-session-archive.service"
ARCHIVE_TIMER = "agent-session-archive.timer"
# Pending embeddings older than this with no successful run mean embedding has stopped.
STALE_SECONDS = 3600
# Every fixture series holds its value for this many one-minute steps (past every eval time).
HOLD = f"x{STALE_SECONDS // 60 + 90}"

# A collection that has not completed a run in this window has stopped (refresh_interval is seconds).
COLLECTION_WINDOW = "15m"
# A collection whose last success is older than this has stopped succeeding, even while runs continue.
COLLECTION_SUCCESS_SECONDS = 900
# The indexer refreshes every few minutes; no successful pass for this long means it has stopped.
INDEX_STALE_SECONDS = 1800
# Lag that never reaches zero across this window means the indexer is not keeping up.
LAG_WINDOW = "1h"
# A parse issue count above its value this long ago means new transcripts failed to parse cleanly.
PARSE_WINDOW = "6h"
# The daily archive writes a receipt; none for this long means at least one run was missed.
ARCHIVE_STALE_SECONDS = 129600


def stored(name: str) -> str:
    return STORED[name] + f"{{{JOB}}}"


RUN_SUCCESS = stored("agent_history_embed_run_success")
FAILURE_REASON = stored("agent_history_embed_last_failure_reason")
LAST_SUCCESS = stored("agent_history_embed_last_success_timestamp_seconds")
PENDING = stored("agent_history_embed_pending_messages")
RUNS = stored("agent_sessions_metrics_collection_runs_total")
COLLECTION_LAST_SUCCESS = stored("agent_sessions_metrics_last_success_timestamp_seconds")
SECTION_SUCCESS = stored("agent_sessions_metrics_section_success")
COLLECTOR_ERRORS = stored("agent_history_exporter_collection_errors_total")
COLLECTION_DURATION = stored("agent_sessions_metrics_collection_duration_seconds")
INDEX_LAST_SUCCESS = stored("agent_history_last_success_timestamp_seconds")
INDEX_RUN_SUCCESS = stored("agent_history_run_success")
LAG = stored("agent_history_lag_bytes")
PARSE_ISSUES = stored("agent_history_parse_issues_total")
ARCHIVE_RECEIPT = stored("agent_sessions_archive_receipt_timestamp_seconds")
COLD_NFS = stored("agent_sessions_cold_nfs_mounted")
NO_LANE = stored("agent_efficiency_root_no_lane_seconds")
HOT_RETENTION_ELIGIBLE = stored("agent_sessions_hot_retention_eligible_files")
TIMER_ACTIVE = f'node_systemd_unit_state{{{SYSTEMD_JOB},name="{ARCHIVE_TIMER}",state="active"}}'
SERVICE_FAILED = f'node_systemd_unit_state{{{SYSTEMD_JOB},name="{ARCHIVE_SERVICE}",state="failed"}}'


def filesystem_used_percent(tier: str) -> str:
    def part(kind: str) -> str:
        return f'sum({STORED["agent_sessions_filesystem_bytes"]}{{{JOB},tier="{tier}",kind="{kind}"}})'

    return f"100 * {part('used')} / {part('total')}"


# One series per reported reason while the last run failed. A failed run whose reason
# series is missing still fires, as reason="unreported", so a dropped reason never hides it.
# Healthy and absent data both return no series: noDataState OK, so neither fires.
RUN_FAILED = (
    f"max by (reason) (({FAILURE_REASON} == 1) and on() ({RUN_SUCCESS} == 0))"
    f' or on() label_replace(1 - min({RUN_SUCCESS}) == 1, "reason", "unreported", "", "")'
)
# Seconds since the last successful run, only while it exceeds STALE_SECONDS and messages
# are pending. An absent last-success or pending series returns nothing and does not fire.
STALE_WITH_PENDING = f"(time() - max({LAST_SUCCESS}) > {STALE_SECONDS}) and on() (max({PENDING}) > 0)"
# 1 while the collection exists but completed no run in the window (a hung refresher keeps exporting
# its last snapshot, so the runs counter goes flat), or while the series is absent altogether (the
# indexer is down, metric export is off, or collection setup failed). A running collection: nothing.
# A collection that still runs but whose last success is older than COLLECTION_SUCCESS_SECONDS
# (every run fails before recording success) fires too, with the age in seconds as its value.
COLLECTION_STALLED = (
    f"((sum(increase({RUNS}[{COLLECTION_WINDOW}])) < bool 1) == 1)"
    f" or on() absent_over_time({RUNS}[{COLLECTION_WINDOW}])"
    f" or on() (time() - max({COLLECTION_LAST_SUCCESS}) > {COLLECTION_SUCCESS_SECONDS})"
)
# One series, value 1, per collector section that is failing now or recorded a collector error in
# the last 30 minutes. The sections and the collectors are the same six names.
SECTION_FAILING = (
    f"max by (section) ("
    f"(min by (section) ({SECTION_SUCCESS}) < bool 1) == 1"
    f' or label_replace((sum by (collector) (increase({COLLECTOR_ERRORS}[30m])) > bool 0) == 1, "section", "$1", "collector", "(.*)")'
    f")"
)
# Seconds since the last successful index pass, while it exceeds INDEX_STALE_SECONDS. The series
# carries service_version, so max() keeps one value across an upgrade. Absent: see metrics-stale.
INDEX_STALE = f"time() - max({INDEX_LAST_SUCCESS}) > {INDEX_STALE_SECONDS}"
# 1 while every current index run series reports failure.
INDEX_RUN_FAILED = f"1 - max({INDEX_RUN_SUCCESS}) > 0"
# The smallest lag in the window, while it never reached zero.
LAG_STUCK = f"min_over_time(max({LAG})[{LAG_WINDOW}:1m]) > 0"
# New parse issues per kind against the window start; a kind that first appears counts in full.
PARSE_ISSUES_NEW = (
    f"(max by (kind) ({PARSE_ISSUES}) - max by (kind) ({PARSE_ISSUES} offset {PARSE_WINDOW}) > 0)"
    f" or (max by (kind) ({PARSE_ISSUES}) > 0 unless max by (kind) ({PARSE_ISSUES} offset {PARSE_WINDOW}))"
)
ARCHIVE_STALE = f"time() - max({ARCHIVE_RECEIPT}) > {ARCHIVE_STALE_SECONDS}"
ARCHIVE_JOB_FAILED = f"max({SERVICE_FAILED}) > 0"
# 1 while the timer is not active or its state series is absent (the exporter or the unit is gone).
ARCHIVE_TIMER_INACTIVE = f"((max({TIMER_ACTIVE}) < bool 1) == 1) or on() absent({TIMER_ACTIVE})"
COLD_NFS_UNAVAILABLE = f"1 - max({COLD_NFS}) > 0"
HOT_FILESYSTEM_PRESSURE = f"{filesystem_used_percent('hot')} > 85"
COLD_FILESYSTEM_PRESSURE = f"{filesystem_used_percent('cold')} > 90"
HOT_RETENTION_BACKLOG = f"max({HOT_RETENTION_ELIGIBLE}) > 0"
COLLECTION_RUNTIME_HIGH = f"max({COLLECTION_DURATION}) > 60"
LOOP_ROOT_STALLED = f"max by (namespace) ({NO_LANE}) > 1200"

ABSENT_NOTE = (
    " Absent series do not fire (noDataState OK): this rule cannot tell a healthy embedder from"
    " a metric collection that does not see the worker snapshot. agent-history-metrics-stale covers"
    " a stopped collection."
)
RULES = [
    {
        "name": "agent-history-embed-run-failed",
        "title": "agent-history embedding run failed",
        "summary": "agent-history embedding run failed: {{ $labels.reason }}",
        "description": (
            "The last embedding run failed with reason {{ $labels.reason }} (auth, billing_quota,"
            " rate_limit, route, provider_error, network, other, or unreported when the run reported"
            " no reason) for at least 15 minutes. billing_quota means the provider refused for credit"
            " or quota." + ABSENT_NOTE
        ),
        "expr": RUN_FAILED,
        "for": "15m",
        "severity": "warning",
    },
    {
        "name": "agent-history-embed-stale-pending",
        "title": "agent-history embeddings stalled with messages pending",
        "summary": "agent-history embeddings have not succeeded for over an hour with messages pending",
        "description": (
            f"The last successful embedding run is more than {STALE_SECONDS} seconds old while"
            " agent_history_embed_pending_messages is above zero. Check the embedding run failed"
            " alert for the reason." + ABSENT_NOTE
        ),
        "expr": STALE_WITH_PENDING,
        "for": "15m",
        "severity": "critical",
    },
    {
        "name": "agent-history-metrics-stale",
        "title": "agent-history metric collection stopped",
        "summary": "agent-history metric collection has stopped or stopped succeeding",
        "description": (
            "agent_sessions_metrics_collection_runs_total from the agent-history-index service has not"
            f" increased in {COLLECTION_WINDOW}, is absent, or the last successful collection is more than"
            f" {COLLECTION_SUCCESS_SECONDS} seconds old. The periodic indexer runs the metric collectors on"
            " its own thread; check that the indexer container is running with OTLP metric export"
            " configured, and look for telemetry.configuration.invalid in its OTLP logs. A single failing"
            " collector section fires agent-history-collector-section-failing instead."
        ),
        "expr": COLLECTION_STALLED,
        "for": "5m",
        "severity": "critical",
    },
    {
        "name": "agent-history-collector-section-failing",
        "title": "agent-history collector section failing",
        "summary": "The {{ $labels.section }} metric collector section is failing",
        "description": (
            "agent_sessions_metrics_section_success_ratio is below 1 for this section, or"
            " agent_history_exporter_collection_errors_total increased for its collector in the last 30"
            " minutes. Sections are isolated: the others keep updating while this one fails. Read the"
            " indexer logs for the section's error."
        ),
        "expr": SECTION_FAILING,
        "for": "30m",
        "severity": "warning",
    },
    {
        "name": "agent-history-collection-runtime-high",
        "title": "agent-history metric collection slow",
        "summary": "agent-history metric collection takes {{ $values.A.Value | humanizeDuration }}",
        "description": (
            "A metric collection run takes more than 60 seconds. Inspect storage metadata traversal"
            " and catalogue queries; agent_sessions_metrics_section_duration_seconds names the slow section."
        ),
        "expr": COLLECTION_RUNTIME_HIGH,
        "for": "10m",
        "severity": "warning",
    },
    {
        "name": "agent-history-index-stale",
        "title": "agent-history index pass stale",
        "summary": "The agent-history index has not completed a successful pass for {{ $values.A.Value | humanizeDuration }}",
        "description": (
            f"agent_history_last_success_timestamp_seconds is more than {INDEX_STALE_SECONDS} seconds old."
            " The indexer refreshes every few minutes, so new transcripts are not reaching the catalogue."
            " Check the indexer container and the agent-history index run failed alert for a failing pass."
            " An absent series does not fire: agent-history-metrics-stale covers a stopped indexer."
        ),
        "expr": INDEX_STALE,
        "for": "5m",
        "severity": "critical",
    },
    {
        "name": "agent-history-index-run-failed",
        "title": "agent-history index run failed",
        "summary": "agent-history index runs are failing",
        "description": (
            "agent_history_run_success_ratio has been 0 for 15 minutes: index passes start but fail."
            " Read the indexer logs for the failing source or database error."
        ),
        "expr": INDEX_RUN_FAILED,
        "for": "15m",
        "severity": "warning",
    },
    {
        "name": "agent-history-index-lag-stuck",
        "title": "agent-history index lag not clearing",
        "summary": f"agent-history index lag has not reached zero in {LAG_WINDOW}",
        "description": (
            f"agent_history_lag_bytes stayed above zero for the whole of the last {LAG_WINDOW}; its"
            " smallest value is the alert value in bytes. Each pass normally clears the lag, so the"
            " indexer is falling behind or skipping a source."
        ),
        "expr": LAG_STUCK,
        "for": "5m",
        "severity": "warning",
    },
    {
        "name": "agent-history-parse-issues-new",
        "title": "agent-history new parse issues",
        "summary": "{{ $values.A.Value }} new {{ $labels.kind }} parse issues in the last " + PARSE_WINDOW,
        "description": (
            f"agent_history_parse_issues_total for this kind rose in the last {PARSE_WINDOW}. A new"
            " unknown_type or missing_field often means a harness changed its transcript format; query"
            " the catalogue's parse issues for the affected sessions."
        ),
        "expr": PARSE_ISSUES_NEW,
        "for": "0s",
        "severity": "warning",
    },
    {
        "name": "agent-history-archive-stale",
        "title": "agent-history archive receipt stale",
        "summary": "The latest session archive receipt is {{ $values.A.Value | humanizeDuration }} old",
        "description": (
            f"The daily hot-to-cold archive has not written a successful receipt for {ARCHIVE_STALE_SECONDS}"
            " seconds. Check the archive service's journal and the cold tier's availability."
        ),
        "expr": ARCHIVE_STALE,
        "for": "10m",
        "severity": "warning",
    },
    {
        "name": "agent-history-archive-job-failed",
        "title": "agent-history archive job failed",
        "summary": f"{ARCHIVE_SERVICE} is in the failed state",
        "description": (
            "The systemd collector reports the archive service failed. Check its journal and the cold"
            " tier's NFS availability."
        ),
        "expr": ARCHIVE_JOB_FAILED,
        "for": "10m",
        "severity": "warning",
    },
    {
        "name": "agent-history-archive-timer-inactive",
        "title": "agent-history archive timer inactive",
        "summary": f"{ARCHIVE_TIMER} is not active",
        "description": (
            "The archive timer is not active, or its systemd state series is absent. Cold archive and"
            " hot retention will not run until the timer is started."
        ),
        "expr": ARCHIVE_TIMER_INACTIVE,
        "for": "10m",
        "severity": "critical",
    },
    {
        "name": "agent-history-cold-nfs-unavailable",
        "title": "agent-history cold archive not on NFS",
        "summary": "The permanent session archive is not on NFS",
        "description": (
            "The configured cold root no longer resolves to an NFS mount. Archive and index jobs must"
            " not treat a local fallback directory as the permanent authority."
        ),
        "expr": COLD_NFS_UNAVAILABLE,
        "for": "10m",
        "severity": "critical",
    },
    {
        "name": "agent-history-hot-filesystem-pressure",
        "title": "agent-history hot filesystem pressure",
        "summary": 'The hot session filesystem is {{ $values.A.Value | printf "%.1f" }}% used',
        "description": (
            "The filesystem holding the hot session tier is above 85% used. This is archive-specific"
            " context for the host's own capacity alert."
        ),
        "expr": HOT_FILESYSTEM_PRESSURE,
        "for": "10m",
        "severity": "warning",
    },
    {
        "name": "agent-history-cold-filesystem-pressure",
        "title": "agent-history cold filesystem pressure",
        "summary": 'The cold session archive volume is {{ $values.A.Value | printf "%.1f" }}% used',
        "description": (
            "The permanent archive volume is above 90% used. Cold JSONL and version history are"
            " append-only and must not be pruned ad hoc."
        ),
        "expr": COLD_FILESYSTEM_PRESSURE,
        "for": "10m",
        "severity": "warning",
    },
    {
        "name": "agent-history-hot-retention-backlog",
        "title": "agent-history hot retention backlog",
        "summary": '{{ $values.A.Value | printf "%.0f" }} hot JSONL files are past retention',
        "description": (
            "Files past the hot retention window remain in the hot tier. The archive service prunes"
            " them only after a successful copy to the cold tier."
        ),
        "expr": HOT_RETENTION_BACKLOG,
        "for": "36h",
        "severity": "warning",
    },
    {
        "name": "agent-history-loop-root-stalled",
        "title": "agent-history loop root stalled",
        "summary": "{{ $labels.namespace }} has had no lane in flight for {{ $values.A.Value | humanizeDuration }}",
        "description": (
            "A live loop root in this namespace has had no lane in flight for over 20 minutes. A"
            " deliberate pause or a drain can trigger it as easily as a stuck loop."
        ),
        "expr": LOOP_ROOT_STALLED,
        "for": "5m",
        "severity": "warning",
    },
]


def labels(severity: str) -> dict:
    # The notification policy pages a critical rule unless page is "false"; warnings never page and
    # skip automatic investigation.
    page = severity == "critical"
    result = {
        "domain": "observability",
        "page": "true" if page else "false",
        "service": "agent-history",
        "severity": severity,
        "source": "agent-history",
    }
    if not page:
        result["skipinvestigation"] = "true"
    return result


def resource(rule: dict) -> dict:
    return {
        "apiVersion": "rules.alerting.grafana.app/v0alpha1",
        "kind": "AlertRule",
        "metadata": {
            "name": rule["name"],
            "annotations": {"grafana.app/folder": FOLDER},
            "labels": {"grafana.app/folder": FOLDER},
        },
        "spec": {
            "title": rule["title"],
            "trigger": {"interval": "1m"},
            "for": rule["for"],
            "paused": False,
            "noDataState": "Ok",
            "execErrState": "Error",
            "labels": labels(rule["severity"]),
            "annotations": {"summary": rule["summary"], "description": rule["description"]},
            "expressions": {
                "A": {
                    "datasourceUID": PROM,
                    "queryType": "instant",
                    "relativeTimeRange": {"from": "15m", "to": "0s"},
                    "model": {
                        "refId": "A",
                        "expr": rule["expr"],
                        "instant": True,
                        "range": False,
                        "datasource": {"type": "prometheus", "uid": PROM},
                    },
                },
                "B": {
                    "datasourceUID": "__expr__",
                    "model": {
                        "refId": "B",
                        "type": "reduce",
                        "expression": "A",
                        "reducer": "last",
                        "datasource": {"type": "__expr__", "uid": "__expr__"},
                    },
                },
                "C": {
                    "source": True,
                    "datasourceUID": "__expr__",
                    "model": {
                        "refId": "C",
                        "type": "threshold",
                        "expression": "B",
                        "datasource": {"type": "__expr__", "uid": "__expr__"},
                        "conditions": [{"evaluator": {"type": "gt", "params": [0]}}],
                    },
                },
            },
        },
    }


# Worker snapshot lines exactly as the collection renders them after a run (see
# tests/test_alert_rules.py, which renders them through the real RunCollector).
def failed_snapshot(reason: str | None) -> list[str]:
    lines = ["agent_history_embed_run_success 0", "agent_history_embed_last_success_timestamp_seconds 0"]
    if reason is not None:
        lines.append(f'agent_history_embed_last_failure_reason{{reason="{reason}"}} 1')
    return lines


SUCCESS_SNAPSHOT = ["agent_history_embed_run_success 1", "agent_history_embed_last_success_timestamp_seconds 0"]


def series(line: str) -> str:
    """One exposition line's series as stored in Mimir: the translated name plus the job label."""
    name, _value = line.rsplit(" ", 1)
    family, brace, labels = name.partition("{")
    if brace:
        return STORED[family] + "{" + labels[:-1] + f",{JOB}}}"
    return STORED[family] + f"{{{JOB}}}"


def fixtures() -> dict:
    """Promtool cases. Expectations come from the alert contract, not the expressions.

    The failure snapshots stand for the runs a fake upstream answering HTTP 402 and 429
    produces (tests/test_embed_pg.py proves those statuses yield these lines).
    """
    tests = []
    pending = {"series": PENDING}

    def case(name, snapshot, pending_value, eval_time, failed, stale):
        input_series = [{"series": series(line), "values": line.rsplit(" ", 1)[1] + HOLD} for line in snapshot]
        if pending_value is not None:
            input_series.append({**pending, "values": str(pending_value) + HOLD})
        tests.append(
            {
                "name": name,
                "interval": "1m",
                "input_series": input_series,
                "promql_expr_test": [
                    {"expr": RUN_FAILED, "eval_time": eval_time, "exp_samples": failed},
                    {"expr": STALE_WITH_PENDING, "eval_time": eval_time, "exp_samples": stale},
                ],
            }
        )

    def fired(reason):
        return [{"labels": f'{{reason="{reason}"}}', "value": 1}]

    # name, snapshot, pending, eval time, expected run-failed, expected stale samples
    case("HTTP 402 billing_quota failed run", failed_snapshot("billing_quota"), 5, "10m", fired("billing_quota"), [])
    case("HTTP 429 rate_limit failed run", failed_snapshot("rate_limit"), 5, "10m", fired("rate_limit"), [])
    case("failed run without a reported reason", failed_snapshot(None), 5, "10m", fired("unreported"), [])
    case("successful run", SUCCESS_SNAPSHOT, 5, "10m", [], [])
    case("absent worker snapshot", [], 5, "2h", [], [])
    case(
        "stale success with messages pending",
        failed_snapshot("billing_quota"),
        5,
        f"{STALE_SECONDS + 60}s",
        fired("billing_quota"),
        [{"labels": "{}", "value": STALE_SECONDS + 60}],
    )
    case("stale success at the threshold", SUCCESS_SNAPSHOT, 5, f"{STALE_SECONDS}s", [], [])
    case("stale success with nothing pending", SUCCESS_SNAPSHOT, 0, "2h", [], [])
    case("stale success with pending count absent", SUCCESS_SNAPSHOT, None, "2h", [], [])

    def collection(name, values, expected):
        input_series = [{"series": RUNS, "values": values}] if values is not None else []
        tests.append(
            {
                "name": name,
                "interval": "1m",
                "input_series": input_series,
                "promql_expr_test": [{"expr": COLLECTION_STALLED, "eval_time": "30m", "exp_samples": expected}],
            }
        )

    # Runs every minute (well under the real 15 s cadence), flat for the last 20 minutes, absent.
    collection("collection running", "0+4x40", [])
    collection("collection stalled", "0+4x10 40x30", [{"labels": "{}", "value": 1}])
    collection("collection absent", None, [{"labels": f"{{{JOB}}}", "value": 1}])
    return {"rule_files": [], "evaluation_interval": "1m", "tests": tests}


def indexer_fixtures() -> dict:
    """Promtool cases for the indexer, collection and archive rules, from the alert contract."""
    tests = []

    def case(name, input_series, expr, eval_time, expected):
        tests.append(
            {
                "name": name,
                "interval": "1m",
                "input_series": [{"series": s, "values": v} for s, v in input_series],
                "promql_expr_test": [{"expr": expr, "eval_time": eval_time, "exp_samples": expected}],
            }
        )

    def one(value, labels="{}"):
        return [{"labels": labels, "value": value}]

    # Timestamps of 0 make the age at an eval time equal to that eval time in seconds.
    case(
        "collection succeeding", [(RUNS, "0+4x40"), (COLLECTION_LAST_SUCCESS, "0+60x40")], COLLECTION_STALLED, "30m", []
    )
    case(
        "collection runs without succeeding",
        [(RUNS, "0+4x40"), (COLLECTION_LAST_SUCCESS, "0x40")],
        COLLECTION_STALLED,
        "30m",
        one(1800),
    )
    case("index fresh at the threshold", [(INDEX_LAST_SUCCESS, "0x40")], INDEX_STALE, f"{INDEX_STALE_SECONDS}s", [])
    case(
        "index stale past the threshold",
        [(INDEX_LAST_SUCCESS, "0x40")],
        INDEX_STALE,
        f"{INDEX_STALE_SECONDS + 60}s",
        one(INDEX_STALE_SECONDS + 60),
    )
    case("index run failing", [(INDEX_RUN_SUCCESS, "0x40")], INDEX_RUN_FAILED, "10m", one(1))
    case("index run succeeding", [(INDEX_RUN_SUCCESS, "1x40")], INDEX_RUN_FAILED, "10m", [])
    case("lag never clears", [(LAG, "5x90")], LAG_STUCK, "80m", one(5))
    case("lag clears within the window", [(LAG, "5x40 0 5x50")], LAG_STUCK, "80m", [])
    kind = PARSE_ISSUES.replace("{", '{kind="unknown_type",', 1)
    case("parse issues flat", [(kind, "10x400")], PARSE_ISSUES_NEW, "390m", [])
    case("parse issues rose", [(kind, "10x200 12x200")], PARSE_ISSUES_NEW, "390m", one(2, '{kind="unknown_type"}'))
    case(
        "parse issue kind first seen",
        [(kind, "_x200 3x200")],
        PARSE_ISSUES_NEW,
        "390m",
        one(3, '{kind="unknown_type"}'),
    )
    section = SECTION_SUCCESS.replace("{", '{section="runs",', 1)
    collector = COLLECTOR_ERRORS.replace("{", '{collector="loops",', 1)
    case("sections healthy", [(section, "1x60"), (collector, "0x60")], SECTION_FAILING, "45m", [])
    case(
        "section failing", [(section, "0x60"), (collector, "0x60")], SECTION_FAILING, "45m", one(1, '{section="runs"}')
    )
    case(
        "collector errors without a failed section",
        [(section, "1x60"), (collector, "0+1x60")],
        SECTION_FAILING,
        "45m",
        one(1, '{section="loops"}'),
    )
    case("archive timer active", [(TIMER_ACTIVE, "1x20")], ARCHIVE_TIMER_INACTIVE, "10m", [])
    case("archive timer inactive", [(TIMER_ACTIVE, "0x20")], ARCHIVE_TIMER_INACTIVE, "10m", one(1))
    case(
        "archive timer series absent",
        [],
        ARCHIVE_TIMER_INACTIVE,
        "10m",
        one(1, "{" + TIMER_ACTIVE.split("{", 1)[1]),
    )
    return {"rule_files": [], "evaluation_interval": "1m", "tests": tests}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    artifacts = [(OUT / f"{rule['name']}.json", resource(rule)) for rule in RULES]
    artifacts.append((OUT / "fixtures" / "embed.test.yaml", fixtures()))
    artifacts.append((OUT / "fixtures" / "indexer.test.yaml", indexer_fixtures()))
    for path, artifact in artifacts:
        content = json.dumps(artifact, indent=2, sort_keys=True) + "\n"
        if args.check:
            if not path.exists() or path.read_text(encoding="utf-8") != content:
                raise SystemExit(f"generated rule drift: {path.relative_to(ROOT)}")
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")


if __name__ == "__main__":
    main()

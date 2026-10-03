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
}
# Pending embeddings older than this with no successful run mean embedding has stopped.
STALE_SECONDS = 3600
# Every fixture series holds its value for this many one-minute steps (past every eval time).
HOLD = f"x{STALE_SECONDS // 60 + 90}"

# A collection that has not completed a run in this window has stopped (refresh_interval is seconds).
COLLECTION_WINDOW = "15m"


def stored(name: str) -> str:
    return STORED[name] + f"{{{JOB}}}"


RUN_SUCCESS = stored("agent_history_embed_run_success")
FAILURE_REASON = stored("agent_history_embed_last_failure_reason")
LAST_SUCCESS = stored("agent_history_embed_last_success_timestamp_seconds")
PENDING = stored("agent_history_embed_pending_messages")
RUNS = stored("agent_sessions_metrics_collection_runs_total")

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
COLLECTION_STALLED = (
    f"((sum(increase({RUNS}[{COLLECTION_WINDOW}])) < bool 1) == 1)"
    f" or on() absent_over_time({RUNS}[{COLLECTION_WINDOW}])"
)

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
        "summary": "agent-history metric collection has not completed a run for 15 minutes",
        "description": (
            "agent_sessions_metrics_collection_runs_total from the agent-history-index service has not"
            f" increased in {COLLECTION_WINDOW}, or is absent. The periodic indexer runs the metric"
            " collectors on its own thread; check that the indexer container is running with OTLP metric"
            " export configured, and look for telemetry.configuration.invalid in its OTLP logs. A failing"
            " collector section does not fire this rule: read agent_sessions_metrics_section_success_ratio."
        ),
        "expr": COLLECTION_STALLED,
        "for": "5m",
        "severity": "warning",
    },
]


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
            "labels": {
                "domain": "observability",
                "service": "agent-history",
                "severity": rule["severity"],
                "source": "agent-history",
            },
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


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    artifacts = [(OUT / f"{rule['name']}.json", resource(rule)) for rule in RULES]
    artifacts.append((OUT / "fixtures" / "embed.test.yaml", fixtures()))
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

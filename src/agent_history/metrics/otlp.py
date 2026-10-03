"""Legacy metric bridge: the exporter's one collection, also published through the OTel meter.

The bridge consumes the immutable public snapshot that `server.render` builds while rendering the
Prometheus text, so there is exactly one collection, one pass over `State.observe` and one set of
public labels. Callbacks only read that snapshot; they never query, parse, mutate state or collect.
Nothing here imports OpenTelemetry unless metric export is explicitly enabled.
"""

from __future__ import annotations

import importlib
import os
import threading
from dataclasses import dataclass
from typing import Iterable

from agent_history import telemetry

from . import Family
from .catalogue import EMBED_FAILURE_REASONS, RUN_REASONS, RUN_RESULTS, STATUSES, TABLES


@dataclass(frozen=True)
class Spec:
    kind: str  # "G" ObservableGauge, "C" cumulative monotonic ObservableCounter
    unit: str
    attributes: tuple[str, ...]  # sorted


@dataclass(frozen=True)
class HistogramSpec:
    attributes: tuple[str, ...]  # sorted, without `le`
    bounds: tuple[str, ...]  # finite bounds then +Inf, as rendered in the exposition


STORAGE = ("tier", "namespace", "agent", "profile", "machine")
EFFICIENCY = ("agent", "namespace")


def _rows(kind: str, text: str, prefix: tuple[str, ...] = (), suffix: tuple[str, ...] = ()) -> dict[str, Spec]:
    """Rows of `name unit attributes`, where `-` is no attribute and `S` the storage attribute set."""
    result = {}
    for line in text.strip().splitlines():
        name, unit, raw = line.split()
        names = [] if raw == "-" else list(STORAGE) if raw == "S" else raw.split(",")
        result[name] = Spec(kind, unit, tuple(sorted((*prefix, *names, *suffix))))
    return result


# docs/otel-design.md section 7, one row per exported family. Even a gauge named *_total stays a gauge.
SPECS: dict[str, Spec] = {
    **_rows(
        "G",
        """
agent_history_run_success 1 -
agent_history_run_duration_seconds s -
agent_history_lock_held 1 -
agent_history_run_files {file} result
agent_history_run_lines {line} -
agent_history_run_rows {row} -
agent_history_embed_last_failure_reason 1 reason
agent_history_embed_run_success 1 -
agent_history_embed_run_duration_seconds s -
agent_history_embed_run_items {item} -
agent_history_embed_run_chunks {chunk} -
agent_history_embed_run_api_inputs {input} -
agent_history_embed_run_cached_inputs {input} -
agent_history_embed_run_tokens {token} -
agent_history_embed_run_skipped 1 reason
agent_history_embed_last_success_timestamp_seconds s -
agent_history_embed_gc_last_run_timestamp_seconds s -
agent_history_embed_gc_dry_run 1 -
agent_history_embed_gc_eligible {vector} -
agent_history_embed_gc_deleted {vector} -
agent_history_embed_gc_skipped 1 reason
agent_history_last_success_timestamp_seconds s -
agent_history_sources {file} status
agent_history_lag_bytes By -
agent_history_parse_issues_total {issue} kind
agent_history_unresolved_links {link} kind
agent_history_dirty_sessions {session} -
agent_history_rows {row} table
agent_history_embed_pending_messages {chunk} -
agent_history_embed_failed_inputs {input} -
agent_history_embed_vectors {vector} -
agent_history_exporter_collection_duration_seconds s collector
agent_history_cold_tier_available 1 -
agent_sessions_storage_root_available 1 tier
agent_sessions_filesystem_bytes By tier,kind
agent_sessions_filesystem_inodes {inode} tier,kind
agent_sessions_storage_files {file} S
agent_sessions_storage_bytes By S
agent_sessions_storage_oldest_mtime_seconds s S
agent_sessions_storage_newest_mtime_seconds s S
agent_sessions_archive_pending_files {file} -
agent_sessions_archive_pending_bytes By -
agent_sessions_hot_retention_eligible_files {file} -
agent_sessions_hot_retention_eligible_bytes By -
agent_sessions_cold_nfs_mounted 1 -
agent_sessions_archive_receipts {receipt} -
agent_sessions_archive_last_success_timestamp_seconds s -
agent_sessions_archive_receipt_jsonl_files {file} -
agent_sessions_archive_receipt_jsonl_bytes By -
agent_sessions_archive_version_snapshots {snapshot} -
agent_sessions_archive_version_files {file} -
agent_sessions_archive_version_bytes By -
agent_sessions_archive_version_newest_mtime_seconds s -
agent_sessions_archive_available 1 tier
agent_sessions_archive_files {file} tier
agent_sessions_archive_bytes By tier
agent_sessions_archive_receipt_timestamp_seconds s -
agent_sessions_metrics_section_success 1 section
agent_sessions_metrics_section_duration_seconds s section
agent_sessions_metrics_section_last_success_timestamp_seconds s section
agent_sessions_metrics_collection_success 1 -
agent_sessions_metrics_collection_duration_seconds s -
agent_sessions_metrics_build_info 1 version
agent_sessions_metrics_last_success_timestamp_seconds s -
agent_efficiency_tracked_files {file} -
agent_efficiency_baseline_timestamp_seconds s -
agent_efficiency_loop_map_loops {loop} -
agent_efficiency_loop_map_age_seconds s -
agent_efficiency_loop_labels {loop} -
""",
    ),
    **_rows(
        "G",
        """
agent_efficiency_rate_limit_used_percent % window
agent_efficiency_rate_limit_resets_at_seconds s window
agent_efficiency_rate_limit_window_minutes min window
""",
        prefix=EFFICIENCY,
    ),
    **_rows(
        "G",
        """
agent_efficiency_active_roots_with_idle_workers {thread} -
agent_efficiency_roots_without_lanes {thread} -
agent_efficiency_root_no_lane_seconds s -
""",
        prefix=EFFICIENCY,
        suffix=("loop",),
    ),
    **_rows(
        "G",
        """
agent_efficiency_active_threads {thread} role
agent_efficiency_context_fill_ratio 1 role,quantile
agent_efficiency_context_tokens {token} role,quantile
""",
        prefix=EFFICIENCY,
        suffix=("loop",),
    ),
    **_rows(
        "C",
        """
agent_sessions_metrics_collection_runs_total {run} -
agent_sessions_metrics_collection_failures_total {run} -
agent_history_exporter_collection_errors_total {error} collector
""",
    ),
    **_rows(
        "C",
        """
agent_efficiency_time_seconds_total s role,state
agent_efficiency_llm_calls_total {call} role,trigger
agent_efficiency_input_tokens_total {token} role,trigger,cache
agent_efficiency_output_tokens_total {token} role
agent_efficiency_model_seconds_total s role,model
agent_efficiency_model_calls_total {call} role,model
agent_efficiency_tool_calls_total {call} role,class
agent_efficiency_poll_calls_total {call} role,target,result
agent_efficiency_poll_seconds_total s role,target,result
agent_efficiency_wait_timeout_ms_total ms role,tool
agent_efficiency_wait_requests_total {request} role,tool
agent_efficiency_spawns_total {spawn} -
agent_efficiency_compactions_total {compaction} role
agent_efficiency_turn_errors_total {error} kind
agent_efficiency_spawns_by_route_total {spawn} role,spawn_model,effort,agent_type,fork
agent_efficiency_spawn_errors_total {error} kind
agent_efficiency_git_pushes_total {push} role,outcome
agent_efficiency_ci_waits_total {wait} role,outcome
agent_efficiency_gate_runs_total {run} role,outcome
agent_efficiency_coderabbit_findings_total {finding} role,severity
agent_efficiency_coderabbit_reviews_total {review} role,outcome
agent_efficiency_tool_failures_total {failure} role,class
agent_efficiency_interventions_total {intervention} role,kind
agent_efficiency_root_llm_calls_by_protocol_total {call} protocol,poll
agent_efficiency_root_time_seconds_by_protocol_total s protocol,state
""",
        prefix=EFFICIENCY,
        suffix=("loop",),
    ),
}
# The two legacy histograms are exposed as three cumulative Sums each, never as a native Histogram.
HISTOGRAMS: dict[str, HistogramSpec] = {
    "agent_efficiency_lane_seconds": HistogramSpec(
        ("agent", "loop", "namespace"), ("300", "900", "1800", "3600", "7200", "14400", "+Inf")
    ),
    "agent_efficiency_first_spawn_seconds": HistogramSpec(
        ("agent", "loop", "namespace"), ("60", "300", "900", "1800", "3600", "+Inf")
    ),
}
COMPONENT_UNITS = {"_bucket": "{observation}", "_count": "{observation}", "_sum": "s"}

# Producer vocabularies. A value outside its producer's enum is refused by OTLP, never reclassified.
PARSE_ISSUE_KINDS = frozenset(
    {
        "json_error",
        "not_object",
        "parser_exception",
        "missing_field",
        "unknown_type",
        "session_mismatch",
        "missing_session_meta",
        "late_session_meta",
        "ordinal_not_monotonic",
        "missing_call_id",
        "orphan_output",
        "missing_item_id",
        "missing_session_header",
        "late_session_header",
    }
)
COLLECTORS = frozenset({"archive", "catalogue", "runs", "efficiency", "self"})
SECTIONS = COLLECTORS | {"storage", "loops"}
TIERS = frozenset({"hot", "cold", "incoming", "conflicts"})
EFFORTS = frozenset({"low", "medium", "high", "xhigh", "max", "inherit", "other"})
SEVERITIES = frozenset({"critical", "major", "minor", "trivial", "info"})
ENUMS: dict[tuple[str, str], frozenset[str]] = {
    ("agent_history_run_files", "result"): RUN_RESULTS,
    ("agent_history_embed_last_failure_reason", "reason"): EMBED_FAILURE_REASONS,
    ("agent_history_embed_run_skipped", "reason"): RUN_REASONS,
    ("agent_history_embed_gc_skipped", "reason"): RUN_REASONS,
    ("agent_history_sources", "status"): frozenset(STATUSES),
    ("agent_history_parse_issues_total", "kind"): PARSE_ISSUE_KINDS,
    ("agent_history_unresolved_links", "kind"): frozenset({"spawn_child", "session_root"}),
    ("agent_history_rows", "table"): frozenset(TABLES),
    ("agent_sessions_filesystem_bytes", "kind"): frozenset({"total", "free", "available", "used"}),
    ("agent_sessions_filesystem_inodes", "kind"): frozenset({"total", "free"}),
    ("agent_efficiency_spawns_by_route_total", "effort"): EFFORTS,
    ("agent_efficiency_coderabbit_findings_total", "severity"): SEVERITIES,
}
LABEL_ENUMS: dict[str, frozenset[str]] = {
    "collector": SECTIONS,  # the exporter's per-collector series also carry each collector's sub-sections
    "section": SECTIONS,
    "tier": TIERS,
    "window": frozenset({"primary", "secondary"}),
    "quantile": frozenset({"0.5", "0.9"}),
    "role": frozenset({"root", "worker", "solo"}),
}


def _allowed(family: str, key: str, value: str) -> bool:
    allowed = ENUMS.get((family, key)) or LABEL_ENUMS.get(key)
    return allowed is None or value in allowed


@dataclass(frozen=True)
class _Index:
    observations: dict[str, tuple[tuple[tuple[tuple[str, str], ...], float], ...]]
    helps: dict[str, tuple[str, str]]  # instrument name -> (unit, description)
    kinds: dict[str, str]
    rejected: tuple[tuple[str, str], ...]
    unmapped: frozenset[str]


def _index(snapshot: Iterable[Family]) -> _Index:
    observations: dict[str, list] = {}
    helps: dict[str, tuple[str, str]] = {}
    kinds: dict[str, str] = {}
    rejected: set[tuple[str, str]] = set()
    unmapped: set[str] = set()

    def add(instrument, kind, unit, description, labels, value):
        observations.setdefault(instrument, []).append((labels, value))
        helps[instrument] = (unit, description)
        kinds[instrument] = kind

    for family in snapshot:
        if family.name in HISTOGRAMS:
            spec = HISTOGRAMS[family.name]
            if family.type != "histogram":
                rejected.add((family.name, "type"))
                continue
            groups: dict[tuple, dict[str, list]] = {}
            for sample in family.samples:
                suffix = (sample.name or "")[len(family.name) :]
                labels = tuple((k, v) for k, v in sample.labels if k != "le")
                groups.setdefault(labels, {}).setdefault(suffix, []).append(sample)
            for labels, parts in groups.items():
                keys = tuple(sorted(k for k, _ in labels))
                bounds = [dict(s.labels).get("le") for s in parts.get("_bucket", [])]
                shape = (
                    keys == spec.attributes
                    and sorted(parts) == ["_bucket", "_count", "_sum"]
                    and len(parts["_count"]) == len(parts["_sum"]) == 1
                    and sorted(bounds, key=str) == sorted(spec.bounds)
                    and all(
                        sorted(k for k, _ in s.labels) == sorted((*spec.attributes, "le")) for s in parts["_bucket"]
                    )
                )
                if not shape:
                    rejected.add((family.name, "label_set"))
                    continue
                if not all(_allowed(family.name, k, v) for k, v in labels):
                    rejected.add((family.name, "label_value"))
                    continue
                for suffix, samples in parts.items():
                    for sample in samples:
                        add(
                            family.name + suffix,
                            "C",
                            COMPONENT_UNITS[suffix],
                            family.help,
                            tuple(sample.labels),
                            float(sample.value),
                        )
            continue
        spec = SPECS.get(family.name)
        if spec is None:
            unmapped.add(family.name)
            continue
        if family.type != ("gauge" if spec.kind == "G" else "counter"):
            rejected.add((family.name, "type"))
            continue
        for sample in family.samples:
            if tuple(sorted(k for k, _ in sample.labels)) != spec.attributes:
                rejected.add((family.name, "label_set"))
            elif not all(_allowed(family.name, k, v) for k, v in sample.labels):
                rejected.add((family.name, "label_value"))
            else:
                add(family.name, spec.kind, spec.unit, family.help, tuple(sample.labels), float(sample.value))
    return _Index(
        {name: tuple(values) for name, values in observations.items()},
        helps,
        kinds,
        tuple(sorted(rejected)),
        frozenset(unmapped),
    )


class Bridge:
    """Publishes every mapped legacy family through the module-owned meter."""

    def __init__(self, meter, observation):
        self._meter = meter
        self._observation = observation
        self._instruments: dict[str, object] = {}
        self._sentinel: str | None = None
        self._current: dict = {}
        self._pinned: dict = {}
        self._announced: set[tuple[str, str]] = set()
        self.rejected: tuple[tuple[str, str], ...] = ()
        self.unmapped: frozenset[str] = frozenset()

    @classmethod
    def create(cls) -> "Bridge | None":
        """A bridge only when metric export is explicitly enabled and cumulative; otherwise None."""
        if not telemetry.metrics_enabled():
            return None
        preference = os.environ.get("OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE", "").strip().upper()
        if preference not in ("", "CUMULATIVE"):
            # Legacy snapshot instruments are only honest as cumulative values: never publish deltas.
            telemetry.emit("telemetry.configuration.invalid", {})
            return None
        try:
            observation = importlib.import_module("opentelemetry.metrics").Observation
        except Exception:
            return None
        return cls(telemetry.meter(), observation)

    def publish(self, snapshot: Iterable[Family]) -> None:
        """Atomically replace the shared snapshot after a successful collection. Never raises."""
        try:
            index = _index(snapshot)
            self._register(index)
            self._current = index.observations
            self.rejected, self.unmapped = index.rejected, index.unmapped
            for refusal in index.rejected:
                if refusal not in self._announced:
                    self._announced.add(refusal)
                    # Fixed event: no family, label or value, which could carry untrusted text.
                    telemetry.emit("telemetry.export.failed", {"error.type": "config"})
        except Exception:
            telemetry.emit("telemetry.export.failed", {"error.type": "error"})

    def _register(self, index: _Index) -> None:
        for name in sorted(index.helps):
            if name in self._instruments:
                continue
            unit, description = index.helps[name]
            create = (
                self._meter.create_observable_gauge
                if index.kinds[name] == "G"
                else self._meter.create_observable_counter
            )
            # The first instrument is the sentinel: the SDK runs callbacks in registration order under
            # one lock, so it pins the snapshot every other callback of that export reads.
            sentinel = self._sentinel is None
            if sentinel:
                self._sentinel = name
            self._instruments[name] = create(
                name, callbacks=[self._callback(name, sentinel)], unit=unit, description=description
            )

    def _callback(self, name: str, sentinel: bool):
        def callback(options):
            if sentinel:
                self._pinned = self._current
            for labels, value in self._pinned.get(name, ()):
                yield self._observation(value, dict(labels))

        return callback


class Refresher(threading.Thread):
    """Drives the exporter's cached collection at its refresh cadence, with or without a scraper."""

    def __init__(self, server):
        super().__init__(name="agent-history-otlp-refresh", daemon=True)
        self.server = server
        self._halt = threading.Event()

    def run(self) -> None:
        while not self._halt.is_set():
            try:
                self.server.metrics()
                delay = self.server.seconds_until_refresh()
            except Exception:
                delay = self.server.refresh
            self._halt.wait(max(delay, 0.01))

    def stop(self, timeout: float = 60) -> None:
        """Stop and join; a collection already running finishes first."""
        self._halt.set()
        if self.is_alive():
            self.join(timeout)

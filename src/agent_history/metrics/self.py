"""Exporter collection health, including the private collector's run/section families."""

from __future__ import annotations

import time

from . import Family, Sample
from .catalogue import gauge


class SelfCollector:
    name = "self"

    def __init__(self):
        self.durations: dict[str, float] = {}
        self.errors: dict[str, int] = {}
        self.success: dict[str, int] = {}
        self.last_success: dict[str, float] = {}
        self.runs = 0
        self.failures = 0
        self.last_run_duration = 0.0
        self.last_complete_success = 0.0

    def record(self, collector: str, duration: float, failed: bool):
        self.durations[collector] = duration
        self.success[collector] = int(not failed)
        if failed:
            self.errors[collector] = self.errors.get(collector, 0) + 1
        else:
            self.last_success[collector] = time.time()

    def complete(self, duration: float, failed: bool):
        self.runs += 1
        self.failures += int(failed)
        self.last_run_duration = duration
        if not failed:
            self.last_complete_success = time.time()

    def collect(self):
        sections = tuple(sorted(self.durations))
        section = lambda values: tuple(Sample((("section", name),), values[name]) for name in sections)
        result = [
            Family(
                "agent_sessions_metrics_section_success",
                "gauge",
                "Whether an agent session metrics collection section succeeded on the latest run.",
                section(self.success),
            ),
            Family(
                "agent_sessions_metrics_section_duration_seconds",
                "gauge",
                "Duration of an agent session metrics collection section.",
                section(self.durations),
            ),
            Family(
                "agent_sessions_metrics_section_last_success_timestamp_seconds",
                "gauge",
                "Last successful completion time for an agent session metrics section.",
                tuple(Sample((("section", name),), stamp) for name, stamp in sorted(self.last_success.items())),
            ),
            gauge(
                "agent_sessions_metrics_collection_success",
                "Whether every agent session metrics section succeeded on the latest run.",
                int(bool(self.runs and all(self.success.values()))),
            ),
            Family(
                "agent_sessions_metrics_collection_runs_total",
                "counter",
                "Agent session metrics collection attempts.",
                (Sample((), self.runs),),
            ),
            Family(
                "agent_sessions_metrics_collection_failures_total",
                "counter",
                "Agent session metrics collection attempts with at least one failed section.",
                (Sample((), self.failures),),
            ),
            gauge(
                "agent_sessions_metrics_collection_duration_seconds",
                "Duration of the complete agent session metrics collection run.",
                self.last_run_duration,
            ),
            Family(
                "agent_sessions_metrics_build_info",
                "gauge",
                "Agent session metrics collector build information.",
                (Sample((("version", "1"),), 1),),
            ),
        ]
        if self.last_complete_success:
            result.append(
                gauge(
                    "agent_sessions_metrics_last_success_timestamp_seconds",
                    "Last time every agent session metrics section completed successfully.",
                    self.last_complete_success,
                )
            )
        result.extend(
            (
                Family(
                    "agent_history_exporter_collection_duration_seconds",
                    "gauge",
                    "Last collector duration in seconds.",
                    tuple(Sample((("collector", name),), self.durations[name]) for name in sections),
                ),
                Family(
                    "agent_history_exporter_collection_errors_total",
                    "counter",
                    "Collector failures.",
                    tuple(Sample((("collector", name),), self.errors.get(name, 0)) for name in sections),
                ),
            )
        )
        return tuple(result)

"""Deterministic synthetic collections covering every mapped legacy family.

The family and label inventory comes from docs/otel-design.md; label values are synthetic.
"""

from __future__ import annotations

import design_spec
from agent_history.metrics import Family, Sample
from agent_history.metrics.catalogue import EMBED_FAILURE_REASONS, RUN_REASONS, RUN_RESULTS, STATUSES, TABLES

PARSE_KINDS = (
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
)
POOLS = {
    "agent": ("claude", "codex"),
    "namespace": ("claude-personal", "codex-personal"),
    "loop": ("loop1", "loop2"),
    "role": ("root", "worker"),
    "model": ("claude-sonnet-5", "private-model-one", "private-model-two"),
    "quantile": ("0.5", "0.9"),
    "tier": ("hot", "cold"),
    "kind": ("a", "b"),
    "window": ("primary", "secondary"),
    "profile": ("personal", "standalone"),
    "machine": ("shared", "other"),
    "collector": ("archive", "catalogue"),
    "section": ("archive", "loops"),
    "version": ("1",),
}
OVERRIDES = {
    ("agent_history_run_files", "result"): sorted(RUN_RESULTS),
    ("agent_history_embed_last_failure_reason", "reason"): sorted(EMBED_FAILURE_REASONS),
    ("agent_history_embed_run_skipped", "reason"): sorted(RUN_REASONS),
    ("agent_history_embed_gc_skipped", "reason"): sorted(RUN_REASONS),
    ("agent_history_sources", "status"): list(STATUSES),
    ("agent_history_parse_issues_total", "kind"): list(PARSE_KINDS),
    ("agent_history_unresolved_links", "kind"): ["spawn_child", "session_root"],
    ("agent_history_rows", "table"): list(TABLES),
    ("agent_sessions_filesystem_bytes", "kind"): ["total", "free"],
    ("agent_sessions_filesystem_inodes", "kind"): ["total", "free"],
    ("agent_efficiency_coderabbit_findings_total", "severity"): ["critical", "info"],
    ("agent_efficiency_spawns_by_route_total", "effort"): ["high", "inherit"],
}
CAP = 16


def _combinations(name: str, labels: tuple[str, ...]) -> list[tuple[tuple[str, str], ...]]:
    result: list[tuple[tuple[str, str], ...]] = [()]
    for label in labels:
        values = OVERRIDES.get((name, label)) or POOLS.get(label) or ("x1", "x2")
        result = [(*prefix, (label, value)) for prefix in result for value in values]
    cap = 20 if any((name, label) in OVERRIDES for label in labels) else CAP
    return [tuple(sorted(combo)) for combo in result][:cap]


def _value(index: int, kind: str) -> float:
    if kind == "gauge":
        return (index + 1) / 7.0 if index % 5 == 0 else (index + 1) * 1.25
    return float(index + 1) if index % 3 else (index + 1) / 7.0


def families(step: int = 0) -> tuple[list[Family], dict[str, set[str]]]:
    """One synthetic collection. Step 0 and 1 repeat; step 2 resets, omits, retires and widens."""
    specs, histograms = design_spec.load()
    result: list[Family] = []
    for name, spec in sorted(specs.items()):
        if name in histograms:
            continue
        kind = "gauge" if spec.kind == "G" else "counter"
        if step == 2 and name == "agent_history_lag_bytes":
            continue  # an absent gauge, never a fabricated zero
        combos = _combinations(name, spec.attributes)
        if step == 2 and "loop" in spec.attributes:
            combos = [c for c in combos if ("loop", "loop2") not in c or name != "agent_efficiency_llm_calls_total"]
            if name in ("agent_efficiency_active_threads", "agent_efficiency_tool_calls_total"):
                base = dict(combos[0])
                combos += [tuple(sorted({**base, "loop": f"wide{n}"}.items())) for n in range(60)]
        samples = []
        for index, labels in enumerate(combos):
            value = _value(index, kind)
            if step == 2 and kind == "counter":
                value /= 2  # a counter reset: the original source dropped below its previous raw value
            samples.append(Sample(labels, value))
        result.append(Family(name, kind, f"Synthetic {name}.", tuple(samples)))
    for name, spec in sorted(histograms.items()):
        samples = []
        for labels in _combinations(name, spec.attributes):
            for position, bound in enumerate(spec.bounds):
                count = (position + 1) * (4 if step != 2 else 2)
                samples.append(Sample((*labels, ("le", bound)), count, name + "_bucket"))
            samples.append(Sample(labels, 12.5 if step != 2 else 6.25, name + "_sum"))
            samples.append(Sample(labels, len(spec.bounds) * (4 if step != 2 else 2), name + "_count"))
        samples = [Sample(tuple(sorted(s.labels)), s.value, s.name) for s in samples]
        result.append(Family(name, "histogram", f"Synthetic {name}.", tuple(samples)))
    retired = {"agent_efficiency_llm_calls_total": {"loop2"}} if step == 2 else {}
    return result, retired


class Stub:
    """A collector replaying the scenario, so a Collection drives the whole public path."""

    name = "efficiency"

    def __init__(self):
        self.step = 0
        self.retired_loops: dict[str, set[str]] = {}
        self.collections = 0

    def collect(self):
        self.collections += 1
        result, self.retired_loops = families(self.step)
        return result

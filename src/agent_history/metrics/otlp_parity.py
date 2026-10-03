"""Compare one Prometheus exposition with the OTLP metrics exported from the same collection.

The decoded OTLP body is the real outgoing request, not an in-memory helper. Only resource and
timestamp differences, and the documented histogram component representation, are canonicalised.
Missing, extra, rejected and unvalidated families are reported separately and any of them fails.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Iterable

from . import otlp

CUMULATIVE = 2  # AggregationTemporality.AGGREGATION_TEMPORALITY_CUMULATIVE
NAME = r"[a-zA-Z_:][a-zA-Z0-9_:]*"
SAMPLE = re.compile(rf"^({NAME})(\{{.*\}})?\s+(\S+)(?:\s+[0-9]+)?$")
LABEL = re.compile(r'([a-zA-Z_][a-zA-Z0-9_]*)="((?:[^"\\]|\\[\\"n])*)"')


@dataclass
class Capture:
    types: dict[str, str]
    samples: dict[str, dict[tuple[str, tuple[tuple[str, str], ...]], float]]


def _labels(raw: str | None) -> tuple[tuple[str, str], ...]:
    if not raw or raw == "{}":
        return ()
    text = raw[1:-1]
    labels = {}
    position = 0
    while position < len(text):
        match = LABEL.match(text, position)
        if match is None or match[1] in labels:
            raise ValueError("invalid or duplicate label")
        labels[match[1]] = re.sub(r'\\([\\"n])', lambda m: "\n" if m[1] == "n" else m[1], match[2])
        position = match.end()
        if position != len(text):
            if text[position] != ",":
                raise ValueError("invalid label separator")
            position += 1
            if position == len(text):
                raise ValueError("trailing label separator")
    return tuple(sorted(labels.items()))


def parse(text: str) -> Capture:
    """Families and samples of one Prometheus exposition, refusing malformed or duplicate input."""
    types = {}
    raw_samples = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("# TYPE "):
            parts = line.split()
            if len(parts) != 4 or not re.fullmatch(NAME, parts[2]) or parts[3] not in {"gauge", "counter", "histogram"}:
                raise ValueError("invalid family type")
            if parts[2] in types:
                raise ValueError("duplicate family type")
            types[parts[2]] = parts[3]
        elif line and not line.startswith("#"):
            match = SAMPLE.fullmatch(line)
            if match is None:
                raise ValueError("invalid sample")
            value = float(match[3])
            if not math.isfinite(value):
                raise ValueError("non-finite sample")
            raw_samples.append((match[1], _labels(match[2]), value))
    samples = {name: {} for name in types}
    for name, labels, value in raw_samples:
        family = name
        if family not in types:
            family = next(
                (
                    name.removesuffix(suffix)
                    for suffix in ("_bucket", "_sum", "_count")
                    if name.endswith(suffix) and types.get(name.removesuffix(suffix)) == "histogram"
                ),
                "",
            )
        if not family:
            raise ValueError("sample without family type")
        if types[family] != "gauge" and value < 0:
            raise ValueError("negative counter or histogram")
        key = name, labels
        if key in samples[family]:
            raise ValueError("duplicate sample")
        samples[family][key] = value
    return Capture(types, samples)


def decode(body: bytes) -> dict[str, dict]:
    """One ExportMetricsServiceRequest body to {name: {kind, monotonic, temporality, unit, description, points}}."""
    from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import ExportMetricsServiceRequest

    result: dict[str, dict] = {}
    for resource in ExportMetricsServiceRequest.FromString(body).resource_metrics:
        for scope in resource.scope_metrics:
            for metric in scope.metrics:
                kind = metric.WhichOneof("data")
                data = getattr(metric, kind)
                points = {}
                for point in data.data_points:
                    labels = tuple(sorted((a.key, a.value.string_value) for a in point.attributes))
                    value = point.as_int if point.WhichOneof("value") == "as_int" else point.as_double
                    points[labels] = float(value)
                result[metric.name] = {
                    "kind": kind,
                    "monotonic": data.is_monotonic if kind == "sum" else None,
                    "temporality": data.aggregation_temporality if kind == "sum" else None,
                    "unit": metric.unit,
                    "description": metric.description,
                    "points": points,
                }
    return result


def _helps(text: str) -> dict[str, str]:
    result = {}
    for line in text.splitlines():
        match = re.fullmatch(r"# HELP (\S+) ?(.*)", line)
        if match:
            result[match[1]] = re.sub(r"\\([\\n])", lambda m: "\n" if m[1] == "n" else "\\", match[2])
    return result


def _rendered(value: float) -> float:
    """The exposition's own number rendering, so equality is exact rather than tolerance based."""
    return float(str(int(value)) if value.is_integer() else format(value, ".12g"))


def _scalar(name, expected, metric, prom, help_text, problems):
    kind = "gauge" if expected.kind == "G" else "sum"
    if metric["kind"] != kind:
        problems.append(f"{name}: instrument kind {metric['kind']}, expected {kind}")
    if kind == "sum" and (not metric["monotonic"] or metric["temporality"] != CUMULATIVE):
        problems.append(f"{name}: not a cumulative monotonic sum")
    if metric["unit"] != expected.unit:
        problems.append(f"{name}: unit {metric['unit']!r}, expected {expected.unit!r}")
    if metric["description"] != help_text:
        problems.append(f"{name}: description differs from the help text")
    _points(name, metric["points"], prom, problems)


def _points(name, exported, prom, problems):
    for labels in sorted(set(prom) - set(exported)):
        problems.append(f"{name}: sample missing from OTLP {dict(labels)}")
    for labels in sorted(set(exported) - set(prom)):
        problems.append(f"{name}: extra OTLP sample {dict(labels)}")
    for labels in sorted(set(prom) & set(exported)):
        if _rendered(exported[labels]) != prom[labels]:
            problems.append(f"{name}: value differs for {dict(labels)}")


def compare(
    text: str,
    decoded: dict[str, dict],
    *,
    rejected: Iterable[tuple[str, str]] = (),
    unmapped: Iterable[str] = (),
) -> dict:
    capture = parse(text)
    helps = _helps(text)
    rejected = sorted(set(rejected))
    refused = {family for family, _ in rejected}
    report = {
        "ok": False,
        "families": 0,
        "missing": [],
        "extra": [],
        "rejected": rejected,
        "unvalidated": [],
        "mismatched": [],
    }
    accounted: set[str] = set()
    for family, kind in capture.types.items():
        spec = otlp.SPECS.get(family)
        histogram = otlp.HISTOGRAMS.get(family)
        if spec is None and histogram is None:
            report["unvalidated"].append(family)
            continue
        names = [family + suffix for suffix in otlp.COMPONENT_UNITS] if histogram else [family]
        accounted.update(names)
        if family in refused:
            continue  # reported as rejected; the bridge deliberately did not export it
        report["families"] += 1
        expected_type = "histogram" if histogram else "gauge" if spec.kind == "G" else "counter"
        if kind != expected_type:
            report["mismatched"].append(f"{family}: Prometheus type {kind}, expected {expected_type}")
            continue
        absent = [name for name in names if name not in decoded]
        if absent:
            report["missing"].append(family)
            continue
        if histogram:
            for suffix, unit in otlp.COMPONENT_UNITS.items():
                metric = decoded[family + suffix]
                problems = report["mismatched"]
                if metric["kind"] != "sum" or not metric["monotonic"] or metric["temporality"] != CUMULATIVE:
                    problems.append(f"{family + suffix}: not a cumulative monotonic sum")
                if metric["unit"] != unit:
                    problems.append(f"{family + suffix}: unit {metric['unit']!r}, expected {unit!r}")
                if metric["description"] != helps.get(family, ""):
                    problems.append(f"{family + suffix}: description differs from the help text")
                prom = {
                    labels: value
                    for (name, labels), value in capture.samples[family].items()
                    if name == family + suffix
                }
                _points(family + suffix, metric["points"], prom, problems)
        else:
            prom = {labels: value for (_, labels), value in capture.samples[family].items()}
            _scalar(family, spec, decoded[family], prom, helps.get(family, ""), report["mismatched"])
    report["unvalidated"] = sorted(set(report["unvalidated"]) | set(unmapped))
    report["extra"] = sorted(set(decoded) - accounted)
    report["ok"] = not any(report[key] for key in ("missing", "extra", "rejected", "unvalidated", "mismatched"))
    return report

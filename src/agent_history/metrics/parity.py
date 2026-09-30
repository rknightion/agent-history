"""Deterministic Prometheus parity comparison, with explicit cutover exceptions.

This compares a legacy inventory with its replacement, not two identical deployments:
new families are reported separately and do not excuse a missing legacy family.
"""

from __future__ import annotations

import json
import math
import re
import time
from dataclasses import dataclass
from datetime import datetime
from http.client import HTTPException
from pathlib import Path
from urllib.request import HTTPRedirectHandler, build_opener

NAME = r"[a-zA-Z_:][a-zA-Z0-9_:]*"
SAMPLE = re.compile(rf"^({NAME})(\{{.*\}})?\s+(\S+)(?:\s+[0-9]+)?$")
LABEL = re.compile(r'([a-zA-Z_][a-zA-Z0-9_]*)="((?:[^"\\]|\\[\\"n])*)"')
ROSTER = Path(__file__).with_name("parity-roster.json")
SECTION_FAMILIES = frozenset(
    {
        "agent_sessions_metrics_section_success",
        "agent_sessions_metrics_section_duration_seconds",
        "agent_sessions_metrics_section_last_success_timestamp_seconds",
    }
)


class NotSynchronised(ValueError):
    """Capture times cannot prove a same-minute comparison."""


def refusal() -> dict:
    return {
        "status": "not synchronised",
        "comparison_performed": False,
        "differences": [],
        "rostered": [],
        "counts": {"kept": 0, "differences": 0, "rostered": 0},
    }


@dataclass
class Capture:
    types: dict[str, str]
    samples: dict[str, dict[tuple[str, tuple[tuple[str, str], ...]], float]]
    captured_at: float | None


def timestamp(raw: str | float) -> float:
    try:
        value = float(raw)
    except ValueError:
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("capture timestamp needs a timezone")
        value = parsed.timestamp()
    if not math.isfinite(value) or value < 0:
        raise ValueError("invalid capture timestamp")
    return value


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


def parse(text: str, captured_at: str | float | None = None) -> Capture:
    types = {}
    raw_samples = []
    stamps = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("# captured_at "):
            stamps.append(timestamp(line.removeprefix("# captured_at ")))
        elif line.startswith("# TYPE "):
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
    if len(stamps) > 1:
        raise ValueError("duplicate capture timestamp")
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
    stamp = timestamp(captured_at) if captured_at is not None else (stamps[0] if stamps else None)
    return Capture(types, samples, stamp)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # The supplied URL grants no authority to fetch a redirect destination.
        return None


def read(source: str, captured_at: str | None = None) -> Capture:
    if source.startswith(("http://", "https://")):
        started = time.time()
        with build_opener(_NoRedirect()).open(source, timeout=15) as response:
            payload = response.read(32 * 1024 * 1024 + 1)
        ended = time.time()
        if int(started // 60) != int(ended // 60):
            raise NotSynchronised("fetch crossed a minute boundary")
        default_stamp = started
    else:
        payload = Path(source).read_bytes()
        default_stamp = None  # A file's copy time is not its capture time.
    if len(payload) > 32 * 1024 * 1024:
        raise ValueError("capture too large")
    result = parse(payload.decode("utf-8"), captured_at)
    if result.captured_at is None:
        result.captured_at = default_stamp
    return result


def load_roster(path: Path) -> list[dict]:
    data = json.loads(path.read_text())
    if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("entries"), list):
        raise ValueError("invalid roster")
    seen = set()
    for entry in data["entries"]:
        if not isinstance(entry, dict) or set(entry) - {"scope", "name", "action", "reason", "target"}:
            raise ValueError("invalid roster entry")
        if entry.get("scope") not in {"family", "section"} or entry.get("action") not in {
            "dropped",
            "renamed",
            "rebase-allowed",
            "not-emitted",
        }:
            raise ValueError("invalid roster scope or action")
        if (
            not isinstance(entry.get("name"), str)
            or not entry["name"]
            or not isinstance(entry.get("reason"), str)
            or not entry["reason"]
        ):
            raise ValueError("roster entry needs a name and reason")
        if entry["scope"] == "section" and entry["name"] in {"loops", "efficiency", "archive", "storage"}:
            raise ValueError("kept section cannot be rostered")
        if entry["action"] == "not-emitted" and entry["scope"] != "family":
            raise ValueError("not-emitted applies only to whole families")
        if entry["action"] == "renamed" and (not isinstance(entry.get("target"), str) or not entry["target"]):
            raise ValueError("rename needs a target")
        if entry["action"] == "rebase-allowed" and (
            entry["scope"] != "family"
            or entry["name"]
            not in {
                "agent_sessions_metrics_collection_runs_total",
                "agent_sessions_metrics_collection_failures_total",
            }
        ):
            raise ValueError("only process-health counters may rebase")
        key = entry["scope"], entry["name"]
        if key in seen:
            raise ValueError("duplicate roster entry")
        seen.add(key)
    return data["entries"]


def compare(legacy: Capture, new: Capture, roster: list[dict], ended_loops: frozenset[str] = frozenset()) -> dict:
    report = {
        "status": "equal",
        "differences": [],
        "rostered": [],
        "new_families": sorted(set(new.types) - set(legacy.types)),
    }
    a, b = legacy.captured_at, new.captured_at
    if a is None or b is None or abs(a - b) > 60 or int(a // 60) != int(b // 60):
        return refusal()
    families = {e["name"]: e for e in roster if e["scope"] == "family"}
    sections = {e["name"]: e for e in roster if e["scope"] == "section"}
    kept = 0

    def difference(kind, family, **detail):
        report["differences"].append({"class": kind, "family": family, **detail})

    def rostered(entry, family):
        item = {**entry, "family": family}
        if item not in report["rostered"]:
            report["rostered"].append(item)

    for entry in roster:
        if entry["action"] == "not-emitted":
            rostered(entry, entry["name"])
            if entry["name"] in legacy.types or entry["name"] in new.types:
                difference("unexpected-emission", entry["name"])

    for family, metric_type in sorted(legacy.types.items()):
        entry = families.get(family)
        if entry and entry["action"] == "not-emitted":
            continue
        if entry and entry["action"] == "dropped":
            rostered(entry, family)
            continue
        target = entry["target"] if entry and entry["action"] == "renamed" else family
        if entry and entry["action"] == "renamed":
            rostered(entry, family)
        if target not in new.types:
            difference("missing-family", family)
            continue
        if new.types[target] != metric_type:
            difference("family-type", family)
            continue
        kept += 1
        old_values = dict(legacy.samples[family])
        new_values = dict(new.samples[target])
        if family in SECTION_FAMILIES:
            transformed = {}
            for (name, labels), value in old_values.items():
                section = dict(labels).get("section")
                mapping = sections.get(section)
                if mapping:
                    rostered(mapping, family)
                    if mapping["action"] == "dropped":
                        continue
                    if mapping["action"] == "renamed":
                        labels = tuple((k, mapping["target"] if k == "section" else v) for k, v in labels)
                transformed[name, labels] = value
            old_values = transformed
            # Removed section health is not required from the new process.
            new_values = {
                k: v
                for k, v in new_values.items()
                if not (
                    dict(k[1]).get("section") in sections and sections[dict(k[1])["section"]]["action"] == "dropped"
                )
            }
        if target != family:
            old_values = {(target + name[len(family) :], labels): value for (name, labels), value in old_values.items()}
        old_keys = {(name, tuple(k for k, _ in labels)) for name, labels in old_values}
        new_keys = {(name, tuple(k for k, _ in labels)) for name, labels in new_values}
        if old_keys != new_keys:
            difference("label-keys", family)
        # Added self sections are intentional extra coverage, but every kept old section is required.
        missing = set(old_values) - set(new_values)
        extra = set(new_values) - set(old_values) if family not in SECTION_FAMILIES else set()
        if missing or extra:
            difference("label-values", family, legacy_only=len(missing), new_only=len(extra))
        if entry and entry["action"] == "rebase-allowed":
            if metric_type != "counter":
                difference("invalid-rebase-type", family)
            else:
                rostered(entry, family)
            kept -= 1
            continue
        # Every kept value carries the same numeric contract, including gauges, except the
        # collector's own process timings: two processes never time the same work alike.
        if (
            metric_type == "gauge"
            and family.startswith("agent_sessions_metrics_")
            and family.endswith(("_duration_seconds", "_timestamp_seconds"))
        ):
            continue
        for key in sorted(set(old_values) & set(new_values)):
            old, current = old_values[key], new_values[key]
            ended = dict(key[1]).get("loop") in ended_loops
            tolerance = 0 if ended else 0.005 * abs(old)
            if abs(old - current) > tolerance:
                difference(
                    "ended-loop-counter" if ended else "counter-tolerance",
                    family,
                    sample=key[0],
                    absolute_difference=abs(old - current),
                    allowed_difference=tolerance,
                )
    if report["differences"]:
        report["status"] = "different"
    report["counts"] = {"kept": kept, "differences": len(report["differences"]), "rostered": len(report["rostered"])}
    return report


def run(args) -> int:
    try:
        report = compare(
            read(args.legacy, args.legacy_at),
            read(args.new, args.new_at),
            load_roster(args.roster),
            frozenset(args.ended_loop),
        )
    except NotSynchronised:
        report = refusal()
    except (OSError, ValueError, KeyError, TypeError, HTTPException) as exc:
        # Never echo input samples, URLs, paths or credentials in public diagnostics.
        report = {"status": "invalid input", "error": type(exc).__name__}
    print(json.dumps(report, sort_keys=True, separators=(",", ":")))
    return {"equal": 0, "different": 1}.get(report["status"], 2)

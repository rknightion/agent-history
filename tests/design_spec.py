"""The legacy metric mapping, read from docs/otel-design.md so tests derive expectations from the
requirement and never from the implementation under test."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

DESIGN = Path(__file__).resolve().parents[1] / "docs" / "otel-design.md"
STORAGE = ("tier", "namespace", "agent", "profile", "machine")
ROW = re.compile(r"^\| `(?P<name>[a-z_]+)` \| (?P<kind>[GC]) \| `(?P<unit>[^`]+)` \| (?P<attrs>.+) \|$")
HISTOGRAM_ROW = re.compile(r"^\| `(?P<name>[a-z_]+)` \| `(?P<attrs>[^`]+)` \| `(?P<bounds>[0-9,]+)` \|$")


@dataclass(frozen=True)
class Spec:
    kind: str  # G or C
    unit: str
    attributes: tuple[str, ...]  # sorted


@dataclass(frozen=True)
class HistogramSpec:
    attributes: tuple[str, ...]  # sorted
    bounds: tuple[str, ...]


def _attributes(raw: str, section: str) -> tuple[str, ...]:
    raw = raw.strip()
    if raw == "none":
        names: list[str] = []
    elif raw == "S":
        names = list(STORAGE)
    else:
        names = raw.strip("`").split(",")
    if section == "Efficiency counters":
        names = ["agent", "namespace", *names, "loop"]
    return tuple(sorted(names))


def load() -> tuple[dict[str, Spec], dict[str, HistogramSpec]]:
    specs: dict[str, Spec] = {}
    histograms: dict[str, HistogramSpec] = {}
    section = ""
    for line in DESIGN.read_text().splitlines():
        if line.startswith("#"):
            section = line.lstrip("# ").strip()
        row = ROW.match(line)
        if row:
            assert row["name"] not in specs, row["name"]
            specs[row["name"]] = Spec(row["kind"], row["unit"], _attributes(row["attrs"], section))
            continue
        histogram = HISTOGRAM_ROW.match(line)
        if histogram:
            histograms[histogram["name"]] = HistogramSpec(
                tuple(sorted(histogram["attrs"].split(","))),
                (*histogram["bounds"].split(","), "+Inf"),
            )
    return specs, histograms

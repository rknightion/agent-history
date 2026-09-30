"""Frozen collector seam shared by the exporter and every collector."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Literal, Protocol


@dataclass(frozen=True)
class Sample:
    labels: tuple[tuple[str, str], ...]  # sorted by label name
    value: float
    name: str | None = None  # histogram bucket/sum/count sample; otherwise the family name


@dataclass(frozen=True)
class Family:
    name: str  # full metric name, e.g. agent_efficiency_llm_calls_total
    type: Literal["counter", "gauge", "histogram"]
    help: str
    samples: tuple[Sample, ...]


class Collector(Protocol):
    name: str

    def collect(self) -> Iterable[Family]: ...

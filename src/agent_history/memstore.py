"""In-memory stand-in for the loader's upsert semantics, for tests and offline validation.

Applies each Row class's KEY and POLICY exactly as load.py does in SQL, so parser tests and
corpus sweeps can assert on the rows the database would hold without a Postgres instance.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

from .model import (KEEP, MAX, MIN, NOTHING, OR, UPDATE, FileContext, LinePos, ParseIssueRow,
                    Row)


def _merge(policy: str, old: Any, new: Any) -> Any:
    if policy == KEEP:
        return old if old is not None else new
    if policy == UPDATE:
        return new if new is not None else old
    if policy == MAX:
        if old is None:
            return new
        return old if new is None else max(old, new)
    if policy == MIN:
        if old is None:
            return new
        return old if new is None else min(old, new)
    if policy == OR:
        if old is None and new is None:
            return None
        return bool(old) or bool(new)
    raise ValueError(policy)


class MemStore:
    def __init__(self) -> None:
        self.tables: dict[str, dict[tuple, dict[str, Any]]] = {}
        self.issues: list[ParseIssueRow] = []

    def upsert(self, row: Row, source: str = "") -> None:
        if isinstance(row, ParseIssueRow):
            self.issues.append(row)
        cols = row.columns()
        key = tuple(source if k == "source" else _hashable(cols.get(k)) for k in row.KEY)
        table = self.tables.setdefault(row.TABLE, {})
        existing = table.get(key)
        if existing is None:
            table[key] = dict(cols)
            return
        if row.TABLE == "record_type_seen":  # the loader adds counts across batches
            existing["count"] = (existing.get("count") or 0) + (cols.get("count") or 0)
            return
        for name, value in cols.items():
            if name in row.KEY:
                continue
            policy = row.POLICY.get(name, row.DEFAULT_POLICY)
            if policy == NOTHING:
                continue
            existing[name] = _merge(policy, existing.get(name), value)

    def rows(self, table: str) -> list[dict[str, Any]]:
        return list(self.tables.get(table, {}).values())


def _hashable(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return json.dumps(value, sort_keys=True)
    return value


def run_file(parser_cls: type, ctx: FileContext, store: MemStore, *, start: int = 0,
             state: dict[str, Any] | None = None, line_base: int = 0, batch_lines: int = 5000,
             stop_after_lines: int | None = None) -> tuple[int, dict[str, Any]]:
    """Feed a JSONL file through a parser like the loader does; returns (end_offset, state).

    Reads whole lines only, flushes and snapshots state every `batch_lines`, and recreates the
    parser from the snapshot at each batch boundary so tests exercise incremental resumption.
    """
    offset = start
    state = dict(state or {})
    parser = parser_cls(ctx, state)
    count = 0
    with Path(ctx.path).open("rb") as handle:
        handle.seek(start)
        line_number = line_base
        while True:
            raw = handle.readline()
            if not raw or not raw.endswith(b"\n"):
                break
            line_number += 1
            pos = LinePos(offset, len(raw), line_number)
            try:
                record = json.loads(raw)
            except (ValueError, UnicodeDecodeError):
                store.upsert(ParseIssueRow(byte_offset=offset, kind="json_error", line_number=line_number),
                             ctx.rel_path)
                record = None
            if isinstance(record, dict):
                for row in parser.line(record, pos):
                    store.upsert(row, ctx.rel_path)
            offset += len(raw)
            count += 1
            if stop_after_lines is not None and count >= stop_after_lines:
                break
            if count % batch_lines == 0:
                for row in parser.flush():
                    store.upsert(row, ctx.rel_path)
                state = json.loads(json.dumps(parser.state()))
                parser = parser_cls(ctx, state)
    for row in parser.flush():
        store.upsert(row, ctx.rel_path)
    return offset, json.loads(json.dumps(parser.state()))


def upsert_all(store: MemStore, rows: Iterable[Row], source: str = "") -> None:
    for row in rows:
        store.upsert(row, source)

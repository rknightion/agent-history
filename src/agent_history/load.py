"""Loader: source inventory, incremental parsing, idempotent upserts, post-passes, metrics.

Owns every database write. Parsers (parse_claude, parse_codex, parse_pi) are pure and only yield rows.
One transaction per source-file batch covers the rows, the dirty-session marks and the source
offset/state update, so a crash can never double-count or lose a batch.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, Iterator

import psycopg
from psycopg.types.json import Jsonb

from . import model
from .common import parse_ts
from .model import (KEEP, MAX, MIN, NOTHING, OR, UPDATE, AttachmentRow, FileContext, LinePos, MessageRow,
                    ParseIssueRow, RecordTypeRow, Row, SessionKey, SessionRow, ToolIoRow)

HOT_ROOT: Path | None = None    # archive layout: <root>/<namespace>/... (optional)
COLD_ROOT: Path | None = None   # second archive tier, checked for fresh receipts (optional)
TEXTFILE: Path | None = None    # Prometheus textfile output (optional)
ADVISORY_LOCK = 0x61676869  # 'aghi'
CHECKPOINT_BYTES = 64 * 1024
BATCH_LINES = 20_000
BATCH_BYTES = 64 * 1024 * 1024
SCHEMA_VERSION = "1"
PACKAGE_DIR = Path(__file__).resolve().parent
SQL_DIR = PACKAGE_DIR / "sql"


# --- inventory ---------------------------------------------------------------------------------


def namespace_details(namespace: str) -> tuple[str, str, str | None]:
    agent = "claude" if namespace.startswith("claude-") else ("pi" if namespace.startswith("pi-") else "codex")
    rest = namespace.removeprefix(agent + "-")
    if rest.startswith("standalone-"):
        return agent, "standalone", rest.removeprefix("standalone-")
    return agent, rest, None


def file_role(agent: str, parts: tuple[str, ...]) -> str | None:
    """Role of a transcript path relative to its namespace directory, or None to skip it."""
    if not parts or not parts[-1].endswith(".jsonl") or "_history" in parts:
        return None
    if agent == "codex":
        return "main" if parts[0] in {"sessions", "archived_sessions"} else None
    if agent == "pi":
        # sessions/<cwd-slug>/<file> is a session; pi-subagents children live under
        # <root base>/<run dir>/run-<i>/ (parse_pi.lineage). Artifact transcript copies contribute
        # run/response evidence only, never messages or LLM calls.
        if parts[0] != "sessions":
            return None
        if len(parts) == 4 and parts[2] == "subagent-artifacts":
            return "pi_artifact" if parts[3].endswith("_transcript.jsonl") else None
        if "subagent-artifacts" in parts:
            return None
        if len(parts) == 3:
            return "main"
        return "subagent" if len(parts) >= 6 and parts[-2].startswith("run-") else None
    if parts[0] != "projects":
        return None
    if "subagents" in parts:
        if "workflows" in parts:
            return "workflow_journal" if parts[-1] == "journal.jsonl" else "workflow_agent"
        return "subagent" if parts[-1].startswith("agent-") else None
    return "main" if len(parts) == 3 else None


def logical_uid(agent: str, role: str, parts: tuple[str, ...]) -> str | None:
    stem = parts[-1].removesuffix(".jsonl")
    if agent == "pi":
        if role == "pi_artifact":
            return None
        from .parse_pi import lineage
        found = lineage("/".join(parts))
        return found.get("path") or found.get("root")
    if agent == "codex":
        return stem[-36:] if len(stem) >= 36 else stem
    if role == "main":
        return stem
    try:
        session = parts[parts.index("subagents") - 1]
    except ValueError:
        return None
    return f"{session}/{stem.removeprefix('agent-')}"


@dataclass
class SourceEntry:
    rel_path: str
    namespace: str
    agent: str
    profile: str
    machine: str | None
    role: str
    logical_uid: str | None
    hot: Path | None = None
    cold: Path | None = None

    @property
    def path(self) -> Path:
        return self.hot or self.cold  # type: ignore[return-value]

    @property
    def tier(self) -> str:
        return "hot" if self.hot else "cold"


def list_namespaces(*roots: Path | None) -> list[str]:
    names: set[str] = set()
    for root in roots:
        if root is None or not root.is_dir():
            continue
        for child in root.iterdir():
            if child.is_dir() and not child.is_symlink() and child.name.startswith(("claude-", "codex-", "pi-")):
                names.add(child.name)
    return sorted(names)


def inventory(hot: Path | None, cold: Path | None, namespaces: Iterable[str] | None = None) -> dict[str, SourceEntry]:
    result: dict[str, SourceEntry] = {}
    selected = list(namespaces) if namespaces else list_namespaces(hot, cold)
    for tier, root in (("cold", cold), ("hot", hot)):
        if root is None or not root.is_dir():
            continue
        for namespace in selected:
            base = root / namespace
            if not base.is_dir() or base.is_symlink():
                continue
            agent, profile, machine = namespace_details(namespace)
            for dirpath, dirnames, filenames in os.walk(base):
                dirnames[:] = [d for d in dirnames if not d.startswith(".") and d != "_history"]
                for name in filenames:
                    if not name.endswith(".jsonl"):
                        continue
                    path = Path(dirpath) / name
                    if path.is_symlink():
                        continue
                    parts = path.relative_to(base).parts
                    role = file_role(agent, parts)
                    if role is None:
                        continue
                    rel = f"{namespace}/{'/'.join(parts)}"
                    entry = result.get(rel)
                    if entry is None:
                        entry = result[rel] = SourceEntry(rel, namespace, agent, profile, machine, role,
                                                          logical_uid(agent, role, parts))
                    setattr(entry, tier, path)
    return result


def inventory_map(sources: dict[str, Path], *, strict: bool = False) -> dict[str, SourceEntry]:
    """Inventory from a namespace -> agent home map (config.py), e.g. claude-local -> ~/.claude.

    Only the subtrees a parser reads are walked: Claude projects/, Codex sessions/ and
    archived_sessions/, pi sessions/. Symlinked files are skipped, as in the archive layout.
    """
    result: dict[str, SourceEntry] = {}
    for namespace, base in sorted(sources.items()):
        if strict:
            with os.scandir(base):
                pass
        if not base.is_dir():
            continue
        agent, profile, machine = namespace_details(namespace)
        tops = {"claude": ("projects",), "codex": ("sessions", "archived_sessions"),
                "pi": ("sessions",)}[agent]
        for top in tops:
            start = base / top
            if strict:
                import stat
                try:
                    is_directory = stat.S_ISDIR(start.stat().st_mode)
                except FileNotFoundError:
                    continue
            else:
                is_directory = start.is_dir()
            if not is_directory or start.is_symlink():
                continue
            def walk_error(exc):
                if strict:
                    raise exc
            for dirpath, dirnames, filenames in os.walk(start, onerror=walk_error):
                dirnames[:] = sorted(d for d in dirnames if not d.startswith(".") and d != "_history")
                for name in sorted(filenames):
                    if not name.endswith(".jsonl"):
                        continue
                    path = Path(dirpath) / name
                    if path.is_symlink():
                        continue
                    parts = path.relative_to(base).parts
                    role = file_role(agent, parts)
                    if role is None:
                        continue
                    if strict:
                        with path.open('rb'):
                            pass
                    rel = f"{namespace}/{'/'.join(parts)}"
                    result[rel] = SourceEntry(rel, namespace, agent, profile, machine, role,
                                              logical_uid(agent, role, parts), hot=path)
    return result


def check_rebuild_sources(conn: psycopg.Connection, sources: dict[str, Path],
                          cold_sources: dict[str, Path] | None = None
                          ) -> tuple[dict[str, SourceEntry], dict[str, dict[str, int | bool]]]:
    """Read-only preflight: no lock or mutation; diagnostics contain counts, never paths."""
    cold_sources = cold_sources or {}
    if set(cold_sources) - set(sources):
        raise ValueError("cold_sources namespaces must have a hot source")
    entries = inventory_map(sources)
    report: dict[str, dict[str, int | bool]] = {}
    catalogued: dict[str, set[str]] = {n: set() for n in cold_sources}
    if cold_sources:
        for namespace, rel in conn.execute("SELECT namespace, rel_path FROM ah.source_file"):
            if namespace in catalogued:
                catalogued[namespace].add(rel)
    for namespace in sorted(sources):
        hot_keys = {rel for rel, e in entries.items() if e.namespace == namespace}
        cold_entries: dict[str, SourceEntry] = {}
        unreadable = 0
        if namespace in cold_sources:
            try:
                cold_entries = inventory_map({namespace: cold_sources[namespace]}, strict=True)
            except OSError:
                unreadable = 1
        cold_keys = set(cold_entries)
        missing = len(catalogued.get(namespace, set()) - hot_keys - cold_keys)
        # An unreadable tree has unknown coverage; do not double-count its missing files.
        count = unreadable or missing
        report[namespace] = {"hot": len(hot_keys), "cold": len(cold_keys),
                             "cold_only": len(cold_keys - hot_keys), "missing": missing,
                             "unreadable": unreadable, "count": count, "refused": bool(count)}
        for rel, cold_entry in cold_entries.items():
            if rel in entries:
                entries[rel].cold = cold_entry.path
            else:
                cold_entry.cold, cold_entry.hot = cold_entry.path, None
                entries[rel] = cold_entry
    return entries, report


def cold_tier_ok(cold: Path | None, max_age_s: int = 48 * 3600) -> bool:
    """The cold tree is mounted and its archive receipts are fresh."""
    if cold is None or not cold.is_dir():
        return False
    receipts = cold / ".archive-receipts"
    try:
        newest = max((p.stat().st_mtime for p in receipts.iterdir()), default=0)
    except OSError:
        return False
    return time.time() - newest < max_age_s


# --- digests -----------------------------------------------------------------------------------


def file_digest(path: Path, start: int, end: int) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        handle.seek(start)
        remaining = max(0, end - start)
        while remaining:
            block = handle.read(min(1 << 20, remaining))
            if not block:
                break
            digest.update(block)
            remaining -= len(block)
    return digest.hexdigest()


def checkpoints(path: Path, offset: int) -> tuple[int, str, str]:
    start = max(0, offset - CHECKPOINT_BYTES)
    return start, file_digest(path, start, offset), file_digest(path, 0, min(offset, CHECKPOINT_BYTES))


def prefix_verified(row: dict[str, Any], path: Path) -> bool:
    offset = row["indexed_offset"]
    if not offset:
        return False
    try:
        return (file_digest(path, row["checkpoint_start"], offset) == row["checkpoint_sha256"]
                and file_digest(path, 0, min(offset, CHECKPOINT_BYTES)) == row["head_sha256"])
    except OSError:
        return False


# --- value adaptation --------------------------------------------------------------------------


def clean_text(value: str) -> str:
    if "\x00" in value:
        value = value.replace("\x00", "")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        value = value.encode("utf-8", "replace").decode("utf-8")
    return value


def adapt(value: Any) -> Any:
    if isinstance(value, Jsonb):
        return value
    if isinstance(value, str):
        return clean_text(value)
    if isinstance(value, dict):
        return Jsonb(json.loads(clean_text(json.dumps(value, ensure_ascii=False, default=str))))
    if isinstance(value, list):
        return [adapt(v) for v in value]
    return value


def policy_sql(table: str, column: str, policy: str) -> str:
    old, new = f"{table}.{column}", f"EXCLUDED.{column}"
    if policy == KEEP:
        return f"{column} = COALESCE({old}, {new})"
    if policy == UPDATE:
        return f"{column} = COALESCE({new}, {old})"
    if policy == MAX:
        return f"{column} = GREATEST({old}, {new})"
    if policy == MIN:
        return f"{column} = LEAST({old}, {new})"
    if policy == OR:
        return f"{column} = CASE WHEN {old} IS NULL AND {new} IS NULL THEN NULL ELSE (COALESCE({old}, false) OR COALESCE({new}, false)) END"
    raise ValueError(policy)


# --- writer ------------------------------------------------------------------------------------


SESSION_REF_COLUMN = {"subagent_spawn": "parent_session_id"}
# tool_io text column -> prefix of the derived <prefix>_bytes / <prefix>_sha256 columns
TOOL_IO_TEXT = {"input_text": "input", "output_text": "output", "stdout_text": "stdout",
                "stderr_text": "stderr", "result_json": "result"}


class Writer:
    """Turns row objects into upserts inside the caller's transaction."""

    def __init__(self, conn: psycopg.Connection) -> None:
        self.conn = conn
        self.session_ids: dict[SessionKey, int] = {}
        self._sql: dict[tuple[type[Row], tuple[str, ...]], str] = {}

    def session_id(self, key: SessionKey) -> int:
        cached = self.session_ids.get(key)
        if cached is not None:
            return cached
        params = (key.agent, key.session_uid, key.agent_id)
        row = self.conn.execute("SELECT id FROM ah.session WHERE agent=%s AND session_uid=%s AND agent_id=%s",
                                params).fetchone()
        if row is None:
            row = self.conn.execute(
                "INSERT INTO ah.session (agent, session_uid, agent_id) VALUES (%s,%s,%s) "
                "ON CONFLICT (agent, session_uid, agent_id) DO NOTHING RETURNING id", params).fetchone()
        if row is None:
            row = self.conn.execute("SELECT id FROM ah.session WHERE agent=%s AND session_uid=%s AND agent_id=%s",
                                    params).fetchone()
        assert row is not None
        self.session_ids[key] = row[0]
        return row[0]

    @staticmethod
    def key_columns(cls: type[Row]) -> tuple[str, ...]:
        if cls is ParseIssueRow:
            return ("source_id", "byte_offset", "kind")
        ref = SESSION_REF_COLUMN.get(cls.TABLE, "session_id")
        return tuple(ref if k == "session" else k for k in cls.KEY)

    def _statement(self, cls: type[Row], columns: tuple[str, ...]) -> str:
        cache_key = (cls, columns)
        sql = self._sql.get(cache_key)
        if sql:
            return sql
        key_columns = self.key_columns(cls)
        head = (f"INSERT INTO ah.{cls.TABLE} AS {cls.TABLE} ({','.join(columns)}) "
                f"VALUES ({','.join(['%s'] * len(columns))}) ON CONFLICT ({','.join(key_columns)}) ")
        updates = []
        if cls.DEFAULT_POLICY != NOTHING:
            for column in columns:
                if column in key_columns or column == "source_id":
                    continue
                field_name = "session" if column in {"session_id", "parent_session_id"} else column
                if column in {"namespace", "profile"}:
                    field_name = "session"   # KEEP: first observer's filter columns win
                updates.append(policy_sql(cls.TABLE, column, cls.POLICY.get(field_name, cls.DEFAULT_POLICY)))
        sql = head + ("DO UPDATE SET " + ", ".join(updates) if updates else "DO NOTHING")
        self._sql[cache_key] = sql
        return sql

    def write(self, rows: list[Row], source_id: int, ctx: FileContext) -> set[int]:
        """Upsert rows; return the session ids they touched."""
        touched: set[int] = set()
        grouped: dict[tuple[type[Row], tuple[str, ...]], list[tuple]] = {}
        for row in rows:
            if isinstance(row, SessionRow):
                self._write_session(row, ctx)
                touched.add(self.session_id(row.session))
                continue
            if isinstance(row, RecordTypeRow):
                self._write_record_type(row)
                continue
            out: dict[str, Any] = {}
            for name, value in row.columns().items():
                if name == "session":
                    sid = self.session_id(value)
                    touched.add(sid)
                    out[SESSION_REF_COLUMN.get(row.TABLE, "session_id")] = sid
                else:
                    out[name] = value
            if isinstance(row, MessageRow):
                text = clean_text(row.text)
                out["text"] = text
                out["content_sha256"] = out["text_sha256"] = hashlib.sha256(text.encode()).hexdigest()
                out["namespace"] = ctx.namespace
                out["profile"] = ctx.profile
            elif isinstance(row, ToolIoRow):
                for column, prefix in TOOL_IO_TEXT.items():
                    value = out.get(column)
                    if value is not None:
                        value = out[column] = clean_text(value)
                        raw = value.encode()
                        out[f"{prefix}_bytes"] = len(raw)
                        out[f"{prefix}_sha256"] = hashlib.sha256(raw).hexdigest()
                    else:
                        out[f"{prefix}_bytes"] = out[f"{prefix}_sha256"] = None
                out["namespace"] = ctx.namespace
                out["profile"] = ctx.profile
                if out.get("output_parts") is not None:   # a JSON list, not a Postgres array
                    out["output_parts"] = Jsonb(json.loads(clean_text(json.dumps(out["output_parts"], default=str))))
            elif isinstance(row, AttachmentRow):
                if out.get("text") is not None:
                    out["text"] = clean_text(out["text"])
                    out["text_sha256"] = hashlib.sha256(out["text"].encode()).hexdigest()
                out["namespace"] = ctx.namespace
            if isinstance(row, ParseIssueRow) and out.get("detail"):
                out["detail"] = str(out["detail"])[:120]
            out["source_id"] = source_id
            columns = tuple(out.keys())
            grouped.setdefault((type(row), columns), []).append(tuple(adapt(out[c]) for c in columns))
        with self.conn.cursor() as cur:
            for (cls, columns), params in grouped.items():
                cur.executemany(self._statement(cls, columns), params)
        return touched

    def _write_session(self, row: SessionRow, ctx: FileContext) -> None:
        sid = self.session_id(row.session)
        cols = {k: v for k, v in row.columns().items() if k not in {"session", "byte_offset"}}
        sets = ["is_stub = false", "namespace = COALESCE(namespace, %s)", "profile = COALESCE(profile, %s)",
                "machine = COALESCE(machine, %s)"]
        params: list[Any] = [ctx.namespace, ctx.profile, ctx.machine]
        for name, value in cols.items():
            if value is None:
                continue
            policy = SessionRow.POLICY.get(name, SessionRow.DEFAULT_POLICY)
            if policy == KEEP:
                sets.append(f"{name} = COALESCE({name}, %s)")
            elif policy == MAX:
                sets.append(f"{name} = GREATEST({name}, %s)")
            elif policy == MIN:
                sets.append(f"{name} = LEAST({name}, %s)")
            elif policy == OR:
                sets.append(f"{name} = (COALESCE({name}, false) OR %s)")
            else:
                sets.append(f"{name} = %s")
            params.append(adapt(value))
        params.append(sid)
        self.conn.execute(f"UPDATE ah.session SET {', '.join(sets)} WHERE id = %s", params)

    def _write_record_type(self, row: RecordTypeRow) -> None:
        self.conn.execute(
            "INSERT INTO ah.record_type_seen (agent, record_type, subtype, key_set, first_seen, last_seen, "
            "seen_count, cli_version) VALUES (%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT (agent, record_type, subtype) DO UPDATE SET "
            "first_seen = LEAST(record_type_seen.first_seen, EXCLUDED.first_seen), "
            "last_seen = GREATEST(record_type_seen.last_seen, EXCLUDED.last_seen), "
            "seen_count = record_type_seen.seen_count + EXCLUDED.seen_count, "
            "key_set = COALESCE(EXCLUDED.key_set, record_type_seen.key_set), "
            "cli_version = COALESCE(EXCLUDED.cli_version, record_type_seen.cli_version)",
            (row.agent, row.record_type, row.subtype or "", row.key_set, row.ts, row.ts, row.count,
             row.cli_version),
        )


# --- parsing driver ----------------------------------------------------------------------------


def parser_for(agent: str, role: str | None = None) -> tuple[type, str]:
    if agent == "claude":
        from .parse_claude import ClaudeParser
        return ClaudeParser, model.PARSER_VERSION_CLAUDE
    if agent == "pi":
        from .parse_pi import PARSER_VERSION, PiArtifactParser, PiParser
        return (PiArtifactParser if role == "pi_artifact" else PiParser), PARSER_VERSION
    from .parse_codex import CodexParser
    return CodexParser, model.PARSER_VERSION_CODEX


def read_batches(path: Path, offset: int, line_base: int) -> Iterator[list[tuple[bytes, LinePos]]]:
    with path.open("rb") as handle:
        handle.seek(offset)
        batch: list[tuple[bytes, LinePos]] = []
        size = 0
        line_number = line_base
        while True:
            raw = handle.readline()
            if not raw or not raw.endswith(b"\n"):
                break
            line_number += 1
            batch.append((raw, LinePos(offset, len(raw), line_number)))
            offset += len(raw)
            size += len(raw)
            if len(batch) >= BATCH_LINES or size >= BATCH_BYTES:
                yield batch
                batch, size = [], 0
        if batch:
            yield batch


@dataclass
class RunStats:
    started: float = field(default_factory=time.time)
    files_seen: int = 0
    files_parsed: int = 0
    files_rewritten: int = 0
    files_tier_only: int = 0
    lines: int = 0
    bytes: int = 0
    rows: int = 0
    errors: int = 0
    lock_held: bool = False


OWNED_TABLES = ("message", "llm_call", "tool_call", "tool_op", "subagent_spawn", "pi_run_response", "hook_event", "compaction",
                "git_event", "artifact", "session_event", "rate_limit_sample", "cost_state", "turn", "parse_issue",
                "tool_io", "attachment", "file_touch", "session_continuation")


def process_source(conn: psycopg.Connection, writer: Writer, entry: SourceEntry,
                   existing: dict[str, Any] | None, stats: RunStats) -> None:
    """Index one source. Any failure is contained to this file: rolled back, recorded, skipped."""
    try:
        _process_source(conn, writer, entry, existing, stats)
    except Exception as exc:  # one bad file must never stop the run
        conn.rollback()
        writer.session_ids.clear()
        stats.errors += 1
        try:
            row = conn.execute("SELECT id FROM ah.source_file WHERE rel_path = %s", (entry.rel_path,)).fetchone()
            if row:
                conn.execute(
                    "INSERT INTO ah.parse_issue (source_id, byte_offset, kind, detail) VALUES (%s,-1,'load_error',%s) "
                    "ON CONFLICT (source_id, byte_offset, kind) DO UPDATE SET detail = EXCLUDED.detail, created_at = now()",
                    (row[0], f"{type(exc).__name__}:{getattr(exc, 'sqlstate', '') or ''}"))
                conn.execute("UPDATE ah.source_file SET status='error' WHERE id=%s", (row[0],))
            conn.commit()
        except psycopg.Error:
            conn.rollback()


def purge_source(conn: psycopg.Connection, source_id: int) -> None:
    """Delete every row this source owns and reset its bookkeeping (inside the caller's transaction)."""
    conn.execute("INSERT INTO ah.dirty_session (session_id) "
                 "SELECT DISTINCT session_id FROM ah.llm_call WHERE source_id = %s "
                 "UNION SELECT DISTINCT session_id FROM ah.message WHERE source_id = %s ON CONFLICT DO NOTHING",
                 (source_id, source_id))
    for table in OWNED_TABLES:
        conn.execute(f"DELETE FROM ah.{table} WHERE source_id = %s", (source_id,))
    conn.execute("UPDATE ah.source_file SET indexed_offset = 0, line_count = 0, checkpoint_start = 0, "
                 "checkpoint_sha256 = NULL, head_sha256 = NULL, parser_state = '{}', status = 'pending', "
                 "rewrite_count = rewrite_count + 1 WHERE id = %s", (source_id,))


def _process_source(conn: psycopg.Connection, writer: Writer, entry: SourceEntry,
                    existing: dict[str, Any] | None, stats: RunStats) -> None:
    path = entry.path
    try:
        stat = path.stat()
    except OSError:
        return
    parser_cls, parser_version = parser_for(entry.agent, entry.role)
    ctx = FileContext(str(path), entry.rel_path, entry.namespace, entry.agent, entry.profile,
                      entry.machine, entry.role)
    tier_cols = (entry.tier, entry.hot is not None, entry.cold is not None, stat.st_size, stat.st_mtime_ns)

    if existing is None:
        source_id = conn.execute(
            "INSERT INTO ah.source_file (rel_path, namespace, agent, profile, machine, file_role, logical_uid, "
            "tier, available_hot, available_cold, size_bytes, mtime_ns, parser_version) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
            (entry.rel_path, entry.namespace, entry.agent, entry.profile, entry.machine, entry.role,
             entry.logical_uid, *tier_cols, parser_version),
        ).fetchone()[0]
        conn.commit()
        offset, line_base, state = 0, 0, {}
    else:
        source_id = existing["id"]
        offset = existing["indexed_offset"]
        if existing["parser_version"] != parser_version and offset:
            # Reparsing in place cannot correct DO NOTHING / KEEP rows: a parser upgrade needs
            # `agent-history rebuild`. Refuse, loudly, rather than mix parser versions.
            conn.execute(
                "INSERT INTO ah.parse_issue (source_id, byte_offset, kind, detail) VALUES (%s,-1,'parser_upgraded',%s) "
                "ON CONFLICT DO NOTHING", (source_id, f"{existing['parser_version']}->{parser_version}"))
            conn.execute("UPDATE ah.source_file SET status='error' WHERE id=%s", (source_id,))
            conn.commit()
            stats.errors += 1
            return
        unchanged = (stat.st_size == offset and existing["status"] == "indexed"
                     and existing["mtime_ns"] == stat.st_mtime_ns and existing["tier"] == entry.tier)
        if unchanged:
            return
        if offset and stat.st_size >= offset and prefix_verified(existing, path):
            if stat.st_size == offset:
                conn.execute(
                    "UPDATE ah.source_file SET tier=%s, available_hot=%s, available_cold=%s, size_bytes=%s, "
                    "mtime_ns=%s, status='indexed' WHERE id=%s", (*tier_cols, source_id))
                conn.commit()
                stats.files_tier_only += 1
                return
            line_base, state = existing["line_count"], existing["parser_state"] or {}
        else:
            # Rewritten or truncated: drop what this source contributed and reparse from zero.
            if offset:
                stats.files_rewritten += 1
                with conn.transaction():
                    purge_source(conn, source_id)
                    conn.execute(
                        "INSERT INTO ah.parse_issue (source_id, byte_offset, kind, detail) VALUES (%s,-1,'source_rewritten',%s) "
                        "ON CONFLICT (source_id, byte_offset, kind) DO UPDATE SET detail = EXCLUDED.detail, created_at = now()",
                        (source_id, f"size={stat.st_size} offset={offset}"))
                conn.commit()
            offset, line_base, state = 0, 0, {}

    parser = parser_cls(ctx, dict(state))
    stats.files_parsed += 1
    for batch in read_batches(path, offset, line_base):
        rows: list[Row] = []
        for raw, pos in batch:
            try:
                record = json.loads(raw)
            except (ValueError, UnicodeDecodeError):
                rows.append(ParseIssueRow(byte_offset=pos.byte_offset, kind="json_error", line_number=pos.line_number))
                continue
            if not isinstance(record, dict):
                rows.append(ParseIssueRow(byte_offset=pos.byte_offset, kind="not_object", line_number=pos.line_number))
                continue
            try:
                rows.extend(parser.line(record, pos))
            except Exception as exc:  # parser bug on one record must not stop the file
                rows.append(ParseIssueRow(byte_offset=pos.byte_offset, kind="parser_exception",
                                          line_number=pos.line_number, detail=type(exc).__name__))
        rows.extend(parser.flush())
        last = batch[-1][1]
        end = last.byte_offset + last.byte_length
        new_state = json.loads(json.dumps(parser.state(), default=str))
        start, tail, head = checkpoints(path, end)
        with conn.transaction():
            touched = writer.write(rows, source_id, ctx)
            if touched:
                with conn.cursor() as cur:
                    cur.executemany("INSERT INTO ah.dirty_session (session_id) VALUES (%s) ON CONFLICT DO NOTHING",
                                    [(sid,) for sid in touched])
            conn.execute(
                "UPDATE ah.source_file SET tier=%s, available_hot=%s, available_cold=%s, size_bytes=%s, "
                "mtime_ns=%s, indexed_offset=%s, line_count=%s, checkpoint_start=%s, checkpoint_sha256=%s, "
                "head_sha256=%s, parser_version=%s, parser_state=%s, status=%s, indexed_at=now() WHERE id=%s",
                (*tier_cols, end, last.line_number, start, tail, head, parser_version,
                 Jsonb(new_state), "indexed" if end >= stat.st_size else "partial", source_id))
        conn.commit()
        stats.lines += len(batch)
        stats.bytes += end - batch[0][1].byte_offset
        stats.rows += len(rows)
    if offset == 0 and stat.st_size == 0:
        conn.execute("UPDATE ah.source_file SET status='indexed', parser_version=%s, indexed_at=now() WHERE id=%s",
                     (parser_version, source_id))
        conn.commit()


# --- post passes -------------------------------------------------------------------------------


LINK_SQL = [
    # Claude subagents: root = the main session with the same sessionId; parent = the session
    # that spawned it (nested agents), else the main session. Runs over every unresolved row so a
    # parent that arrives in a later run still links its children.
    """
    UPDATE ah.session c SET root_session_id = p.id, parent_session_id = COALESCE(c.parent_session_id, p.id)
    FROM ah.session p
    WHERE c.agent = 'claude' AND c.agent_id <> '' AND c.root_session_id IS NULL
      AND p.agent = 'claude' AND p.session_uid = c.session_uid AND p.agent_id = ''
    """,
    # Codex: parent by thread id, root by root session uid.
    """
    UPDATE ah.session c SET parent_session_id = p.id
    FROM ah.session p
    WHERE c.agent = 'codex' AND c.parent_session_id IS NULL AND c.parent_session_uid IS NOT NULL
      AND p.agent = 'codex' AND p.session_uid = c.parent_session_uid AND p.agent_id = ''
    """,
    """
    UPDATE ah.session c SET root_session_id = r.id
    FROM ah.session r
    WHERE c.agent = 'codex' AND c.root_session_id IS NULL AND c.root_session_uid IS NOT NULL
      AND c.root_session_uid <> c.session_uid
      AND r.agent = 'codex' AND r.session_uid = c.root_session_uid AND r.agent_id = ''
    """,
    # pi: parent and root by header id from the child's path (parse_pi.lineage); a nested child's
    # parent is the session whose agent_path is its own minus the last run dir.
    """
    UPDATE ah.session c SET parent_session_id = p.id
    FROM ah.session p
    WHERE c.agent = 'pi' AND c.parent_session_id IS NULL AND c.parent_session_uid IS NOT NULL
      AND p.agent = 'pi' AND p.session_uid = c.parent_session_uid AND p.agent_id = ''
    """,
    """
    UPDATE ah.session c SET parent_session_id = p.id
    FROM ah.session p
    WHERE c.agent = 'pi' AND c.parent_session_id IS NULL AND c.parent_session_uid IS NULL
      AND c.spawn_depth > 1 AND c.agent_path IS NOT NULL
      AND p.agent = 'pi' AND p.agent_path = regexp_replace(c.agent_path, '/[^/]+/[^/]+$', '')
    """,
    """
    UPDATE ah.session c SET root_session_id = r.id
    FROM ah.session r
    WHERE c.agent = 'pi' AND c.root_session_id IS NULL AND c.root_session_uid IS NOT NULL
      AND c.root_session_uid <> c.session_uid
      AND r.agent = 'pi' AND r.session_uid = c.root_session_uid AND r.agent_id = ''
    """,
    # Top-level sessions are their own root.
    """
    UPDATE ah.session c SET root_session_id = c.id
    FROM ah.dirty_now d
    WHERE c.id = d.session_id AND c.root_session_id IS NULL AND NOT c.is_subagent
      AND c.parent_session_uid IS NULL AND (c.root_session_uid IS NULL OR c.root_session_uid = c.session_uid)
    """,
    # Spawns -> child sessions: exact ids first (Claude agentId, Codex child thread id), then the
    # Codex parent-thread + agent_path suffix rule.
    """
    UPDATE ah.subagent_spawn s SET child_session_id = c.id
    FROM ah.session c
    WHERE s.child_session_id IS NULL AND s.agent = 'claude' AND s.child_agent_id IS NOT NULL
      AND c.agent = 'claude' AND c.session_uid = s.child_session_uid AND c.agent_id = s.child_agent_id
    """,
    """
    UPDATE ah.subagent_spawn s SET child_session_id = c.id
    FROM ah.session c
    WHERE s.child_session_id IS NULL AND s.agent = 'codex' AND s.child_session_uid IS NOT NULL
      AND c.agent = 'codex' AND c.session_uid = s.child_session_uid AND c.agent_id = ''
    """,
    """
    UPDATE ah.subagent_spawn s SET child_session_id = c.id
    FROM ah.session p, ah.session c
    WHERE s.child_session_id IS NULL AND s.agent = 'codex' AND s.child_task_name IS NOT NULL
      AND p.id = s.parent_session_id
      AND c.agent = 'codex' AND c.parent_session_uid = p.session_uid
      AND c.agent_path LIKE '%%/' || s.child_task_name
    """,
    # pi: a spawn names its child by "<run dir>/run-<i>", the tail of the child's agent_path.
    """
    UPDATE ah.subagent_spawn s SET child_session_id = c.id
    FROM ah.session p, ah.session c
    WHERE s.child_session_id IS NULL AND s.agent = 'pi' AND s.child_task_name IS NOT NULL
      AND p.id = s.parent_session_id
      AND c.agent = 'pi' AND c.agent_path = COALESCE(p.agent_path, p.session_uid) || '/' || s.child_task_name
    """,
    # Pi artifact copies pair run id with the child's API response id. The response is indexed
    # only in the real child session, so this is an exact link even when notifications truncate
    # the child path. A run mapping to multiple children or conflicting agent files is ignored.
    """
    WITH matched AS (
        SELECT e.run_id, min(l.session_id) AS child_id, min(e.agent_type) AS agent_type
        FROM ah.pi_run_response e JOIN ah.llm_call l ON l.agent = 'pi' AND l.response_id = e.response_id
        JOIN ah.session c ON c.id = l.session_id AND c.agent = 'pi' AND c.spawn_kind = 'pi_subagent'
        GROUP BY e.run_id
        HAVING count(DISTINCT l.session_id) = 1 AND count(DISTINCT e.agent_type) <= 1
    )
    UPDATE ah.subagent_spawn s SET child_session_id = m.child_id,
           requested_type = COALESCE(s.requested_type, m.agent_type),
           requested_type_source = CASE WHEN s.requested_type IS NULL AND m.agent_type IS NOT NULL
                                        THEN 'explicit' ELSE s.requested_type_source END
    FROM matched m JOIN ah.session c ON c.id = m.child_id
    WHERE s.agent = 'pi' AND s.parent_session_id = c.parent_session_id
      AND (s.workflow_id = m.run_id OR s.spawn_uid = m.run_id)
      AND (s.child_session_id IS NULL OR s.child_session_id = m.child_id)
      AND (s.requested_type IS NULL OR m.agent_type IS NULL OR s.requested_type = m.agent_type)
      AND (s.child_session_id IS NULL OR (s.requested_type IS NULL AND m.agent_type IS NOT NULL))
    """,
    # An artifact can identify a completed child even when no launch notification survived in
    # the parent JSONL. Preserve that evidence as one spawn keyed by the recorded run id, with no
    # invented launch timestamp; direct launch rows above always win when present.
    """
    WITH matched AS (
        SELECT e.run_id, min(l.session_id) AS child_id, min(e.agent_type) AS agent_type,
               (array_agg(e.source_id ORDER BY e.source_id, e.byte_offset))[1] AS source_id,
               (array_agg(e.byte_offset ORDER BY e.source_id, e.byte_offset))[1] AS byte_offset
        FROM ah.pi_run_response e JOIN ah.llm_call l ON l.agent = 'pi' AND l.response_id = e.response_id
        JOIN ah.session c ON c.id = l.session_id AND c.agent = 'pi' AND c.spawn_kind = 'pi_subagent'
        GROUP BY e.run_id
        HAVING count(DISTINCT l.session_id) = 1 AND count(DISTINCT e.agent_type) <= 1
    )
    INSERT INTO ah.subagent_spawn (agent, spawn_uid, parent_session_id, child_session_id,
                                   requested_type, requested_type_source, workflow_id,
                                   launch_status, source_id, byte_offset)
    SELECT 'pi', m.run_id, c.parent_session_id, m.child_id, m.agent_type,
           CASE WHEN m.agent_type IS NOT NULL THEN 'explicit' END, m.run_id,
           'launched', m.source_id, m.byte_offset
    FROM matched m JOIN ah.session c ON c.id = m.child_id
    WHERE c.parent_session_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM ah.subagent_spawn s WHERE s.agent = 'pi'
          AND s.parent_session_id = c.parent_session_id
          AND (s.workflow_id = m.run_id OR s.spawn_uid = m.run_id))
    ON CONFLICT (agent, spawn_uid) DO NOTHING
    """,
    # A child learns its spawning call and its real parent (nested Claude agents) from the spawn.
    """
    UPDATE ah.session c SET parent_call_uid = COALESCE(c.parent_call_uid, s.spawn_uid),
           agent_type = COALESCE(c.agent_type, s.requested_type),
           agent_type_source = CASE WHEN c.agent_type IS NOT NULL AND c.agent_type = s.requested_type
                                      THEN COALESCE(c.agent_type_source, s.requested_type_source, 'explicit')
                                    WHEN c.agent_type IS NOT NULL THEN COALESCE(c.agent_type_source, 'explicit')
                                    WHEN s.requested_type IS NOT NULL THEN COALESCE(s.requested_type_source, 'explicit')
                                    ELSE c.agent_type_source END,
           parent_session_id = s.parent_session_id
    FROM ah.subagent_spawn s
    WHERE s.child_session_id = c.id
      AND (s.spawn_uid = c.parent_call_uid OR
           (c.parent_call_uid IS NULL AND
            (SELECT count(*) FROM ah.subagent_spawn o WHERE o.child_session_id = c.id) = 1))
      AND (c.parent_call_uid IS NULL OR c.parent_session_id IS DISTINCT FROM s.parent_session_id
           OR (c.agent_type IS NULL AND s.requested_type IS NOT NULL)
           OR (c.agent_type IS NOT NULL AND c.agent_type_source IS NULL))
    """,
    # Some harnesses report the same child in more than one spawn result without preserving a
    # parent call id. A unanimous linked request is still evidence; conflicting requests are not.
    """
    WITH agreed AS (
        SELECT child_session_id, min(requested_type) AS agent_type,
               min(requested_type_source) AS agent_type_source
        FROM ah.subagent_spawn
        WHERE child_session_id IS NOT NULL
        GROUP BY child_session_id
        HAVING bool_and(requested_type IS NOT NULL AND requested_type_source IS NOT NULL)
           AND count(DISTINCT requested_type) = 1 AND count(DISTINCT requested_type_source) = 1
    )
    UPDATE ah.session c SET agent_type = a.agent_type, agent_type_source = a.agent_type_source
    FROM agreed a WHERE c.id = a.child_session_id AND c.is_subagent AND c.agent_type IS NULL
    """,
    # A later spawn result can name the same child. Only a single request preceding the child's
    # first source event is causally eligible; never choose among competing earlier requests.
    """
    WITH eligible AS (
        SELECT c.id AS child_id, min(s.requested_type) AS agent_type,
               min(s.requested_type_source) AS agent_type_source
        FROM ah.session c JOIN ah.subagent_spawn s ON s.child_session_id = c.id
        WHERE c.is_subagent AND c.agent_type IS NULL AND c.first_event_at IS NOT NULL
          AND s.spawned_at <= c.first_event_at
        GROUP BY c.id HAVING count(*) = 1
           AND bool_and(s.requested_type IS NOT NULL AND s.requested_type_source IS NOT NULL)
    )
    UPDATE ah.session c SET agent_type = e.agent_type, agent_type_source = e.agent_type_source
    FROM eligible e WHERE c.id = e.child_id AND c.agent_type IS NULL
    """,
    # Depth from the resolved parent chain (bounded: agent trees are shallow).
    """
    WITH RECURSIVE chain AS (
        SELECT c.id, c.parent_session_id, 1 AS depth FROM ah.session c
        WHERE c.parent_session_id IS NOT NULL AND c.spawn_depth IS NULL
        UNION ALL
        SELECT chain.id, p.parent_session_id, chain.depth + 1 FROM chain JOIN ah.session p ON p.id = chain.parent_session_id
        WHERE p.parent_session_id IS NOT NULL AND p.parent_session_id <> p.id AND chain.depth < 16
    )
    UPDATE ah.session s SET spawn_depth = d.depth
    FROM (SELECT id, max(depth) AS depth FROM chain GROUP BY id) d WHERE s.id = d.id
    """,
]

ROLLUP_SQL = """
INSERT INTO ah.session_rollup AS r (session_id, computed_at, turns_human, turns_other, messages, llm_calls,
    input_uncached, cache_read, cache_write, output, reasoning, peak_context_tokens, tool_calls, tool_errors,
    tool_denials, tool_interrupts, subagents, compactions, commits, pushes, api_errors, interrupts, models,
    cli_versions, duration_s, claude_cost_usd, snapshot_calls)
SELECT s.id, now(),
    (SELECT count(*) FROM ah.turn t WHERE t.session_id = s.id AND t.origin = 'human'),
    (SELECT count(*) FROM ah.turn t WHERE t.session_id = s.id AND t.origin IS DISTINCT FROM 'human'),
    (SELECT count(*) FROM ah.message m WHERE m.session_id = s.id AND m.message_class = ANY(ah.conversation_classes())),
    l.calls, l.input_uncached, l.cache_read, l.cache_write, l.output, l.reasoning, l.peak,
    tc.calls, tc.errors, tc.denials, tc.interrupts,
    (SELECT count(*) FROM ah.subagent_spawn x WHERE x.parent_session_id = s.id),
    (SELECT count(*) FROM ah.compaction x WHERE x.session_id = s.id),
    (SELECT count(*) FROM ah.git_event g WHERE g.session_id = s.id AND g.op IN ('commit','cherry_pick')),
    (SELECT count(*) FROM ah.git_event g WHERE g.session_id = s.id AND g.op = 'push'),
    l.api_errors,
    (SELECT count(*) FROM ah.session_event e WHERE e.session_id = s.id AND e.kind = 'interrupt'),
    l.models,
    ARRAY_REMOVE(ARRAY[s.cli_version_first, NULLIF(s.cli_version_last, s.cli_version_first)], NULL),
    EXTRACT(EPOCH FROM (s.last_event_at - s.first_event_at))::bigint,
    (SELECT c.total_cost_usd FROM ah.cost_state c WHERE c.session_id = s.id ORDER BY c.ts DESC LIMIT 1),
    CASE WHEN s.agent = 'claude' THEN l.snapshot_calls END
FROM ah.session s
JOIN ah.dirty_now d ON d.session_id = s.id
LEFT JOIN LATERAL (
    SELECT count(*) AS calls, sum(input_uncached) AS input_uncached, sum(cache_read) AS cache_read,
           sum(COALESCE(cache_write_5m,0) + COALESCE(cache_write_1h,0)) AS cache_write, sum(output) AS output,
           sum(reasoning) AS reasoning, max(context_tokens) AS peak,
           count(*) FILTER (WHERE is_api_error) AS api_errors,
           count(*) FILTER (WHERE stop_reason IS NULL AND NOT is_api_error) AS snapshot_calls,
           array_agg(DISTINCT model) FILTER (WHERE model IS NOT NULL) AS models
    FROM ah.llm_call WHERE session_id = s.id) l ON true
LEFT JOIN LATERAL (
    SELECT count(*) AS calls, count(*) FILTER (WHERE outcome = 'error') AS errors,
           count(*) FILTER (WHERE outcome = 'denied') AS denials,
           count(*) FILTER (WHERE outcome = 'interrupted') AS interrupts
    FROM ah.tool_call WHERE session_id = s.id) tc ON true
ON CONFLICT (session_id) DO UPDATE SET computed_at = EXCLUDED.computed_at, turns_human = EXCLUDED.turns_human,
    turns_other = EXCLUDED.turns_other, messages = EXCLUDED.messages, llm_calls = EXCLUDED.llm_calls,
    input_uncached = EXCLUDED.input_uncached, cache_read = EXCLUDED.cache_read, cache_write = EXCLUDED.cache_write,
    output = EXCLUDED.output, reasoning = EXCLUDED.reasoning, peak_context_tokens = EXCLUDED.peak_context_tokens,
    tool_calls = EXCLUDED.tool_calls, tool_errors = EXCLUDED.tool_errors, tool_denials = EXCLUDED.tool_denials,
    tool_interrupts = EXCLUDED.tool_interrupts, subagents = EXCLUDED.subagents, compactions = EXCLUDED.compactions,
    commits = EXCLUDED.commits, pushes = EXCLUDED.pushes, api_errors = EXCLUDED.api_errors,
    interrupts = EXCLUDED.interrupts, models = EXCLUDED.models, cli_versions = EXCLUDED.cli_versions,
    duration_s = EXCLUDED.duration_s, claude_cost_usd = EXCLUDED.claude_cost_usd,
    snapshot_calls = EXCLUDED.snapshot_calls
"""


TASK_REF_SQL = """
WITH prefixes AS (SELECT string_agg(prefix, '|') AS alt FROM ah.task_prefix),
hits AS (
    SELECT m.session_id, m.ts, m.message_class, upper(x.match[1]) AS prefix, x.match[2]::int AS num
    FROM {sessions} d
    JOIN ah.message m ON m.session_id = d.session_id
    CROSS JOIN prefixes p
    CROSS JOIN LATERAL regexp_matches(m.text, '\\m(' || p.alt || ')-([0-9]{{1,6}})\\M', 'gi') AS x(match)
    WHERE p.alt IS NOT NULL
      -- lower case only in the tracker's own padded filename form (abc-0091 - Title.md); upper case otherwise
      AND (x.match[1] = upper(x.match[1]) OR length(x.match[2]) >= 3)
),
keyed AS (
    SELECT h.session_id, h.ts, h.message_class,
           h.prefix || '-' || CASE WHEN tp.zero_pad IS NULL THEN h.num::text
                                   ELSE lpad(h.num::text, tp.zero_pad, '0') END AS task_key
    FROM hits h JOIN ah.task_prefix tp ON tp.prefix = h.prefix
)
INSERT INTO ah.task_ref (session_id, task_key, first_ts, last_ts, mentions, in_human, in_brief)
SELECT k.session_id, k.task_key, min(k.ts), max(k.ts), count(*),
       bool_or(k.message_class IN ('human_prompt', 'queued_prompt')), bool_or(k.message_class = 'subagent_brief')
FROM keyed k
WHERE EXISTS (SELECT 1 FROM ah.backlog_task b WHERE b.task_key = k.task_key AND b.status IS DISTINCT FROM 'removed')
GROUP BY k.session_id, k.task_key
ON CONFLICT (session_id, task_key) DO UPDATE SET first_ts = EXCLUDED.first_ts, last_ts = EXCLUDED.last_ts,
    mentions = EXCLUDED.mentions, in_human = EXCLUDED.in_human, in_brief = EXCLUDED.in_brief
"""


def task_refs(conn: psycopg.Connection) -> str:
    """Incremental over dirty_now; a full rescan whenever the known prefix/task set changes."""
    current = conn.execute(
        "SELECT md5(COALESCE((SELECT string_agg(prefix || ':' || COALESCE(zero_pad, 0), ',' ORDER BY prefix) "
        "FROM ah.task_prefix), '') || COALESCE((SELECT string_agg(task_key, ',' ORDER BY task_key) "
        "FROM ah.backlog_task WHERE status IS DISTINCT FROM 'removed'), ''))").fetchone()[0]
    stored = conn.execute("SELECT value FROM ah.meta WHERE key = 'task_ref_hash'").fetchone()
    if stored is None or stored[0] != current:
        conn.execute("DELETE FROM ah.task_ref")
        conn.execute(TASK_REF_SQL.format(sessions="(SELECT id AS session_id FROM ah.session WHERE NOT is_stub)"))
        conn.execute("INSERT INTO ah.meta VALUES ('task_ref_hash', %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
                     (current,))
        return "full"
    conn.execute(TASK_REF_SQL.format(sessions="dirty_now"))
    return "incremental"


def post_passes(conn: psycopg.Connection, refresh_id: int | None = None) -> dict[str, int]:
    """Link, roll up and loop-tag the sessions dirty at the start; marks added meanwhile survive.

    Also runs the v4 structure passes (structure.py) and appends one change_log row per changed
    session under refresh_id (allocated here when the caller did not)."""
    from . import loops, structure
    result: dict[str, int] = {}
    with conn.transaction():
        standalone = refresh_id is None
        if refresh_id is None:
            refresh_id = new_refresh(conn, "refresh")
        conn.execute("CREATE TEMP TABLE IF NOT EXISTS dirty_now (session_id bigint PRIMARY KEY) ON COMMIT DELETE ROWS")
        conn.execute("INSERT INTO dirty_now SELECT session_id FROM ah.dirty_session ON CONFLICT DO NOTHING")
        # Artifact copies can arrive after the child JSONL. Their run/response evidence can
        # resolve an existing child without a further transcript append, so enqueue those
        # children and parents before the ordinary dirty-session early return.
        conn.execute("""
            WITH matched AS (
                SELECT e.run_id, min(l.session_id) AS child_id, min(e.agent_type) AS agent_type
                FROM ah.pi_run_response e
                JOIN ah.llm_call l ON l.agent = 'pi' AND l.response_id = e.response_id
                JOIN ah.session c ON c.id = l.session_id
                WHERE c.agent = 'pi' AND c.spawn_kind = 'pi_subagent'
                GROUP BY e.run_id
                HAVING count(DISTINCT l.session_id) = 1 AND count(DISTINCT e.agent_type) <= 1
            ), pending AS (
                SELECT DISTINCT c.id AS child_id, c.parent_session_id
                FROM matched m JOIN ah.session c ON c.id = m.child_id
                WHERE (c.agent_type IS NULL AND m.agent_type IS NOT NULL AND EXISTS (
                       SELECT 1 FROM ah.subagent_spawn s WHERE s.agent = 'pi'
                         AND s.child_session_id = c.id
                         AND (s.workflow_id = m.run_id OR s.spawn_uid = m.run_id)
                         AND (s.requested_type IS NULL OR s.requested_type = m.agent_type)))
                   OR EXISTS (
                       SELECT 1 FROM ah.subagent_spawn s WHERE s.agent = 'pi'
                         AND s.parent_session_id = c.parent_session_id
                         AND (s.workflow_id = m.run_id OR s.spawn_uid = m.run_id)
                         AND s.child_session_id IS NULL
                         AND (s.requested_type IS NULL OR m.agent_type IS NULL
                              OR s.requested_type = m.agent_type))
                   OR NOT EXISTS (
                       SELECT 1 FROM ah.subagent_spawn s WHERE s.agent = 'pi'
                         AND s.parent_session_id = c.parent_session_id
                         AND (s.workflow_id = m.run_id OR s.spawn_uid = m.run_id))
            )
            INSERT INTO dirty_now (session_id)
            SELECT child_id FROM pending
            UNION SELECT parent_session_id FROM pending WHERE parent_session_id IS NOT NULL
            ON CONFLICT DO NOTHING
        """)
        dirty = conn.execute("SELECT count(*) FROM dirty_now").fetchone()[0]
        result["dirty_sessions"] = dirty
        if not dirty:
            result.update(loops.refresh_live(conn))
            result["task_refs"] = task_refs(conn)  # type: ignore[assignment]  (prefix changes rescan)
            result.update(structure.session_embeddings(conn))
            if standalone:
                finish_refresh(conn, refresh_id, True)
            return result
        for statement in LINK_SQL:
            conn.execute(statement.replace("ah.dirty_now", "dirty_now"))
        conn.execute(ROLLUP_SQL.replace("ah.dirty_now", "dirty_now"))
        result["task_refs"] = task_refs(conn)  # type: ignore[assignment]
        result.update(loops.run(conn))
        result.update(loops.refresh_live(conn))
        result.update(structure.run(conn, refresh_id))
        conn.execute("DELETE FROM ah.dirty_session d USING dirty_now n WHERE d.session_id = n.session_id")
        if standalone:
            finish_refresh(conn, refresh_id, True)
    return result


def new_refresh(conn: psycopg.Connection, kind: str) -> int:
    """Allocate a refresh id (ah.refresh_id_seq, never reset) and open its refresh_log row."""
    refresh_id = conn.execute("SELECT nextval('ah.refresh_id_seq')").fetchone()[0]
    conn.execute("INSERT INTO ah.refresh_log (refresh_id, kind) VALUES (%s, %s)", (refresh_id, kind))
    return refresh_id


def finish_refresh(conn: psycopg.Connection, refresh_id: int, ok: bool) -> None:
    """Close the refresh_log row and NOTIFY ah_refresh with the id; delivered when this commits."""
    conn.execute(
        "UPDATE ah.refresh_log SET finished_at = now(), ok = %s, "
        "sessions_changed = (SELECT count(*) FROM ah.change_log WHERE refresh_id = %s) WHERE refresh_id = %s",
        (ok, refresh_id, refresh_id))
    conn.execute("INSERT INTO ah.meta VALUES ('last_refresh_id', %s) "
                 "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", (str(refresh_id),))
    conn.execute("SELECT pg_notify('ah_refresh', %s)", (str(refresh_id),))


# --- metrics -----------------------------------------------------------------------------------


def write_metrics(conn: psycopg.Connection | None, stats: RunStats, cold_ok: bool, success: bool,
                  path: Path = TEXTFILE, previous_success: float | None = None) -> None:
    lines = [
        "# HELP agent_history_run_success Whether the last index run succeeded.",
        "# TYPE agent_history_run_success gauge",
        f"agent_history_run_success {int(success)}",
        "# TYPE agent_history_run_duration_seconds gauge",
        f"agent_history_run_duration_seconds {time.time() - stats.started:.3f}",
        "# TYPE agent_history_cold_tier_available gauge",
        f"agent_history_cold_tier_available {int(cold_ok)}",
        "# TYPE agent_history_lock_held gauge",
        f"agent_history_lock_held {int(stats.lock_held)}",
        "# TYPE agent_history_run_files gauge",
        f'agent_history_run_files{{result="parsed"}} {stats.files_parsed}',
        f'agent_history_run_files{{result="rewritten"}} {stats.files_rewritten}',
        f'agent_history_run_files{{result="tier_only"}} {stats.files_tier_only}',
        f'agent_history_run_files{{result="load_error"}} {stats.errors}',
        "# TYPE agent_history_run_lines gauge",
        f"agent_history_run_lines {stats.lines}",
        "# TYPE agent_history_run_rows gauge",
        f"agent_history_run_rows {stats.rows}",
    ]
    last_success = time.time() if success else previous_success
    if last_success:
        lines += ["# TYPE agent_history_last_success_timestamp_seconds gauge",
                  f"agent_history_last_success_timestamp_seconds {last_success:.0f}"]
    if conn is not None:
        try:
            lines += ["# TYPE agent_history_sources gauge"]
            for status, count in conn.execute("SELECT status, count(*) FROM ah.source_file GROUP BY 1"):
                lines.append(f'agent_history_sources{{status="{status}"}} {count}')
            lag = conn.execute("SELECT COALESCE(sum(GREATEST(size_bytes - indexed_offset, 0)),0) FROM ah.source_file "
                               "WHERE status <> 'unavailable'").fetchone()[0]
            lines += ["# TYPE agent_history_lag_bytes gauge", f"agent_history_lag_bytes {lag}"]
            lines += ["# TYPE agent_history_parse_issues_total gauge"]
            for kind, count in conn.execute("SELECT kind, count(*) FROM ah.parse_issue GROUP BY 1"):
                lines.append(f'agent_history_parse_issues_total{{kind="{kind}"}} {count}')
            unresolved = conn.execute(
                "SELECT count(*) FROM ah.subagent_spawn WHERE child_session_id IS NULL "
                "AND (child_agent_id IS NOT NULL OR child_task_name IS NOT NULL)").fetchone()[0]
            orphans = conn.execute(
                "SELECT count(*) FROM ah.session WHERE NOT is_stub AND root_session_id IS NULL").fetchone()[0]
            lines += ["# TYPE agent_history_unresolved_links gauge",
                      f'agent_history_unresolved_links{{kind="spawn_child"}} {unresolved}',
                      f'agent_history_unresolved_links{{kind="session_root"}} {orphans}']
            dirty = conn.execute("SELECT count(*) FROM ah.dirty_session").fetchone()[0]
            lines += ["# TYPE agent_history_dirty_sessions gauge", f"agent_history_dirty_sessions {dirty}"]
            lines += ["# TYPE agent_history_rows gauge"]
            for table in ("session", "turn", "message", "llm_call", "tool_call", "tool_op", "subagent_spawn",
                          "git_event", "loop_run", "lane", "tool_io", "attachment", "file_touch"):
                est = conn.execute("SELECT reltuples::bigint FROM pg_class WHERE oid = %s::regclass",
                                   (f"ah.{table}",)).fetchone()[0]
                lines.append(f'agent_history_rows{{table="{table}"}} {max(est, 0)}')
            conn.rollback()
        except psycopg.Error:
            conn.rollback()
    if path.parent.is_dir():
        tmp = path.with_suffix(".prom.tmp")
        tmp.write_text("\n".join(lines) + "\n")
        os.replace(tmp, path)


def previous_success_timestamp(path: Path = TEXTFILE) -> float | None:
    try:
        for line in path.read_text().splitlines():
            if line.startswith("agent_history_last_success_timestamp_seconds "):
                return float(line.split()[1])
    except (OSError, ValueError):
        pass
    return None


# --- entry points ------------------------------------------------------------------------------


def connect(dsn: str | None = None) -> psycopg.Connection:
    """Writer connection: an explicit DSN, else $AGENT_HISTORY_DSN, else the config file's dsn."""
    if not dsn:
        from .config import load_config
        dsn = os.environ.get("AGENT_HISTORY_DSN") or load_config().dsn
    if not dsn:
        raise SystemExit("agent-history: no database DSN (set AGENT_HISTORY_DSN or dsn in the config)")
    return psycopg.connect(dsn, application_name="agent-history-index", autocommit=False)


ANALYTICS_FILES = ("analytics.sql", "search.sql", "structure.sql", "efficiency.sql")


def schema_sources() -> list[Path]:
    files = [SQL_DIR / "baseline.sql"]
    migrations = SQL_DIR / "migrations"
    if migrations.is_dir():
        files += sorted(migrations.glob("*.sql"))
    files += [SQL_DIR / name for name in ANALYTICS_FILES]
    return [f for f in files if f.exists()]


def schema_hash() -> str:
    digest = hashlib.sha256()
    for path in schema_sources():
        digest.update(path.name.encode() + b"\0" + path.read_bytes())
    digest.update(f"{SCHEMA_VERSION}:{model.PARSER_VERSION_CLAUDE}:{model.PARSER_VERSION_CODEX}".encode())
    return digest.hexdigest()


def apply_schema(conn: psycopg.Connection, force: bool = False) -> bool:
    """Apply the baseline (once), new migrations (once each) and the analytics files when their
    content changed. Returns True if applied.

    The baseline is the squashed schema, applied only to a database without schema `ah`.
    Migrations after it are numbered and recorded in ah.meta as `migration:<file>`.
    """
    wanted = schema_hash()
    if not force:
        try:
            row = conn.execute("SELECT value FROM ah.meta WHERE key = 'schema_hash'").fetchone()
            conn.commit()
            if row and row[0] == wanted:
                return False
        except psycopg.Error:
            conn.rollback()
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (ADVISORY_LOCK + 1,))
        conn.execute("SET LOCAL lock_timeout = '30s'")
        present = conn.execute("SELECT to_regclass('ah.meta') IS NOT NULL").fetchone()[0]
        if not present:
            conn.execute((SQL_DIR / "baseline.sql").read_text())
            conn.execute("SET LOCAL search_path = ah, public")
        applied = {r[0] for r in conn.execute("SELECT key FROM ah.meta WHERE key LIKE 'migration:%%'")}
        migrations = SQL_DIR / "migrations"
        if migrations.is_dir():
            for script in sorted(migrations.glob("*.sql")):
                key = f"migration:{script.name}"
                if key not in applied:
                    conn.execute(script.read_text())
                    conn.execute("INSERT INTO ah.meta VALUES (%s, now()::text)", (key,))
        for name in ANALYTICS_FILES:
            script = SQL_DIR / name
            if script.exists():
                conn.execute(script.read_text())
        for key, value in (("schema_version", SCHEMA_VERSION),
                           ("parser_version_claude", model.PARSER_VERSION_CLAUDE),
                           ("parser_version_codex", model.PARSER_VERSION_CODEX),
                           ("schema_hash", wanted)):
            conn.execute("INSERT INTO ah.meta VALUES (%s,%s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
                         (key, value))
    return True


def create_post_load_indexes(conn: psycopg.Connection) -> None:
    conn.autocommit = True
    try:
        conn.execute("SET maintenance_work_mem = '2GB'")
        conn.execute("SET max_parallel_maintenance_workers = 4")
        for table in ("message", "llm_call", "tool_call", "session", "turn", "tool_io"):
            conn.execute(f"ANALYZE ah.{table}")
        conn.execute((SQL_DIR / "indexes_post_load.sql").read_text())
    finally:
        conn.autocommit = False


def refresh(conn: psycopg.Connection, hot: Path | None = HOT_ROOT, cold: Path | None = COLD_ROOT,
            namespaces: Iterable[str] | None = None, limit_files: int | None = None,
            textfile: Path | None = TEXTFILE, log=print, kind: str = "refresh",
            sources: dict[str, Path] | None = None,
            source_entries: dict[str, SourceEntry] | None = None) -> RunStats:
    """Index new transcript bytes from `sources` (namespace -> agent home) or the hot/cold archive."""
    stats = RunStats()
    previous = previous_success_timestamp(textfile) if textfile else None
    got = conn.execute("SELECT pg_try_advisory_lock(%s)", (ADVISORY_LOCK,)).fetchone()[0]
    conn.commit()
    if not got:
        stats.lock_held = True
        log("another index run holds the lock; exiting")
        if textfile:
            write_metrics(None, stats, cold_tier_ok(cold), False, textfile, previous)
        return stats
    success = False
    cold_ok = cold_tier_ok(cold)
    refresh_id: int | None = None
    try:
        apply_schema(conn)
        refresh_id = new_refresh(conn, kind)
        conn.commit()
        entries = (source_entries if source_entries is not None else
                   inventory_map(sources) if sources is not None else
                   inventory(hot, cold if cold_ok else None, namespaces))
        existing = {r[1]: dict(zip(("id", "rel_path", "indexed_offset", "line_count", "checkpoint_start",
                                     "checkpoint_sha256", "head_sha256", "parser_version", "parser_state",
                                     "status", "mtime_ns", "tier"), r))
                    for r in conn.execute(
                        "SELECT id, rel_path, indexed_offset, line_count, checkpoint_start, checkpoint_sha256, "
                        "head_sha256, parser_version, parser_state, status, mtime_ns, tier FROM ah.source_file")}
        conn.commit()
        writer = Writer(conn)
        # Main files first so parents exist before children; stable order otherwise.
        order = sorted(entries.values(), key=lambda e: (e.role != "main", e.rel_path))
        for entry in order:
            if limit_files is not None and stats.files_parsed >= limit_files:
                break
            stats.files_seen += 1
            process_source(conn, writer, entry, existing.get(entry.rel_path), stats)
            if len(writer.session_ids) > 200_000:
                writer.session_ids.clear()
        if cold_ok and not namespaces:
            missing = set(existing) - set(entries)
            if missing:
                with conn.cursor() as cur:
                    cur.executemany("UPDATE ah.source_file SET status='unavailable', available_hot=false, "
                                    "available_cold=false WHERE rel_path=%s", [(m,) for m in missing])
        conn.commit()
        post = post_passes(conn, refresh_id)
        conn.execute("INSERT INTO ah.meta VALUES ('last_refresh_at', now()::text) "
                     "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value")
        success = stats.errors == 0
        finish_refresh(conn, refresh_id, success)
        conn.commit()
        post["refresh_id"] = refresh_id
        log(json.dumps({"files_seen": stats.files_seen, "parsed": stats.files_parsed,
                        "rewritten": stats.files_rewritten, "tier_only": stats.files_tier_only,
                        "lines": stats.lines, "rows": stats.rows, "errors": stats.errors,
                        "seconds": round(time.time() - stats.started, 1), **post}))
    finally:
        if refresh_id is not None and not success:
            try:
                conn.rollback()
                conn.execute("UPDATE ah.refresh_log SET finished_at = now(), ok = false "
                             "WHERE refresh_id = %s AND finished_at IS NULL", (refresh_id,))
                conn.commit()
            except psycopg.Error:
                conn.rollback()
        try:
            conn.execute("SELECT pg_advisory_unlock(%s)", (ADVISORY_LOCK,))
            conn.commit()
        except psycopg.Error:
            conn.rollback()
        if textfile:
            write_metrics(conn, stats, cold_ok, success, textfile, previous)
    return stats


def stats_report(conn: psycopg.Connection) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for table in ("source_file", "session", "turn", "message", "llm_call", "tool_call", "tool_op",
                  "subagent_spawn", "hook_event", "compaction", "git_event", "artifact", "session_event",
                  "rate_limit_sample", "cost_state", "loop_run", "lane", "parse_issue", "tool_io", "attachment",
                  "file_touch", "session_continuation", "session_embedding", "change_log"):
        out[table] = conn.execute(f"SELECT count(*) FROM ah.{table}").fetchone()[0]
    out["meta"] = dict(conn.execute("SELECT key, value FROM ah.meta WHERE key NOT LIKE 'migration:%%'").fetchall())
    out["sources"] = dict(conn.execute("SELECT status, count(*) FROM ah.source_file GROUP BY 1").fetchall())
    conn.rollback()
    return out


def _same_number(a: Any, b: Any) -> bool:
    a = float(a) if isinstance(a, (int, float, Decimal)) and not isinstance(a, bool) else None
    b = float(b) if isinstance(b, (int, float, Decimal)) and not isinstance(b, bool) else None
    return a == b or (a is not None and b is not None and math.isclose(a, b, rel_tol=1e-9))


def _cost_state_start(path: Path, offset: int, stored: tuple[Any, Any, Any]) -> datetime | None:
    """startTime of the cost-state line at offset, if it is still the record stored with these totals."""
    try:
        with path.open("rb") as handle:
            handle.seek(offset)
            rec = json.loads(handle.readline())
    except (OSError, ValueError):
        return None
    if not isinstance(rec, dict) or rec.get("type") != "cost-state":
        return None
    seen = (rec.get("totalCostUSD"), rec.get("totalAPIDuration"), rec.get("totalDuration"))
    if not all(_same_number(x, y) for x, y in zip(seen, stored)):
        return None
    return parse_ts(rec.get("startTime"))


def backfill_cost_start(conn: psycopg.Connection, hot: Path | None = HOT_ROOT,
                        cold: Path | None = COLD_ROOT) -> dict[str, int]:  # archive layout only
    """Fill ah.cost_state.start_time for Claude rows parsed before migration 006.

    CostStateRow is DO NOTHING on conflict, so a refresh never revisits an old row: reread each row's
    own source line at its byte_offset instead of rebuilding. The line must still be the stored record:
    a cost-state whose total cost, API duration and total duration match the row. The hot copy is
    tried first, then the cold one. Idempotent; a source on neither tier counts as unavailable, one
    where no copy yields a matching record as unreadable, and either leaves the row untouched.
    """
    rows = conn.execute(
        "SELECT c.id, c.byte_offset, f.rel_path, c.total_cost_usd, c.api_duration_ms, c.total_duration_ms "
        "FROM ah.cost_state c JOIN ah.source_file f ON f.id = c.source_id "
        "WHERE c.agent = 'claude' AND c.start_time IS NULL ORDER BY f.rel_path, c.byte_offset").fetchall()
    stats = {"updated": 0, "unavailable": 0, "unreadable": 0}
    updates: list[tuple[datetime, int]] = []
    for row_id, offset, rel_path, *stored in rows:
        paths = [root / rel_path for root in (hot, cold) if root is not None and (root / rel_path).is_file()]
        if not paths:
            stats["unavailable"] += 1
            continue
        start = next((found for path in paths if (found := _cost_state_start(path, offset, tuple(stored)))), None)
        if start is None:
            stats["unreadable"] += 1
            continue
        updates.append((start, row_id))
    if updates:
        with conn.cursor() as cur:
            cur.executemany("UPDATE ah.cost_state SET start_time = %s WHERE id = %s AND start_time IS NULL", updates)
        conn.commit()
    stats["updated"] = len(updates)
    return stats


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


DATA_TABLES = ("loops", "lane", "loop_run", "session_rollup", "dirty_session", "parse_issue", "record_type_seen", "cost_state",
               "pi_run_response", "rate_limit_sample", "session_event", "artifact", "git_event", "compaction", "hook_event",
               "subagent_spawn", "tool_op", "tool_call", "llm_call", "message", "turn", "tool_io", "attachment",
               "file_touch", "session_continuation", "session_embedding", "session", "repo", "source_file")
# Never truncated: change_log, refresh_log (consumer cursors), collector tables, ah.embedding (paid cache).
POST_LOAD_INDEXES = ("message_search_idx", "tool_io_search_idx")


def try_lock(conn: psycopg.Connection) -> bool:
    got = conn.execute("SELECT pg_try_advisory_lock(%s)", (ADVISORY_LOCK,)).fetchone()[0]
    conn.commit()
    return bool(got)


def unlock(conn: psycopg.Connection) -> None:
    try:
        conn.execute("SELECT pg_advisory_unlock(%s)", (ADVISORY_LOCK,))
        conn.commit()
    except psycopg.Error:
        conn.rollback()


def rebuild(conn: psycopg.Connection, hot: Path | None = HOT_ROOT, cold: Path | None = COLD_ROOT,
            textfile: Path | None = TEXTFILE, log=print, sources: dict[str, Path] | None = None,
            cold_sources: dict[str, Path] | None = None) -> RunStats:
    """Empty every derived table and re-index from JSONL (parser upgrades, breaking fixes).

    Readers see a partial catalogue until it finishes. With the archive layout it refuses unless
    the cold archive is mounted with fresh receipts, so a rebuild can never silently drop cold-only
    history. Source maps optionally union cold directories, preferring hot copies, and refuse
    before mutation if protected namespaces have unreadable cold trees or missing transcripts.
    """
    source_entries = None
    if sources is not None and cold_sources:
        source_entries, report = check_rebuild_sources(conn, sources, cold_sources)
        failures = [f'{n} count={r["count"]}' for n, r in report.items() if r['refused']]
        if failures:
            raise SystemExit('rebuild refused: ' + '; '.join(failures))
        conn.commit()
    if sources is None and not cold_tier_ok(cold):
        raise SystemExit("rebuild refused: cold archive missing or receipts older than 48 h")
    if not try_lock(conn):
        raise SystemExit("rebuild refused: another index run holds the lock")
    try:
        # A writer may have added protected paths after the read-only preflight.
        # Re-inventory and validate the current catalogue while holding its lock,
        # before dropping indexes, updating rows, or truncating any table.
        if sources is not None and cold_sources:
            source_entries, report = check_rebuild_sources(conn, sources, cold_sources)
            failures = [f'{n} count={r["count"]}' for n, r in report.items() if r['refused']]
            if failures:
                raise SystemExit('rebuild refused: ' + '; '.join(failures))
            conn.commit()
        with conn.transaction():
            for index in POST_LOAD_INDEXES:
                conn.execute(f"DROP INDEX IF EXISTS ah.{index}")
            # Remember stable session keys before IDs are recycled. The columns also survive an
            # interrupted rebuild, unlike a temporary map confined to this connection.
            for table, agent_col, agent_id_col in (("session_summary", "agent", "agent_id"),
                                                    ("session_topic", "session_agent", "session_agent_id")):
                conn.execute(f"UPDATE ah.{table} j SET {agent_col} = s.agent, "
                             f"session_uid = s.session_uid, {agent_id_col} = s.agent_id "
                             "FROM ah.session s WHERE j.session_id = s.id")
                conn.execute(f"UPDATE ah.{table} SET session_id = NULL WHERE session_id IS NOT NULL")
            conn.execute("TRUNCATE " + ", ".join(f"ah.{t}" for t in DATA_TABLES) + " RESTART IDENTITY CASCADE")
            # Derived enrichment is re-derived from scratch; collector-owned tables are kept.
            conn.execute("DELETE FROM ah.meta WHERE key IN ('task_ref_hash')")
            # The embedder skips while this is set (it never holds the refresh lock across API calls).
            conn.execute("INSERT INTO ah.meta (key, value) VALUES ('rebuild_in_progress', now()::text) "
                         "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value")
            # Durable: embed-gc never deletes vectors within 7 days of ah.chunk being emptied.
            conn.execute("INSERT INTO ah.meta (key, value) VALUES ('chunks_reset_at', now()::text) "
                         "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value")
            # Consumers of the change feed see one marker, then every session again as it reloads.
            marker = new_refresh(conn, "rebuild")
            conn.execute("INSERT INTO ah.change_log (refresh_id, agent, session_uid, agent_id, kind) "
                         "VALUES (%s, '*', '*', '', 'rebuild')", (marker,))
            finish_refresh(conn, marker, True)
    finally:
        unlock(conn)
    try:
        stats = refresh(conn, hot, cold, None, None, textfile, log, kind="rebuild",
                        sources=sources, source_entries=source_entries)
        with conn.transaction():
            for table, agent_col, agent_id_col in (("session_summary", "agent", "agent_id"),
                                                    ("session_topic", "session_agent", "session_agent_id")):
                conn.execute(f"UPDATE ah.{table} j SET session_id = s.id FROM ah.session s "
                             f"WHERE j.{agent_col} = s.agent AND j.session_uid = s.session_uid "
                             f"AND j.{agent_id_col} = s.agent_id AND j.session_id IS DISTINCT FROM s.id")
        if not try_lock(conn):
            raise SystemExit("rebuild: another run holds the lock, BM25 indexes NOT created; "
                             "run `agent-history create-indexes`")
        try:
            create_post_load_indexes(conn)
        finally:
            unlock(conn)
    finally:
        conn.execute("INSERT INTO ah.meta (key, value) VALUES ('chunks_reset_at', now()::text) "
                     "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value")
        conn.execute("DELETE FROM ah.meta WHERE key = 'rebuild_in_progress'")
        conn.commit()
    return stats

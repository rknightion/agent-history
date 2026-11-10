"""Read-only health gauges from the ParadeDB catalogue."""

from __future__ import annotations

import re
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from agent_history import telemetry

from . import Family, Sample
from . import loop_health

TABLES = (
    "session",
    "turn",
    "message",
    "llm_call",
    "tool_call",
    "tool_op",
    "subagent_spawn",
    "git_event",
    "loop_run",
    "lane",
    "tool_io",
    "attachment",
    "file_touch",
)
STATUSES = ("pending", "indexed", "partial", "unavailable", "error")


def gauge(name: str, help_text: str, value: float) -> Family:
    return Family(name, "gauge", help_text, (Sample((), value),))


# Producer-written run snapshots in the shared state volume, read by the indexer's metric
# collection. Nothing scrapes these files: the collection owns validation and OTLP publication.
RUN_METRICS = {
    "agent_history_run_success": (),
    "agent_history_run_duration_seconds": (),
    "agent_history_lock_held": (),
    "agent_history_run_files": ("result",),
    "agent_history_run_lines": (),
    "agent_history_run_rows": (),
    "agent_history_embed_last_failure_reason": ("reason",),
    "agent_history_embed_run_success": (),
    "agent_history_embed_run_duration_seconds": (),
    "agent_history_embed_run_items": (),
    "agent_history_embed_run_chunks": (),
    "agent_history_embed_run_api_inputs": (),
    "agent_history_embed_run_cached_inputs": (),
    "agent_history_embed_run_tokens": (),
    "agent_history_embed_run_skipped": ("reason",),
    "agent_history_embed_last_success_timestamp_seconds": (),
    "agent_history_embed_gc_last_run_timestamp_seconds": (),
    "agent_history_embed_gc_dry_run": (),
    "agent_history_embed_gc_eligible": (),
    "agent_history_embed_gc_deleted": (),
    "agent_history_embed_gc_skipped": ("reason",),
}
EMBED_FAILURE_REASONS = frozenset(
    {"auth", "billing_quota", "rate_limit", "route", "provider_error", "network", "other"}
)
RUN_RESULTS = frozenset({"parsed", "rewritten", "tier_only", "load_error"})
# These are the complete skip vocabularies in the producers, not input-derived values.
RUN_REASONS = frozenset(
    {
        "none",
        "daily_cap",
        "lock_held",
        "embed_running",
        "refresh_running",
        "rebuild_in_progress",
        "no_reset_marker",
        "bad_reset_marker",
        "recent_chunk_reset",
        "backlog_pending",
        "no_model",
        "no_new_chunks",
        "disabled",
        "no_api_key",
        "api_error",
        "limit",
        "other",
    }
)
RUN_LINE = re.compile(r'^([a-z_]+)(?:\{([a-z_]+)="([a-z_]+)"\})? (-?[0-9]+(?:\.[0-9]+)?)$')


class RunCollector:
    name = "runs"

    def __init__(self, directory: Path):
        self.directory = directory

    def collect(self):
        values: dict[str, list[Sample]] = {}
        for filename in ("agent-history.prom", "agent-history-embed.prom"):
            path = self.directory / filename
            if path.is_symlink() or not path.is_file():
                continue
            if path.stat().st_size > 65536:
                raise ValueError("worker metrics snapshot too large")
            for line in path.read_text(encoding="utf-8").splitlines():
                match = RUN_LINE.fullmatch(line)
                if not match:
                    continue
                name, label, value, raw = match.groups()
                expected = RUN_METRICS.get(name)
                if expected is None or tuple([label] if label else []) != expected:
                    continue
                if label == "result" and value not in RUN_RESULTS:
                    continue
                if name == "agent_history_embed_last_failure_reason":
                    if value not in EMBED_FAILURE_REASONS:
                        continue
                elif label == "reason" and value not in RUN_REASONS:
                    continue
                values.setdefault(name, []).append(Sample(((label, value),) if label else (), float(raw)))
        return tuple(
            Family(
                name,
                "gauge",
                "Last worker run: " + name.removeprefix("agent_history_").replace("_", " ") + ".",
                tuple(samples),
            )
            for name, samples in sorted(values.items())
        )


# The sections whose queries scan whole tables, and every table each one reads. A section is
# recomputed only when one of its tables' change tokens moves (see CatalogueCollector); the rest
# (refresh, dirty, rows) are cheap and run on every collection.
GATED_SECTIONS = {
    "sources": ("source_file",),
    "lag": ("source_file",),
    "parse_issues": ("parse_issue",),
    "links": ("subagent_spawn", "session"),
    "embed_pending": ("chunk", "embedding"),
    "embed_failures": ("embed_failure",),
    "embed_vectors": ("embedding",),
    # Every relation sql/loop_health.sql reads, including ah.meta through ah.canonical_repo.
    "loop_health": ("loops", "loop_run", "session", "meta", "llm_call", "request_progress", "message", "lane"),
}
# Token per table: the file node (changes on TRUNCATE, VACUUM FULL, CLUSTER and rewriting ALTERs, none of
# which move the tuple counters) and the cumulative insert, update and delete counts.
TOKEN_QUERY = (
    "SELECT c.relname, c.relfilenode::bigint, s.n_tup_ins, s.n_tup_upd, s.n_tup_del "
    "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
    "JOIN pg_stat_user_tables s ON s.relid = c.oid "
    "WHERE n.nspname = 'ah' AND c.relkind = 'r' AND c.relname = ANY(%s)"
)


@dataclass(frozen=True)
class _Cached:
    token: tuple
    computed_at: float
    families: tuple[Family, ...]
    dependencies: object = None


class _Gate:
    """What a gated section body needs to know: whether the cached families were already published."""

    reused = False
    error: Exception | None = None  # A failed probe: the body is skipped and the section then fails.


class CatalogueCollector:
    """Catalogue health gauges, with the table-scanning sections gated on change tokens.

    The exporter calls `collect` every `[exporter] refresh_interval` (15 seconds by default) but the
    catalogue only changes when an index, embed or journal-sync pass commits, roughly every few minutes.
    The cheap sections (refresh, dirty, rows) run on every call. The heavy ones (`GATED_SECTIONS`)
    keep their last published families and recompute only when their inputs have changed:

    * Each gated section has a token built from, for every table it reads, `pg_class.relfilenode` plus
      `n_tup_ins`, `n_tup_upd` and `n_tup_del` from `pg_stat_user_tables`. The relfilenode makes a
      `TRUNCATE` (as the rebuild does) visible, which the tuple counters alone would miss.
    * All tokens are read in one statement before any section runs, so a write that lands during a
      section's query always changes the token the next call compares. A failed token read, or a table
      without statistics, means that section recomputes.
    * A cached section is reused only while its token is unchanged and it is younger than `max_age`
      (15 minutes, the backstop for counters that were reset or never flushed). If the token moved but
      the last computation is younger than `min_interval` (60 seconds), it is reused too, so a long
      multi-transaction pass cannot make the heavy queries run on every scrape.
    * Completed-loop health additionally compares exact scoped row versions when broad tokens move.
      Unrelated activity keeps its families without renewing the full-query maximum age.
    * A reused section still runs `SELECT 1 FROM ah.<table> WHERE false` for every table it reads,
      inside the section's own transaction and statement timeout. Permission loss or an
      ACCESS EXCLUSIVE lock therefore fails a reused section exactly as it would a recomputed one.
    * A section that fails, on its probe or its query, publishes nothing and loses its cache entry:
      never a partial family, a stale value or a false zero.

    Staleness bound: a change is visible at the first collection that both follows its statistics
    flush (a backend flushes pending counters within about one second of a transaction end, or up to
    ten seconds when it goes idle) and falls at least `min_interval` after the section's last
    computation; an undetected change is visible after `max_age`. A reused section counts as a
    success in `section_health`, since its probes passed against the live catalogue.
    """

    name = "catalogue"
    MIN_RECOMPUTE_SECONDS = 60.0
    MAX_AGE_SECONDS = 900.0

    def __init__(
        self,
        dsn: str,
        *,
        clock=time.monotonic,
        min_interval: float = MIN_RECOMPUTE_SECONDS,
        max_age: float = MAX_AGE_SECONDS,
    ):
        self.dsn = dsn
        self.section_health: dict[str, tuple[float, bool]] = {}
        self.clock = clock
        self.min_interval = min_interval
        self.max_age = max_age
        self.cache: dict[str, _Cached] = {}
        self._tokens: dict[str, tuple] = {}
        self._now = 0.0

    def read_tokens(self, conn) -> dict[str, tuple]:
        """Per-section change tokens, in a statement of their own so a failure only forces recomputes."""
        wanted = sorted({table for tables in GATED_SECTIONS.values() for table in tables})
        try:
            with conn.transaction():
                conn.execute("SELECT set_config('statement_timeout', %s, true)", ("5s",))
                rows = conn.execute(TOKEN_QUERY, (wanted,)).fetchall()
        except Exception:
            return {}
        per_table = {row[0]: tuple(row[1:]) for row in rows}
        return {
            name: tuple(per_table[table] for table in tables)
            for name, tables in GATED_SECTIONS.items()
            if all(table in per_table and None not in per_table[table] for table in tables)
        }

    def _reusable(self, name) -> _Cached | None:
        entry, token = self.cache.get(name), self._tokens.get(name)
        if entry is None or token is None:
            return None
        age = self._now - entry.computed_at
        if age < 0 or age >= self.max_age:
            return None
        return entry if entry.token == token or age < self.min_interval else None

    @contextmanager
    def section(self, conn, result, name):
        """Publish a whole section or none, rolling back before the next query."""
        start = time.monotonic()
        first = len(result)
        failed = False
        gate = _Gate()
        tables = GATED_SECTIONS.get(name, ())
        try:
            with conn.transaction():
                # The heavier health query has its own budget, never a session-wide override.
                timeout = "60s" if name == "loop_health" else "5s"
                conn.execute("SELECT set_config('statement_timeout', %s, true)", (timeout,))
                entry = self._reusable(name) if tables else None
                dependencies = None
                token = self._tokens.get(name)
                if name == "loop_health" and entry is None and token is not None:
                    try:
                        # Table churn is only a reason to inspect this completed-loop scope.
                        # Relation identity protects row versions across TRUNCATE/rewrite.
                        dependencies = (tuple(t[0] for t in token), loop_health.dependencies(conn))
                        previous = self.cache.get(name)
                        if (
                            previous is not None
                            and 0 <= self._now - previous.computed_at < self.max_age
                            and dependencies == previous.dependencies
                        ):
                            entry = _Cached(token, previous.computed_at, previous.families, dependencies)
                    except Exception as exc:
                        gate.reused = True
                        gate.error = exc
                if entry is not None:
                    # Same relations, same transaction, same timeout as the real query would touch.
                    gate.reused = True
                    try:
                        for table in tables:
                            conn.execute(f"SELECT 1 FROM ah.{table} WHERE false")
                    except Exception as exc:
                        gate.error = exc
                    else:
                        result.extend(entry.families)
                # A context manager must always reach its yield; the body is skipped when reused.
                yield gate
                if gate.error is not None:
                    raise gate.error
                token = self._tokens.get(name)
                if tables and not gate.reused:
                    if token is None:
                        self.cache.pop(name, None)
                    else:
                        self.cache[name] = _Cached(token, self._now, tuple(result[first:]), dependencies)
                elif entry is not None:
                    # Accept the new broad token without renewing the full-query age.
                    self.cache[name] = entry
        except Exception:
            # No partial family, stale value or false zero for an unknown section.
            del result[first:]
            self.cache.pop(name, None)
            failed = True
        finally:
            self.section_health[f"catalogue_{name}"] = (time.monotonic() - start, failed)

    def collect(self):
        self.section_health = {}
        # The shared factory is psycopg.connect unchanged unless telemetry is enabled.
        with telemetry.db_connect(
            self.dsn, application_name="agent-history-metrics", autocommit=True, connect_timeout=5
        ) as conn:
            conn.read_only = True
            result = []
            # Read before any section query: a write that commits while a section runs then moves the
            # token the next collection compares, so it is never absorbed into a cached value.
            self._now = self.clock()
            self._tokens = self.read_tokens(conn)
            with self.section(conn, result, "refresh"):
                success = conn.execute(
                    "SELECT extract(epoch FROM max(finished_at)) FROM ah.refresh_log WHERE ok"
                ).fetchone()[0]
                if success is not None:
                    result.append(
                        gauge("agent_history_last_success_timestamp_seconds", "Last successful index refresh.", success)
                    )
            with self.section(conn, result, "sources") as gate:
                if not gate.reused:
                    statuses = dict(conn.execute("SELECT status, count(*) FROM ah.source_file GROUP BY status"))
                    result.append(
                        Family(
                            "agent_history_sources",
                            "gauge",
                            "Catalogue source files by status.",
                            tuple(Sample((("status", status),), statuses.get(status, 0)) for status in STATUSES),
                        )
                    )
            with self.section(conn, result, "lag") as gate:
                if not gate.reused:
                    lag, invalid = conn.execute(
                        "SELECT COALESCE(sum(GREATEST(size_bytes-indexed_offset,0)),0), "
                        "COALESCE(sum(CASE WHEN status='partial' THEN "
                        "LEAST(nul_tail_bytes,GREATEST(size_bytes-indexed_offset,0)) ELSE 0 END),0) "
                        "FROM ah.source_file WHERE status <> 'unavailable'"
                    ).fetchone()
                    result.append(
                        gauge("agent_history_lag_bytes", "Unindexed source bytes, including invalid tails.", lag)
                    )
                    result.append(
                        gauge("agent_history_nul_tail_bytes", "Observed NUL-only invalid tail bytes.", invalid)
                    )
                    result.append(
                        gauge(
                            "agent_history_actionable_lag_bytes",
                            "Unindexed bytes excluding observed NUL-only tails.",
                            lag - invalid,
                        )
                    )
            with self.section(conn, result, "parse_issues") as gate:
                if not gate.reused:
                    issues = dict(conn.execute("SELECT kind, count(*) FROM ah.parse_issue GROUP BY kind"))
                    # Parser kinds are implementation-defined and finite, but never expose arbitrary input.
                    result.append(
                        Family(
                            "agent_history_parse_issues_total",
                            "gauge",
                            "Current parse issues by kind.",
                            tuple(
                                Sample((("kind", kind),), value)
                                for kind, value in sorted(issues.items())
                                if kind.isidentifier() and len(kind) <= 64
                            ),
                        )
                    )
            with self.section(conn, result, "links") as gate:
                if not gate.reused:
                    unresolved = conn.execute(
                        "SELECT count(*) FROM ah.subagent_spawn WHERE child_session_id IS NULL "
                        "AND (child_agent_id IS NOT NULL OR child_task_name IS NOT NULL)"
                    ).fetchone()[0]
                    orphans = conn.execute(
                        "SELECT count(*) FROM ah.session WHERE NOT is_stub AND root_session_id IS NULL"
                    ).fetchone()[0]
                    result.append(
                        Family(
                            "agent_history_unresolved_links",
                            "gauge",
                            "Unresolved catalogue links.",
                            (
                                Sample((("kind", "spawn_child"),), unresolved),
                                Sample((("kind", "session_root"),), orphans),
                            ),
                        )
                    )
            with self.section(conn, result, "dirty"):
                dirty = conn.execute("SELECT count(*) FROM ah.dirty_session").fetchone()[0]
                result.append(gauge("agent_history_dirty_sessions", "Sessions awaiting post-pass.", dirty))
            with self.section(conn, result, "rows"):
                estimates = [
                    (
                        table,
                        conn.execute(
                            "SELECT GREATEST(reltuples::bigint,0) FROM pg_class WHERE oid = %s::regclass",
                            (f"ah.{table}",),
                        ).fetchone()[0],
                    )
                    for table in TABLES
                ]
                result.append(
                    Family(
                        "agent_history_rows",
                        "gauge",
                        "Estimated catalogue rows.",
                        tuple(Sample((("table", table),), count) for table, count in estimates),
                    )
                )
            with self.section(conn, result, "embed_pending") as gate:
                if not gate.reused:
                    pending = conn.execute(
                        "SELECT count(*) FROM ah.chunk c WHERE NOT EXISTS "
                        "(SELECT 1 FROM ah.embedding e WHERE e.model = c.model "
                        "AND e.input_sha256 = c.input_sha256)"
                    ).fetchone()[0]
                    result.append(gauge("agent_history_embed_pending_messages", "Chunks awaiting embeddings.", pending))
            with self.section(conn, result, "embed_failures") as gate:
                if not gate.reused:
                    # Same count as embed.FAILED_INPUTS_SQL: the embedder clears embedded and orphaned rows.
                    failures = conn.execute("SELECT count(*) FROM ah.embed_failure").fetchone()[0]
                    result.append(
                        gauge("agent_history_embed_failed_inputs", "Inputs with embedding failures.", failures)
                    )
            with self.section(conn, result, "embed_vectors") as gate:
                if not gate.reused:
                    vectors = conn.execute("SELECT count(*) FROM ah.embedding").fetchone()[0]
                    result.append(gauge("agent_history_embed_vectors", "Stored embedding vectors.", vectors))
            with self.section(conn, result, "loop_health") as gate:
                if not gate.reused:
                    result.extend(loop_health.collect(conn))
            return result

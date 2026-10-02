"""Read-only health gauges from the ParadeDB catalogue."""

from __future__ import annotations

import re
from pathlib import Path

from . import Family, Sample

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


# Producer-written run snapshots in the shared state volume. This is not an Alloy
# textfile endpoint: the HTTP exporter owns exposition, validation and escaping.
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


class CatalogueCollector:
    name = "catalogue"

    def __init__(self, dsn: str):
        self.dsn = dsn

    def collect(self):
        import psycopg

        with psycopg.connect(self.dsn, application_name="agent-history-exporter", autocommit=True) as conn:
            conn.read_only = True
            result = []
            success = conn.execute(
                "SELECT extract(epoch FROM max(finished_at)) FROM ah.refresh_log WHERE ok"
            ).fetchone()[0]
            if success is not None:
                result.append(
                    gauge("agent_history_last_success_timestamp_seconds", "Last successful index refresh.", success)
                )
            statuses = dict(conn.execute("SELECT status, count(*) FROM ah.source_file GROUP BY status"))
            result.append(
                Family(
                    "agent_history_sources",
                    "gauge",
                    "Catalogue source files by status.",
                    tuple(Sample((("status", status),), statuses.get(status, 0)) for status in STATUSES),
                )
            )
            lag = conn.execute(
                "SELECT COALESCE(sum(GREATEST(size_bytes-indexed_offset,0)),0) "
                "FROM ah.source_file WHERE status <> 'unavailable'"
            ).fetchone()[0]
            result.append(gauge("agent_history_lag_bytes", "Unindexed source bytes.", lag))
            issues = dict(conn.execute("SELECT kind, count(*) FROM ah.parse_issue GROUP BY kind"))
            # Parser kinds are implementation-defined and finite in the schema, but never expose arbitrary input.
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
                    (Sample((("kind", "spawn_child"),), unresolved), Sample((("kind", "session_root"),), orphans)),
                )
            )
            dirty = conn.execute("SELECT count(*) FROM ah.dirty_session").fetchone()[0]
            result.append(gauge("agent_history_dirty_sessions", "Sessions awaiting post-pass.", dirty))
            estimates = [
                (
                    table,
                    conn.execute(
                        "SELECT GREATEST(reltuples::bigint,0) FROM pg_class WHERE oid = %s::regclass", (f"ah.{table}",)
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
            pending = conn.execute(
                "SELECT count(*) FROM ah.chunk c WHERE NOT EXISTS "
                "(SELECT 1 FROM ah.embedding e WHERE e.model = c.model "
                "AND e.input_sha256 = c.input_sha256)"
            ).fetchone()[0]
            result.append(gauge("agent_history_embed_pending_messages", "Chunks awaiting embeddings.", pending))
            failures = conn.execute("SELECT count(*) FROM ah.embed_failure").fetchone()[0]
            result.append(gauge("agent_history_embed_failed_inputs", "Inputs with embedding failures.", failures))
            vectors = conn.execute("SELECT count(*) FROM ah.embedding").fetchone()[0]
            result.append(gauge("agent_history_embed_vectors", "Stored embedding vectors.", vectors))
            return result

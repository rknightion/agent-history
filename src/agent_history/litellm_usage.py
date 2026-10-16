"""Fill unknown Claude Code usage from a LiteLLM proxy's spend logs.

A Claude Code home routed through LiteLLM's Anthropic-to-Responses adapter receives a
message_start event whose usage is all zero. Claude Code writes every subagent content block
with that usage, so those llm_call rows are stored as unknown (see LlmCallRow.unknown_if_zero).
LiteLLM records the real counts in "LiteLLM_SpendLogs", keyed by the same `msg_<uuid>` id the
transcript records as the response id.

Only rows in the configured namespaces whose usage is still unknown (NULL, or the all-zero shape
indexed before that rule) are touched; a known count is never overwritten. The spend-log
connection is read-only. Without a configured DSN nothing runs and usage stays unknown.
"""

from __future__ import annotations

from typing import Any, Iterable

import psycopg

from .config import LiteLLM

UNKNOWN_SQL = """
    SELECT c.id, c.response_id, c.session_id
    FROM ah.llm_call c JOIN ah.session s ON s.id = c.session_id
    WHERE s.namespace = ANY(%(namespaces)s) AND c.agent = 'claude' AND c.response_id LIKE 'msg\\_%%'
      AND c.ts >= now() - make_interval(days => %(days)s)
      AND c.stop_reason IS NULL AND NOT c.is_api_error
      AND COALESCE(c.input_uncached, 0) = 0 AND COALESCE(c.cache_read, 0) = 0
      AND COALESCE(c.cache_write_5m, 0) = 0 AND COALESCE(c.cache_write_1h, 0) = 0
      AND COALESCE(c.output, 0) = 0
      AND c.id > %(after)s
    ORDER BY c.id LIMIT %(limit)s
"""

SPEND_SQL = """
    SELECT request_id, prompt_tokens, completion_tokens, metadata -> 'usage_object'
    FROM "LiteLLM_SpendLogs" WHERE request_id = ANY(%s)
"""

# The same unknown predicate guards the write, so a row a concurrent pass already filled is kept.
UPDATE_SQL = """
    UPDATE ah.llm_call SET input_uncached = %s, cache_read = %s, cache_write_5m = %s, cache_write_1h = 0,
        output = %s, reasoning = COALESCE(reasoning, %s)
    WHERE id = %s AND stop_reason IS NULL
      AND COALESCE(input_uncached, 0) = 0 AND COALESCE(cache_read, 0) = 0
      AND COALESCE(cache_write_5m, 0) = 0 AND COALESCE(cache_write_1h, 0) = 0 AND COALESCE(output, 0) = 0
"""


def _count(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float) and value.is_integer() and value >= 0:
        return int(value)
    return None


def usage_from_spend(prompt: Any, completion: Any, usage_object: Any) -> tuple[int, int, int, int, int | None] | None:
    """(input_uncached, cache_read, cache_write, output, reasoning) from one spend-log row.

    LiteLLM's prompt_tokens includes cached and cache-creation tokens; the transcript convention
    stores them apart. A row without integer prompt and completion counts supplies nothing.
    """
    prompt, completion = _count(prompt), _count(completion)
    if prompt is None or completion is None:
        return None
    usage = usage_object if isinstance(usage_object, dict) else {}
    details = usage.get("prompt_tokens_details") if isinstance(usage.get("prompt_tokens_details"), dict) else {}
    cached = _count(details.get("cached_tokens"))
    if cached is None:
        cached = _count(usage.get("cache_read_input_tokens")) or 0
    written = _count(usage.get("cache_creation_input_tokens")) or 0
    output_details = (
        usage.get("completion_tokens_details") if isinstance(usage.get("completion_tokens_details"), dict) else {}
    )
    reasoning = _count(output_details.get("reasoning_tokens"))
    if cached + written > prompt:
        return None
    return prompt - cached - written, cached, written, completion, reasoning


def _spend_rows(source: psycopg.Connection, ids: list[str]) -> dict[str, tuple]:
    with source.transaction():
        source.execute("SET TRANSACTION READ ONLY")
        # The fill runs under the index lock: a stalled spend log must fail, not hold indexing.
        source.execute("SET LOCAL statement_timeout = '30s'")
        return {row[0]: row[1:] for row in source.execute(SPEND_SQL, (ids,))}


def fill(
    conn: psycopg.Connection,
    source: psycopg.Connection,
    namespaces: Iterable[str],
    days: int,
    batch: int = 500,
    max_rows: int | None = None,
) -> dict[str, int]:
    """Fill unknown rows in `conn` from spend logs read through `source`. The caller commits.

    Each filled row's session is marked dirty, so the next post pass recomputes the session
    rollup and every loop that contains it.
    """
    namespaces = list(namespaces)
    stats = {"candidates": 0, "filled": 0, "missing": 0, "unusable": 0}
    after = 0
    sessions: set[int] = set()
    while max_rows is None or stats["candidates"] < max_rows:
        limit = batch if max_rows is None else min(batch, max_rows - stats["candidates"])
        rows = conn.execute(
            UNKNOWN_SQL, {"namespaces": namespaces, "days": days, "after": after, "limit": limit}
        ).fetchall()
        if not rows:
            break
        after = rows[-1][0]
        stats["candidates"] += len(rows)
        spend = _spend_rows(source, sorted({r[1] for r in rows}))
        for row_id, response_id, session_id in rows:
            found = spend.get(response_id)
            if found is None:
                stats["missing"] += 1
                continue
            usage = usage_from_spend(*found)
            if usage is None:
                stats["unusable"] += 1
                continue
            uncached, cached, written, output, reasoning = usage
            if conn.execute(UPDATE_SQL, (uncached, cached, written, output, reasoning, row_id)).rowcount:
                stats["filled"] += 1
                sessions.add(session_id)
        if len(rows) < limit:
            break
    if sessions:
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO ah.dirty_session (session_id) VALUES (%s) ON CONFLICT DO NOTHING",
                [(sid,) for sid in sorted(sessions)],
            )
    return stats


def connect_source(dsn: str) -> psycopg.Connection:
    return psycopg.connect(dsn, autocommit=False, connect_timeout=10, application_name="agent-history-usage")


def filler(config: LiteLLM, days: int | None = None, max_rows: int | None = None):
    """A refresh hook for load.refresh(usage_fill=...), or None when no DSN is configured."""
    if not config.enabled:
        return None

    def run(conn: psycopg.Connection) -> dict[str, int]:
        with connect_source(config.dsn) as source:
            return fill(conn, source, config.namespaces, days or config.lookback_days, config.batch, max_rows)

    return run

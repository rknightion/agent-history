"""Latest completed-loop observations, bounded to repository and harness dimensions."""

from __future__ import annotations

from importlib.resources import files

from . import Family, Sample

QUERY = files("agent_history").joinpath("sql/loop_health.sql").read_text(encoding="utf-8")
# Both queries select exactly the same completed runs, windows and relaunch roots.
# OFFSET 0 preserves parameterised root reads instead of scanning unrelated message/call tables.
_SCOPE, _ = QUERY.split("\nSELECT w.repo,", 1)
DEPENDENCY_QUERY = (
    _SCOPE
    + """
, selected_calls AS MATERIALIZED (
    SELECT DISTINCT c.id, c.version, c.session_id, c.response_id
    FROM windows w JOIN roots r ON r.id = w.id
    CROSS JOIN LATERAL (
        SELECT c.id, c.xmin::text AS version, c.session_id, c.response_id, c.ts, c.stop_reason
        FROM ah.llm_call c WHERE c.session_id = r.session_id
        OFFSET 0
    ) c
    WHERE c.ts >= w.launch_ts AND c.ts < w.window_end
      AND (c.stop_reason IS NULL OR (c.stop_reason NOT LIKE 'usage:%'
           AND c.stop_reason NOT IN ('compaction', 'branch_summary')))
), selected_messages AS (
    SELECT DISTINCT m.id, m.version
    FROM windows w JOIN roots r ON r.id = w.id
    CROSS JOIN LATERAL (
        SELECT m.id, m.xmin::text AS version, m.ts, m.role, m.message_class FROM ah.message m
        WHERE m.session_id = r.session_id
        OFFSET 0
    ) m
    WHERE m.ts >= w.launch_ts AND m.ts < w.window_end
      AND m.role = 'assistant' AND m.message_class = 'assistant_text'
), selected_progress AS (
    SELECT DISTINCT p.session_id, p.request_uid, p.version
    FROM selected_calls c CROSS JOIN LATERAL (
        SELECT p.session_id, p.request_uid, p.xmin::text AS version FROM ah.request_progress p
        WHERE p.session_id = c.session_id AND p.response_id = c.response_id
        OFFSET 0
    ) p
)
SELECT jsonb_build_object(
    'windows', (SELECT jsonb_agg(to_jsonb(w) ORDER BY w.id) FROM windows w),
    'roots', (SELECT jsonb_agg(to_jsonb(r) ORDER BY r.id, r.session_id) FROM roots r),
    'calls', (SELECT jsonb_agg(jsonb_build_array(id, version) ORDER BY id) FROM selected_calls),
    'progress', (SELECT jsonb_agg(jsonb_build_array(session_id, request_uid, version)
                                ORDER BY session_id, request_uid) FROM selected_progress),
    'messages', (SELECT jsonb_agg(jsonb_build_array(id, version) ORDER BY id) FROM selected_messages),
    'lanes', (SELECT jsonb_agg(jsonb_build_array(n.id, n.xmin::text) ORDER BY n.id)
              FROM ah.lane n WHERE n.loop_run_id IN (SELECT id FROM windows))
)

"""
)


def dependencies(conn):
    """Exact scoped row versions, never transcript text; max_age bounds xmin reuse."""
    return conn.execute(DEPENDENCY_QUERY).fetchone()[0]


PREFIX = "agent_history_loop_health_"
# All are gauges, including counts: a newer completed loop replaces the entire observation.
METRICS = {
    "completed_timestamp_seconds": "End of the latest completed loop, not collection time.",
    "root_model_time_share": "Root assistant model time / wall time, capped at one; omitted unless timing is complete.",
    "root_model_calls": "Root assistant calls in the completed-loop window, excluding compaction and usage records.",
    "root_model_timed_calls": "Root assistant calls with valid finished persisted pi timing in the window.",
    "root_messages": "Root assistant text messages in the completed-loop window, excluding reasoning and briefs.",
    "owner_questions_per_1k_root_messages": "Approximate owner-question text matches per 1000 root assistant text messages.",
    "owner_parks_per_1k_root_messages": "Approximate park text matches per 1000 root assistant text messages.",
    "ratification_rulings": "Approximate root assistant messages mentioning for-ratification, not verified decisions.",
    "lane_complete_rate": "Indexed lanes whose latest return status is complete / all indexed lanes; not acceptance.",
    "tasks_done": "Catalogue delivery count for the completed loop, retaining catalogue evidence precedence.",
    "estimated_cost_usd": "Catalogue list-price estimate for root and linked lanes, not an invoice; unknown omitted.",
}


def families(rows):
    """Do not coalesce NULL, or reuse an older known field when the newest loop is unknown."""
    result = []
    for field, help_text in METRICS.items():
        samples = tuple(
            Sample((("harness", row["harness"]), ("repo", row["repo"])), float(row[field]))
            for row in rows
            if row[field] is not None
        )
        if samples:
            result.append(Family(PREFIX + field, "gauge", help_text, samples))
    return result


def collect(conn):
    from psycopg.rows import dict_row

    with conn.cursor(row_factory=dict_row) as cursor:
        cursor.execute(QUERY)
        return families(cursor.fetchall())

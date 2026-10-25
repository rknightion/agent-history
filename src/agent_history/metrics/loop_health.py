"""Latest completed-loop observations, bounded to repository and harness dimensions."""

from __future__ import annotations

from importlib.resources import files

from . import Family, Sample

QUERY = files("agent_history").joinpath("sql/loop_health.sql").read_text(encoding="utf-8")
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

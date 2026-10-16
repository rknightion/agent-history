"""Observed v3 WAITING/PAUSED lifecycle, independent of process liveness.

Only exact root/relaunch transcript membership supplies evidence. No state file,
child message, heartbeat or local process lookup supplies a lifecycle marker.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

import psycopg

WAITING = re.compile(
    r"WAITING: (\S(?:.*\S)?) until "
    r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?(?:Z|\+00:00))"
)
PAUSED = re.compile(r"PAUSED: \S(?:.*\S)?")
TERMINAL = {"completion_receipt", "next_launch", "report_write"}


def parse_status(text: str, *, now: datetime) -> tuple[str, datetime | None] | None:
    """Read the last nonempty line; invalid/expired waits supply no override.

    The caller supplies an aware observation clock, so expiry is deterministic.
    UTC must be explicit, not an assumed timezone or a relative duration.
    """
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("observation clock must be timezone-aware")
    line = next((line.strip() for line in reversed(text.splitlines()) if line.strip()), "")
    if PAUSED.fullmatch(line):
        return "paused", None
    match = WAITING.fullmatch(line)
    if match:
        try:
            until = datetime.fromisoformat(match[2].replace("Z", "+00:00"))
        except ValueError:
            return None
        if until > now:
            return "waiting", until
    return None


def refresh(conn: psycopg.Connection) -> int:
    """Apply v3 observations to the current ordinary lifecycle projection.

    Qualified state closes must retain final precedence. A caller projecting them
    afterwards must also clear waiting_until on finished rows.

    Revisit open v3 rows even on collector-only passes: waits expire without dirty
    transcripts. Newer root activity invalidates the line, restoring the ordinary
    24-hour running/stale heuristic. Finished evidence always wins and never gains
    a fabricated timestamp. Historical v2 rows are not written.
    """
    now = conn.execute("SELECT now()").fetchone()[0].astimezone(timezone.utc)
    rows = conn.execute("""
        WITH windows AS (
            SELECT l.*, lead(launch_ts) OVER (
                PARTITION BY root_session_id ORDER BY launch_ts, id
            ) AS following
            FROM ah.loop_run l
        )
        SELECT l.launch_uid, live.status, live.end_ts, live.waiting_until,
               l.launch_ts, l.end_ts, l.end_evidence,
               greatest(l.launch_ts, roots.last_activity), msg.ts, msg.text
        FROM windows l JOIN ah.loops live USING (launch_uid)
        LEFT JOIN LATERAL (
            SELECT max(s.last_event_at) AS last_activity FROM ah.session s
            WHERE s.id = l.root_session_id
               OR (s.loop_run_id = l.id AND s.loop_link_method = 'relaunch')
        ) roots ON true
        LEFT JOIN LATERAL (
            SELECT m.ts, CASE WHEN m.message_class = 'assistant_text' AND NOT EXISTS (
                -- Another root's tied activity has no comparable source order.
                -- Its per-session seq (or catalogue id) cannot choose a winner.
                SELECT 1 FROM ah.session other
                WHERE (other.id = l.root_session_id
                    OR (other.loop_run_id = l.id AND other.loop_link_method = 'relaunch'))
                  AND other.id <> m.session_id AND other.last_event_at >= m.ts
            ) AND NOT EXISTS (
                -- Physical positions resolve ties only within one retained source.
                SELECT 1 FROM (
                    SELECT source_id, greatest(byte_offset, output_byte_offset) AS pos,
                           greatest(ts, output_at) AS ts
                    FROM ah.tool_io WHERE session_id = m.session_id
                    UNION ALL
                    SELECT source_id, byte_offset, ts FROM ah.llm_call WHERE session_id = m.session_id
                    UNION ALL
                    SELECT source_id, byte_offset, ts FROM ah.session_event WHERE session_id = m.session_id
                    UNION ALL
                    SELECT source_id, byte_offset, ts FROM ah.message WHERE session_id = m.session_id
                ) activity WHERE (activity.source_id = m.source_id AND activity.pos > m.byte_offset)
                    OR (activity.source_id <> m.source_id AND activity.ts = m.ts)
            ) THEN m.text ELSE '' END AS text
            FROM ah.message m JOIN ah.session s ON s.id = m.session_id
            WHERE (s.id = l.root_session_id
                OR (s.loop_run_id = l.id AND s.loop_link_method = 'relaunch'))
              AND m.ts >= l.launch_ts AND (l.following IS NULL OR m.ts < l.following)
            -- The guards above withhold cross-root/source ties. Within one
            -- physical record, match structure.SEQ_SQL: reasoning precedes text.
            ORDER BY m.ts DESC, m.byte_offset DESC,
                     CASE WHEN m.message_class = 'reasoning' THEN 0 ELSE 1 END DESC,
                     m.event_uid DESC LIMIT 1
        ) msg ON true
        WHERE l.mode = 'v3' AND (live.status <> 'finished' OR live.waiting_until IS NOT NULL)
    """).fetchall()
    changed = 0
    for uid, old_status, old_end, old_until, launch, end, evidence, activity, msg_at, text in rows:
        # State close is terminal only when this pass projected it into ah.loops.
        # loop_run can retain old state_close metadata after its snapshot disappears.
        if old_status == "finished" or (evidence in TERMINAL and end is not None):
            status, end_ts, until = "finished", old_end if old_status == "finished" else end, None
        else:
            marker = parse_status(text, now=now) if msg_at is not None and activity <= msg_at else None
            if marker:
                status, until = marker
                end_ts = None
            elif old_status in ("waiting", "paused"):
                status = "running" if activity >= now - timedelta(hours=24) else "stale"
                end_ts, until = (None if status == "running" else max(launch, activity)), None
            else:
                # No evidence means no change to ordinary lifecycle semantics.
                status, end_ts, until = old_status, old_end, None
        if (status, end_ts, until) != (old_status, old_end, old_until):
            conn.execute(
                "UPDATE ah.loops SET status=%s, end_ts=%s, waiting_until=%s, observed_at=%s WHERE launch_uid=%s",
                (status, end_ts, until, now, uid),
            )
            changed += 1
    return changed

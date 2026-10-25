-- Read-only metric query, not schema DDL. Pick the run before computing nullable fields:
-- a newer unknown observation must never fall back to an older known loop.
WITH latest AS MATERIALIZED (
    SELECT DISTINCT ON (ah.canonical_repo(l.repo), s.agent)
           r.id, r.root_session_id, r.report_path, r.launch_ts, l.end_ts,
           ah.canonical_repo(l.repo) AS repo, s.agent AS harness,
           l.tasks_done, l.priced_cost_usd
    FROM ah.loops l
    JOIN ah.loop_run r ON r.launch_uid = l.launch_uid
    JOIN ah.session s ON s.id = r.root_session_id
    WHERE l.status = 'finished' AND l.end_ts > r.launch_ts
      AND ah.canonical_repo(l.repo) IS NOT NULL AND s.agent IN ('pi', 'claude', 'codex')
      AND NOT EXISTS (SELECT 1 FROM ah.meta WHERE key = 'rebuild_in_progress')
    ORDER BY ah.canonical_repo(l.repo), s.agent, l.end_ts DESC, r.launch_ts DESC, r.launch_uid
), windows AS (
    SELECT l.*, LEAST(l.end_ts,
        (SELECT min(r.launch_ts) FROM ah.loop_run r
         WHERE r.id <> l.id AND r.launch_ts >= l.launch_ts
           AND (r.root_session_id = l.root_session_id OR r.report_path = l.report_path))) AS window_end
    FROM latest l
), roots AS MATERIALIZED (
    SELECT w.id, w.root_session_id AS session_id FROM windows w
    UNION
    SELECT w.id, s.id FROM windows w JOIN ah.session s
      ON s.loop_run_id = w.id AND s.loop_link_method = 'relaunch'
)
SELECT w.repo, w.harness, extract(epoch FROM w.end_ts) AS completed_timestamp_seconds,
       CASE WHEN w.harness = 'pi' AND c.calls > 0 AND c.timed = c.calls
                 AND w.window_end > w.launch_ts
            THEN LEAST(1, c.model_seconds / extract(epoch FROM (w.window_end - w.launch_ts)))
       END AS root_model_time_share,
       c.calls AS root_model_calls, c.timed AS root_model_timed_calls,
       m.messages AS root_messages,
       1000.0 * m.questions / NULLIF(m.messages, 0) AS owner_questions_per_1k_root_messages,
       1000.0 * m.parks / NULLIF(m.messages, 0) AS owner_parks_per_1k_root_messages,
       CASE WHEN m.messages > 0 THEN m.ratifications END AS ratification_rulings,
       n.complete::numeric / NULLIF(n.lanes, 0) AS lane_complete_rate,
       w.tasks_done, w.priced_cost_usd AS estimated_cost_usd
FROM windows w
LEFT JOIN LATERAL (
    SELECT count(*) AS calls, count(p.started_at) AS timed,
           sum(GREATEST(0, extract(epoch FROM
               (LEAST(p.ended_at, w.window_end) - GREATEST(p.started_at, w.launch_ts))))) AS model_seconds
    FROM roots r JOIN ah.llm_call c ON c.session_id = r.session_id
    LEFT JOIN LATERAL (
        -- Only final assistant response identity links timing, never ordinal or proximity.
        -- Multiple request UUIDs for one response are ambiguous, not additive timing.
        SELECT min(p.started_at) AS started_at, min(p.ended_at) AS ended_at
        FROM ah.request_progress p
        WHERE p.session_id = c.session_id AND p.response_id = c.response_id
          AND p.state = 'ended' AND p.ended_at >= p.started_at
          AND p.ended_at <= w.window_end AND p.written_at <= w.window_end
        HAVING count(*) = 1
    ) p ON true
    WHERE r.id = w.id AND c.ts >= w.launch_ts AND c.ts < w.window_end
      AND (c.stop_reason IS NULL OR
           (c.stop_reason NOT LIKE 'usage:%' AND c.stop_reason NOT IN ('compaction', 'branch_summary')))
) c ON true
LEFT JOIN LATERAL (
    -- Text is matched in PostgreSQL and never returned to the metric collector.
    SELECT count(*) AS messages,
           count(*) FILTER (WHERE m.text ~* '\m(owner question|questions? for (the )?owner)\M|\m(owner|you)\M[^\n?]{0,160}\?') AS questions,
           count(*) FILTER (WHERE m.text ~* '\mpark(ed|s|ing)?\M') AS parks,
           count(*) FILTER (WHERE m.text ~* '\mfor[ -]ratification\M') AS ratifications
    FROM roots r JOIN ah.message m ON m.session_id = r.session_id
    WHERE r.id = w.id AND m.ts >= w.launch_ts AND m.ts < w.window_end
      AND m.role = 'assistant' AND m.message_class = 'assistant_text'
) m ON true
LEFT JOIN LATERAL (
    SELECT count(*) AS lanes, count(*) FILTER (WHERE return_status = 'complete') AS complete
    FROM ah.lane n WHERE n.loop_run_id = w.id
) n ON true
WHERE w.window_end > w.launch_ts;

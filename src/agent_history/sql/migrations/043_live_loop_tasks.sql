-- Live loop tasks for a narrow consumer: one SECURITY DEFINER function a role can EXECUTE without
-- any table privilege, limited to the repositories that role is scoped to. Migration-owned (not in analytics.sql, which drops and recreates its
-- functions) so a consumer's EXECUTE grant survives analytics re-application and rebuild.
-- String bodies record no dependencies, so re-applying the analytics files never cascades here.

-- Which repositories a narrow login role may ask about. The function admits a session whose login
-- role (session_user, which SET ROLE cannot change) is a member of ah_reader or of the schema owner,
-- both of which can read every row anyway, or has a row here for the requested repository. Rows are
-- written by sql/live_loop_tasks_role.sql as a superuser; no consumer role can read or write them.
-- Operator configuration, not derived data: rebuild does not truncate it.
CREATE TABLE IF NOT EXISTS ah.live_loop_tasks_scope (
    role_name name NOT NULL,
    repo text NOT NULL CHECK (repo = lower(repo) AND repo ~ '^[a-z0-9_.-]+/[a-z0-9_.-]+$'),
    PRIMARY KEY (role_name, repo)
);
REVOKE ALL ON ah.live_loop_tasks_scope FROM PUBLIC;

-- The task a lane brief names, from its first line only:
--   Lane: <id> · Task: <ID> (<title>) · Tier: routine|guarded
-- No row when the first line is not exactly that header; the task is never guessed from other text.
-- src/agent_history/live_loop_tasks.py holds the same pattern and the tests hold the two to it.
CREATE OR REPLACE FUNCTION ah.lane_brief_task(brief text)
RETURNS TABLE (task_id text, title text)
LANGUAGE sql IMMUTABLE PARALLEL SAFE
SET search_path = pg_catalog, pg_temp
AS $fn$
    SELECT h.m[1], h.m[2]
    FROM (SELECT regexp_match(rtrim(split_part(brief, E'\n', 1), E'\r'),
                              '^Lane: [!-~]+ · Task: ([A-Za-z][A-Za-z0-9]*-[0-9]+(?:\.[0-9]+)*) \((.+)\) · Tier: (?:routine|guarded)$') AS m) h
    WHERE h.m IS NOT NULL AND char_length(h.m[2]) <= 500
$fn$;

-- Every open (running, waiting or paused) loop of one repository and the tasks its live lanes are
-- working on. A lane is one subagent brief in the loop's root (or exactly linked relaunch) at or
-- after launch; it is live until the catalogue records its return (see CONTRACT.md for how each
-- harness links a brief to its dispatch and return). The document carries loop numbers,
-- lifecycle fields, counts and header task ids and titles only: no session ids, paths, models,
-- costs or other transcript text.
CREATE OR REPLACE FUNCTION ah.live_loop_tasks(repo text)
RETURNS jsonb
LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $fn$
DECLARE
    document jsonb;
BEGIN
    IF live_loop_tasks.repo IS NULL OR live_loop_tasks.repo !~ '^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$' THEN
        RAISE EXCEPTION 'repo must be an owner/repo slug' USING ERRCODE = '22023';
    END IF;
    -- current_user is the owner here; the caller is the login role, which SET ROLE cannot change.
    IF NOT (EXISTS (SELECT 1 FROM pg_roles r
                    WHERE r.rolname = 'ah_reader' AND pg_has_role(session_user, r.oid, 'MEMBER'))
            OR EXISTS (SELECT 1 FROM pg_namespace n
                       WHERE n.nspname = 'ah' AND pg_has_role(session_user, n.nspowner, 'MEMBER'))
            OR EXISTS (SELECT 1 FROM ah.live_loop_tasks_scope sc
                       WHERE sc.role_name = session_user AND sc.repo = lower(live_loop_tasks.repo))) THEN
        RAISE EXCEPTION 'this role may not read live loop tasks for that repository' USING ERRCODE = '42501';
    END IF;
    IF EXISTS (SELECT 1 FROM ah.meta WHERE key = 'rebuild_in_progress') THEN
        RAISE EXCEPTION 'catalogue rebuild in progress' USING ERRCODE = '55000';
    END IF;

    WITH open_loops AS (
        SELECT l.launch_uid, r.id AS loop_run_id, r.root_session_id, l.loop AS label, l.launch_ts,
               l.status, l.waiting_until, l.observed_at
        FROM ah.loops l
        JOIN ah.loop_run r ON r.launch_uid = l.launch_uid
        WHERE l.status IN ('running', 'waiting', 'paused')
          AND lower(l.repo) = lower(live_loop_tasks.repo)
    ),
    roots AS (
        SELECT o.launch_uid, o.launch_ts, o.root_session_id AS session_id
        FROM open_loops o
        WHERE o.root_session_id IS NOT NULL
        UNION
        SELECT o.launch_uid, o.launch_ts, s.id
        FROM open_loops o
        JOIN ah.session s ON s.loop_run_id = o.loop_run_id AND s.loop_link_method = 'relaunch'
    ),
    briefs AS (
        SELECT rt.launch_uid, m.id AS message_id, m.agent, m.session_id, m.source_id, m.byte_offset, m.ts,
               m.text,
               CASE m.agent
                   WHEN 'claude' THEN substring(m.event_uid FROM '^(.+):brief$')
                   WHEN 'pi' THEN substring(m.event_uid FROM ':brief:(.+):[0-9]+$')
               END AS brief_call,
               CASE WHEN m.agent = 'pi' THEN substring(m.event_uid FROM ':([0-9]+)$') END AS brief_index
        FROM roots rt
        JOIN ah.message m ON m.session_id = rt.session_id
        WHERE m.message_class = 'subagent_brief' AND m.ts >= rt.launch_ts
    ),
    -- The dispatching tool call: Claude names it in the brief's event uid (<call>:brief), pi in
    -- <session>:<entry>:brief:<call>:<index>; a Codex brief is written from the spawn_agent call's own line.
    dispatched AS (
        SELECT b.*, COALESCE(t.call_uid, b.brief_call) AS call_uid, t.outcome, t.ended_at,
               t.background AS call_background, t.meta
        FROM briefs b
        LEFT JOIN LATERAL (
            SELECT c.call_uid, c.outcome, c.ended_at, c.background, c.meta
            FROM ah.tool_call c
            WHERE c.agent = b.agent AND c.session_id = b.session_id
              AND ((b.agent IN ('claude', 'pi') AND c.call_uid = b.brief_call)
                   OR (b.agent = 'codex' AND c.tool_name = 'spawn_agent' AND c.source_id = b.source_id
                       AND c.byte_offset = b.byte_offset))
            ORDER BY c.id
            LIMIT 1
        ) t ON true
    ),
    lanes AS (
        SELECT d.launch_uid, d.message_id, d.ts, d.text,
               -- unknown is not returned: a NULL test (no dispatch call, no outcome yet) stays live
               COALESCE((
                   -- the dispatch itself failed, was denied or was interrupted: no lane is running
                   d.outcome IN ('error', 'denied', 'interrupted', 'timeout')
                   -- the harness recorded the lane's end (Claude result or task notification, Codex
                   -- activity, pi async notice or foreground result), or a failed launch
                   OR EXISTS (
                       SELECT 1 FROM ah.subagent_spawn sp
                       WHERE sp.agent = d.agent AND sp.parent_session_id = d.session_id
                         AND sp.spawn_uid IN (d.call_uid, d.call_uid || ':' || d.brief_index)
                         AND (sp.completed_at IS NOT NULL OR sp.completion_status IS NOT NULL
                              OR sp.launch_status = 'error'))
                   -- the lane's own transcript holds a recorded lane-return
                   OR EXISTS (
                       SELECT 1 FROM ah.subagent_spawn sp
                       JOIN ah.lane ln ON ln.session_id = sp.child_session_id
                       WHERE sp.agent = d.agent AND sp.parent_session_id = d.session_id
                         AND sp.spawn_uid IN (d.call_uid, d.call_uid || ':' || d.brief_index)
                         AND (ln.returns IS NOT NULL OR ln.lane_return IS NOT NULL))
                   -- a pi foreground call returns only when its children have finished
                   OR (d.agent = 'pi' AND d.ended_at IS NOT NULL AND d.call_background IS NOT TRUE
                       AND COALESCE(d.meta ->> 'async', '') <> 'true'
                       AND COALESCE(d.meta ->> 'mode', '') <> 'async')
                   -- a pi asynchronous parallel or workflow run: completed children cannot be
                   -- matched to briefs, so its briefs return together once as many children as
                   -- briefs have recorded completion
                   OR (d.agent = 'pi' AND d.meta ->> 'run_id' IS NOT NULL
                       AND (SELECT count(*) FROM ah.subagent_spawn sp
                            WHERE sp.agent = 'pi' AND sp.parent_session_id = d.session_id
                              AND sp.workflow_id = d.meta ->> 'run_id' AND sp.spawn_uid <> d.call_uid
                              AND (sp.completed_at IS NOT NULL OR sp.completion_status IS NOT NULL))
                           >= (SELECT count(*) FROM briefs x
                               WHERE x.session_id = d.session_id AND x.brief_call = d.call_uid))
               ), false) AS returned
        FROM dispatched d
    ),
    live_lanes AS (
        SELECT * FROM lanes WHERE NOT returned
    ),
    tasks AS (
        SELECT DISTINCT ON (a.launch_uid, h.task_id)
               a.launch_uid, h.task_id, h.title,
               min(a.ts) OVER (PARTITION BY a.launch_uid, h.task_id) AS first_ts
        FROM live_lanes a
        CROSS JOIN LATERAL ah.lane_brief_task(a.text) h
        ORDER BY a.launch_uid, h.task_id, a.ts DESC, a.message_id DESC
    )
    SELECT jsonb_build_object('loops', COALESCE(jsonb_agg(jsonb_build_object(
               'loop', substring(o.label FROM '^loop([0-9]+)$')::integer,
               'launched_at', to_char(o.launch_ts AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),
               'status', o.status,
               'waiting_until', to_char(o.waiting_until AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),
               'observed_at', to_char(o.observed_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),
               'active_lanes', (SELECT count(*) FROM live_lanes a WHERE a.launch_uid = o.launch_uid),
               'tasks', COALESCE((SELECT jsonb_agg(jsonb_build_object('id', t.task_id, 'title', t.title)
                                                   ORDER BY t.first_ts, t.task_id)
                                  FROM tasks t WHERE t.launch_uid = o.launch_uid), '[]'::jsonb))
             ORDER BY o.launch_ts, o.launch_uid), '[]'::jsonb))
    INTO document
    FROM open_loops o;
    RETURN document;
END
$fn$;

REVOKE ALL ON FUNCTION ah.lane_brief_task(text) FROM PUBLIC;
REVOKE ALL ON FUNCTION ah.live_loop_tasks(text) FROM PUBLIC;

-- Consumers that already exist get EXECUTE now; one created later gets it from
-- sql/live_loop_tasks_role.sql. The plain reader may call it too: it can read every row anyway.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ah_loop_tasks') THEN
        GRANT EXECUTE ON FUNCTION ah.live_loop_tasks(text) TO ah_loop_tasks;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ah_reader') THEN
        GRANT EXECUTE ON FUNCTION ah.live_loop_tasks(text) TO ah_reader;
        GRANT EXECUTE ON FUNCTION ah.lane_brief_task(text) TO ah_reader;
    END IF;
END
$$;

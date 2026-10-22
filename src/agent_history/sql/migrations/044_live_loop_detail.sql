-- Charter first-wave task IDs and a richer live loop document for the same narrow consumer as
-- ah.live_loop_tasks (migration 043), which stays unchanged.

-- The ordered task IDs that open the rows under a charter's `## First wave` heading, and nothing
-- else from its body. NULL on closeouts, on charters the collector could not read as UTF-8 within
-- its size bound, and on rows written by a collector that predates this column; [] when a readable
-- charter has no first-wave rows. Existing rows stay NULL until collected again.
ALTER TABLE ah.loop_run_metadata ADD COLUMN IF NOT EXISTS task_ids text[];

-- One document for every open (running, waiting or paused) loop of one repository: lifecycle
-- fields, lane counts, the loop's tasks with their state, recent lane events and recent landings.
-- Same admission and guards as ah.live_loop_tasks. Lanes, briefs and returns are linked exactly
-- as there (see CONTRACT.md); the projected fields are whitelisted: loop numbers, timestamps,
-- counts, task IDs and brief header titles, lane names, role classes, raw return statuses and
-- commit short SHAs, first subject lines and Task trailers. No session or call ids, paths, hosts,
-- models, costs or other transcript or lane-return text.
CREATE OR REPLACE FUNCTION ah.live_loop_detail(repo text)
RETURNS jsonb
LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $fn$
DECLARE
    document jsonb;
    strict_id constant text := '^[A-Z][A-Z0-9]{0,15}-[0-9]{1,6}(\.[0-9]{1,4}){0,3}$';
BEGIN
    IF live_loop_detail.repo IS NULL OR live_loop_detail.repo !~ '^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$' THEN
        RAISE EXCEPTION 'repo must be an owner/repo slug' USING ERRCODE = '22023';
    END IF;
    -- current_user is the owner here; the caller is the login role, which SET ROLE cannot change.
    IF NOT (EXISTS (SELECT 1 FROM pg_catalog.pg_roles r
                    WHERE r.rolname = 'ah_reader' AND pg_catalog.pg_has_role(session_user, r.oid, 'MEMBER'))
            OR EXISTS (SELECT 1 FROM pg_catalog.pg_namespace n
                       WHERE n.nspname = 'ah' AND pg_catalog.pg_has_role(session_user, n.nspowner, 'MEMBER'))
            OR EXISTS (SELECT 1 FROM ah.live_loop_tasks_scope sc
                       WHERE sc.role_name = session_user AND sc.repo = pg_catalog.lower(live_loop_detail.repo))) THEN
        RAISE EXCEPTION 'this role may not read live loop detail for that repository' USING ERRCODE = '42501';
    END IF;
    IF EXISTS (SELECT 1 FROM ah.meta WHERE key = 'rebuild_in_progress') THEN
        RAISE EXCEPTION 'catalogue rebuild in progress' USING ERRCODE = '55000';
    END IF;

    WITH open_loops AS (
        -- The 20 most recent open loops. A loop's window runs from its launch to the earlier of its
        -- recorded end and the next launch on the same root or exact report path.
        SELECT l.launch_uid, r.id AS loop_run_id, r.root_session_id, r.goal_path, l.loop AS label,
               l.launch_ts, l.status, l.waiting_until, l.observed_at, l.last_activity_at,
               l.tasks_landed, l.commits,
               LEAST(l.end_ts, (SELECT min(r2.launch_ts) FROM ah.loop_run r2
                                WHERE r2.launch_ts > l.launch_ts
                                  AND (r2.root_session_id = r.root_session_id
                                       OR r2.report_path = r.report_path))) AS window_end
        FROM ah.loops l
        JOIN ah.loop_run r ON r.launch_uid = l.launch_uid
        WHERE l.status IN ('running', 'waiting', 'paused')
          AND pg_catalog.lower(l.repo) = pg_catalog.lower(live_loop_detail.repo)
        ORDER BY l.launch_ts DESC, l.launch_uid DESC
        LIMIT 20
    ),
    roots AS (
        SELECT o.launch_uid, o.launch_ts, o.window_end, o.root_session_id AS session_id
        FROM open_loops o
        WHERE o.root_session_id IS NOT NULL
        UNION
        SELECT o.launch_uid, o.launch_ts, o.window_end, s.id
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
          AND (rt.window_end IS NULL OR m.ts < rt.window_end)
    ),
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
    -- The returned test is ah.live_loop_tasks's, unchanged.
    lanes AS (
        SELECT d.launch_uid, d.message_id, d.agent, d.session_id, d.call_uid, d.brief_index, d.ts, d.text,
               d.ended_at,
               COALESCE((
                   d.outcome IN ('error', 'denied', 'interrupted', 'timeout')
                   OR EXISTS (
                       SELECT 1 FROM ah.subagent_spawn sp
                       WHERE sp.agent = d.agent AND sp.parent_session_id = d.session_id
                         AND sp.spawn_uid IN (d.call_uid, d.call_uid || ':' || d.brief_index)
                         AND (sp.completed_at IS NOT NULL OR sp.completion_status IS NOT NULL
                              OR sp.launch_status = 'error'))
                   OR EXISTS (
                       SELECT 1 FROM ah.subagent_spawn sp
                       JOIN ah.lane ln ON ln.session_id = sp.child_session_id
                       WHERE sp.agent = d.agent AND sp.parent_session_id = d.session_id
                         AND sp.spawn_uid IN (d.call_uid, d.call_uid || ':' || d.brief_index)
                         AND (ln.returns IS NOT NULL OR ln.lane_return IS NOT NULL))
                   OR (d.agent = 'pi' AND d.ended_at IS NOT NULL AND d.call_background IS NOT TRUE
                       AND COALESCE(d.meta ->> 'async', '') <> 'true'
                       AND COALESCE(d.meta ->> 'mode', '') <> 'async')
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
    -- Each lane with its spawn row, its lane transcript's catalogue row and its brief header.
    lane_facts AS (
        SELECT a.launch_uid, a.message_id, a.ts, a.returned,
               pg_catalog.left(COALESCE(ln.lane_name, hd.lane), 200) AS lane,
               CASE WHEN ln.role IS NOT NULL THEN pg_catalog.regexp_replace(ln.role, '^.*:', '') END AS role_class,
               ln.return_status,
               CASE WHEN h.task_id ~ strict_id THEN h.task_id END AS header_task,
               h.title AS header_title,
               -- a returned lane is for the task its lane-return names, else its header's
               CASE WHEN a.returned AND ln.lane_return ->> 'task' ~ strict_id THEN ln.lane_return ->> 'task'
                    WHEN h.task_id ~ strict_id THEN h.task_id END AS task_id,
               CASE WHEN a.returned
                    THEN GREATEST(a.ts, COALESCE(sp.completed_at, cs.last_event_at, a.ended_at, a.ts)) END AS returned_at
        FROM lanes a
        LEFT JOIN LATERAL (
            SELECT sp.completed_at, sp.child_session_id
            FROM ah.subagent_spawn sp
            WHERE sp.agent = a.agent AND sp.parent_session_id = a.session_id
              AND sp.spawn_uid IN (a.call_uid, a.call_uid || ':' || a.brief_index)
            ORDER BY sp.id
            LIMIT 1
        ) sp ON true
        LEFT JOIN LATERAL (
            SELECT l2.lane_name, l2.role, l2.return_status, l2.lane_return
            FROM ah.lane l2
            WHERE l2.session_id = sp.child_session_id
            ORDER BY l2.id DESC
            LIMIT 1
        ) ln ON true
        LEFT JOIN ah.session cs ON cs.id = sp.child_session_id
        LEFT JOIN LATERAL ah.lane_brief_task(a.text) h ON true
        LEFT JOIN LATERAL (
            SELECT substring(pg_catalog.rtrim(pg_catalog.split_part(a.text, E'\n', 1), E'\r')
                                        FROM '^Lane: ([!-~]+) · Task: ') AS lane
            WHERE h.task_id IS NOT NULL
        ) hd ON true
    ),
    -- The run's charter: the newest collected copy of its exact path from this repository that
    -- carries task IDs. The same path on another machine can belong to another repository.
    planned AS (
        SELECT DISTINCT ON (o.launch_uid, t.id) o.launch_uid, t.id, t.ord
        FROM open_loops o
        CROSS JOIN LATERAL (
            SELECT m.task_ids
            FROM ah.loop_run_metadata m
            WHERE m.kind = 'charter' AND m.path = o.goal_path AND m.task_ids IS NOT NULL
              AND m.repo_origin IS NOT NULL
              AND pg_catalog.lower(pg_catalog.regexp_replace(m.repo_origin, '^github\.com/', ''))
                  = pg_catalog.lower(live_loop_detail.repo)
            ORDER BY m.file_mtime DESC, m.seen_at DESC, m.machine
            LIMIT 1
        ) c
        CROSS JOIN LATERAL pg_catalog.unnest(c.task_ids) WITH ORDINALITY AS t(id, ord)
        WHERE t.id ~ strict_id
        ORDER BY o.launch_uid, t.id, t.ord
    ),
    named AS (
        SELECT f.launch_uid, f.header_task AS id, f.ts FROM lane_facts f WHERE f.header_task IS NOT NULL
        UNION ALL
        SELECT f.launch_uid, f.task_id, f.ts FROM lane_facts f WHERE f.task_id IS NOT NULL
    ),
    task_list AS (
        SELECT x.launch_uid, x.id, bool_or(x.planned) AS planned, min(x.ord) AS ord, min(x.first_ts) AS first_ts
        FROM (SELECT p.launch_uid, p.id, true AS planned, p.ord, NULL::timestamptz AS first_ts FROM planned p
              UNION ALL
              SELECT n.launch_uid, n.id, false, NULL::bigint, n.ts FROM named n) x
        GROUP BY x.launch_uid, x.id
    ),
    -- On-default commits of this repository inside each loop's window that carry a task of the
    -- loop. Collector slugs may keep their github.com/ host component; machine copies count once.
    loop_commits AS (
        SELECT DISTINCT ON (o.launch_uid, g.sha) o.launch_uid, g.sha, g.subject, g.committed_at,
               ARRAY(SELECT DISTINCT u.tid COLLATE "C" FROM pg_catalog.unnest(g.task_ids) AS u(tid)
                     WHERE u.tid ~ strict_id ORDER BY 1) AS task_ids
        FROM open_loops o
        JOIN ah.git_commit g
          ON pg_catalog.lower(pg_catalog.regexp_replace(g.repo_slug, '^github\.com/', '', 'i'))
             = pg_catalog.lower(live_loop_detail.repo)
         AND g.on_default
         AND g.committed_at >= o.launch_ts
         AND (o.window_end IS NULL OR g.committed_at < o.window_end)
        WHERE EXISTS (SELECT 1 FROM task_list t WHERE t.launch_uid = o.launch_uid AND t.id = ANY (g.task_ids))
        ORDER BY o.launch_uid, g.sha, g.seen_at DESC
    ),
    tasks AS (
        SELECT t.launch_uid, t.id, t.planned,
               row_number() OVER (PARTITION BY t.launch_uid ORDER BY t.ord NULLS LAST, t.first_ts, t.id COLLATE "C") AS rn,
               (SELECT f.header_title FROM lane_facts f
                WHERE f.launch_uid = t.launch_uid AND f.header_task = t.id
                ORDER BY f.ts DESC, f.message_id DESC LIMIT 1) AS title,
               CASE
                   WHEN EXISTS (SELECT 1 FROM loop_commits c WHERE c.launch_uid = t.launch_uid AND t.id = ANY (c.task_ids))
                       THEN 'landed'
                   WHEN EXISTS (SELECT 1 FROM lane_facts f
                                WHERE f.launch_uid = t.launch_uid AND NOT f.returned AND f.header_task = t.id)
                       THEN 'running'
                   ELSE COALESCE((
                       SELECT CASE WHEN f.return_status IN ('blocked', 'partial') THEN 'blocked'
                                   ELSE 'returned' END
                       FROM lane_facts f
                       WHERE f.launch_uid = t.launch_uid AND f.returned AND f.task_id = t.id
                         AND f.role_class ~ '^(worker|senior)(-|$)'
                       ORDER BY f.returned_at DESC, f.message_id DESC
                       LIMIT 1), 'planned')
               END AS state
        FROM task_list t
    ),
    events AS (
        SELECT e.*, row_number() OVER (PARTITION BY e.launch_uid ORDER BY e.at DESC, e.ord DESC, e.message_id DESC) AS rn
        FROM (SELECT f.launch_uid, f.ts AS at, 0 AS ord, f.message_id, f.lane, f.role_class, f.header_task AS task_id,
                     'dispatched' AS kind, NULL::text AS status
              FROM lane_facts f
              UNION ALL
              SELECT f.launch_uid, f.returned_at, 1, f.message_id, f.lane, f.role_class, f.task_id,
                     'returned', f.return_status
              FROM lane_facts f WHERE f.returned) e
    ),
    landings AS (
        SELECT c.*, row_number() OVER (PARTITION BY c.launch_uid ORDER BY c.committed_at DESC, c.sha DESC) AS rn
        FROM loop_commits c
    )
    SELECT jsonb_build_object('schema', 1, 'loops', COALESCE(jsonb_agg(jsonb_build_object(
               'loop', substring(o.label FROM '^loop([0-9]+)$')::integer,
               'status', o.status,
               'launched_at', pg_catalog.to_char(o.launch_ts AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),
               'waiting_until', pg_catalog.to_char(o.waiting_until AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),
               'observed_at', pg_catalog.to_char(o.observed_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),
               'last_activity_at', pg_catalog.to_char(o.last_activity_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),
               'lanes', (SELECT jsonb_build_object(
                                'total', count(*),
                                'running', count(*) FILTER (WHERE NOT f.returned),
                                'returned', count(*) FILTER (WHERE f.returned),
                                'blocked', count(*) FILTER (WHERE f.returned AND f.return_status IN ('blocked', 'partial')))
                         FROM lane_facts f WHERE f.launch_uid = o.launch_uid),
               'tasks_landed', o.tasks_landed,
               'commits', o.commits,
               'tasks', COALESCE((SELECT jsonb_agg(jsonb_build_object('id', t.id, 'title', t.title,
                                                                      'planned', t.planned, 'state', t.state)
                                                   ORDER BY t.rn)
                                  FROM tasks t WHERE t.launch_uid = o.launch_uid AND t.rn <= 200), '[]'::jsonb),
               'events', COALESCE((SELECT jsonb_agg(jsonb_build_object(
                                              'at', pg_catalog.to_char(e.at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),
                                              'lane', e.lane, 'role', e.role_class, 'task_id', e.task_id,
                                              'kind', e.kind, 'status', e.status)
                                          ORDER BY e.rn)
                                   FROM events e WHERE e.launch_uid = o.launch_uid AND e.rn <= 20), '[]'::jsonb),
               'landings', COALESCE((SELECT jsonb_agg(jsonb_build_object(
                                                'sha', pg_catalog.left(c.sha, 7),
                                                'subject', pg_catalog.left(pg_catalog.rtrim(pg_catalog.split_part(
                                                    COALESCE(c.subject, ''), E'\n', 1), E'\r'), 200),
                                                'at', pg_catalog.to_char(c.committed_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),
                                                'task_ids', pg_catalog.to_jsonb(c.task_ids))
                                            ORDER BY c.rn)
                                     FROM landings c WHERE c.launch_uid = o.launch_uid AND c.rn <= 10), '[]'::jsonb))
             ORDER BY o.launch_ts, o.launch_uid), '[]'::jsonb))
    INTO document
    FROM open_loops o;
    RETURN document;
END
$fn$;

REVOKE ALL ON FUNCTION ah.live_loop_detail(text) FROM PUBLIC;

-- The same grantees as ah.live_loop_tasks: consumers that already exist get EXECUTE now; one
-- created later gets it from sql/live_loop_tasks_role.sql or roles.sql.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ah_loop_tasks') THEN
        GRANT EXECUTE ON FUNCTION ah.live_loop_detail(text) TO ah_loop_tasks;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ah_reader') THEN
        GRANT EXECUTE ON FUNCTION ah.live_loop_detail(text) TO ah_reader;
    END IF;
END
$$;

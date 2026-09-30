-- Analytics views and functions over the ah schema. Re-applied on every refresh (idempotent).
-- Owned by the writer role; the reader role gets SELECT/EXECUTE (sql/roles.sql).
-- Structural data only, except ah.search and ah.session_timeline which return bounded excerpts
-- of the message text surface (never tool I/O).
-- Codex cost is NULL until ah.model_pricing is seeded; Claude cost comes from cost-state records.

SET search_path = ah, public, paradedb;

-- Dropped and recreated each apply so a changed column list or RETURNS TABLE never wedges refresh.
DROP FUNCTION IF EXISTS ah.loop_compare(bigint[]), ah.recent_loops(text, integer), ah.loop_lanes(bigint),
    ah.search(text, text[], timestamptz, integer, text, text[]),
    ah.session_timeline(text, text, integer, integer), ah.tool_reliability(timestamptz, text[]),
    ah.who_touched(text, integer), ah.hook_latency(timestamptz, text[]), ah.denials(timestamptz, text[]),
    ah.unused_features(integer), ah.why(text), ah.find_sessions(text, text[], integer),
    ah.correction_digest(timestamptz, text[]), ah.task_effort(text),
    ah.search_summaries(text, text[], timestamptz, integer), ah.week(timestamptz, text[]),
    ah.priced_usd(text, date, numeric, numeric, numeric, numeric, numeric), ah.remote_slug(text),
    ah.project_of(text), ah.excerpt_around(text, text, integer), ah.feature_key(text),
    ah.session_cost(bigint),
    ah.policy_changes(text, timestamptz), ah.rule_effect(text, text, integer, text[]),
    ah.permission_candidates(timestamptz, text[]), ah.routing_report(timestamptz, text[]),
    ah.infra_actions(text, timestamptz, interval), ah.active_sessions(integer),
    ah.open_threads(timestamptz, text[]), ah.lesson_candidates(timestamptz, text[], integer),
    ah.wrapped(timestamptz, timestamptz, text[]),
    ah.wrapped(timestamptz, timestamptz, text[], text)
    CASCADE;
DROP VIEW IF EXISTS ah.v_session_summary, ah.v_session_tree, ah.v_loop_summary, ah.v_tool_reliability,
    ah.v_daily_usage, ah.v_correction_signals, ah.v_cli_model_timeline, ah.v_feature_usage,
    ah.v_git_commits, ah.v_session_cost, ah.v_task_effort, ah.v_agent_commit_quality,
    ah.v_agent_commit_quality_weekly, ah.v_rate_limit_forecast,
    ah.v_spawn_outcome, ah.v_infra_action CASCADE;

-- ---- helpers ------------------------------------------------------------------------------------

-- ParadeDB records every distinct tokenizer typmod in paradedb._typmod_cache the first time it is
-- parsed, which is an INSERT: parsed first by ah_reader (read-only), a new cast fails with "cannot
-- execute INSERT in a read-only transaction". Register the casts the search functions use, as the writer.
SELECT NULL::text::pdb.alias('title_en'), NULL::text::pdb.alias('objective_en'),
       NULL::text::pdb.alias('narrative_en');

-- USD for a token bundle at the model's price on a day (NULL when the model is not priced).
-- Cache writes: 5m at cache_write_per_mtok, 1h at cache_write_1h_per_mtok (falling back to the 5m
-- price). Reasoning tokens are not added: Codex output_tokens already includes them.
CREATE OR REPLACE FUNCTION ah.priced_usd(p_model text, p_day date, inp numeric, cread numeric,
                                         cw5 numeric, cw1 numeric, outp numeric)
RETURNS numeric LANGUAGE sql STABLE AS $$
    SELECT (COALESCE(inp, 0) * p.input_per_mtok
            + COALESCE(cread, 0) * COALESCE(p.cached_input_per_mtok, p.input_per_mtok)
            + COALESCE(cw5, 0) * COALESCE(p.cache_write_per_mtok, p.input_per_mtok)
            + COALESCE(cw1, 0) * COALESCE(p.cache_write_1h_per_mtok, p.cache_write_per_mtok, p.input_per_mtok)
            + COALESCE(outp, 0) * p.output_per_mtok) / 1e6
    FROM ah.model_pricing p
    WHERE p.model = p_model AND p.effective_from <= p_day
    ORDER BY p.effective_from DESC LIMIT 1
$$;

-- host/owner/name from a git remote URL (https, ssh or scp form), the git_commit.repo_slug shape.
CREATE OR REPLACE FUNCTION ah.remote_slug(url text)
RETURNS text LANGUAGE sql IMMUTABLE AS $$
    SELECT NULLIF(lower(regexp_replace(regexp_replace(url, '(\.git)?/*$', ''),
                  '^(?:[a-z+]+://)?(?:[^@/]+@)?([^:/]+)(?::[0-9]+)?[:/]+', '\1/')), '')
$$;

-- Project label from a cwd: the ~/repos/<name> checkout, else the directory name.
CREATE OR REPLACE FUNCTION ah.project_of(cwd text)
RETURNS text LANGUAGE sql IMMUTABLE AS $$
    SELECT COALESCE(substring(cwd FROM '/repos/([^/]+)'), NULLIF(regexp_replace(cwd, '^.*/', ''), ''), '(none)')
$$;

-- Normalised feature name: lower case, leading '/' dropped, every separator folded to '_'
-- ('plugin:cloudflare:x', 'plugin_cloudflare_x' and 'claude.ai Slack' / 'claude_ai_Slack' agree).
CREATE OR REPLACE FUNCTION ah.feature_key(name text)
RETURNS text LANGUAGE sql IMMUTABLE AS $$
    SELECT regexp_replace(lower(ltrim(name, '/')), '[^a-z0-9-]+', '_', 'g')
$$;

-- A bounded excerpt of txt around the first query term found in it (ParadeDB cannot snippet the
-- aliased session_summary fields); falls back to the head of the text.
CREATE OR REPLACE FUNCTION ah.excerpt_around(txt text, q text, width integer DEFAULT 240)
RETURNS text LANGUAGE plpgsql IMMUTABLE AS $$
DECLARE
    term text;
    pos integer;
BEGIN
    IF txt IS NULL THEN RETURN NULL; END IF;
    FOREACH term IN ARRAY regexp_split_to_array(lower(COALESCE(q, '')), '[^[:alnum:]]+') LOOP
        CONTINUE WHEN length(term) < 3;
        pos := strpos(lower(txt), left(term, GREATEST(length(term) - 2, 3)));
        IF pos > 0 THEN
            RETURN CASE WHEN pos > width / 3 THEN '...' ELSE '' END
                   || substr(txt, GREATEST(pos - width / 3, 1), width)
                   || CASE WHEN length(txt) > GREATEST(pos - width / 3, 1) + width THEN '...' ELSE '' END;
        END IF;
    END LOOP;
    RETURN left(txt, width) || CASE WHEN length(txt) > width THEN '...' ELSE '' END;
END $$;

-- Priced cost per session from llm_call x model_pricing (all agents). Filter by session_id: the
-- predicate is pushed into the aggregate, so a lookup reads only that session's calls.
-- snapshot_calls (Claude only, NULL for Codex): calls with no stop_reason carry just the streamed
-- usage snapshot (anthropics/claude-code#84223), so output is a lower bound when it is above zero.
CREATE OR REPLACE VIEW ah.v_session_cost AS
SELECT x.session_id, sum(x.calls) AS llm_calls,
       sum(x.calls) FILTER (WHERE x.cost IS NULL) AS unpriced_calls,
       sum(x.input_uncached) AS input_uncached, sum(x.cache_read) AS cache_read,
       sum(x.cache_write_5m) AS cache_write_5m, sum(x.cache_write_1h) AS cache_write_1h,
       sum(x.output) AS output, sum(x.cost) AS priced_cost_usd,
       CASE WHEN bool_or(x.claude) THEN sum(x.snapshot) END AS snapshot_calls
FROM (SELECT g.*, ah.priced_usd(g.model, g.day, g.input_uncached, g.cache_read, g.cache_write_5m,
                                g.cache_write_1h, g.output) AS cost
      FROM (SELECT c.session_id, c.model, c.ts::date AS day, count(*) AS calls,
                   sum(c.input_uncached) AS input_uncached, sum(c.cache_read) AS cache_read,
                   sum(c.cache_write_5m) AS cache_write_5m, sum(c.cache_write_1h) AS cache_write_1h,
                   sum(c.output) AS output,
                   count(*) FILTER (WHERE c.stop_reason IS NULL AND NOT c.is_api_error) AS snapshot,
                   bool_or(c.agent = 'claude') AS claude
            FROM ah.llm_call c GROUP BY 1, 2, 3) g) x
GROUP BY x.session_id;

-- One session's priced cost via ah.v_session_cost. A per-call lookup (llm_call_session_idx): a
-- LATERAL join on the view directly is pulled up into a hash join that aggregates all of llm_call.
CREATE OR REPLACE FUNCTION ah.session_cost(sid bigint)
RETURNS numeric LANGUAGE sql STABLE AS $$
    SELECT v.priced_cost_usd FROM ah.v_session_cost v WHERE v.session_id = sid
$$;

-- Per-session rollup plus identity, lineage and loop columns.
CREATE OR REPLACE VIEW ah.v_session_summary AS
SELECT s.id AS session_id, s.agent, s.namespace, s.profile, s.machine, s.session_uid, s.agent_id,
       s.is_subagent, s.spawn_kind, s.agent_type, s.root_session_id, s.parent_session_id,
       COALESCE(s.custom_title, s.title) AS title, s.cwd, s.git_branch, s.entrypoint,
       s.cli_version_last, s.first_event_at, s.last_event_at, s.loop_run_id, s.loop_link_method,
       r.turns_human, r.turns_other, r.messages, r.llm_calls, r.input_uncached, r.cache_read,
       r.cache_write, r.output, r.reasoning, r.peak_context_tokens, r.tool_calls, r.tool_errors,
       r.tool_denials, r.tool_interrupts, r.subagents, r.compactions, r.commits, r.pushes,
       r.api_errors, r.interrupts, r.models, r.duration_s, r.claude_cost_usd
FROM ah.session s LEFT JOIN ah.session_rollup r ON r.session_id = s.id
WHERE NOT s.is_stub;

-- A root session with its whole descendant tree summed (subagents, Codex children).
CREATE OR REPLACE VIEW ah.v_session_tree AS
SELECT root.id AS root_session_id, root.agent, root.namespace, root.session_uid,
       COALESCE(root.custom_title, root.title) AS title, root.cwd, root.first_event_at,
       max(s.last_event_at) AS last_event_at, count(*) AS sessions,
       sum(r.turns_human) AS turns_human, sum(r.llm_calls) AS llm_calls,
       sum(r.input_uncached) AS input_uncached, sum(r.cache_read) AS cache_read,
       sum(r.cache_write) AS cache_write, sum(r.output) AS output, sum(r.reasoning) AS reasoning,
       sum(r.tool_calls) AS tool_calls, sum(r.tool_errors) AS tool_errors,
       sum(r.tool_denials) AS tool_denials, sum(r.compactions) AS compactions,
       sum(r.commits) AS commits, sum(r.pushes) AS pushes
FROM ah.session root
JOIN ah.session s ON s.id = root.id OR s.root_session_id = root.id
LEFT JOIN ah.session_rollup r ON r.session_id = s.id
WHERE root.root_session_id = root.id AND NOT root.is_stub
GROUP BY root.id;

-- One row per loop run. Lineage and heuristic sessions count whole (they were tagged by start time
-- inside the loop window); the root session only contributes activity inside [launch_ts, end_ts],
-- because one root session can host several loops.
CREATE OR REPLACE VIEW ah.v_loop_summary AS
SELECT l.id AS loop_run_id, l.status, l.repo_slug, l.campaign_slug, l.loop_number, l.mode,
       l.report_path, l.launch_ts, l.end_ts, l.end_evidence, l.budget_s,
       EXTRACT(EPOCH FROM (l.end_ts - l.launch_ts))::bigint AS wall_s,
       root.agent AS root_agent, root.namespace, root.session_uid AS root_session_uid,
       (SELECT count(*) FROM ah.lane x WHERE x.loop_run_id = l.id) AS lanes,
       1 + COALESCE(m.sessions, 0) AS sessions,
       COALESCE(m.heuristic_sessions, 0) AS heuristic_sessions,
       COALESCE(rw.turns_human, 0) + COALESCE(m.turns_human, 0) AS turns_human,
       COALESCE(rw.llm_calls, 0) + COALESCE(m.llm_calls, 0) AS llm_calls,
       COALESCE(rw.input_uncached, 0) + COALESCE(m.input_uncached, 0) AS input_uncached,
       COALESCE(rw.cache_read, 0) + COALESCE(m.cache_read, 0) AS cache_read,
       COALESCE(rw.cache_write, 0) + COALESCE(m.cache_write, 0) AS cache_write,
       COALESCE(rw.output, 0) + COALESCE(m.output, 0) AS output,
       COALESCE(rw.reasoning, 0) + COALESCE(m.reasoning, 0) AS reasoning,
       COALESCE(rw.output, 0) AS root_output,
       COALESCE(m.claude_output, 0) + CASE WHEN root.agent = 'claude' THEN COALESCE(rw.output, 0) ELSE 0 END AS claude_output,
       COALESCE(m.codex_output, 0) + CASE WHEN root.agent = 'codex' THEN COALESCE(rw.output, 0) ELSE 0 END AS codex_output,
       COALESCE(rw.tool_calls, 0) + COALESCE(m.tool_calls, 0) AS tool_calls,
       COALESCE(rw.tool_errors, 0) + COALESCE(m.tool_errors, 0) AS tool_errors,
       COALESCE(rw.tool_denials, 0) + COALESCE(m.tool_denials, 0) AS tool_denials,
       COALESCE(rw.tool_interrupts, 0) + COALESCE(m.tool_interrupts, 0) AS tool_interrupts,
       COALESCE(rw.subagents, 0) + COALESCE(m.subagents, 0) AS subagents,
       COALESCE(rw.compactions, 0) + COALESCE(m.compactions, 0) AS compactions,
       COALESCE(rw.commits, 0) + COALESCE(m.commits, 0) AS commits,
       COALESCE(rw.pushes, 0) + COALESCE(m.pushes, 0) AS pushes,
       COALESCE(rw.api_errors, 0) + COALESCE(m.api_errors, 0) AS api_errors,
       GREATEST(rw.peak_context_tokens, m.peak_context_tokens) AS peak_context_tokens,
       -- Priced at model_pricing: root activity inside the window plus member sessions whole.
       COALESCE(rw.priced_cost_usd, 0) + COALESCE(m.priced_cost_usd, 0) AS priced_cost_usd
FROM ah.loop_run l
LEFT JOIN ah.session root ON root.id = l.root_session_id
LEFT JOIN LATERAL (
    SELECT count(*) AS sessions, count(*) FILTER (WHERE s.loop_link_method = 'heuristic') AS heuristic_sessions,
           sum(r.turns_human) AS turns_human, sum(r.llm_calls) AS llm_calls,
           sum(r.input_uncached) AS input_uncached, sum(r.cache_read) AS cache_read,
           sum(r.cache_write) AS cache_write, sum(r.output) AS output, sum(r.reasoning) AS reasoning,
           sum(r.output) FILTER (WHERE s.agent = 'claude') AS claude_output,
           sum(r.output) FILTER (WHERE s.agent = 'codex') AS codex_output,
           sum(r.tool_calls) AS tool_calls, sum(r.tool_errors) AS tool_errors,
           sum(r.tool_denials) AS tool_denials, sum(r.tool_interrupts) AS tool_interrupts,
           sum(r.subagents) AS subagents, sum(r.compactions) AS compactions, sum(r.commits) AS commits,
           sum(r.pushes) AS pushes, sum(r.api_errors) AS api_errors, max(r.peak_context_tokens) AS peak_context_tokens,
           sum(sc.priced_cost_usd) AS priced_cost_usd
    FROM ah.session s JOIN ah.session_rollup r ON r.session_id = s.id
    LEFT JOIN LATERAL (SELECT ah.session_cost(s.id) AS priced_cost_usd) sc ON true
    WHERE s.loop_run_id = l.id AND s.id <> l.root_session_id) m ON true
LEFT JOIN LATERAL (
    -- Human prompts, not turn starts: a turn record can start seconds before its prompt is logged.
    SELECT (SELECT count(*) FROM ah.message mm WHERE mm.session_id = l.root_session_id
              AND mm.message_class IN ('human_prompt', 'queued_prompt')
              AND mm.ts >= l.launch_ts AND mm.ts < COALESCE(l.end_ts, 'infinity')) AS turns_human,
           c.llm_calls, c.input_uncached, c.cache_read, c.cache_write, c.output, c.reasoning, c.api_errors,
           c.peak_context_tokens, tc.tool_calls, tc.tool_errors, tc.tool_denials, tc.tool_interrupts,
           (SELECT count(*) FROM ah.subagent_spawn x WHERE x.parent_session_id = l.root_session_id
              AND x.spawned_at >= l.launch_ts AND x.spawned_at < COALESCE(l.end_ts, 'infinity')) AS subagents,
           (SELECT count(*) FROM ah.compaction x WHERE x.session_id = l.root_session_id
              AND x.ts >= l.launch_ts AND x.ts < COALESCE(l.end_ts, 'infinity')) AS compactions,
           (SELECT count(*) FROM ah.git_event g WHERE g.session_id = l.root_session_id AND g.op IN ('commit','cherry_pick')
              AND g.ts >= l.launch_ts AND g.ts < COALESCE(l.end_ts, 'infinity')) AS commits,
           (SELECT count(*) FROM ah.git_event g WHERE g.session_id = l.root_session_id AND g.op = 'push'
              AND g.ts >= l.launch_ts AND g.ts < COALESCE(l.end_ts, 'infinity')) AS pushes,
           (SELECT sum(ah.priced_usd(z.model, z.day, z.i, z.cr, z.w5, z.w1, z.o))
            FROM (SELECT model, ts::date AS day, sum(input_uncached) AS i, sum(cache_read) AS cr,
                         sum(cache_write_5m) AS w5, sum(cache_write_1h) AS w1, sum(output) AS o
                  FROM ah.llm_call WHERE session_id = l.root_session_id
                    AND ts >= l.launch_ts AND ts < COALESCE(l.end_ts, 'infinity') GROUP BY 1, 2) z) AS priced_cost_usd
    FROM (SELECT count(*) AS llm_calls, sum(input_uncached) AS input_uncached, sum(cache_read) AS cache_read,
                 sum(COALESCE(cache_write_5m,0) + COALESCE(cache_write_1h,0)) AS cache_write, sum(output) AS output,
                 sum(reasoning) AS reasoning, count(*) FILTER (WHERE is_api_error) AS api_errors,
                 max(context_tokens) AS peak_context_tokens
          FROM ah.llm_call WHERE session_id = l.root_session_id
            AND ts >= l.launch_ts AND ts < COALESCE(l.end_ts, 'infinity')) c,
         (SELECT count(*) AS tool_calls, count(*) FILTER (WHERE outcome = 'error') AS tool_errors,
                 count(*) FILTER (WHERE outcome = 'denied') AS tool_denials,
                 count(*) FILTER (WHERE outcome = 'interrupted') AS tool_interrupts
          FROM ah.tool_call WHERE session_id = l.root_session_id
            AND started_at >= l.launch_ts AND started_at < COALESCE(l.end_ts, 'infinity')) tc) rw ON true;

CREATE OR REPLACE FUNCTION ah.loop_compare(loop_ids bigint[])
RETURNS SETOF ah.v_loop_summary LANGUAGE sql STABLE AS $$
    SELECT * FROM ah.v_loop_summary WHERE loop_run_id = ANY(loop_ids) ORDER BY launch_ts
$$;

-- Most recent loops, optionally for one repo.
CREATE OR REPLACE FUNCTION ah.recent_loops(repo text DEFAULT NULL, lim integer DEFAULT 20)
RETURNS SETOF ah.v_loop_summary LANGUAGE sql STABLE AS $$
    SELECT * FROM ah.v_loop_summary WHERE repo IS NULL OR repo_slug = repo
    ORDER BY launch_ts DESC LIMIT lim
$$;

CREATE OR REPLACE FUNCTION ah.loop_lanes(loop_id bigint)
RETURNS TABLE (session_id bigint, lane_name text, role text, link_method text, agent text,
               model text, first_event_at timestamptz, last_event_at timestamptz, wall_s bigint,
               llm_calls integer, input_uncached bigint, cache_read bigint, output bigint,
               tool_calls integer, tool_errors integer, tool_denials integer, compactions integer,
               commits integer, return_status text, spawn_completion text, priced_cost_usd numeric)
LANGUAGE sql STABLE AS $$
    SELECT s.id, ln.lane_name, ln.role, ln.link_method, s.agent,
           (r.models)[1], s.first_event_at, s.last_event_at,
           EXTRACT(EPOCH FROM (s.last_event_at - s.first_event_at))::bigint,
           r.llm_calls, r.input_uncached, r.cache_read, r.output, r.tool_calls, r.tool_errors,
           r.tool_denials, r.compactions, r.commits, ln.return_status, sp.completion_status, sc.priced_cost_usd
    FROM ah.lane ln
    JOIN ah.session s ON s.id = ln.session_id
    LEFT JOIN ah.session_rollup r ON r.session_id = s.id
    LEFT JOIN ah.subagent_spawn sp ON sp.child_session_id = s.id
    LEFT JOIN LATERAL (SELECT ah.session_cost(s.id) AS priced_cost_usd) sc ON true
    WHERE ln.loop_run_id = loop_id
    ORDER BY s.first_event_at
$$;

-- The conversational text surface (the pre-v4 classes): the default for interactive search and
-- find_sessions, so harness-injected classes (system prompts, reminders, context) do not drown it.
CREATE OR REPLACE FUNCTION ah.conversation_classes()
RETURNS text[] LANGUAGE sql IMMUTABLE AS $$
    SELECT ARRAY['human_prompt', 'queued_prompt', 'assistant_text', 'subagent_brief', 'subagent_report',
                 'compaction_summary', 'task_notification_summary']
$$;

-- BM25 search over the message text surface with snippets. mode: all | any | phrase.
-- Namespaces narrow search scope; they are not access control. Use separate databases for real
-- separation; reader roles on shared tables do not isolate rows without row-level security.
CREATE OR REPLACE FUNCTION ah.search(q text, namespaces text[] DEFAULT NULL,
                                     since timestamptz DEFAULT NULL, lim integer DEFAULT 20,
                                     mode text DEFAULT 'all', classes text[] DEFAULT NULL)
RETURNS TABLE (message_id bigint, session_id bigint, agent text, namespace text, session_uid text,
               agent_id text, ts timestamptz, role text, message_class text, score real, snippet text,
               cwd text, title text)
LANGUAGE plpgsql STABLE AS $$
DECLARE
    op text := CASE mode WHEN 'any' THEN '|||' WHEN 'phrase' THEN '###' ELSE '&&&' END;
    filters text := '';
BEGIN
    -- The BM25 index is dropped during `rebuild` and recreated after the load: no index, no hits.
    IF to_regclass('ah.message_search_idx') IS NULL THEN RETURN; END IF;
    -- Built per call (EXECUTE) so only the filters actually given appear, as plain predicates that
    -- push down into the ParadeDB scan with the top-K limit, on any connection or plan cache state.
    IF namespaces IS NOT NULL THEN filters := filters || ' AND m.namespace = ANY($2)'; END IF;
    IF classes IS NOT NULL THEN filters := filters || ' AND m.message_class = ANY($3)'; END IF;
    IF since IS NOT NULL THEN filters := filters || ' AND m.ts >= $4'; END IF;
    RETURN QUERY EXECUTE format($q$
        SELECT h.id, h.session_id, h.agent, h.namespace, s.session_uid, s.agent_id, h.ts, h.role,
               h.message_class, h.score, h.snippet, s.cwd, COALESCE(s.custom_title, s.title)
        FROM (SELECT m.id, m.session_id, m.agent, m.namespace, m.ts, m.role, m.message_class,
                     pdb.score(m.id)::real AS score, pdb.snippet(m.text, max_num_chars => 240) AS snippet
              FROM ah.message m
              WHERE m.text %s $1 %s
              ORDER BY pdb.score(m.id) DESC, m.id LIMIT $5) h
        JOIN ah.session s ON s.id = h.session_id
        ORDER BY h.score DESC, h.id $q$, op, filters)
    USING q, namespaces, classes, since, lim;
END $$;

-- Structural timeline of one session (bounded): turns, message excerpts, tool calls, spawns,
-- compactions, git events. Tool input/output is never included.
CREATE OR REPLACE FUNCTION ah.session_timeline(uid text, sub_agent_id text DEFAULT '',
                                               lim integer DEFAULT 400, excerpt integer DEFAULT 200)
RETURNS TABLE (ts timestamptz, kind text, detail text)
LANGUAGE sql STABLE AS $$
    WITH s AS (SELECT id FROM ah.session WHERE session_uid = uid AND agent_id = sub_agent_id LIMIT 1)
    SELECT * FROM (
        SELECT t.started_at, 'turn', concat_ws(' ', t.origin, t.model, t.status, t.duration_ms::text || 'ms')
        FROM ah.turn t, s WHERE t.session_id = s.id
        UNION ALL
        SELECT m.ts, m.message_class, left(m.text, excerpt) FROM ah.message m, s WHERE m.session_id = s.id
        UNION ALL
        SELECT c.started_at, 'tool', concat_ws(' ', c.tool_name, c.mcp_server, c.outcome,
               c.duration_ms::text || 'ms', c.meta->>'cmd_verb')
        FROM ah.tool_call c, s WHERE c.session_id = s.id
        UNION ALL
        SELECT sp.spawned_at, 'spawn', concat_ws(' ', sp.requested_type, sp.name, sp.child_task_name,
               sp.resolved_model, sp.completion_status)
        FROM ah.subagent_spawn sp, s WHERE sp.parent_session_id = s.id
        UNION ALL
        SELECT k.ts, 'compaction', concat_ws(' ', k.trigger, k.pre_tokens::text || '->' || k.post_tokens::text)
        FROM ah.compaction k, s WHERE k.session_id = s.id
        UNION ALL
        SELECT g.ts, 'git', concat_ws(' ', g.op, g.branch, g.sha_short, g.pr_url)
        FROM ah.git_event g, s WHERE g.session_id = s.id
    ) x (ts, kind, detail)
    ORDER BY ts NULLS LAST
    LIMIT lim
$$;

-- Tool reliability since a point in time.
CREATE OR REPLACE FUNCTION ah.tool_reliability(since timestamptz DEFAULT now() - interval '30 days',
                                               namespaces text[] DEFAULT NULL)
RETURNS TABLE (agent text, tool_name text, mcp_server text, calls bigint, error_rate numeric,
               denial_rate numeric, interrupt_rate numeric, p50_ms double precision, p95_ms double precision)
LANGUAGE sql STABLE AS $$
    SELECT c.agent, c.tool_name, c.mcp_server, count(*),
           round(avg((c.outcome = 'error')::int), 4), round(avg((c.outcome = 'denied')::int), 4),
           round(avg((c.outcome = 'interrupted')::int), 4),
           percentile_cont(0.5) WITHIN GROUP (ORDER BY c.duration_ms),
           percentile_cont(0.95) WITHIN GROUP (ORDER BY c.duration_ms)
    FROM ah.tool_call c JOIN ah.session s ON s.id = c.session_id
    WHERE c.started_at >= since AND (namespaces IS NULL OR s.namespace = ANY(namespaces))
    GROUP BY 1, 2, 3 ORDER BY 4 DESC
$$;

CREATE OR REPLACE VIEW ah.v_tool_reliability AS SELECT * FROM ah.tool_reliability();

-- Codex fake-cell waits (error_class 'fake_cell_wait', structure.py ERROR_SQL) per top-level (root)
-- session and clock hour, against that hour's wait_agent calls and root LLM calls. Subagent sessions
-- are excluded. An hour with no fake-cell wait has no row.
CREATE OR REPLACE VIEW ah.v_fake_cell_wait_hourly AS
WITH f AS (
    SELECT c.session_id, date_trunc('hour', c.started_at) AS hour, count(*) AS fake_cell_waits
    FROM ah.tool_call c
    WHERE c.error_class = 'fake_cell_wait'
    GROUP BY 1, 2)
SELECT s.id AS session_id, s.agent, s.session_uid, s.namespace, s.orchestration_kind, s.loop_run_id,
       r.repo_slug, f.hour, f.fake_cell_waits, w.wait_agent_calls, l.root_llm_calls,
       round(1000.0 * f.fake_cell_waits / nullif(l.root_llm_calls, 0), 1) AS fake_per_1k_llm_calls
FROM f
JOIN ah.session s ON s.id = f.session_id AND NOT s.is_subagent
LEFT JOIN ah.session_rollup r ON r.session_id = s.id
CROSS JOIN LATERAL (SELECT count(*) AS wait_agent_calls FROM ah.tool_call w
                    WHERE w.session_id = f.session_id AND w.tool_name = 'wait_agent'
                      AND w.started_at >= f.hour AND w.started_at < f.hour + interval '1 hour') w
CROSS JOIN LATERAL (SELECT count(*) AS root_llm_calls FROM ah.llm_call l
                    WHERE l.session_id = f.session_id
                      AND l.ts >= f.hour AND l.ts < f.hour + interval '1 hour') l;

-- The same per root session over its whole life, with its worst hour.
CREATE OR REPLACE VIEW ah.v_fake_cell_wait_root AS
WITH f AS (
    SELECT session_id, sum(n)::bigint AS fake_cell_waits, max(n) AS peak_hour_fake_cell_waits,
           min(hour) AS first_fake_hour, max(hour) AS last_fake_hour
    FROM (SELECT c.session_id, date_trunc('hour', c.started_at) AS hour, count(*) AS n
          FROM ah.tool_call c WHERE c.error_class = 'fake_cell_wait' GROUP BY 1, 2) h
    GROUP BY 1)
SELECT s.id AS session_id, s.agent, s.session_uid, s.namespace, s.orchestration_kind, s.loop_run_id,
       r.repo_slug, s.first_event_at, s.last_event_at, f.first_fake_hour, f.last_fake_hour,
       f.fake_cell_waits, f.peak_hour_fake_cell_waits, w.wait_agent_calls, l.root_llm_calls,
       round(1000.0 * f.fake_cell_waits / nullif(l.root_llm_calls, 0), 1) AS fake_per_1k_llm_calls
FROM f
JOIN ah.session s ON s.id = f.session_id AND NOT s.is_subagent
LEFT JOIN ah.session_rollup r ON r.session_id = s.id
CROSS JOIN LATERAL (SELECT count(*) AS wait_agent_calls FROM ah.tool_call w
                    WHERE w.session_id = f.session_id AND w.tool_name = 'wait_agent') w
CROSS JOIN LATERAL (SELECT count(*) AS root_llm_calls FROM ah.llm_call l WHERE l.session_id = f.session_id) l;

-- Daily token usage per namespace/agent/model, priced at model_pricing (NULL = model not priced).
-- Tokens are summed per day first and priced once per group (5m and 1h cache writes priced apart).
CREATE OR REPLACE VIEW ah.v_daily_usage AS
SELECT d.day, d.namespace, d.agent, d.model, d.llm_calls, d.input_uncached, d.cache_read,
       COALESCE(d.cache_write_5m, 0) + COALESCE(d.cache_write_1h, 0) AS cache_write,
       d.output, d.reasoning,
       ah.priced_usd(d.model, d.day::date, d.input_uncached, d.cache_read, d.cache_write_5m,
                     d.cache_write_1h, d.output) AS priced_cost_usd,
       d.cache_write_5m, d.cache_write_1h
FROM (SELECT date_trunc('day', c.ts) AS day, s.namespace, c.agent, c.model, count(*) AS llm_calls,
             sum(c.input_uncached) AS input_uncached, sum(c.cache_read) AS cache_read,
             sum(c.cache_write_5m) AS cache_write_5m, sum(c.cache_write_1h) AS cache_write_1h,
             sum(c.output) AS output, sum(c.reasoning) AS reasoning
      FROM ah.llm_call c
      JOIN ah.session s ON s.id = c.session_id
      GROUP BY 1, 2, 3, 4) d;

-- User-correction signals per session: interrupts, rejections, questions, aborted turns, queue edits.
CREATE OR REPLACE VIEW ah.v_correction_signals AS
SELECT s.id AS session_id, s.agent, s.namespace, s.session_uid, s.first_event_at,
       count(*) FILTER (WHERE e.kind = 'interrupt') AS interrupts,
       count(*) FILTER (WHERE e.kind = 'denial' AND e.value = 'user-rejected') AS user_rejections,
       count(*) FILTER (WHERE e.kind = 'user_question') AS user_questions,
       count(*) FILTER (WHERE e.kind = 'queue_op') AS queue_ops,
       (SELECT count(*) FROM ah.turn t WHERE t.session_id = s.id AND t.status = 'aborted') AS aborted_turns
FROM ah.session s LEFT JOIN ah.session_event e ON e.session_id = s.id
WHERE NOT s.is_stub
GROUP BY s.id;

-- CLI version and model adoption over time.
CREATE OR REPLACE VIEW ah.v_cli_model_timeline AS
SELECT date_trunc('day', s.first_event_at) AS day, s.agent, s.cli_version_last AS cli_version,
       m.model, count(DISTINCT s.id) AS sessions
FROM ah.session s
LEFT JOIN LATERAL unnest((SELECT r.models FROM ah.session_rollup r WHERE r.session_id = s.id)) AS m(model) ON true
WHERE NOT s.is_stub
GROUP BY 1, 2, 3, 4;

-- Skill, slash command and MCP server usage.
CREATE OR REPLACE VIEW ah.v_feature_usage AS
SELECT date_trunc('day', e.ts) AS day, s.namespace, e.agent, e.kind, e.value, count(*) AS uses
FROM ah.session_event e JOIN ah.session s ON s.id = e.session_id
WHERE e.kind IN ('skill_invoke', 'slash_command', 'mcp_server_state', 'plan_mode', 'model_switch', 'fallback')
GROUP BY 1, 2, 3, 4, 5
UNION ALL
SELECT date_trunc('day', c.started_at), s.namespace, c.agent, 'mcp_tool', c.mcp_server, count(*)
FROM ah.tool_call c JOIN ah.session s ON s.id = c.session_id
WHERE c.mcp_server IS NOT NULL
GROUP BY 1, 2, 3, 4, 5;

-- Commits made by agents, with the session that made them.
CREATE OR REPLACE VIEW ah.v_git_commits AS
SELECT g.ts, g.sha_short, g.branch, g.op, g.evidence, g.cwd, s.agent, s.namespace, s.session_uid,
       s.agent_id, COALESCE(s.custom_title, s.title) AS title, s.loop_run_id
FROM ah.git_event g JOIN ah.session s ON s.id = g.session_id
WHERE g.op IN ('commit', 'cherry_pick', 'push', 'pr');

-- Which session touched a file path.
CREATE OR REPLACE FUNCTION ah.who_touched(path_like text, lim integer DEFAULT 50)
RETURNS TABLE (ts timestamptz, action text, path text, agent text, namespace text, session_uid text,
               agent_id text, title text)
LANGUAGE sql STABLE AS $$
    SELECT a.ts, a.action, a.path, s.agent, s.namespace, s.session_uid, s.agent_id,
           COALESCE(s.custom_title, s.title)
    FROM ah.artifact a JOIN ah.session s ON s.id = a.session_id
    WHERE a.path LIKE path_like
    ORDER BY a.ts DESC LIMIT lim
$$;

-- =================================================================================================
-- Analytics. Every function is bounded: by a
-- since window through session.last_event_at (session_last_idx) and per-session indexes, by a
-- ParadeDB top-K, or by an explicit LIMIT.
-- =================================================================================================

-- Hook latency per hook event/name. Claude only: Codex transcripts record no hook runs.
-- coverage = share of runs that carry a duration (async and context-only outcomes do not).
CREATE OR REPLACE FUNCTION ah.hook_latency(since timestamptz DEFAULT now() - interval '7 days',
                                           namespaces text[] DEFAULT NULL)
RETURNS TABLE (hook_event text, hook_name text, runs bigint, commands bigint, sessions bigint,
               with_duration bigint, coverage numeric, p50_ms double precision, p95_ms double precision,
               max_ms integer, total_s numeric, error_rate numeric, blocked_rate numeric,
               timeout_rate numeric, data_scope text)
LANGUAGE sql STABLE AS $$
    SELECT h.hook_event, h.hook_name, count(*), count(DISTINCT h.command_sha256), count(DISTINCT h.session_id),
           count(h.duration_ms), round(count(h.duration_ms)::numeric / count(*), 3),
           percentile_cont(0.5) WITHIN GROUP (ORDER BY h.duration_ms),
           percentile_cont(0.95) WITHIN GROUP (ORDER BY h.duration_ms),
           max(h.duration_ms), round(COALESCE(sum(h.duration_ms), 0) / 1000.0, 1),
           round(avg((h.outcome IN ('error', 'non_blocking_error', 'blocking_error'))::int), 4),
           round(avg((h.outcome = 'blocking_error' OR COALESCE(h.prevented_continuation, false))::int), 4),
           round(avg(COALESCE(h.timed_out, false)::int), 4),
           'claude only'
    FROM ah.session s
    JOIN ah.hook_event h ON h.session_id = s.id AND h.ts >= since
    WHERE s.last_event_at >= since AND (namespaces IS NULL OR s.namespace = ANY(namespaces))
    GROUP BY h.hook_event, h.hook_name
    ORDER BY 11 DESC NULLS LAST, 3 DESC
$$;

-- Denials that cost a human action: user rejections and auto-mode blocks from transcripts (tool,
-- command verb), plus the permission-denied log's prompt source; permission-rule denials are
-- reported last as 'permission-rule (rules working)'. Bounded by since and a 300-row cap.
CREATE OR REPLACE FUNCTION ah.denials(since timestamptz DEFAULT now() - interval '7 days',
                                      namespaces text[] DEFAULT NULL)
RETURNS TABLE (source text, category text, namespace text, machine text, tool_name text, cmd_verb text,
               reason text, events bigint, sessions bigint, last_ts timestamptz)
LANGUAGE sql STABLE AS $$
    SELECT * FROM (
        SELECT 'transcript'::text, CASE e.value WHEN 'permission-rule' THEN 'permission-rule (rules working)'
                                           ELSE e.value END,
               s.namespace, s.machine, COALESCE(c.tool_name, e.detail->>'tool'), c.meta->>'cmd_verb',
               NULL::text, count(*), count(DISTINCT e.session_id), max(e.ts)
        FROM ah.session_event e
        JOIN ah.session s ON s.id = e.session_id
        LEFT JOIN ah.tool_call c ON c.agent = e.agent AND e.event_uid LIKE '%:denial'
                                AND c.call_uid = left(e.event_uid, -length(':denial'))
        WHERE e.kind = 'denial' AND e.ts >= since AND (namespaces IS NULL OR s.namespace = ANY(namespaces))
        GROUP BY 2, 3, 4, 5, 6
        UNION ALL
        SELECT 'permission_log', 'prompt-log', p.namespace, p.machine, p.tool_name, p.cmd_verb, p.reason,
               count(*), NULL::bigint, max(p.ts)
        FROM ah.permission_log p
        WHERE p.ts >= since AND (namespaces IS NULL OR p.namespace = ANY(namespaces))
        GROUP BY 3, 4, 5, 6, 7
    ) x (source, category, namespace, machine, tool_name, cmd_verb, reason, events, sessions, last_ts)
    ORDER BY CASE x.category WHEN 'user-rejected' THEN 0 WHEN 'automode-blocked' THEN 1 WHEN 'prompt-log' THEN 2
                             WHEN 'permission-rule (rules working)' THEN 9 ELSE 5 END,
             x.events DESC
    LIMIT 300
$$;

-- Installed skills/plugins/MCP servers and whether each was used in the last `days` days, in the
-- namespace its home writes. Use = skill_invoke/slash_command events, tool_call.attribution_skill /
-- attribution_plugin, and MCP tool calls (server from tool_name and mcp_server). Names compared
-- via ah.feature_key; a qualified skill 'p:s' also matches a bare 's' use and vice versa.
-- status: used | unused | new (first seen within 14 days, no use yet) | unknown (Codex skills and
-- plugins record no use; claude.ai connectors are account-level, so no use is not evidence).
CREATE OR REPLACE FUNCTION ah.unused_features(days integer DEFAULT 30)
RETURNS TABLE (status text, machine text, home text, namespace text, kind text, name text,
               enabled boolean, first_seen_at timestamptz, seen_at timestamptz, uses bigint,
               last_used_at timestamptz)
LANGUAGE sql STABLE AS $$
    WITH win AS (SELECT now() - make_interval(days => days) AS since),
    ss AS (SELECT s.id, s.namespace FROM ah.session s, win WHERE s.last_event_at >= win.since),
    raw AS (
        -- skills and slash commands (the 'invoked_skills' value is a count artefact, not a name)
        SELECT ss.namespace, 'skill'::text AS kind, e.value AS name, e.ts
        FROM ah.session_event e JOIN ss ON ss.id = e.session_id, win
        WHERE e.kind IN ('skill_invoke', 'slash_command') AND e.ts >= win.since
          AND e.value IS NOT NULL AND e.value <> 'invoked_skills'
        UNION ALL
        SELECT t.namespace, v.kind, v.name, t.ts
        FROM (SELECT ss.namespace, c.started_at AS ts, c.attribution_skill, c.attribution_plugin, c.mcp_server,
                     CASE WHEN c.tool_name LIKE 'mcp\_\_%' THEN split_part(substr(c.tool_name, 6), '__', 1) END AS tool_server
              FROM ah.tool_call c JOIN ss ON ss.id = c.session_id, win
              WHERE c.started_at >= win.since
                AND (c.attribution_skill IS NOT NULL OR c.attribution_plugin IS NOT NULL
                     OR c.tool_name LIKE 'mcp\_\_%')) t
        CROSS JOIN LATERAL (VALUES ('skill', t.attribution_skill), ('plugin', t.attribution_plugin),
                                   ('mcp_server', CASE WHEN t.tool_server IS NOT NULL THEN t.mcp_server END),
                                   ('mcp_server', t.tool_server)) v(kind, name)
        WHERE v.name IS NOT NULL
    ),
    used AS MATERIALIZED (SELECT namespace, kind, ah.feature_key(name) AS key, count(*) AS uses, max(ts) AS last_ts
             FROM raw GROUP BY 1, 2, 3),
    matched AS (
        SELECT f.*, m.uses, m.last_ts
        FROM ah.installed_feature f
        LEFT JOIN LATERAL (
            SELECT sum(u.uses) AS uses, max(u.last_ts) AS last_ts
            FROM used u
            WHERE u.namespace = f.namespace AND (
                  -- skills: exact, or the bare skill part of either side
                  (f.kind = 'skill' AND u.kind = 'skill' AND (
                       u.key = ah.feature_key(f.name)
                    OR u.key = ah.feature_key(regexp_replace(f.name, '^.*:', ''))
                    OR (f.name NOT LIKE '%:%' AND u.key LIKE '%\_' || ah.feature_key(f.name))))
               -- plugins: attribution, a '<plugin>:' skill, or a plugin-provided MCP server
               OR (f.kind = 'plugin' AND (
                       (u.kind = 'plugin' AND u.key = ah.feature_key(split_part(f.name, '@', 1)))
                    OR (u.kind = 'skill' AND u.key LIKE ah.feature_key(split_part(f.name, '@', 1)) || '\_%')
                    OR (u.kind = 'mcp_server' AND u.key LIKE 'plugin\_' || ah.feature_key(split_part(f.name, '@', 1)) || '\_%')))
               OR (f.kind = 'mcp_server' AND u.kind = 'mcp_server' AND (
                       u.key = ah.feature_key(f.name)
                    OR u.key LIKE 'plugin\_%\_' || ah.feature_key(regexp_replace(f.name, '^.*:', '')))))) m ON true
    )
    SELECT CASE WHEN COALESCE(m.uses, 0) > 0 THEN 'used'
                WHEN m.namespace LIKE 'codex-%' AND m.kind IN ('skill', 'plugin') THEN 'unknown'
                WHEN m.kind = 'mcp_server' AND ah.feature_key(m.name) LIKE 'claude\_ai\_%' THEN 'unknown'
                WHEN m.first_seen_at >= now() - interval '14 days' THEN 'new'
                ELSE 'unused' END,
           m.machine, m.home, m.namespace, m.kind, m.name, m.enabled, m.first_seen_at, m.seen_at,
           COALESCE(m.uses, 0)::bigint, m.last_ts
    FROM matched m
    ORDER BY 1 DESC, m.namespace, m.kind, m.name
$$;

-- Why was this commit made: agent commit events for a sha prefix (7-40 hex; either side may be the
-- longer), each with its session, the last human prompt before it (in the session, else its root),
-- the sub-agent brief when a sub-agent committed, ground-truth git_commit rows for the prefix
-- (near_event = committed within 15 min of an agent event) and the latest CI run per workflow.
CREATE OR REPLACE FUNCTION ah.why(sha_prefix text)
RETURNS TABLE (kind text, ts timestamptz, agent text, namespace text, session_uid text, agent_id text,
               machine text, cwd text, title text, loop_run_id bigint, sha text, repo_slug text, detail text)
LANGUAGE plpgsql STABLE AS $$
DECLARE
    p text := lower(btrim(sha_prefix));
BEGIN
    IF p !~ '^[0-9a-f]{7,40}$' THEN
        RAISE EXCEPTION 'ah.why: sha prefix must be 7-40 hex characters, got %', sha_prefix;
    END IF;
    RETURN QUERY
    WITH ev AS (
        SELECT g.id, g.ts, g.op, g.branch, g.evidence, lower(g.sha_short) AS sha_short, g.session_id,
               s.agent, s.namespace, s.session_uid, s.agent_id, s.machine, s.cwd,
               COALESCE(s.custom_title, s.title) AS title, s.loop_run_id, s.is_subagent, s.root_session_id,
               ah.remote_slug(s.git_remote_url) AS slug
        FROM ah.git_event g JOIN ah.session s ON s.id = g.session_id
        WHERE g.op IN ('commit', 'cherry_pick') AND length(g.sha_short) >= 7
          AND (lower(g.sha_short) LIKE p || '%' OR p LIKE lower(g.sha_short) || '%')
        ORDER BY g.ts DESC LIMIT 20),
    gc AS (
        SELECT c.*, EXISTS (SELECT 1 FROM ev WHERE abs(EXTRACT(EPOCH FROM c.committed_at - ev.ts)) <= 900) AS near_event
        FROM ah.git_commit c
        WHERE c.sha ~>=~ p AND c.sha ~<~ (p || 'g') AND c.sha LIKE p || '%'
        ORDER BY c.committed_at DESC LIMIT 20)
    SELECT 'agent_commit'::text, ev.ts, ev.agent, ev.namespace, ev.session_uid, ev.agent_id, ev.machine, ev.cwd,
           ev.title, ev.loop_run_id, ev.sha_short, ev.slug,
           concat_ws(' ', ev.op, 'branch=' || ev.branch, 'evidence=' || ev.evidence,
                     CASE WHEN ev.is_subagent THEN 'subagent' END)
    FROM ev
    UNION ALL
    SELECT 'prompt', pr.ts, ev.agent, pr.namespace, pr.session_uid, pr.agent_id, NULL, NULL, NULL,
           ev.loop_run_id, ev.sha_short, ev.slug, left(pr.text, 400)
    FROM ev
    CROSS JOIN LATERAL (
        SELECT * FROM (
            (SELECT m.ts, m.text, m.namespace, 0 AS pri, s2.session_uid, s2.agent_id
             FROM ah.message m JOIN ah.session s2 ON s2.id = m.session_id
             WHERE m.session_id = ev.session_id AND m.message_class IN ('human_prompt', 'queued_prompt')
               AND m.ts <= ev.ts ORDER BY m.ts DESC LIMIT 1)
            UNION ALL
            (SELECT m.ts, m.text, m.namespace, 1, s2.session_uid, s2.agent_id
             FROM ah.message m JOIN ah.session s2 ON s2.id = m.session_id
             WHERE m.session_id = ev.root_session_id AND ev.root_session_id <> ev.session_id
               AND m.message_class IN ('human_prompt', 'queued_prompt') AND m.ts <= ev.ts
             ORDER BY m.ts DESC LIMIT 1)) z
        ORDER BY z.pri LIMIT 1) pr
    UNION ALL
    SELECT 'brief', b.ts, ev.agent, ev.namespace, ev.session_uid, ev.agent_id, NULL, NULL, NULL,
           ev.loop_run_id, ev.sha_short, ev.slug, left(b.text, 400)
    FROM ev
    CROSS JOIN LATERAL (SELECT m.ts, m.text FROM ah.message m
                        WHERE m.session_id = ev.session_id AND m.message_class = 'subagent_brief'
                        ORDER BY m.ts LIMIT 1) b
    WHERE ev.is_subagent
    UNION ALL
    SELECT 'git_commit', gc.committed_at, NULL, NULL, NULL, NULL, NULL, NULL, gc.subject, NULL, gc.sha,
           gc.repo_slug,
           concat_ws(' ', CASE WHEN gc.near_event THEN 'near_event' END,
                     CASE WHEN gc.on_default THEN 'on_default' END,
                     '+' || gc.insertions || ' -' || gc.deletions, gc.files_changed || ' files',
                     CASE WHEN EXISTS (SELECT 1 FROM ah.git_commit r WHERE r.reverts_sha = gc.sha) THEN 'REVERTED' END)
    FROM gc
    UNION ALL
    SELECT 'ci', r.created_at, NULL, NULL, NULL, NULL, NULL, NULL, r.workflow, NULL, r.head_sha, r.repo_slug,
           concat_ws(' ', r.status, r.conclusion, 'event=' || r.event, 'attempt=' || r.attempt, 'run=' || r.run_id)
    FROM (SELECT DISTINCT ON (c.head_sha, c.workflow) c.*
          FROM ah.ci_run c WHERE c.head_sha IN (SELECT gc.sha FROM gc)
          ORDER BY c.head_sha, c.workflow, c.created_at DESC, c.run_id DESC) r;
END $$;

-- Best sessions for a phrase: BM25 over messages (top 300 hits, grouped by session) plus the
-- journal summaries' title/objective (top 100). Returns the hit session itself; is_subagent and
-- root_session_uid let a caller resume the root instead.
CREATE OR REPLACE FUNCTION ah.find_sessions(q text, namespaces text[] DEFAULT NULL, lim integer DEFAULT 10)
RETURNS TABLE (agent text, namespace text, session_uid text, agent_id text, cwd text, title text,
               last_event_at timestamptz, machine text, is_subagent boolean, root_session_uid text,
               score real, message_hits bigint, summary_hit boolean)
LANGUAGE plpgsql STABLE AS $$
DECLARE
    mfilter text := '';
    sfilter text := '';
BEGIN
    IF namespaces IS NOT NULL THEN
        mfilter := ' AND m.namespace = ANY($2)';
        sfilter := ' AND ss.namespace = ANY($2)';
    END IF;
    RETURN QUERY EXECUTE format($q$
        WITH mh AS (SELECT m.session_id, pdb.score(m.id)::real AS score
                    FROM ah.message m WHERE m.text ||| $1 %s
                      AND m.message_class = ANY(ah.conversation_classes())
                    ORDER BY pdb.score(m.id) DESC, m.id LIMIT 300),
        sh AS (SELECT ss.session_id, pdb.score(ss.id)::real AS score
               FROM ah.session_summary ss
               WHERE (ss.title::pdb.alias('title_en') ||| $1 OR ss.objective::pdb.alias('objective_en') ||| $1) %s
               ORDER BY pdb.score(ss.id) DESC, ss.id LIMIT 100),
        agg AS (SELECT x.session_id, max(x.score) FILTER (WHERE x.src = 'm') AS mscore,
                       count(*) FILTER (WHERE x.src = 'm') AS hits,
                       max(x.score) FILTER (WHERE x.src = 's') AS sscore
                FROM (SELECT session_id, score, 'm' AS src FROM mh
                      UNION ALL SELECT session_id, score, 's' FROM sh WHERE session_id IS NOT NULL) x
                GROUP BY x.session_id)
        SELECT s.agent, s.namespace, s.session_uid, s.agent_id, s.cwd,
               COALESCE(sm.title, s.custom_title, s.title), s.last_event_at, s.machine, s.is_subagent,
               root.session_uid,
               (COALESCE(a.mscore, 0) + ln(1 + a.hits) + COALESCE(a.sscore, 0))::real AS score,
               a.hits, a.sscore IS NOT NULL
        FROM agg a
        JOIN ah.session s ON s.id = a.session_id AND NOT s.is_stub
        LEFT JOIN ah.session root ON root.id = s.root_session_id
        LEFT JOIN LATERAL (SELECT x.title FROM ah.session_summary x WHERE x.session_id = s.id
                           ORDER BY x.analysed_at DESC LIMIT 1) sm ON true
        ORDER BY score DESC, s.last_event_at DESC NULLS LAST
        LIMIT $3 $q$, mfilter, sfilter)
    USING q, namespaces, lim;
END $$;

-- Correction digest: row_kind 'total' (one row), 'session' (top 40 sessions by signal count) and
-- 'followup' (up to 150 interrupts / user rejections / tool errors, each with the human prompt that
-- followed it: within 30 min for interrupts and rejections, within 2 min for tool errors; excerpt
-- 200 chars). queue_ops counts every queue operation, as ah.v_correction_signals does.
CREATE OR REPLACE FUNCTION ah.correction_digest(since timestamptz DEFAULT now() - interval '7 days',
                                                namespaces text[] DEFAULT NULL)
RETURNS TABLE (row_kind text, namespace text, agent text, session_uid text, agent_id text, title text,
               cwd text, ts timestamptz, interrupts bigint, user_rejections bigint, user_questions bigint,
               aborted_turns bigint, queue_ops bigint, errors_then_prompt bigint, signal text, excerpt text)
LANGUAGE sql STABLE AS $$
    WITH ss AS (
        SELECT s.id, s.namespace, s.agent, s.session_uid, s.agent_id, COALESCE(s.custom_title, s.title) AS title, s.cwd
        FROM ah.session s
        WHERE s.last_event_at >= since AND NOT s.is_stub AND (namespaces IS NULL OR s.namespace = ANY(namespaces))),
    ev AS (
        SELECT e.session_id, e.ts, e.kind, e.value
        FROM ah.session_event e JOIN ss ON ss.id = e.session_id
        WHERE e.ts >= since AND (e.kind IN ('interrupt', 'user_question', 'queue_op')
                                 OR (e.kind = 'denial' AND e.value = 'user-rejected'))),
    ab AS (
        SELECT t.session_id, count(*) AS n
        FROM ah.turn t JOIN ss ON ss.id = t.session_id
        WHERE t.status = 'aborted' AND t.started_at >= since GROUP BY 1),
    te AS (
        SELECT c.session_id, COALESCE(c.ended_at, c.started_at) AS ts, c.tool_name, nx.ts AS pts, nx.text
        FROM ah.tool_call c JOIN ss ON ss.id = c.session_id
        JOIN LATERAL (SELECT m.ts, m.text FROM ah.message m
                      WHERE m.session_id = c.session_id AND m.message_class IN ('human_prompt', 'queued_prompt')
                        AND m.ts >= COALESCE(c.ended_at, c.started_at)
                        AND m.ts <= COALESCE(c.ended_at, c.started_at) + interval '2 minutes'
                      ORDER BY m.ts LIMIT 1) nx ON true
        WHERE c.outcome = 'error' AND c.started_at >= since),
    per AS (
        SELECT ss.*,
               count(ev.*) FILTER (WHERE ev.kind = 'interrupt') AS interrupts,
               count(ev.*) FILTER (WHERE ev.kind = 'denial') AS rejections,
               count(ev.*) FILTER (WHERE ev.kind = 'user_question') AS questions,
               COALESCE(max(ab.n), 0) AS aborted,
               count(ev.*) FILTER (WHERE ev.kind = 'queue_op') AS queue_ops,
               (SELECT count(*) FROM te WHERE te.session_id = ss.id) AS err_prompt
        FROM ss LEFT JOIN ev ON ev.session_id = ss.id LEFT JOIN ab ON ab.session_id = ss.id
        GROUP BY ss.id, ss.namespace, ss.agent, ss.session_uid, ss.agent_id, ss.title, ss.cwd),
    fu AS (
        SELECT ev.session_id, ev.ts, ev.kind || ':' || COALESCE(ev.value, '') AS signal, nx.text
        FROM ev
        LEFT JOIN LATERAL (SELECT m.text FROM ah.message m
                           WHERE m.session_id = ev.session_id AND m.message_class IN ('human_prompt', 'queued_prompt')
                             AND m.ts >= ev.ts AND m.ts <= ev.ts + interval '30 minutes'
                           ORDER BY m.ts LIMIT 1) nx ON true
        WHERE ev.kind IN ('interrupt', 'denial')
        UNION ALL
        SELECT te.session_id, te.ts, 'tool_error:' || te.tool_name, te.text FROM te)
    SELECT * FROM (
        SELECT 'total'::text, NULL::text, NULL::text, NULL::text, NULL::text,
               count(*) FILTER (WHERE interrupts + rejections + questions + aborted + queue_ops + err_prompt > 0)
                   || ' of ' || count(*) || ' sessions with signals',
               NULL::text, NULL::timestamptz,
               sum(interrupts)::bigint, sum(rejections)::bigint, sum(questions)::bigint, sum(aborted)::bigint,
               sum(queue_ops)::bigint, sum(err_prompt)::bigint, NULL::text, NULL::text
        FROM per) t
    UNION ALL
    SELECT * FROM (
        SELECT 'session', per.namespace, per.agent, per.session_uid, per.agent_id, per.title, per.cwd, NULL::timestamptz,
               per.interrupts, per.rejections, per.questions, per.aborted, per.queue_ops, per.err_prompt,
               NULL::text, NULL::text
        FROM per
        WHERE per.interrupts + per.rejections + per.questions + per.aborted + per.err_prompt > 0
        ORDER BY per.interrupts + per.rejections + per.aborted + per.err_prompt DESC, per.questions DESC
        LIMIT 40) s
    UNION ALL
    SELECT * FROM (
        SELECT 'followup', ss.namespace, ss.agent, ss.session_uid, ss.agent_id, ss.title, ss.cwd, fu.ts,
               NULL::bigint, NULL::bigint, NULL::bigint, NULL::bigint, NULL::bigint, NULL::bigint,
               fu.signal, left(fu.text, 200)
        FROM fu JOIN ss ON ss.id = fu.session_id
        ORDER BY fu.ts DESC
        LIMIT 150) f
$$;

-- Effort on one backlog task: every session that mentions it (task_ref), weighted. A session counts
-- in the totals only when the task appears in a human prompt or a sub-agent brief; sessions that
-- merely mention it are listed with weight 'mentioned' and add no cost. Key normalised to the
-- prefix's zero padding ('abc-19' -> 'ABC-0019').
CREATE OR REPLACE FUNCTION ah.task_effort(task_key text)
RETURNS TABLE (row_kind text, task text, task_title text, task_status text, weight text, agent text,
               namespace text, session_uid text, agent_id text, session_title text, cwd text,
               mentions bigint, in_human boolean, in_brief boolean, first_ts timestamptz, last_ts timestamptz,
               wall_s bigint, tokens bigint, output bigint, priced_cost_usd numeric, claude_cost_usd numeric,
               commits bigint)
LANGUAGE sql STABLE AS $$
    WITH k AS (
        SELECT CASE WHEN upper(btrim(task_key)) ~ '^[A-Z][A-Z0-9]*-[0-9]+$'
                    THEN split_part(upper(btrim(task_key)), '-', 1) || '-' ||
                         CASE WHEN tp.zero_pad IS NULL THEN split_part(btrim(task_key), '-', 2)::bigint::text
                              ELSE lpad(split_part(btrim(task_key), '-', 2)::bigint::text, tp.zero_pad, '0') END
                    ELSE upper(btrim(task_key)) END AS key
        FROM (SELECT 1) one
        LEFT JOIN ah.task_prefix tp ON tp.prefix = split_part(upper(btrim(task_key)), '-', 1)),
    rows AS (
        SELECT CASE WHEN r.in_human OR r.in_brief THEN 'counted' ELSE 'mentioned' END AS weight,
               s.agent, s.namespace, s.session_uid, s.agent_id, COALESCE(s.custom_title, s.title) AS title, s.cwd,
               r.mentions::bigint AS mentions, r.in_human, r.in_brief, r.first_ts, r.last_ts,
               EXTRACT(EPOCH FROM (s.last_event_at - s.first_event_at))::bigint AS wall_s,
               (COALESCE(ro.input_uncached, 0) + COALESCE(ro.cache_read, 0) + COALESCE(ro.cache_write, 0)
                + COALESCE(ro.output, 0))::bigint AS tokens,
               ro.output, sc.priced_cost_usd, ro.claude_cost_usd, ro.commits::bigint AS commits
        FROM k JOIN ah.task_ref r ON r.task_key = k.key
        JOIN ah.session s ON s.id = r.session_id
        LEFT JOIN ah.session_rollup ro ON ro.session_id = s.id
        LEFT JOIN LATERAL (SELECT ah.session_cost(s.id) AS priced_cost_usd) sc ON true)
    SELECT 'total', k.key, b.title, b.status,
           count(*) FILTER (WHERE r.weight = 'counted') || ' counted, '
               || count(*) FILTER (WHERE r.weight = 'mentioned') || ' mentioned',
           NULL, NULL, NULL, NULL, NULL, NULL,
           sum(r.mentions)::bigint, bool_or(r.in_human), bool_or(r.in_brief), min(r.first_ts), max(r.last_ts),
           sum(r.wall_s) FILTER (WHERE r.weight = 'counted')::bigint,
           sum(r.tokens) FILTER (WHERE r.weight = 'counted')::bigint,
           sum(r.output) FILTER (WHERE r.weight = 'counted')::bigint,
           sum(r.priced_cost_usd) FILTER (WHERE r.weight = 'counted'),
           sum(r.claude_cost_usd) FILTER (WHERE r.weight = 'counted'),
           sum(r.commits) FILTER (WHERE r.weight = 'counted')::bigint
    FROM k LEFT JOIN ah.backlog_task b ON b.task_key = k.key LEFT JOIN rows r ON true
    GROUP BY k.key, b.title, b.status
    UNION ALL
    SELECT * FROM (
        SELECT 'session', k.key, NULL::text, NULL::text, r.weight, r.agent, r.namespace, r.session_uid, r.agent_id,
               r.title, r.cwd, r.mentions, r.in_human, r.in_brief, r.first_ts, r.last_ts, r.wall_s, r.tokens,
               r.output, r.priced_cost_usd, r.claude_cost_usd, r.commits
        FROM k, rows r
        ORDER BY r.weight, r.first_ts
        LIMIT 200) x
$$;

-- Per-task effort over every referenced task, with the same weighting as ah.task_effort.
CREATE OR REPLACE VIEW ah.v_task_effort AS
SELECT r.task_key, b.title, b.status, b.repo_slug, b.context,
       count(*) FILTER (WHERE r.in_human OR r.in_brief) AS sessions_counted,
       count(*) FILTER (WHERE NOT (r.in_human OR r.in_brief)) AS sessions_mentioned,
       min(r.first_ts) AS first_ts, max(r.last_ts) AS last_ts,
       sum(EXTRACT(EPOCH FROM (s.last_event_at - s.first_event_at))::bigint)
           FILTER (WHERE r.in_human OR r.in_brief) AS wall_s,
       sum(COALESCE(ro.input_uncached, 0) + COALESCE(ro.cache_read, 0) + COALESCE(ro.cache_write, 0)
           + COALESCE(ro.output, 0)) FILTER (WHERE r.in_human OR r.in_brief) AS tokens,
       sum(sc.priced_cost_usd) FILTER (WHERE r.in_human OR r.in_brief) AS priced_cost_usd,
       sum(ro.claude_cost_usd) FILTER (WHERE r.in_human OR r.in_brief) AS claude_cost_usd,
       sum(ro.commits) FILTER (WHERE r.in_human OR r.in_brief) AS commits
FROM ah.task_ref r
JOIN ah.session s ON s.id = r.session_id
LEFT JOIN ah.backlog_task b ON b.task_key = r.task_key
LEFT JOIN ah.session_rollup ro ON ro.session_id = s.id
LEFT JOIN LATERAL (SELECT CASE WHEN r.in_human OR r.in_brief THEN ah.session_cost(s.id) END AS priced_cost_usd) sc ON true
GROUP BY r.task_key, b.title, b.status, b.repo_slug, b.context;

-- Agent commits resolved to ground truth. A git_event sha_short resolves to the git_commit whose sha
-- starts with it and that is in the session's remote repo, or (no remote) committed within 15 min
-- of the event; the repo match wins, then the closest time. ci_conclusion is over the latest run
-- per workflow: pending | failure | success | mixed | NULL (no runs).
CREATE OR REPLACE VIEW ah.v_agent_commit_quality AS
SELECT g.id AS git_event_id, g.ts, g.op, g.sha_short, g.branch, s.agent, s.namespace, s.session_uid, s.agent_id,
       s.is_subagent, s.loop_run_id,
       COALESCE(gc.repo_slug, ah.remote_slug(s.git_remote_url)) AS repo_slug,
       gc.sha, gc.committed_at, gc.subject, gc.on_default, gc.files_changed,
       COALESCE(gc.insertions, 0) + COALESCE(gc.deletions, 0) AS lines_changed,
       gc.sha IS NOT NULL AS resolved,
       ci.runs AS ci_runs, ci.failed AS ci_failed_workflows, ci.conclusion AS ci_conclusion,
       COALESCE(rv.reverted, false) AS reverted, rv.reverted_by
FROM ah.git_event g
JOIN ah.session s ON s.id = g.session_id
LEFT JOIN LATERAL (
    SELECT c.* FROM ah.git_commit c
    WHERE length(g.sha_short) >= 7
      AND c.sha ~>=~ lower(g.sha_short) AND c.sha ~<~ (lower(g.sha_short) || 'g') AND c.sha LIKE lower(g.sha_short) || '%'
      AND (lower(c.repo_slug) = ah.remote_slug(s.git_remote_url)
           OR abs(EXTRACT(EPOCH FROM (c.committed_at - g.ts))) <= 900)
    ORDER BY (lower(c.repo_slug) = ah.remote_slug(s.git_remote_url)) DESC NULLS LAST,
             abs(EXTRACT(EPOCH FROM (c.committed_at - g.ts)))
    LIMIT 1) gc ON true
LEFT JOIN LATERAL (
    SELECT count(*) AS runs,
           count(*) FILTER (WHERE r.conclusion IN ('failure', 'timed_out', 'startup_failure', 'action_required')) AS failed,
           CASE WHEN count(*) = 0 THEN NULL
                WHEN bool_or(COALESCE(r.status, '') <> 'completed') THEN 'pending'
                WHEN bool_or(r.conclusion IN ('failure', 'timed_out', 'startup_failure', 'action_required')) THEN 'failure'
                WHEN bool_and(r.conclusion IN ('success', 'skipped', 'neutral')) THEN 'success'
                ELSE 'mixed' END AS conclusion
    FROM (SELECT DISTINCT ON (x.workflow) x.* FROM ah.ci_run x
          WHERE x.head_sha = gc.sha AND x.repo_slug = gc.repo_slug
          ORDER BY x.workflow, x.created_at DESC, x.run_id DESC) r) ci ON true
LEFT JOIN LATERAL (
    SELECT true AS reverted, min(r.sha) AS reverted_by
    FROM ah.git_commit r WHERE r.reverts_sha = gc.sha AND r.repo_slug = gc.repo_slug
    HAVING count(*) > 0) rv ON true
WHERE g.op IN ('commit', 'cherry_pick');

CREATE OR REPLACE VIEW ah.v_agent_commit_quality_weekly AS
SELECT date_trunc('week', q.ts) AS week, q.repo_slug, count(*) AS agent_commits,
       count(*) FILTER (WHERE q.resolved) AS resolved,
       count(*) FILTER (WHERE q.ci_conclusion IS NOT NULL) AS with_ci,
       count(*) FILTER (WHERE q.ci_conclusion = 'success') AS ci_success,
       count(*) FILTER (WHERE q.ci_conclusion = 'failure') AS ci_failure,
       count(*) FILTER (WHERE q.reverted) AS reverted,
       sum(q.lines_changed) FILTER (WHERE q.resolved) AS lines_changed,
       round(count(*) FILTER (WHERE q.ci_conclusion = 'failure')::numeric
             / NULLIF(count(*) FILTER (WHERE q.ci_conclusion IS NOT NULL), 0), 3) AS ci_failure_rate,
       round(count(*) FILTER (WHERE q.reverted)::numeric / NULLIF(count(*) FILTER (WHERE q.resolved), 0), 3) AS revert_rate
FROM ah.v_agent_commit_quality q
GROUP BY 1, 2;

-- Rate-limit forecast per (namespace, plan_type, window_minutes, limit_id) over the last 8 days of
-- samples. Burn = used_percent change per hour across the partition's samples in the 6 h before its
-- latest sample that share the latest resets_at (+-5 min); projected_exhaustion_at only when it
-- falls before resets_at. Claude rows carry no used_percent: note 'no data'.
CREATE OR REPLACE VIEW ah.v_rate_limit_forecast AS
WITH r AS (
    SELECT s.namespace, x.agent, x.plan_type, x.window_minutes, x.limit_id, x.window_kind, x.ts,
           x.used_percent, x.resets_at,
           row_number() OVER w AS rn,
           first_value(x.ts) OVER w AS latest_ts,
           first_value(x.resets_at) OVER w AS latest_resets
    FROM ah.rate_limit_sample x JOIN ah.session s ON s.id = x.session_id
    WHERE x.ts >= now() - interval '8 days' AND (x.used_percent IS NOT NULL OR x.agent = 'claude')
    WINDOW w AS (PARTITION BY s.namespace, x.plan_type, x.window_minutes, x.limit_id ORDER BY x.ts DESC)),
b AS (
    SELECT r.namespace, r.plan_type, r.window_minutes, r.limit_id,
           count(*) FILTER (WHERE r.used_percent IS NOT NULL) AS samples_6h,
           (array_agg(r.used_percent ORDER BY r.ts) FILTER (WHERE r.used_percent IS NOT NULL))[1] AS first_used,
           min(r.ts) FILTER (WHERE r.used_percent IS NOT NULL) AS first_ts
    FROM r
    WHERE r.ts >= r.latest_ts - interval '6 hours'
      AND (r.resets_at IS NULL OR r.latest_resets IS NULL
           OR abs(EXTRACT(EPOCH FROM (r.resets_at - r.latest_resets))) <= 300)
    GROUP BY 1, 2, 3, 4),
f AS (
    SELECT l.namespace, l.agent, l.plan_type, l.window_minutes, l.limit_id, l.window_kind, l.ts AS latest_ts,
           l.used_percent, l.resets_at, b.samples_6h,
           CASE WHEN l.used_percent IS NOT NULL AND b.first_ts < l.ts - interval '10 minutes'
                THEN round((l.used_percent - b.first_used)
                           / (EXTRACT(EPOCH FROM (l.ts - b.first_ts)) / 3600.0), 3) END AS burn_pct_per_hour
    FROM r l JOIN b ON b.namespace IS NOT DISTINCT FROM l.namespace AND b.plan_type IS NOT DISTINCT FROM l.plan_type
                   AND b.window_minutes IS NOT DISTINCT FROM l.window_minutes AND b.limit_id IS NOT DISTINCT FROM l.limit_id
    WHERE l.rn = 1)
SELECT f.namespace, f.agent, f.plan_type, f.window_minutes, f.limit_id, f.window_kind, f.latest_ts,
       f.used_percent, f.resets_at, f.samples_6h, f.burn_pct_per_hour,
       CASE WHEN f.burn_pct_per_hour > 0
                 AND f.latest_ts + make_interval(secs => (100 - f.used_percent) / f.burn_pct_per_hour * 3600)
                     < COALESCE(f.resets_at, 'infinity')
            THEN f.latest_ts + make_interval(secs => (100 - f.used_percent) / f.burn_pct_per_hour * 3600) END
           AS projected_exhaustion_at,
       CASE WHEN f.used_percent IS NULL THEN 'no data'
            WHEN f.resets_at IS NOT NULL AND f.resets_at < now() THEN 'window reset since last sample'
            WHEN f.burn_pct_per_hour IS NULL THEN 'too few samples in 6 h'
            WHEN f.burn_pct_per_hour <= 0 THEN 'not burning'
            ELSE NULL END AS note
FROM f;

-- BM25 over journal session summaries (title, objective, narrative; English stemming), with an
-- excerpt around the first matching term. since filters analysed_at (pushed into the index).
-- Only analysed conversations have summaries: coverage is partial (see ah.week).
CREATE OR REPLACE FUNCTION ah.search_summaries(q text, namespaces text[] DEFAULT NULL,
                                               since timestamptz DEFAULT NULL, lim integer DEFAULT 20)
RETURNS TABLE (summary_id bigint, session_id bigint, namespace text, agent text, session_uid text, agent_id text,
               analysed_at timestamptz, classification text, project text, title text, objective text,
               snippet text, score real, cwd text)
LANGUAGE plpgsql STABLE AS $$
DECLARE
    filters text := '';
BEGIN
    IF namespaces IS NOT NULL THEN filters := filters || ' AND ss.namespace = ANY($2)'; END IF;
    IF since IS NOT NULL THEN filters := filters || ' AND ss.analysed_at >= $3'; END IF;
    RETURN QUERY EXECUTE format($q$
        SELECT h.id, h.session_id, h.namespace, s.agent, s.session_uid, s.agent_id, h.analysed_at,
               h.classification, h.project, h.title, left(h.objective, 300),
               ah.excerpt_around(COALESCE(h.narrative, h.objective), $1, 240), h.score, s.cwd
        FROM (SELECT ss.id, ss.session_id, ss.namespace, ss.analysed_at, ss.classification, ss.project, ss.title,
                     ss.objective, ss.narrative, pdb.score(ss.id)::real AS score
              FROM ah.session_summary ss
              WHERE (ss.title::pdb.alias('title_en') ||| $1 OR ss.objective::pdb.alias('objective_en') ||| $1
                     OR ss.narrative::pdb.alias('narrative_en') ||| $1) %s
              ORDER BY pdb.score(ss.id) DESC, ss.id LIMIT $4) h
        LEFT JOIN ah.session s ON s.id = h.session_id
        ORDER BY h.score DESC, h.id $q$, filters)
    USING q, namespaces, since, lim;
END $$;

-- The week at a glance: one row per day (of the root session's first event) and project (from its
-- cwd), plus a final '(all)' row. Sub-agent tokens and cost roll into their root. coverage = share
-- of root sessions with a journal summary (only analysed conversations have one).
CREATE OR REPLACE FUNCTION ah.week(since timestamptz DEFAULT now() - interval '7 days',
                                   namespaces text[] DEFAULT NULL)
RETURNS TABLE (day date, project text, sessions bigint, subagent_sessions bigint, summarised bigint,
               coverage numeric, titles text, objectives text, commits bigint, loops bigint, tokens bigint,
               priced_cost_usd numeric, claude_cost_usd numeric)
LANGUAGE sql STABLE AS $$
    WITH ss AS (
        SELECT s.id, COALESCE(s.root_session_id, s.id) AS root_id, s.is_subagent, s.first_event_at, s.cwd,
               s.loop_run_id
        FROM ah.session s
        WHERE s.last_event_at >= since AND NOT s.is_stub AND (namespaces IS NULL OR s.namespace = ANY(namespaces))),
    per AS (
        SELECT ss.*, (COALESCE(ro.input_uncached, 0) + COALESCE(ro.cache_read, 0) + COALESCE(ro.cache_write, 0)
                      + COALESCE(ro.output, 0))::bigint AS tokens,
               ro.claude_cost_usd, ro.commits, sc.priced_cost_usd
        FROM ss
        LEFT JOIN ah.session_rollup ro ON ro.session_id = ss.id
        LEFT JOIN LATERAL (SELECT ah.session_cost(ss.id) AS priced_cost_usd) sc ON true),
    roots AS (
        SELECT r.id AS root_id, r.first_event_at::date AS day, ah.project_of(r.cwd) AS project
        FROM ah.session r WHERE r.id IN (SELECT DISTINCT root_id FROM ss)),
    tree AS (
        SELECT per.root_id, count(*) FILTER (WHERE per.is_subagent) AS subagents, sum(per.tokens) AS tokens,
               sum(per.priced_cost_usd) AS priced, sum(per.claude_cost_usd) AS claude, sum(per.commits) AS commits,
               array_agg(DISTINCT per.loop_run_id) FILTER (WHERE per.loop_run_id IS NOT NULL) AS loops
        FROM per GROUP BY per.root_id),
    summ AS (
        SELECT DISTINCT ON (x.session_id) x.session_id, x.title, x.objective
        FROM ah.session_summary x WHERE x.session_id IN (SELECT root_id FROM roots)
        ORDER BY x.session_id, x.analysed_at DESC),
    grp AS (
        SELECT ro.day, ro.project, count(*) AS sessions, sum(t.subagents)::bigint AS subagent_sessions,
               count(sm.session_id) AS summarised,
               string_agg(DISTINCT left(sm.title, 80), ' | ') AS titles,
               string_agg(DISTINCT left(sm.objective, 160), ' | ') AS objectives,
               sum(t.commits)::bigint AS commits,
               sum(COALESCE(cardinality(t.loops), 0))::bigint AS loops, sum(t.tokens)::bigint AS tokens,
               sum(t.priced) AS priced, sum(t.claude) AS claude
        FROM roots ro
        JOIN tree t ON t.root_id = ro.root_id
        LEFT JOIN summ sm ON sm.session_id = ro.root_id
        GROUP BY ro.day, ro.project)
    SELECT * FROM (
        SELECT g.day, g.project, g.sessions, g.subagent_sessions, g.summarised,
               round(g.summarised::numeric / NULLIF(g.sessions, 0), 3), left(g.titles, 600), left(g.objectives, 800),
               g.commits, g.loops, g.tokens, round(g.priced, 2), round(g.claude, 2)
        FROM grp g ORDER BY g.day DESC, g.tokens DESC NULLS LAST LIMIT 300) d
    UNION ALL
    SELECT NULL::date, '(all)', sum(g.sessions)::bigint, sum(g.subagent_sessions)::bigint, sum(g.summarised)::bigint,
           round(sum(g.summarised)::numeric / NULLIF(sum(g.sessions), 0), 3), NULL, NULL, sum(g.commits)::bigint,
           (SELECT count(DISTINCT loop_run_id) FROM ss WHERE loop_run_id IS NOT NULL), sum(g.tokens)::bigint,
           round(sum(g.priced), 2), round(sum(g.claude), 2)
    FROM grp g
$$;

-- =================================================================================================
-- Provenance, policy and operational analytics. Queries are bounded by a time window, an indexed
-- lookup, a ParadeDB top-K operation, or an explicit result limit.

-- ah.policy_changes(path_like text DEFAULT NULL, since timestamptz DEFAULT now() - interval '90 days')
-- Output: repo_slug text, sha text, committed_at timestamptz, subject text, author_is_owner boolean,
--         on_default boolean, path text, change char(1), old_path text, insertions integer, deletions integer
-- With no path_like, matches common policy-file patterns; a supplied path_like replaces that set.
-- Results cover indexed git history and are capped at 1000 rows.
CREATE OR REPLACE FUNCTION ah.policy_changes(path_like text DEFAULT NULL,
                                             since timestamptz DEFAULT now() - interval '90 days')
RETURNS TABLE (repo_slug text, sha text, committed_at timestamptz, subject text, author_is_owner boolean,
               on_default boolean, path text, change char(1), old_path text, insertions integer,
               deletions integer)
LANGUAGE sql STABLE AS $$
    SELECT gc.repo_slug, gc.sha, gc.committed_at, gc.subject, gc.author_is_owner, gc.on_default,
           gcf.path, gcf.change, gcf.old_path, gcf.insertions, gcf.deletions
    FROM ah.git_commit_file gcf
    JOIN ah.git_commit gc ON gc.repo_slug = gcf.repo_slug AND gc.sha = gcf.sha
    WHERE gc.committed_at >= since
      AND (CASE WHEN path_like IS NOT NULL THEN gcf.path LIKE path_like
                ELSE gcf.path LIKE '%/rules/%' OR gcf.path LIKE '%/reference/%' OR gcf.path LIKE '%SKILL.md'
                     OR gcf.path LIKE '%AGENTS.md' OR gcf.path LIKE '%CLAUDE.md' END)
    ORDER BY gc.committed_at DESC
    LIMIT 1000
$$;

-- ah.rule_effect(repo_slug text, path text, days integer DEFAULT 14, namespaces text[] DEFAULT NULL)
-- Output: repo_slug text, path text, sha text, committed_at timestamptz, window_days integer,
--   before_human_turns bigint, before_corrections bigint, before_denials bigint, before_tool_errors bigint,
--   before_interrupts bigint, after_human_turns bigint, after_corrections bigint, after_denials bigint,
--   after_tool_errors bigint, after_interrupts bigint, before_corrections_per100 numeric,
--   before_denials_per100 numeric, before_tool_errors_per100 numeric, before_interrupts_per100 numeric,
--   after_corrections_per100 numeric, after_denials_per100 numeric, after_tool_errors_per100 numeric,
--   after_interrupts_per100 numeric
-- One row per historical change of `path` in `repo_slug` (git_commit_file), before/after windows of
-- `days` each side of that commit. `namespaces` defines the before/after cohort (NULL = all), not
-- only sessions touching that repo, because session-to-repository attribution can be incomplete.
-- This measures namespace-wide behaviour around the change. "corrections" = interrupt + user-rejected denial +
-- user_question + aborted turn, the ah.v_correction_signals signal set, recomputed per time window
-- here since that view totals a whole session rather than a slice. "denials" counts ALL
-- session_event denials of any kind, a broader measure than the user-rejected subset in "corrections".
CREATE OR REPLACE FUNCTION ah.rule_effect(p_repo_slug text, p_path text, days integer DEFAULT 14,
                                          namespaces text[] DEFAULT NULL)
RETURNS TABLE (repo_slug text, path text, sha text, committed_at timestamptz, window_days integer,
               before_human_turns bigint, before_corrections bigint, before_denials bigint,
               before_tool_errors bigint, before_interrupts bigint,
               after_human_turns bigint, after_corrections bigint, after_denials bigint,
               after_tool_errors bigint, after_interrupts bigint,
               before_corrections_per100 numeric, before_denials_per100 numeric,
               before_tool_errors_per100 numeric, before_interrupts_per100 numeric,
               after_corrections_per100 numeric, after_denials_per100 numeric,
               after_tool_errors_per100 numeric, after_interrupts_per100 numeric)
LANGUAGE sql STABLE AS $$
    WITH changes AS (
        SELECT gc.sha, gc.committed_at
        FROM ah.git_commit_file gcf JOIN ah.git_commit gc ON gc.repo_slug = gcf.repo_slug AND gc.sha = gcf.sha
        WHERE gcf.repo_slug = p_repo_slug AND gcf.path = p_path
        ORDER BY gc.committed_at),
    win AS (
        SELECT c.sha, c.committed_at, c.committed_at - make_interval(days => days) AS before_start,
               c.committed_at + make_interval(days => days) AS after_end
        FROM changes c),
    turns AS (
        SELECT w.sha,
               count(*) FILTER (WHERE m.ts >= w.before_start AND m.ts < w.committed_at) AS before_turns,
               count(*) FILTER (WHERE m.ts >= w.committed_at AND m.ts < w.after_end) AS after_turns
        FROM win w
        JOIN ah.message m ON m.message_class IN ('human_prompt', 'queued_prompt')
                          AND m.ts >= w.before_start AND m.ts < w.after_end
        JOIN ah.session s ON s.id = m.session_id
        WHERE namespaces IS NULL OR s.namespace = ANY(namespaces)
        GROUP BY w.sha),
    ev AS (
        SELECT w.sha,
               count(*) FILTER (WHERE e.ts >= w.before_start AND e.ts < w.committed_at
                                     AND (e.kind = 'interrupt' OR e.kind = 'user_question'
                                          OR (e.kind = 'denial' AND e.value = 'user-rejected'))) AS before_corr_ev,
               count(*) FILTER (WHERE e.ts >= w.committed_at AND e.ts < w.after_end
                                     AND (e.kind = 'interrupt' OR e.kind = 'user_question'
                                          OR (e.kind = 'denial' AND e.value = 'user-rejected'))) AS after_corr_ev,
               count(*) FILTER (WHERE e.ts >= w.before_start AND e.ts < w.committed_at AND e.kind = 'denial') AS before_denials,
               count(*) FILTER (WHERE e.ts >= w.committed_at AND e.ts < w.after_end AND e.kind = 'denial') AS after_denials,
               count(*) FILTER (WHERE e.ts >= w.before_start AND e.ts < w.committed_at AND e.kind = 'interrupt') AS before_interrupts,
               count(*) FILTER (WHERE e.ts >= w.committed_at AND e.ts < w.after_end AND e.kind = 'interrupt') AS after_interrupts
        FROM win w
        JOIN ah.session_event e ON e.kind IN ('interrupt', 'user_question', 'denial')
                                AND e.ts >= w.before_start AND e.ts < w.after_end
        JOIN ah.session s ON s.id = e.session_id
        WHERE namespaces IS NULL OR s.namespace = ANY(namespaces)
        GROUP BY w.sha),
    ab AS (
        SELECT w.sha,
               count(*) FILTER (WHERE t.completed_at >= w.before_start AND t.completed_at < w.committed_at) AS before_aborted,
               count(*) FILTER (WHERE t.completed_at >= w.committed_at AND t.completed_at < w.after_end) AS after_aborted
        FROM win w
        JOIN ah.turn t ON t.status = 'aborted' AND t.completed_at >= w.before_start AND t.completed_at < w.after_end
        JOIN ah.session s ON s.id = t.session_id
        WHERE namespaces IS NULL OR s.namespace = ANY(namespaces)
        GROUP BY w.sha),
    te AS (
        SELECT w.sha,
               count(*) FILTER (WHERE c.started_at >= w.before_start AND c.started_at < w.committed_at) AS before_errors,
               count(*) FILTER (WHERE c.started_at >= w.committed_at AND c.started_at < w.after_end) AS after_errors
        FROM win w
        JOIN ah.tool_call c ON c.outcome = 'error' AND c.started_at >= w.before_start AND c.started_at < w.after_end
        JOIN ah.session s ON s.id = c.session_id
        WHERE namespaces IS NULL OR s.namespace = ANY(namespaces)
        GROUP BY w.sha)
    SELECT p_repo_slug, p_path, w.sha, w.committed_at, days,
           COALESCE(tu.before_turns, 0), COALESCE(ev.before_corr_ev, 0) + COALESCE(ab.before_aborted, 0),
           COALESCE(ev.before_denials, 0), COALESCE(te.before_errors, 0), COALESCE(ev.before_interrupts, 0),
           COALESCE(tu.after_turns, 0), COALESCE(ev.after_corr_ev, 0) + COALESCE(ab.after_aborted, 0),
           COALESCE(ev.after_denials, 0), COALESCE(te.after_errors, 0), COALESCE(ev.after_interrupts, 0),
           round((COALESCE(ev.before_corr_ev, 0) + COALESCE(ab.before_aborted, 0)) * 100.0 / NULLIF(tu.before_turns, 0), 2),
           round(COALESCE(ev.before_denials, 0) * 100.0 / NULLIF(tu.before_turns, 0), 2),
           round(COALESCE(te.before_errors, 0) * 100.0 / NULLIF(tu.before_turns, 0), 2),
           round(COALESCE(ev.before_interrupts, 0) * 100.0 / NULLIF(tu.before_turns, 0), 2),
           round((COALESCE(ev.after_corr_ev, 0) + COALESCE(ab.after_aborted, 0)) * 100.0 / NULLIF(tu.after_turns, 0), 2),
           round(COALESCE(ev.after_denials, 0) * 100.0 / NULLIF(tu.after_turns, 0), 2),
           round(COALESCE(te.after_errors, 0) * 100.0 / NULLIF(tu.after_turns, 0), 2),
           round(COALESCE(ev.after_interrupts, 0) * 100.0 / NULLIF(tu.after_turns, 0), 2)
    FROM win w
    LEFT JOIN turns tu ON tu.sha = w.sha
    LEFT JOIN ev ON ev.sha = w.sha
    LEFT JOIN ab ON ab.sha = w.sha
    LEFT JOIN te ON te.sha = w.sha
    ORDER BY w.committed_at
$$;

-- ah.permission_candidates(since timestamptz DEFAULT now() - interval '7 days', namespaces text[] DEFAULT NULL)
-- Output: source text, namespace text, tool_name text, mcp_server text, cmd_verb text, denial_kind text,
--         reason text, denials bigint, sessions bigint, retried_ok bigint, retried_ok_rate numeric,
--         last_ts timestamptz
-- source: 'transcript' (session_event denials joined to the denied tool_call, as ah.denials does) or
-- 'permission_log' (the prompt-log structure; no session_id there, so sessions/retried_ok are NULL --
-- manual approvals are never inferred, only what the transcript/log actually show).
-- retried_ok: the SAME session ran the same tool_name + cmd_verb with outcome='ok' within 10 minutes
-- after the denial (a friction signal: worked around, not stopped).
CREATE OR REPLACE FUNCTION ah.permission_candidates(since timestamptz DEFAULT now() - interval '7 days',
                                                    namespaces text[] DEFAULT NULL)
RETURNS TABLE (source text, namespace text, tool_name text, mcp_server text, cmd_verb text,
               denial_kind text, reason text, denials bigint, sessions bigint, retried_ok bigint,
               retried_ok_rate numeric, last_ts timestamptz)
LANGUAGE sql STABLE AS $$
    WITH t AS (
        SELECT e.session_id, s.namespace, c.tool_name, c.mcp_server, c.meta->>'cmd_verb' AS cmd_verb,
               c.denial_kind, e.ts,
               EXISTS (SELECT 1 FROM ah.tool_call c2
                       WHERE c2.session_id = e.session_id AND c2.tool_name = c.tool_name
                         AND c2.outcome = 'ok' AND (c2.meta->>'cmd_verb') IS NOT DISTINCT FROM (c.meta->>'cmd_verb')
                         AND c2.started_at > COALESCE(c.ended_at, c.started_at, e.ts)
                         AND c2.started_at <= COALESCE(c.ended_at, c.started_at, e.ts) + interval '10 minutes') AS retried
        FROM ah.session_event e
        JOIN ah.session s ON s.id = e.session_id
        LEFT JOIN ah.tool_call c ON c.agent = e.agent AND e.event_uid LIKE '%:denial'
                                AND c.call_uid = left(e.event_uid, -length(':denial'))
        WHERE e.kind = 'denial' AND e.ts >= since AND (namespaces IS NULL OR s.namespace = ANY(namespaces)))
    SELECT 'transcript'::text, t.namespace, t.tool_name, t.mcp_server, t.cmd_verb, t.denial_kind, NULL::text,
           count(*), count(DISTINCT t.session_id), count(*) FILTER (WHERE t.retried),
           round(count(*) FILTER (WHERE t.retried)::numeric / count(*), 3), max(t.ts)
    FROM t
    GROUP BY t.namespace, t.tool_name, t.mcp_server, t.cmd_verb, t.denial_kind
    UNION ALL
    SELECT 'permission_log', p.namespace, p.tool_name, NULL, p.cmd_verb, NULL, p.reason,
           count(*), NULL::bigint, NULL::bigint, NULL::numeric, max(p.ts)
    FROM ah.permission_log p
    WHERE p.ts >= since AND (namespaces IS NULL OR p.namespace = ANY(namespaces))
    GROUP BY p.namespace, p.tool_name, p.cmd_verb, p.reason
    ORDER BY 8 DESC
$$;

-- ah.v_spawn_outcome: one row per subagent_spawn.
-- Columns: spawn_id bigint, parent_session_id bigint, parent_namespace text, parent_agent text,
--   child_session_id bigint, child_session_uid text, child_agent_id text, resolved_model text,
--   child_agent_type text, requested_type text, requested_model text, reasoning_effort text,
--   name text, description text, spawned_at timestamptz, completed_at timestamptz, duration_s bigint,
--   completion_status text, lane_return_status text, priced_cost_usd numeric, child_commits bigint,
--   child_ci_conclusion text, redo boolean
-- redo: a LATER spawn from the same parent, same description or name, within 2 hours of this one.
CREATE OR REPLACE VIEW ah.v_spawn_outcome AS
SELECT sp.id AS spawn_id, sp.parent_session_id, ps.namespace AS parent_namespace, ps.agent AS parent_agent,
       sp.child_session_id, cs.session_uid AS child_session_uid, sp.child_agent_id, sp.resolved_model,
       cs.agent_type AS child_agent_type, sp.requested_type, sp.requested_model, sp.reasoning_effort,
       sp.name, sp.description, sp.spawned_at, sp.completed_at,
       COALESCE(EXTRACT(EPOCH FROM (cs.last_event_at - cs.first_event_at))::bigint,
                sp.reported_duration_ms / 1000) AS duration_s,
       sp.completion_status, ln.return_status AS lane_return_status, sc.priced_cost_usd,
       COALESCE(cq.commits, 0) AS child_commits, cq.ci_conclusion AS child_ci_conclusion,
       EXISTS (SELECT 1 FROM ah.subagent_spawn sp2
               WHERE sp2.parent_session_id = sp.parent_session_id AND sp2.id <> sp.id
                 AND sp2.spawned_at > sp.spawned_at AND sp2.spawned_at <= sp.spawned_at + interval '2 hours'
                 AND ((sp2.description IS NOT NULL AND sp2.description = sp.description)
                      OR (sp2.name IS NOT NULL AND sp2.name = sp.name))) AS redo
FROM ah.subagent_spawn sp
JOIN ah.session ps ON ps.id = sp.parent_session_id
LEFT JOIN ah.session cs ON cs.id = sp.child_session_id
LEFT JOIN ah.lane ln ON ln.session_id = sp.child_session_id
LEFT JOIN LATERAL (SELECT ah.session_cost(sp.child_session_id) AS priced_cost_usd) sc ON true
LEFT JOIN LATERAL (
    SELECT count(*) AS commits,
           CASE WHEN bool_or(q.ci_conclusion = 'failure') THEN 'failure'
                WHEN bool_or(q.ci_conclusion = 'pending') THEN 'pending'
                WHEN bool_or(q.ci_conclusion = 'mixed') THEN 'mixed'
                WHEN bool_or(q.ci_conclusion = 'success') THEN 'success' END AS ci_conclusion
    FROM ah.v_agent_commit_quality q
    WHERE cs.id IS NOT NULL AND q.agent = cs.agent AND q.session_uid = cs.session_uid AND q.agent_id = sp.child_agent_id
    ) cq ON true;

-- ah.routing_report(since timestamptz DEFAULT now() - interval '30 days', namespaces text[] DEFAULT NULL)
-- Output: resolved_model text, agent_type text, spawns bigint, completed bigint, redo_count bigint,
--   redo_rate numeric, avg_duration_s numeric, total_priced_cost_usd numeric, commits bigint,
--   ci_success bigint, ci_failure bigint, ci_success_rate numeric
-- Returns structural and aggregate values only; no spawn name, description or prompt text.
CREATE OR REPLACE FUNCTION ah.routing_report(since timestamptz DEFAULT now() - interval '30 days',
                                             namespaces text[] DEFAULT NULL)
RETURNS TABLE (resolved_model text, agent_type text, spawns bigint, completed bigint, redo_count bigint,
               redo_rate numeric, avg_duration_s numeric, total_priced_cost_usd numeric, commits bigint,
               ci_success bigint, ci_failure bigint, ci_success_rate numeric)
LANGUAGE sql STABLE AS $$
    SELECT v.resolved_model, v.child_agent_type, count(*),
           count(*) FILTER (WHERE v.completion_status IS NOT NULL),
           count(*) FILTER (WHERE v.redo), round(count(*) FILTER (WHERE v.redo)::numeric / count(*), 3),
           round(avg(v.duration_s), 1), sum(v.priced_cost_usd), sum(v.child_commits),
           count(*) FILTER (WHERE v.child_ci_conclusion = 'success'),
           count(*) FILTER (WHERE v.child_ci_conclusion = 'failure'),
           round(count(*) FILTER (WHERE v.child_ci_conclusion = 'success')::numeric
                 / NULLIF(count(*) FILTER (WHERE v.child_ci_conclusion IS NOT NULL), 0), 3)
    FROM ah.v_spawn_outcome v
    WHERE v.spawned_at >= since AND (namespaces IS NULL OR v.parent_namespace = ANY(namespaces))
    GROUP BY v.resolved_model, v.child_agent_type
    ORDER BY 3 DESC
$$;

-- ah.v_infra_action exposes tool_call rows with remote-execution target_host metadata.
-- Columns: tool_call_id bigint, ts timestamptz, ended_at timestamptz, namespace text, machine text,
--   agent text, session_uid text, agent_id text, target_host text, host text, remote_verb text,
--   tool_name text, outcome text, duration_ms integer.
-- Rows with an unexpanded shell variable in target_host are dropped. `host` is a normalized
-- join/filter key: lower-case, first domain label ('build01.example.net' -> 'build01'; bare 'host1'
-- unchanged); IP-shaped values are lower-cased without stripping. `target_host` keeps the raw value.
CREATE OR REPLACE VIEW ah.v_infra_action AS
SELECT c.id AS tool_call_id, c.started_at AS ts, c.ended_at, s.namespace, s.machine, s.agent,
       s.session_uid, s.agent_id, c.meta->>'target_host' AS target_host,
       CASE WHEN (c.meta->>'target_host') ~ '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$' OR (c.meta->>'target_host') LIKE '%:%'
            THEN lower(c.meta->>'target_host')
            ELSE lower(split_part(c.meta->>'target_host', '.', 1)) END AS host,
       c.meta->>'remote_verb' AS remote_verb, c.tool_name, c.outcome, c.duration_ms
FROM ah.tool_call c
JOIN ah.session s ON s.id = c.session_id
WHERE c.meta ? 'target_host' AND (c.meta->>'target_host') !~ '[$`]';

-- ah.infra_actions(p_host text, around timestamptz, win interval DEFAULT '2 hours')
-- Output: same columns as ah.v_infra_action, restricted to the normalised `host` within
-- [around-win, around+win]. p_host is matched as given -- lower-case/strip it the same way as the
-- view's `host` column before calling (the CLI/MCP callers do this once, not per row here).
CREATE OR REPLACE FUNCTION ah.infra_actions(p_host text, around timestamptz, win interval DEFAULT '2 hours')
RETURNS SETOF ah.v_infra_action LANGUAGE sql STABLE AS $$
    SELECT * FROM ah.v_infra_action
    WHERE host = p_host AND ts BETWEEN around - win AND around + win
    ORDER BY ts
$$;

-- ah.active_sessions(minutes integer DEFAULT 15)
-- Output: session_id bigint, namespace text, agent text, session_uid text, agent_id text, machine text,
--   cwd text, model text, context_tokens bigint, context_window bigint, priced_cost_usd numeric,
--   running_subagents bigint, last_human_prompt text, last_event_at timestamptz, data_lag_s bigint
-- Root sessions only (root_session_id = id); running_subagents = descendant sessions whose own
-- last_event_at also falls inside the window (still producing events "now"). last_human_prompt
-- carries a bounded excerpt (first 120 chars) for interactive inspection.
CREATE OR REPLACE FUNCTION ah.active_sessions(minutes integer DEFAULT 15)
RETURNS TABLE (session_id bigint, namespace text, agent text, session_uid text, agent_id text,
               machine text, cwd text, model text, context_tokens bigint, context_window bigint,
               priced_cost_usd numeric, running_subagents bigint, last_human_prompt text,
               last_event_at timestamptz, data_lag_s bigint)
LANGUAGE sql STABLE AS $$
    SELECT s.id, s.namespace, s.agent, s.session_uid, s.agent_id, s.machine, s.cwd,
           lc.model, lc.context_tokens, lc.context_window, ah.session_cost(s.id),
           (SELECT count(*) FROM ah.session d
             WHERE d.root_session_id = s.id AND d.id <> s.id
               AND d.last_event_at >= now() - make_interval(mins => minutes)),
           lp.excerpt, s.last_event_at, EXTRACT(EPOCH FROM (now() - s.last_event_at))::bigint
    FROM ah.session s
    LEFT JOIN LATERAL (SELECT c.model, c.context_tokens, c.context_window FROM ah.llm_call c
                       WHERE c.session_id = s.id ORDER BY c.ts DESC LIMIT 1) lc ON true
    LEFT JOIN LATERAL (SELECT left(m.text, 120) AS excerpt FROM ah.message m
                       WHERE m.session_id = s.id AND m.message_class IN ('human_prompt', 'queued_prompt')
                       ORDER BY m.ts DESC LIMIT 1) lp ON true
    WHERE s.root_session_id = s.id AND NOT s.is_stub
      AND s.last_event_at >= now() - make_interval(mins => minutes)
    ORDER BY s.last_event_at DESC
$$;

-- ah.open_threads(since timestamptz, namespaces text[] DEFAULT NULL)
-- Output: kind text ('unfinished_journal' | 'unmerged_edit' | 'stale_task'), namespace text, agent text,
--   session_uid text, agent_id text, title text, cwd text, ts timestamptz, ref text, detail text
-- (a) unfinished_journal: unfinished session-summary items whose mapped session has no later root
--     session in the same project.
-- (b) unmerged_edit: a written/edited/modified artifact with no later commit touching it. If path or
--     repository attribution is uncertain, it remains open rather than being silently closed.
-- (c) stale_task: an In Progress task without an active referencing session in the last 7 days. This
--     window is fixed independently of `since`; namespace filtering maps task context to namespace.
CREATE OR REPLACE FUNCTION ah.open_threads(since timestamptz, namespaces text[] DEFAULT NULL)
RETURNS TABLE (kind text, namespace text, agent text, session_uid text, agent_id text, title text,
               cwd text, ts timestamptz, ref text, detail text)
LANGUAGE sql STABLE AS $$
    SELECT 'unfinished_journal'::text, s.namespace, s.agent, s.session_uid, s.agent_id,
           COALESCE(s.custom_title, s.title), s.cwd, ss.analysed_at, ss.journal_conversation_id,
           (SELECT string_agg(CASE WHEN jsonb_typeof(x.value) = 'string' THEN x.value #>> '{}' ELSE x.value::text END, '; ')
            FROM jsonb_array_elements(ss.unfinished) x)
    FROM ah.session_summary ss
    JOIN ah.session s ON s.id = ss.session_id
    WHERE ss.unfinished IS NOT NULL AND jsonb_typeof(ss.unfinished) = 'array'
      AND jsonb_array_length(ss.unfinished) > 0
      AND ss.analysed_at >= since AND (namespaces IS NULL OR s.namespace = ANY(namespaces))
      AND NOT EXISTS (SELECT 1 FROM ah.session s2
                      WHERE s2.root_session_id = s2.id AND NOT s2.is_stub
                        AND ah.project_of(s2.cwd) = ah.project_of(s.cwd)
                        AND s2.first_event_at > s.last_event_at)
    UNION ALL
    SELECT 'unmerged_edit', s.namespace, s.agent, s.session_uid, s.agent_id,
           COALESCE(s.custom_title, s.title), s.cwd, e.last_ts, e.path, e.action
    FROM (SELECT a.session_id, a.path, max(a.ts) AS last_ts,
                 (array_agg(a.action ORDER BY a.ts DESC))[1] AS action
          FROM ah.artifact a
          WHERE a.action IN ('written', 'edited', 'modified') AND a.ts >= since
          GROUP BY a.session_id, a.path) e
    JOIN ah.session s ON s.id = e.session_id
    WHERE (namespaces IS NULL OR s.namespace = ANY(namespaces))
      AND NOT EXISTS (
          SELECT 1 FROM ah.git_commit_file gcf
          JOIN ah.git_commit gc ON gc.repo_slug = gcf.repo_slug AND gc.sha = gcf.sha
          WHERE gcf.path = CASE WHEN e.path LIKE s.cwd || '/%' THEN substring(e.path FROM length(s.cwd) + 2) END
            -- No remote on the session (all Claude sessions): the commit's repo name must equal the
            -- checkout directory name; a mismatch keeps the thread open, never silently closes it.
            AND CASE WHEN s.git_remote_url IS NULL THEN split_part(gcf.repo_slug, '/', 3) = ah.project_of(s.cwd)
                     ELSE ah.remote_slug(s.git_remote_url) = gcf.repo_slug END
            AND gc.committed_at > e.last_ts)
    UNION ALL
    SELECT 'stale_task', b.context, NULL, NULL, NULL, b.title, NULL, b.updated_at, b.task_key,
           concat_ws(' ', b.status, 'repo=' || b.repo_slug)
    FROM ah.backlog_task b
    WHERE b.status = 'In Progress'
      AND (namespaces IS NULL OR EXISTS (SELECT 1 FROM unnest(namespaces) ns WHERE ns LIKE '%-' || b.context))
      AND NOT EXISTS (SELECT 1 FROM ah.task_ref r JOIN ah.session s3 ON s3.id = r.session_id
                      WHERE r.task_key = b.task_key AND s3.last_event_at >= now() - interval '7 days')
    ORDER BY 8 DESC NULLS LAST
    LIMIT 500
$$;

-- ah.lesson_candidates(since timestamptz, namespaces text[] DEFAULT NULL, lim integer DEFAULT 50)
-- Output: message_id bigint, session_id bigint, namespace text, agent text, session_uid text,
--   agent_id text, ts timestamptz, message_class text, score real, snippet text, cwd text, title text,
--   session_corrections integer
-- `since` is required. A fixed, non-exhaustive set of correction phrases ("turns out", "root cause",
-- "was wrong", "actually", "the real", "instead of") is matched with the ParadeDB ### (phrase) /
-- ||| (any-term) operators against the message_search_idx
-- BM25 index, same construction style as ah.search/ah.find_sessions. Restricted to
-- human_prompt/queued_prompt/assistant_text (the first-person, belief-bearing classes).
-- session_corrections is a same-session ranking BOOST, not a filter: a count of that session's
-- ah.v_correction_signals-shaped events (interrupt/user-rejected denial/user_question).
CREATE OR REPLACE FUNCTION ah.lesson_candidates(since timestamptz, namespaces text[] DEFAULT NULL,
                                                lim integer DEFAULT 50)
RETURNS TABLE (message_id bigint, session_id bigint, namespace text, agent text, session_uid text,
               agent_id text, ts timestamptz, message_class text, score real, snippet text,
               cwd text, title text, session_corrections integer)
LANGUAGE plpgsql STABLE AS $$
DECLARE
    filters text := '';
BEGIN
    IF since IS NULL THEN
        RAISE EXCEPTION 'ah.lesson_candidates: since is required';
    END IF;
    IF namespaces IS NOT NULL THEN filters := filters || ' AND m.namespace = ANY($2)'; END IF;
    RETURN QUERY EXECUTE format($q$
        SELECT h.id, h.session_id, h.namespace, h.agent, s.session_uid, s.agent_id, h.ts,
               h.message_class, (h.score + ln(1 + COALESCE(cs.n, 0)))::real, h.snippet, s.cwd,
               COALESCE(s.custom_title, s.title), COALESCE(cs.n, 0)::integer
        FROM (SELECT m.id, m.session_id, m.namespace, m.agent, m.ts, m.message_class,
                     pdb.score(m.id)::real AS score, pdb.snippet(m.text, max_num_chars => 240) AS snippet
              FROM ah.message m
              WHERE (m.text ### 'turns out' OR m.text ### 'root cause' OR m.text ### 'was wrong'
                     OR m.text ||| 'actually' OR m.text ### 'the real' OR m.text ### 'instead of')
                AND m.message_class IN ('human_prompt', 'queued_prompt', 'assistant_text')
                AND m.ts >= $1 %s
              ORDER BY pdb.score(m.id) DESC, m.id LIMIT $3 * 3) h
        JOIN ah.session s ON s.id = h.session_id
        LEFT JOIN LATERAL (
            SELECT count(*) AS n FROM ah.session_event ev
            WHERE ev.session_id = h.session_id
              AND (ev.kind IN ('interrupt', 'user_question')
                   OR (ev.kind = 'denial' AND ev.value = 'user-rejected'))) cs ON true
        ORDER BY h.score + ln(1 + COALESCE(cs.n, 0)) DESC, h.id
        LIMIT $3 $q$, filters)
    USING since, namespaces, lim;
END $$;

-- ah.wrapped(since timestamptz, until timestamptz, namespaces text[] DEFAULT NULL, tz text DEFAULT 'UTC') RETURNS jsonb
-- Keys: since, until, sessions (bigint), human_prompts (bigint), busiest_hour_of_day (0-23),
--   busiest_weekday (day name), latest_night_session {session_uid, agent_id, namespace, title, ts,
--   local_time}, tokens_and_cost_by_model [{model, input_uncached, cache_read, cache_write, output,
--   priced_cost_usd}], most_expensive_session {session_uid, agent_id, namespace, title,
--   priced_cost_usd}, top_skills [{name, uses}] (top 10), top_tools [{tool_name, calls}] (top 10),
--   top_mcp_servers [{mcp_server, calls}] (top 10), most_denied_command_verb {cmd_verb, denials},
--   longest_loop {loop_run_id, repo_slug, wall_s}, longest_running_session {session_uid, agent_id,
--   namespace, title, wall_s}.
-- The supplied timezone controls wall-clock hour/weekday buckets and the latest-night label; `tz`
-- defaults to UTC.
CREATE OR REPLACE FUNCTION ah.wrapped(since timestamptz, until timestamptz, namespaces text[] DEFAULT NULL,
                                      tz text DEFAULT 'UTC')
RETURNS jsonb LANGUAGE sql STABLE AS $$
    WITH ss AS (
        SELECT s.* FROM ah.session s
        WHERE s.last_event_at >= since AND s.last_event_at < until AND NOT s.is_stub
          AND (namespaces IS NULL OR s.namespace = ANY(namespaces))),
    hp AS (
        SELECT m.id, m.session_id, m.ts, (m.ts AT TIME ZONE tz) AS local_ts
        FROM ah.message m JOIN ss ON ss.id = m.session_id
        WHERE m.message_class IN ('human_prompt', 'queued_prompt') AND m.ts >= since AND m.ts < until),
    hour_counts AS (
        SELECT extract(hour FROM local_ts)::int AS hour, count(*) AS n FROM hp GROUP BY 1 ORDER BY 2 DESC, 1 LIMIT 1),
    weekday_counts AS (
        SELECT btrim(to_char(local_ts, 'Day')) AS wd, count(*) AS n FROM hp GROUP BY 1 ORDER BY 2 DESC, 1 LIMIT 1),
    night AS (
        SELECT s.session_uid, s.agent_id, s.namespace, COALESCE(s.custom_title, s.title) AS title, h.ts,
               to_char(h.local_ts, 'HH24:MI') AS local_time
        FROM hp h JOIN ah.session s ON s.id = h.session_id
        ORDER BY (CASE WHEN extract(hour FROM h.local_ts) < 12 THEN extract(hour FROM h.local_ts) + 24
                       ELSE extract(hour FROM h.local_ts) END) DESC,
                 extract(minute FROM h.local_ts) DESC
        LIMIT 1),
    tm AS (
        SELECT c.model,
               sum(c.input_uncached) AS input_uncached, sum(c.cache_read) AS cache_read,
               sum(COALESCE(c.cache_write_5m, 0) + COALESCE(c.cache_write_1h, 0)) AS cache_write,
               sum(c.output) AS output,
               sum(ah.priced_usd(c.model, c.ts::date, c.input_uncached, c.cache_read, c.cache_write_5m,
                                 c.cache_write_1h, c.output)) AS priced_cost_usd
        FROM ah.llm_call c JOIN ss ON ss.id = c.session_id
        WHERE c.ts >= since AND c.ts < until
        GROUP BY c.model),
    mx AS (
        SELECT ss.session_uid, ss.agent_id, ss.namespace, COALESCE(ss.custom_title, ss.title) AS title,
               ah.session_cost(ss.id) AS priced_cost_usd
        FROM ss ORDER BY ah.session_cost(ss.id) DESC NULLS LAST LIMIT 1),
    tskills AS (
        SELECT e.value AS name, count(*) AS uses
        FROM ah.session_event e JOIN ss ON ss.id = e.session_id
        WHERE e.kind IN ('skill_invoke', 'slash_command') AND e.ts >= since AND e.ts < until
          AND e.value IS NOT NULL AND e.value <> 'invoked_skills'
        GROUP BY e.value ORDER BY 2 DESC LIMIT 10),
    ttools AS (
        SELECT c.tool_name, count(*) AS calls
        FROM ah.tool_call c JOIN ss ON ss.id = c.session_id
        WHERE c.started_at >= since AND c.started_at < until
        GROUP BY c.tool_name ORDER BY 2 DESC LIMIT 10),
    tmcp AS (
        SELECT c.mcp_server, count(*) AS calls
        FROM ah.tool_call c JOIN ss ON ss.id = c.session_id
        WHERE c.mcp_server IS NOT NULL AND c.started_at >= since AND c.started_at < until
        GROUP BY c.mcp_server ORDER BY 2 DESC LIMIT 10),
    md AS (
        SELECT c.meta->>'cmd_verb' AS cmd_verb, count(*) AS denials
        FROM ah.tool_call c JOIN ss ON ss.id = c.session_id
        WHERE c.outcome = 'denied' AND c.meta->>'cmd_verb' IS NOT NULL
          AND c.started_at >= since AND c.started_at < until
        GROUP BY c.meta->>'cmd_verb' ORDER BY 2 DESC LIMIT 1),
    ll AS (
        SELECT l.loop_run_id, l.repo_slug, l.wall_s
        FROM ah.v_loop_summary l
        WHERE l.launch_ts >= since AND l.launch_ts < until
          AND (namespaces IS NULL OR l.namespace = ANY(namespaces))
        ORDER BY l.wall_s DESC NULLS LAST LIMIT 1),
    lr AS (
        SELECT ss.session_uid, ss.agent_id, ss.namespace, COALESCE(ss.custom_title, ss.title) AS title,
               EXTRACT(EPOCH FROM (ss.last_event_at - ss.first_event_at))::bigint AS wall_s
        FROM ss ORDER BY (ss.last_event_at - ss.first_event_at) DESC NULLS LAST LIMIT 1)
    SELECT jsonb_build_object(
        'since', since, 'until', until,
        'sessions', (SELECT count(*) FROM ss),
        'human_prompts', (SELECT count(*) FROM hp),
        'busiest_hour_of_day', (SELECT hour FROM hour_counts),
        'busiest_weekday', (SELECT wd FROM weekday_counts),
        'latest_night_session', (SELECT to_jsonb(night) FROM night),
        'tokens_and_cost_by_model', COALESCE((SELECT jsonb_agg(tm) FROM tm), '[]'::jsonb),
        'most_expensive_session', (SELECT to_jsonb(mx) FROM mx),
        'top_skills', COALESCE((SELECT jsonb_agg(tskills) FROM tskills), '[]'::jsonb),
        'top_tools', COALESCE((SELECT jsonb_agg(ttools) FROM ttools), '[]'::jsonb),
        'top_mcp_servers', COALESCE((SELECT jsonb_agg(tmcp) FROM tmcp), '[]'::jsonb),
        'most_denied_command_verb', (SELECT to_jsonb(md) FROM md),
        'longest_loop', (SELECT to_jsonb(ll) FROM ll),
        'longest_running_session', (SELECT to_jsonb(lr) FROM lr)
    )
$$;

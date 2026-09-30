-- Efficiency classifier: what triggered each LLM call. Applied on every schema change (idempotent).
--
-- Every model call is attributed to the event that last changed the agent's state before it:
--   user         a human prompt, a queued prompt, or (in a sub-agent) a message from its parent
--   model        the previous model call, with no tool result or event in between
--   work         the result of an ordinary tool call
--   status       the result of a status look: a list/status call, or a shell command that only
--                reads state (git status/log/fetch, gh run view, backlog task list, ps, ...)
--   wait         the result of a wait that timed out or only slept (a poll)
--   event        a wait that delivered something, or an event the harness injected (pi loop-watch,
--                loop-wake; a Claude <task-notification>)
--   noop         the result of an empty or trivial command
--   orchestrate  the result of spawning or messaging an agent, or of arming a wake
--   agent_msg    a sub-agent completion notice
-- A root that polls shows up as a high share of `wait` and `status` calls.
--
-- Ordering: calls, prompts, injected events and tool results are ordered by their physical
-- record offset within the source. Recorded timestamps can move backwards (Claude notification)
-- or tie at millisecond resolution (Codex/pi result). Old tool rows without a result offset fall
-- back to their call offset; rebuild those catalogues before relying on exact per-call parity.
--
-- Claude, Codex and pi all produce classified model calls.

SET search_path = ah, public;

DROP FUNCTION IF EXISTS ah.efficiency_calls(text[], text, text), ah.efficiency(text[], text, text);

-- Python-style truthiness of a JSON value (absent and null are false).
CREATE OR REPLACE FUNCTION ah.eff_truthy(v jsonb)
RETURNS boolean LANGUAGE sql IMMUTABLE AS $$
    SELECT CASE jsonb_typeof(v)
        WHEN 'boolean' THEN v::text = 'true'
        WHEN 'number' THEN v::text::numeric <> 0
        WHEN 'string' THEN (v #>> '{}') <> ''
        WHEN 'array' THEN jsonb_array_length(v) > 0
        WHEN 'object' THEN v <> '{}'::jsonb
        ELSE false END
$$;

-- A JSON object from text, or '{}' when the text is not a JSON object.
CREATE OR REPLACE FUNCTION ah.eff_object(t text)
RETURNS jsonb LANGUAGE plpgsql IMMUTABLE AS $$
DECLARE parsed jsonb;
BEGIN
    IF t IS NULL OR NOT pg_input_is_valid(t, 'jsonb') THEN RETURN '{}'::jsonb; END IF;
    parsed := t::jsonb;
    IF jsonb_typeof(parsed) = 'object' THEN RETURN parsed; END IF;
    RETURN '{}'::jsonb;
END
$$;

CREATE OR REPLACE FUNCTION ah.eff_status_re()
RETURNS text LANGUAGE sql IMMUTABLE AS $$
    SELECT 'gh (run|pr) (view|list|checks|status)|gh api [^\n]*(actions/runs|check-runs|pulls)'
           '|git (fetch|log|status|rev-parse|ls-remote|worktree list)|backlog task (list|view)|list_agents'
           '|\y(cat|tail|head|sed -n|wc)\y[^\n]{0,160}(state-|report-|outcomes-|\.notified|\.claim|lane|loop\d|wave\d)'
           '|\yps\y|pgrep|curr_time'
$$;

-- Classify a shell command or code-mode cell: noop, status, wait or work.
CREATE OR REPLACE FUNCTION ah.eff_cls_exec(t text)
RETURNS text LANGUAGE sql IMMUTABLE AS $$
    SELECT CASE
        WHEN t IS NULL OR t = '' THEN 'noop'
        WHEN strpos(t, 'tools.') = 0 AND strpos(t, '$') = 0 AND length(t) < 80 THEN 'noop'
        WHEN t ~ '^\s*const r\s*=\s*await tools\.clock__curr_time\(\{\}\);\s*text\(r\)\s*$' THEN 'status'
        WHEN t ~ ('write_stdin\(\{[^}]*chars:\s*""|"chars":\s*""|tools\.wait\(|\ysleep\s+\d|tools\.sleep'
                  '|setTimeout|wait_agent|gh run watch|--watch\y|\yuntil\y[^\n]{0,200}\ydo\y'
                  '|while [^\n]{0,200}sleep|timeout \d+ .*(tail -f|wait)') THEN 'wait'
        WHEN (SELECT bool_and(c ~ ah.eff_status_re())
              FROM (SELECT m[1] AS c FROM regexp_matches(t, 'cmd:\s*"((?:[^"\\]|\\[^\n])*)"', 'g') AS m
                    UNION ALL
                    SELECT t WHERE t !~ 'cmd:\s*"(?:[^"\\]|\\[^\n])*"') x) THEN 'status'
        ELSE 'work' END
$$;

-- The trigger a pi tool call's result sets: its class, with a subagent wait resolved by its result.
CREATE OR REPLACE FUNCTION ah.eff_pi_result(tool_name text, input_text text, result_json text)
RETURNS text LANGUAGE sql IMMUTABLE AS $$
    WITH a AS (SELECT ah.eff_object(input_text) AS args)
    SELECT CASE
        WHEN tool_name = 'bash' THEN
            ah.eff_cls_exec('tools.' || CASE WHEN jsonb_typeof(args -> 'command') = 'string'
                                             THEN args ->> 'command' ELSE '' END)
        WHEN tool_name = 'subagent' THEN CASE
            WHEN jsonb_typeof(args -> 'action') = 'string' AND args ->> 'action' IN ('list', 'status') THEN 'status'
            WHEN jsonb_typeof(args -> 'action') = 'string' AND args ->> 'action' = 'wait' THEN
                CASE WHEN ah.eff_truthy(ah.eff_object(result_json) -> 'results') THEN 'event' ELSE 'wait' END
            WHEN (args ? 'workflowScript' AND jsonb_typeof(args -> 'workflowScript') <> 'null')
                 OR (jsonb_typeof(args -> 'action') = 'string' AND args ->> 'action' IN ('run', 'start', 'spawn'))
                 OR ah.eff_truthy(args -> 'tasks') THEN 'orchestrate'
            ELSE 'work' END
        WHEN tool_name IN ('watch_start', 'wake_at') THEN 'orchestrate'
        ELSE 'work' END
    FROM a
$$;

-- Shared result classification: SQL and the Python classifier are checked on the same rules.
CREATE OR REPLACE FUNCTION ah.eff_result(agent text, tool_name text, input_text text, result_json text)
RETURNS text LANGUAGE sql IMMUTABLE AS $$
    WITH a AS (SELECT ah.eff_object(input_text) args, ah.eff_object(result_json) result),
    n AS (SELECT CASE WHEN agent = 'codex' THEN regexp_replace(tool_name, '^.*[.]', '') ELSE tool_name END tool,
                 args, result FROM a),
    c AS (SELECT n.*, CASE WHEN pg_input_is_valid(input_text, 'jsonb')
                           THEN COALESCE(args ->> 'cmd', args ->> 'command', args ->> 'script', '')
                           ELSE input_text END AS command FROM n),
    e AS (SELECT c.*, ah.eff_cls_exec(command) AS exec_class FROM c)
    SELECT CASE
        WHEN agent = 'pi' THEN ah.eff_pi_result(tool_name, input_text, result_json)
        WHEN agent = 'codex' THEN CASE
            WHEN tool IN ('wait', 'sleep', 'wait_agent') THEN
                CASE WHEN (tool = 'wait_agent' AND (result ->> 'timed_out') = 'false')
                           OR (tool <> 'wait_agent' AND result_json ~ 'Process exited with code -?\d+|"?exit_code"?\s*:\s*-?\d+')
                     THEN 'event' ELSE 'wait' END
            WHEN tool IN ('list_agents', 'get_goal') THEN 'status'
            WHEN tool IN ('spawn_agent', 'send_message', 'followup_task', 'interrupt_agent') THEN 'orchestrate'
            WHEN tool IN ('request_user_input', 'request_user_input_async') THEN 'human'
            WHEN tool IN ('exec', 'exec_command', 'js', 'run', 'shell', 'write_stdin') THEN
                CASE WHEN exec_class <> 'wait' THEN exec_class
                WHEN command ~ 'tools[.]sleep|setTimeout' THEN 'wait'
                WHEN result_json ~ 'Process exited with code -?[0-9]+|"?exit_code"?[[:space:]]*:[[:space:]]*-?[0-9]+'
                     THEN 'event' ELSE 'wait' END
            ELSE 'work' END
        WHEN agent = 'claude' THEN CASE
            WHEN tool IN ('TaskOutput', 'BashOutput', 'Monitor', 'ScheduleWakeup', 'Sleep') THEN
                CASE WHEN tool = 'TaskOutput' THEN
                    CASE WHEN COALESCE(result -> 'task' ->> 'status', result ->> 'status') IN ('running', 'pending')
                         THEN 'wait' ELSE 'event' END
                WHEN ah.eff_truthy(result -> 'interrupted') OR ah.eff_truthy(result -> 'timedOutAfterMs')
                     OR ah.eff_truthy(result -> 'backgroundTaskId') THEN 'wait'
                ELSE 'event' END
            WHEN tool IN ('TaskList', 'TaskGet', 'ListAgents') THEN 'status'
            WHEN tool IN ('Agent', 'Task', 'SendMessage', 'TaskStop', 'KillShell', 'Workflow', 'TeamCreate') THEN 'orchestrate'
            WHEN tool = 'AskUserQuestion' THEN 'human'
            WHEN tool = 'Bash' THEN CASE
                WHEN ah.eff_truthy(args -> 'run_in_background') THEN 'work'
                WHEN ah.eff_cls_exec('tools.' || COALESCE(args ->> 'command', '')) = 'wait' THEN
                    CASE WHEN lower(COALESCE(args ->> 'command', '')) ~ '^[[:space:]]*sleep|;[[:space:]]*sleep|until |while '
                         THEN 'wait'
                         WHEN ah.eff_truthy(result -> 'interrupted') OR ah.eff_truthy(result -> 'timedOutAfterMs')
                                   OR ah.eff_truthy(result -> 'backgroundTaskId') THEN 'wait' ELSE 'event' END
                ELSE ah.eff_cls_exec('tools.' || COALESCE(args ->> 'command', '')) END
            ELSE 'work' END
        ELSE NULL END
    FROM e
$$;

-- One row per model call. namespaces NULL = every namespace; session_uid/agent_id narrow to one
-- session (agent_id '' = the main thread, NULL = every thread of that session_uid).
CREATE OR REPLACE FUNCTION ah.efficiency_calls(namespaces text[] DEFAULT NULL, p_session_uid text DEFAULT NULL,
                                               p_agent_id text DEFAULT NULL)
RETURNS TABLE (llm_call_id bigint, session_id bigint, agent text, namespace text, session_uid text,
               agent_id text, role text, source_path text, byte_offset bigint, ts timestamptz,
               response_id text, model text, trigger text, input_tokens bigint, cache_read bigint,
               output bigint)
LANGUAGE sql STABLE AS $$
    WITH sess AS (
        SELECT s.* FROM ah.session s
        WHERE (namespaces IS NULL OR s.namespace = ANY (namespaces))
          AND (p_session_uid IS NULL OR s.session_uid = p_session_uid)
          AND (p_agent_id IS NULL OR s.agent_id = p_agent_id)
          AND NOT s.is_stub
    ),
    calls AS (
        SELECT l.id, l.session_id, l.ts, l.byte_offset, l.source_id, l.response_id, l.model,
               COALESCE(l.input_uncached, 0) + COALESCE(l.cache_read, 0) + COALESCE(l.cache_write_5m, 0)
                   + COALESCE(l.cache_write_1h, 0) AS input_tokens,
               l.cache_read, l.output, s.agent
        FROM ah.llm_call l JOIN sess s ON s.id = l.session_id
        WHERE (l.stop_reason IS NULL OR (l.stop_reason NOT LIKE 'usage:%' AND l.stop_reason NOT IN ('compaction', 'branch_summary')))
          AND num_nonnulls(l.input_uncached, l.cache_read, l.cache_write_5m, l.cache_write_1h, l.output, l.reasoning) > 0
    ),
    -- Per-source physical order, not the timestamp supplied by a record. Each session normally
    -- has one source; source_id provides a stable boundary when a session spans source files.
    pi_events AS (
        SELECT c.session_id, c.source_id, c.byte_offset AS pos, 0 AS rnk, 0::bigint AS tie,
               'model'::text AS trig, c.id AS call_id
        FROM calls c
        UNION ALL
        SELECT m.session_id, m.source_id, m.byte_offset, 0, 0, 'user', NULL
        FROM ah.message m JOIN sess s ON s.id = m.session_id
        WHERE (s.agent = 'pi' AND m.message_class IN ('human_prompt', 'queued_prompt', 'agent_message'))
           OR (s.agent = 'codex' AND m.message_class IN ('human_prompt', 'queued_prompt'))
           OR (s.agent = 'claude' AND m.raw_record_origin = 'user'
               AND (m.message_class IN ('human_prompt', 'queued_prompt')
                    OR (m.message_class = 'agent_message' AND m.detail ->> 'source' = 'peer')))
        UNION ALL
        SELECT m.session_id, m.source_id, m.byte_offset, 0, 0, 'event', NULL
        FROM ah.message m JOIN sess s ON s.id = m.session_id
        WHERE s.agent = 'claude' AND m.message_class = 'agent_message'
          AND m.detail ->> 'source' = 'task-notification' AND m.raw_record_origin = 'user'
        UNION ALL
        SELECT m.session_id, m.source_id, m.byte_offset, 0, 0, 'agent_msg', NULL
        FROM ah.message m JOIN sess s ON s.id = m.session_id
        WHERE s.agent = 'codex' AND m.message_class = 'agent_message'
          AND m.detail ->> 'source' = 'agent_message'
        UNION ALL
        SELECT m.session_id, m.source_id, m.byte_offset, 0, 0,
               CASE m.detail ->> 'source' WHEN 'loop-continuation' THEN 'orchestrate'
                                          WHEN 'subagent-notify' THEN 'agent_msg'
                                          WHEN 'subagent-incremental-child-notify' THEN 'agent_msg'
                                          ELSE 'event' END, NULL
        FROM ah.message m JOIN sess s ON s.id = m.session_id
        WHERE s.agent = 'pi' AND m.detail ->> 'source' IN ('loop-watch', 'loop-wake', 'loop-continuation',
                                                           'subagent-notify', 'subagent-incremental-child-notify')
        UNION ALL
        SELECT i.session_id, i.source_id, COALESCE(i.output_byte_offset, c.byte_offset), 1,
               COALESCE(c.seq, 0),
               ah.eff_result(s.agent, c.tool_name, i.input_text,
                             CASE WHEN s.agent = 'codex' THEN i.output_text ELSE i.result_json END), NULL
        FROM ah.tool_io i JOIN sess s ON s.id = i.session_id
        JOIN ah.tool_call c ON c.agent = i.agent AND c.call_uid = i.call_uid
        WHERE i.kind = 'call' AND i.output_at IS NOT NULL
    ),
    pi_ordered AS (
        SELECT e.call_id,
               COALESCE(lag(e.trig) OVER (PARTITION BY e.session_id ORDER BY e.source_id, e.pos, e.rnk, e.tie), 'user') AS trigger
        FROM pi_events e
    ),
    triggers AS (
        SELECT call_id, trigger FROM pi_ordered WHERE call_id IS NOT NULL
    )
    SELECT c.id, c.session_id, s.agent, s.namespace, s.session_uid, s.agent_id,
           CASE WHEN s.is_subagent THEN 'worker'
                WHEN EXISTS (SELECT 1 FROM ah.session k WHERE k.parent_session_id = s.id AND k.id <> s.id)
                     OR EXISTS (SELECT 1 FROM ah.subagent_spawn p WHERE p.parent_session_id = s.id)
                     OR EXISTS (SELECT 1 FROM ah.loop_run r WHERE r.root_session_id = s.id) THEN 'root'
                ELSE 'solo' END,
           f.rel_path, c.byte_offset, c.ts, c.response_id, c.model, t.trigger, c.input_tokens, c.cache_read, c.output
    FROM calls c
    JOIN sess s ON s.id = c.session_id
    LEFT JOIN ah.source_file f ON f.id = c.source_id
    LEFT JOIN triggers t ON t.call_id = c.id
$$;

-- Per-session totals of ah.efficiency_calls.
CREATE OR REPLACE FUNCTION ah.efficiency(namespaces text[] DEFAULT NULL, p_session_uid text DEFAULT NULL,
                                         p_agent_id text DEFAULT NULL)
RETURNS TABLE (session_id bigint, agent text, namespace text, session_uid text, agent_id text, role text,
               calls bigint, user_calls bigint, model_calls bigint, work_calls bigint, status_calls bigint,
               wait_calls bigint, event_calls bigint, noop_calls bigint, orchestrate_calls bigint,
               agent_msg_calls bigint, unclassified_calls bigint, poll_share numeric, input_tokens bigint,
               cache_read bigint, output bigint)
LANGUAGE sql STABLE AS $$
    SELECT e.session_id, e.agent, e.namespace, e.session_uid, e.agent_id, min(e.role),
           count(*),
           count(*) FILTER (WHERE e.trigger = 'user'),
           count(*) FILTER (WHERE e.trigger = 'model'),
           count(*) FILTER (WHERE e.trigger = 'work'),
           count(*) FILTER (WHERE e.trigger = 'status'),
           count(*) FILTER (WHERE e.trigger = 'wait'),
           count(*) FILTER (WHERE e.trigger = 'event'),
           count(*) FILTER (WHERE e.trigger = 'noop'),
           count(*) FILTER (WHERE e.trigger = 'orchestrate'),
           count(*) FILTER (WHERE e.trigger = 'agent_msg'),
           count(*) FILTER (WHERE e.trigger IS NULL),
           round((count(*) FILTER (WHERE e.trigger IN ('wait', 'status')))::numeric
                 / NULLIF(count(*) FILTER (WHERE e.trigger IS NOT NULL), 0), 4),
           sum(e.input_tokens)::bigint, sum(e.cache_read)::bigint, sum(e.output)::bigint
    FROM ah.efficiency_calls(namespaces, p_session_uid, p_agent_id) e
    GROUP BY e.session_id, e.agent, e.namespace, e.session_uid, e.agent_id
$$;

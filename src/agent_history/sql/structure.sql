-- Structure helpers and views for parser v4 (orchestration, ordering, continuation, change feed,
-- failure detail). Applied after search.sql on every schema change (idempotent). Post-pass SQL that
-- writes these columns lives in structure.py. Contract: CONTRACT.md.

SET search_path = ah, public;

-- Local checkout root of a path or cwd: a Codex/Claude worktree, a ~/repos/<name> checkout, or an
-- agent home under the user's home directory (macOS /Users or Linux /home). NULL when none applies.
CREATE OR REPLACE FUNCTION ah.repo_root(p text)
RETURNS text LANGUAGE sql IMMUTABLE AS $$
    SELECT COALESCE(
        substring(p FROM '^(/(?:Users|home)/[^/]+/\.codex[^/]*/worktrees/[^/]+/[^/]+)'),
        substring(p FROM '^(/(?:Users|home)/[^/]+/repos/[^/]+/\.claude/worktrees/[^/]+)'),
        substring(p FROM '^(/(?:Users|home)/[^/]+/repos/[^/]+)'),
        substring(p FROM '^(/(?:Users|home)/[^/]+/\.(?:claude|codex)[^/]*)(?:/|$)'))
$$;

-- Checkout name used to find the repo slug: the worktree's base repo for Claude worktrees.
CREATE OR REPLACE FUNCTION ah.repo_name(root text)
RETURNS text LANGUAGE sql IMMUTABLE AS $$
    SELECT regexp_replace(regexp_replace(root, '/\.claude/worktrees/[^/]+$', ''), '^.*/', '')
$$;

-- The most informative ~500 characters of a failed tool's output: from just before the first
-- error-looking line, else the stderr tail, else the head of the output.
CREATE OR REPLACE FUNCTION ah.error_excerpt(stderr text, output text, width integer DEFAULT 500)
RETURNS text LANGUAGE plpgsql IMMUTABLE AS $$
DECLARE
    err text := NULLIF(btrim(COALESCE(stderr, '')), '');
    t text := COALESCE(err, NULLIF(btrim(COALESCE(output, '')), ''));
    p integer;
    ls integer;
    seg text;
    nl integer;
BEGIN
    IF t IS NULL THEN RETURN NULL; END IF;
    p := regexp_instr(left(t, 200000),
        '(error|exception|fatal|traceback|panic|failed|failure|denied|not found|no such|refused|invalid|'
        'cannot|could not|unable to|timed out|timeout|forbidden|unauthori[sz]ed|conflict|rejected)', 1, 1, 0, 'i');
    IF p = 0 THEN
        RETURN CASE WHEN err IS NOT NULL THEN right(t, width) ELSE left(t, width) END;
    END IF;
    -- start at the beginning of the matching line (at most 160 characters back)
    ls := GREATEST(p - 160, 1);
    seg := substr(t, ls, p - ls);
    nl := strpos(reverse(seg), E'\n');
    IF nl > 0 THEN ls := ls + length(seg) - nl + 1; END IF;
    RETURN substr(t, ls, width);
END $$;

-- Normalised failure class for a tool call/op row (NULL when the row is not a failure).
CREATE OR REPLACE FUNCTION ah.error_class_of(denial_kind text, timed_out boolean, interrupted boolean,
                                             exit_code integer, is_error boolean, outcome text, excerpt text)
RETURNS text LANGUAGE sql IMMUTABLE AS $$
    SELECT CASE
        WHEN NOT (COALESCE(is_error, false) OR COALESCE(exit_code, 0) <> 0 OR COALESCE(timed_out, false)
                  OR COALESCE(interrupted, false) OR denial_kind IS NOT NULL
                  OR COALESCE(outcome IN ('error', 'denied', 'interrupted', 'timeout'), false)) THEN NULL
        WHEN denial_kind IS NOT NULL OR outcome = 'denied' THEN 'denied'
        WHEN COALESCE(timed_out, false) OR outcome = 'timeout' THEN 'timeout'
        WHEN COALESCE(interrupted, false) OR outcome = 'interrupted' OR exit_code = 130 THEN 'interrupted'
        WHEN excerpt ~* '(InputValidationError|invalid (input|argument|param|value|json)|failed to parse|validation (error|failed)|is required|unexpected (argument|keyword|token)|usage:|unknown (option|flag|argument))' THEN 'validation_error'
        WHEN excerpt ~* '(no such file|not found|does not exist|ENOENT|404|unknown (command|revision|model)|could not find|cannot find)' THEN 'not_found'
        WHEN excerpt ~* '(permission denied|EACCES|EPERM|operation not permitted|\m401\M|\m403\M|unauthori[sz]ed|forbidden|access denied|authentication (failed|required))' THEN 'permission'
        WHEN excerpt ~* '(connection (refused|reset|closed|timed out)|could not resolve|name or service not known|network (is )?unreachable|ECONN|ETIMEDOUT|EHOSTUNREACH|TLS|SSL|\m50[234]\M|temporary failure in name resolution|timed out)' THEN 'network'
        WHEN COALESCE(exit_code, 0) <> 0 THEN 'nonzero_exit'
        WHEN COALESCE(is_error, false) OR outcome = 'error' THEN 'tool_error'
        ELSE 'other' END
$$;

-- Genuine owner text: the prompt_origin values that are the owner's own words.
-- Built-in harness control commands (/model, /clear, ...) are typed but are not a genuine prompt.
DROP FUNCTION IF EXISTS ah.is_genuine_prompt(text, text);
CREATE OR REPLACE FUNCTION ah.is_genuine_prompt(message_class text, prompt_origin text, txt text DEFAULT NULL)
RETURNS boolean LANGUAGE sql IMMUTABLE AS $$
    SELECT message_class IN ('human_prompt', 'queued_prompt')
       AND COALESCE(prompt_origin, 'typed') IN ('typed', 'pasted', 'launch_message', 'slash_command', 'skill')
       AND NOT (prompt_origin = 'slash_command' AND COALESCE(txt, '') ~* ('<command-name>\s*/?(model|effort|clear|'
           'compact|plugins?|config|login|logout|status|cost|context|resume|exit|quit|help|mcp|permissions|hooks|'
           'agents|memory|init|doctor|fast|vim|theme|output-style|add-dir|export|rewind|usage|statusline|'
           'terminal-setup|ide|upgrade|bashes|todos|release-notes|privacy-settings|feedback|bug|remote-control|'
           'rename|copy|keybindings|sandbox|tasks|skills|stickers|migrate-installer|install-github-app|'
           'pr-comments|security-review|artifacts|extra-usage|passes|mobile|chrome|advisor)\s*</command-name>'))
$$;

-- One row per session: orchestration classification with natural keys (the journal's skip list).
CREATE OR REPLACE VIEW ah.v_session_orchestration AS
SELECT s.id AS session_id, s.agent, s.session_uid, s.agent_id, s.namespace, s.is_subagent,
       s.orchestration_kind, s.is_orchestration_descendant, s.orchestration_evidence,
       r.agent AS root_agent, r.session_uid AS root_session_uid, r.agent_id AS root_agent_id,
       s.orchestration_root_session_id, s.loop_run_id, l.launch_uid AS loop_launch_uid, l.repo_slug AS loop_repo_slug,
       l.naming AS loop_naming, l.loop_number, s.first_event_at, s.last_event_at
FROM ah.session s
LEFT JOIN ah.session r ON r.id = s.orchestration_root_session_id
LEFT JOIN ah.loop_run l ON l.id = s.loop_run_id
WHERE NOT s.is_stub;

-- Per-session change/settle state for incremental consumers.
-- is_open: activity in the last 30 minutes, or the last turn is still open and active in the last 6 hours.
-- settled_at: when the session counts as settled (30 minutes after its last event), NULL while open.
CREATE OR REPLACE VIEW ah.v_session_state AS
SELECT s.id AS session_id, s.agent, s.session_uid, s.agent_id, s.namespace, s.first_event_at, s.last_event_at,
       s.content_changed_at, r.content_fingerprint, r.final_turn_status,
       (s.last_event_at > now() - interval '30 minutes'
        OR (r.final_turn_status = 'open' AND s.last_event_at > now() - interval '6 hours')) AS is_open,
       CASE WHEN s.last_event_at > now() - interval '30 minutes'
                 OR (r.final_turn_status = 'open' AND s.last_event_at > now() - interval '6 hours') THEN NULL
            ELSE s.last_event_at + interval '30 minutes' END AS settled_at,
       s.first_prompt_event_uid, s.first_prompt_at, COALESCE(s.custom_title, s.title) AS display_title,
       c.agent AS continued_from_agent, c.session_uid AS continued_from_session_uid,
       c.agent_id AS continued_from_agent_id, s.continuation_kind
FROM ah.session s
LEFT JOIN ah.session_rollup r ON r.session_id = s.id
LEFT JOIN ah.session c ON c.id = s.continued_from_session_id
WHERE NOT s.is_stub;

-- A session's full ordered timeline with content (messages, tool calls with their I/O, ops).
CREATE OR REPLACE FUNCTION ah.session_events(p_agent text, p_session_uid text, p_agent_id text DEFAULT '',
                                             after_seq bigint DEFAULT 0, lim integer DEFAULT 1000)
RETURNS TABLE (seq bigint, kind text, uid text, ts timestamptz, message_class text, role text,
               prompt_origin text, tool_name text, text text, input_text text, output_text text,
               stdout_text text, stderr_text text, error_class text, error_excerpt text)
LANGUAGE sql STABLE AS $$
    WITH s AS (SELECT id FROM ah.session WHERE agent = p_agent AND session_uid = p_session_uid
               AND agent_id = COALESCE(p_agent_id, ''))
    SELECT * FROM (
        SELECT m.seq, 'message', m.event_uid, m.ts, m.message_class, m.role, m.prompt_origin, NULL::text,
               m.text, NULL::text, NULL::text, NULL::text, NULL::text, NULL::text, NULL::text
        FROM ah.message m, s WHERE m.session_id = s.id AND m.seq > after_seq
        UNION ALL
        SELECT c.seq, 'tool_call', c.call_uid, c.started_at, NULL, NULL, NULL, c.tool_name, NULL,
               i.input_text, i.output_text, i.stdout_text, i.stderr_text, c.error_class, c.error_excerpt
        FROM ah.tool_call c CROSS JOIN s
        LEFT JOIN ah.tool_io i ON i.agent = c.agent AND i.io_uid = c.call_uid
        WHERE c.session_id = s.id AND c.seq > after_seq
        UNION ALL
        SELECT o.seq, 'tool_op', o.item_uid, COALESCE(o.started_at, o.completed_at), NULL, NULL, NULL, o.item_type,
               NULL, i.input_text, i.output_text, i.stdout_text, i.stderr_text, o.error_class, o.error_excerpt
        FROM ah.tool_op o CROSS JOIN s
        LEFT JOIN ah.tool_io i ON i.agent = o.agent AND i.io_uid = 'item:' || o.item_uid
        WHERE o.session_id = s.id AND o.seq > after_seq
    ) x (seq, kind, uid, ts, message_class, role, prompt_origin, tool_name, text, input_text, output_text,
         stdout_text, stderr_text, error_class, error_excerpt)
    ORDER BY seq
    LIMIT lim
$$;

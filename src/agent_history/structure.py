"""Parser-v4 structure post-passes: repos, continuations, ordering, genuine first prompt, failure detail,
rollup extension, orchestration classification, change feed and session embeddings.

Runs inside load.post_passes' transaction over `dirty_now` (the sessions touched since the last pass;
every session after a rebuild, which is the backfill). Every statement is set-based and idempotent:
it recomputes and only writes rows whose value changed, so a rebuild reproduces the same result.
Helper functions and views are in structure.sql.
"""

from __future__ import annotations

import psycopg

# --- repos -----------------------------------------------------------------------------------

REPO_SQL = [
    # Checkout roots seen in dirty sessions' cwd and touched paths (relative paths resolve against cwd).
    """
    WITH roots AS (
        SELECT DISTINCT ah.repo_root(s.cwd) AS root FROM ah.session s JOIN dirty_now d ON d.session_id = s.id
        UNION
        SELECT DISTINCT ah.repo_root(CASE WHEN f.path LIKE '/%%' THEN f.path ELSE s.cwd || '/' || f.path END)
        FROM ah.file_touch f JOIN dirty_now d ON d.session_id = f.session_id JOIN ah.session s ON s.id = f.session_id
    ),
    slugs AS (SELECT DISTINCT repo_slug FROM ah.git_commit),
    remotes AS (
        SELECT ah.repo_root(cwd) AS root, min(ah.remote_slug(git_remote_url)) AS slug
        FROM ah.session WHERE git_remote_url IS NOT NULL GROUP BY 1)
    INSERT INTO ah.repo (local_root, slug, remote_url)
    SELECT r.root,
           COALESCE((SELECT CASE WHEN count(*) = 1 THEN min(g.repo_slug) END FROM slugs g
                     WHERE g.repo_slug LIKE '%%/' || ah.repo_name(r.root)),
                    (SELECT x.slug FROM remotes x WHERE x.root = r.root),
                    'local/' || ah.repo_name(r.root)),
           NULL
    FROM roots r WHERE r.root IS NOT NULL
    ON CONFLICT (local_root) DO NOTHING
    """,
    # Late slug resolution for roots first seen before their commits were collected.
    """
    UPDATE ah.repo r SET slug = x.slug
    FROM (SELECT r2.id, min(g.repo_slug) AS slug FROM ah.repo r2
          JOIN (SELECT DISTINCT repo_slug FROM ah.git_commit) g ON g.repo_slug LIKE '%%/' || ah.repo_name(r2.local_root)
          WHERE r2.slug LIKE 'local/%%' GROUP BY r2.id HAVING count(*) = 1) x
    WHERE r.id = x.id
    """,
    """
    UPDATE ah.session s SET repo_id = r.id
    FROM dirty_now d, ah.repo r
    WHERE s.id = d.session_id AND r.local_root = ah.repo_root(s.cwd) AND s.repo_id IS DISTINCT FROM r.id
    """,
    """
    UPDATE ah.file_touch f SET repo_id = r.id,
           repo_path = substr(x.abs, length(r.local_root) + 2)
    FROM (SELECT f2.id, CASE WHEN f2.path LIKE '/%%' THEN f2.path ELSE s.cwd || '/' || f2.path END AS abs
          FROM ah.file_touch f2 JOIN dirty_now d ON d.session_id = f2.session_id
          JOIN ah.session s ON s.id = f2.session_id) x, ah.repo r
    WHERE f.id = x.id AND r.local_root = ah.repo_root(x.abs) AND f.repo_id IS DISTINCT FROM r.id
    """,
]

# --- continuations ---------------------------------------------------------------------------

CONTINUATION_SQL = [
    """
    UPDATE ah.session_continuation c SET child_session_id = s.id
    FROM ah.session s
    WHERE c.child_session_id IS NULL AND s.agent = c.agent AND s.session_uid = c.child_uid AND s.agent_id = ''
    """,
    """
    UPDATE ah.session_continuation c SET parent_session_id = s.id
    FROM ah.session s
    WHERE c.parent_session_id IS NULL AND s.agent = c.agent AND s.session_uid = c.parent_uid AND s.agent_id = ''
    """,
    # One predecessor per session: explicit evidence first (resume > fork > compaction > clear > other),
    # then a Codex fork of a top-level thread (subagent forks are spawns, not continuations).
    """
    WITH cand AS (
        SELECT c.child_session_id AS sid, c.parent_session_id AS pid, c.kind,
               CASE c.kind WHEN 'resume' THEN 1 WHEN 'fork' THEN 2 WHEN 'compaction_continuation' THEN 3
                           WHEN 'clear' THEN 4 ELSE 5 END AS pr, c.ts
        FROM ah.session_continuation c
        WHERE c.child_session_id IS NOT NULL AND c.parent_session_id IS NOT NULL
          AND c.child_session_id <> c.parent_session_id
        UNION ALL
        SELECT s.id, p.id, 'fork', 2, s.first_event_at
        FROM ah.session s JOIN ah.session p ON p.agent = 'codex' AND p.session_uid = s.forked_from_uid AND p.agent_id = ''
        WHERE s.agent = 'codex' AND s.forked_from_uid IS NOT NULL AND NOT s.is_subagent AND p.id <> s.id),
    pick AS (SELECT DISTINCT ON (sid) sid, pid, kind FROM cand ORDER BY sid, pr, ts)
    UPDATE ah.session s SET continued_from_session_id = pick.pid, continuation_kind = pick.kind
    FROM pick
    WHERE s.id = pick.sid
      AND (s.continued_from_session_id IS DISTINCT FROM pick.pid OR s.continuation_kind IS DISTINCT FROM pick.kind)
    """,
]

# --- ordering --------------------------------------------------------------------------------

SEQ_SQL = [
    "DROP TABLE IF EXISTS seq_now",
    # Deterministic from source position: (file, byte offset, reasoning before text before calls before
    # ops, uid). Every row of a session comes from its own source file, so this is stable across rebuild
    # and new lines append. tool_io shares its call's or op's seq; unlinked tool_io rows get their own.
    """
    CREATE TEMP TABLE seq_now ON COMMIT DROP AS
    WITH items AS (
        SELECT 1 AS t, m.id, m.session_id, m.source_id, m.byte_offset,
               CASE WHEN m.message_class = 'reasoning' THEN 0 ELSE 1 END AS r, m.event_uid AS u
        FROM ah.message m JOIN dirty_now d ON d.session_id = m.session_id
        UNION ALL
        SELECT 2, c.id, c.session_id, c.source_id, c.byte_offset, 2, c.call_uid
        FROM ah.tool_call c JOIN dirty_now d ON d.session_id = c.session_id
        UNION ALL
        SELECT 3, o.id, o.session_id, o.source_id, o.byte_offset, 3, o.item_uid
        FROM ah.tool_op o JOIN dirty_now d ON d.session_id = o.session_id
        UNION ALL
        SELECT 4, i.id, i.session_id, i.source_id, i.byte_offset, 2, i.io_uid
        FROM ah.tool_io i JOIN dirty_now d ON d.session_id = i.session_id
        WHERE NOT (i.kind = 'call' AND EXISTS (SELECT 1 FROM ah.tool_call c WHERE c.agent = i.agent AND c.call_uid = i.call_uid))
          AND NOT (i.kind = 'op' AND EXISTS (SELECT 1 FROM ah.tool_op o WHERE o.agent = i.agent AND o.item_uid = i.item_uid)))
    SELECT items.t, items.id,
           row_number() OVER (PARTITION BY items.session_id ORDER BY f.rel_path, items.byte_offset, items.r, items.u, items.t) AS seq
    FROM items JOIN ah.source_file f ON f.id = items.source_id
    """,
    "ANALYZE seq_now",
    "UPDATE ah.message m SET seq = n.seq FROM seq_now n WHERE n.t = 1 AND m.id = n.id AND m.seq IS DISTINCT FROM n.seq",
    "UPDATE ah.tool_call c SET seq = n.seq FROM seq_now n WHERE n.t = 2 AND c.id = n.id AND c.seq IS DISTINCT FROM n.seq",
    "UPDATE ah.tool_op o SET seq = n.seq FROM seq_now n WHERE n.t = 3 AND o.id = n.id AND o.seq IS DISTINCT FROM n.seq",
    "UPDATE ah.tool_io i SET seq = n.seq FROM seq_now n WHERE n.t = 4 AND i.id = n.id AND i.seq IS DISTINCT FROM n.seq",
    """
    UPDATE ah.tool_io i SET seq = c.seq
    FROM dirty_now d, ah.tool_call c
    WHERE i.session_id = d.session_id AND i.kind = 'call' AND c.agent = i.agent AND c.call_uid = i.call_uid
      AND i.seq IS DISTINCT FROM c.seq
    """,
    """
    UPDATE ah.tool_io i SET seq = o.seq
    FROM dirty_now d, ah.tool_op o
    WHERE i.session_id = d.session_id AND i.kind = 'op' AND o.agent = i.agent AND o.item_uid = i.item_uid
      AND i.seq IS DISTINCT FROM o.seq
    """,
]

FIRST_PROMPT_SQL = """
WITH first AS (
    SELECT DISTINCT ON (m.session_id) m.session_id, m.event_uid, m.ts
    FROM ah.message m JOIN dirty_now d ON d.session_id = m.session_id
    WHERE ah.is_genuine_prompt(m.message_class, m.prompt_origin, m.text)
    ORDER BY m.session_id, m.seq NULLS LAST, m.ts, m.event_uid)
UPDATE ah.session s SET first_prompt_event_uid = f.event_uid, first_prompt_at = f.ts
FROM dirty_now d LEFT JOIN first f ON f.session_id = d.session_id
WHERE s.id = d.session_id AND s.first_prompt_event_uid IS DISTINCT FROM f.event_uid
"""

# --- failure detail --------------------------------------------------------------------------

# fake_cell_wait: a Codex code-mode `wait` failing with "exec cell <id> not found" where <id> was never
# returned ("Script running with cell ID <id>") by an exec or wait call in the same session: an invented
# id (none, x, bogus, ...) where the model meant collaboration wait_agent. A wait on the session's own
# cell that has since finished stays not_found. migrations/008_fake_cell_wait.sql backfills the same rule.
ERROR_SQL = [
    """
    WITH own_cells AS MATERIALIZED (
        SELECT DISTINCT c.session_id,
               substring(left(i.output_text, 80) FROM '^Script running with cell ID ([^[:space:]]+)') AS cell_id
        FROM ah.tool_call c JOIN ah.tool_io i ON i.agent = c.agent AND i.io_uid = c.call_uid
        WHERE c.agent = 'codex' AND c.tool_name IN ('exec', 'wait')
          AND left(i.output_text, 28) = 'Script running with cell ID '
          AND c.session_id IN (SELECT w.session_id FROM ah.tool_call w JOIN dirty_now d ON d.session_id = w.session_id
                               WHERE w.agent = 'codex' AND w.tool_name = 'wait'
                                 AND (COALESCE(w.is_error, false) OR w.outcome = 'error'))),
    x0 AS (
        SELECT c.id, c.session_id, c.agent, c.tool_name,
               ah.error_class_of(c.denial_kind, c.timed_out, c.interrupted, c.exit_code, c.is_error,
                                 c.outcome, e.excerpt) AS cls, e.excerpt
        FROM ah.tool_call c JOIN dirty_now d ON d.session_id = c.session_id
        LEFT JOIN ah.tool_io i ON i.agent = c.agent AND i.io_uid = c.call_uid
        CROSS JOIN LATERAL (SELECT CASE WHEN COALESCE(c.is_error, false) OR COALESCE(c.exit_code, 0) <> 0
                                             OR COALESCE(c.timed_out, false) OR COALESCE(c.interrupted, false)
                                             OR c.denial_kind IS NOT NULL
                                             OR c.outcome IN ('error', 'denied', 'interrupted', 'timeout')
                                        THEN ah.error_excerpt(i.stderr_text, i.output_text) END AS excerpt) e),
    x AS (
        SELECT x0.id, x0.excerpt,
               CASE WHEN x0.agent = 'codex' AND x0.tool_name = 'wait' AND x0.cls IS NOT NULL
                         AND x0.excerpt ~ 'exec cell .* not found'
                         AND NOT EXISTS (SELECT 1 FROM own_cells o WHERE o.session_id = x0.session_id
                                         AND o.cell_id = substring(x0.excerpt FROM 'exec cell (.*?) not found'))
                    THEN 'fake_cell_wait' ELSE x0.cls END AS cls
        FROM x0)
    UPDATE ah.tool_call c SET error_class = x.cls, error_excerpt = CASE WHEN x.cls IS NOT NULL THEN x.excerpt END
    FROM x
    WHERE c.id = x.id AND (c.error_class IS DISTINCT FROM x.cls
                           OR c.error_excerpt IS DISTINCT FROM CASE WHEN x.cls IS NOT NULL THEN x.excerpt END)
    """,
    """
    WITH x AS (
        SELECT o.id, ah.error_class_of(NULL, NULL, COALESCE(o.status = 'interrupted' OR o.exit_code = 130, false), o.exit_code,
                                       o.is_error, CASE WHEN o.status = 'failed' THEN 'error' END, e.excerpt) AS cls,
               e.excerpt
        FROM ah.tool_op o JOIN dirty_now d ON d.session_id = o.session_id
        LEFT JOIN ah.tool_io i ON i.agent = o.agent AND i.io_uid = 'item:' || o.item_uid
        CROSS JOIN LATERAL (SELECT CASE WHEN COALESCE(o.is_error, false) OR COALESCE(o.exit_code, 0) <> 0
                                             OR o.status IN ('failed', 'interrupted')
                                        THEN ah.error_excerpt(i.stderr_text, i.output_text) END AS excerpt) e)
    UPDATE ah.tool_op o SET error_class = x.cls, error_excerpt = CASE WHEN x.cls IS NOT NULL THEN x.excerpt END
    FROM x
    WHERE o.id = x.id AND (o.error_class IS DISTINCT FROM x.cls
                           OR o.error_excerpt IS DISTINCT FROM CASE WHEN x.cls IS NOT NULL THEN x.excerpt END)
    """,
]

# --- rollup extension ------------------------------------------------------------------------

ROLLUP_V4_SQL = """
UPDATE ah.session_rollup r SET
    human_prompts = (SELECT count(*) FROM ah.message m
                     WHERE m.session_id = r.session_id AND ah.is_genuine_prompt(m.message_class, m.prompt_origin, m.text)),
    tool_calls_by_family = (SELECT jsonb_object_agg(x.family, x.n) FROM (
                                SELECT COALESCE(c.tool_family, 'unknown') AS family, count(*) AS n
                                FROM ah.tool_call c WHERE c.session_id = r.session_id GROUP BY 1) x),
    error_count = (SELECT count(*) FROM ah.tool_call c WHERE c.session_id = r.session_id AND c.error_class IS NOT NULL)
                + (SELECT count(*) FROM ah.tool_op o WHERE o.session_id = r.session_id AND o.error_class IS NOT NULL),
    priced_cost_usd = ah.session_cost(r.session_id),
    repo_slug = (SELECT p.slug FROM ah.repo p WHERE p.id = s.repo_id),
    git_branch = s.git_branch,
    final_turn_status = (SELECT t.status FROM ah.turn t WHERE t.session_id = r.session_id
                         ORDER BY t.started_at DESC NULLS LAST, t.id DESC LIMIT 1),
    -- active time: gaps of at most 5 minutes between consecutive events (idle longer is excluded)
    active_s = (SELECT COALESCE(sum(g.gap) FILTER (WHERE g.gap <= 300), 0)::bigint FROM (
                    SELECT EXTRACT(EPOCH FROM e.ts - lag(e.ts) OVER (ORDER BY e.ts)) AS gap FROM (
                        SELECT m.ts FROM ah.message m WHERE m.session_id = r.session_id
                        UNION ALL SELECT l.ts FROM ah.llm_call l WHERE l.session_id = r.session_id
                        UNION ALL SELECT c.started_at FROM ah.tool_call c WHERE c.session_id = r.session_id AND c.started_at IS NOT NULL
                        UNION ALL SELECT c.ended_at FROM ah.tool_call c WHERE c.session_id = r.session_id AND c.ended_at IS NOT NULL
                    ) e) g),
    -- identity of the stored content, stable across rebuild: changes only when a message or tool I/O
    -- text changes, appears or disappears.
    content_fingerprint = md5(
        COALESCE((SELECT string_agg(m.event_uid || ':' || m.content_sha256, ',' ORDER BY m.event_uid)
                  FROM ah.message m WHERE m.session_id = r.session_id), '') || '|' ||
        COALESCE((SELECT string_agg(i.io_uid || ':' || COALESCE(i.input_sha256, '-') || ':' || COALESCE(i.output_sha256, '-')
                                    || ':' || COALESCE(i.stdout_sha256, '-') || ':' || COALESCE(i.stderr_sha256, '-')
                                    || ':' || COALESCE(i.result_sha256, '-'), ',' ORDER BY i.io_uid)
                  FROM ah.tool_io i WHERE i.session_id = r.session_id), '') || '|' ||
        COALESCE((SELECT string_agg(a.attachment_uid || ':' || COALESCE(a.text_sha256, '-') || ':'
                                    || COALESCE(a.size_bytes::text, '-'), ',' ORDER BY a.attachment_uid)
                  FROM ah.attachment a WHERE a.session_id = r.session_id), ''))
FROM dirty_now d, ah.session s
WHERE r.session_id = d.session_id AND s.id = r.session_id
"""

# --- orchestration ---------------------------------------------------------------------------

LOOP_SKILL = r"^/?(loop|agent-workflows:(loop|evidence-led-unattended-campaign))$"
WAVE_SKILL = r"^/?agent-workflows:wave-(cycle|fan-out)$"
LANE_ROLE = r"(lane[-_ ]?worker|complex[-_ ]?(lane[-_ ]?)?worker|gate[-_ ]?runner|mapper|rescue|worktree[-_ ]?auditor)"
POLLER_ROLE = r"poller"
REVIEWER_ROLE = r"(review|auditor)"
FANOUT_TYPES = r"^(agent-workflows:)"
FANOUT_MIN_SPAWNS = 8

ORCH_SQL = [
    "DROP TABLE IF EXISTS orch_roots, orch_tree, orch_root_kind, orch_new",
    # Trees to recompute: the roots of dirty sessions, plus loop roots of dirty heuristic members.
    """
    CREATE TEMP TABLE orch_roots ON COMMIT DROP AS
    SELECT DISTINCT COALESCE(s.root_session_id, s.id) AS root_id
    FROM dirty_now d JOIN ah.session s ON s.id = d.session_id
    UNION
    SELECT l.root_session_id FROM dirty_now d JOIN ah.session s ON s.id = d.session_id
    JOIN ah.loop_run l ON l.id = s.loop_run_id WHERE l.root_session_id IS NOT NULL
    """,
    # Members: the lineage tree plus sessions linked by the loop command heuristic and their subtrees.
    # A session in several trees keeps its lineage tree.
    """
    CREATE TEMP TABLE orch_tree ON COMMIT DROP AS
    SELECT DISTINCT ON (session_id) root_id, session_id, via FROM (
        SELECT r.root_id, s.id AS session_id, 0 AS via FROM orch_roots r
        JOIN ah.session s ON s.id = r.root_id OR s.root_session_id = r.root_id
        UNION ALL
        SELECT r.root_id, s.id, 1 FROM orch_roots r JOIN ah.loop_run l ON l.root_session_id = r.root_id
        JOIN ah.session m ON m.loop_run_id = l.id AND m.loop_link_method = 'heuristic'
        JOIN ah.session s ON s.id = m.id OR s.root_session_id = m.id
    ) t
    WHERE NOT EXISTS (SELECT 1 FROM ah.session x WHERE x.id = t.session_id AND x.is_stub)
    ORDER BY session_id, (root_id = session_id), via
    """,
    "CREATE INDEX ON orch_tree (root_id)",
    "ANALYZE orch_tree",
    "ANALYZE orch_roots",
    f"""
    CREATE TEMP TABLE orch_root_kind ON COMMIT DROP AS
    SELECT r.root_id, k.kind, k.evidence FROM orch_roots r
    CROSS JOIN LATERAL (
        SELECT CASE
            WHEN EXISTS (SELECT 1 FROM ah.loop_run l WHERE l.root_session_id = r.root_id AND l.naming = 'wave')
                THEN 'wave_root'
            WHEN EXISTS (SELECT 1 FROM ah.session_event e WHERE e.session_id = r.root_id
                         AND e.kind IN ('skill_invoke', 'slash_command') AND e.value ~* '{WAVE_SKILL}')
                THEN 'wave_root'
            WHEN EXISTS (SELECT 1 FROM ah.loop_run l WHERE l.root_session_id = r.root_id) THEN 'loop_root'
            WHEN EXISTS (SELECT 1 FROM ah.message m WHERE m.session_id = r.root_id
                         AND m.message_class IN ('human_prompt', 'queued_prompt') AND m.prompt_origin = 'launch_message')
                THEN 'loop_root'
            WHEN EXISTS (SELECT 1 FROM ah.session_event e WHERE e.session_id = r.root_id
                         AND e.kind IN ('skill_invoke', 'slash_command') AND e.value ~* '{LOOP_SKILL}')
              OR EXISTS (SELECT 1 FROM ah.message m WHERE m.session_id = r.root_id
                         AND m.message_class IN ('human_prompt', 'queued_prompt')
                         AND m.text ~* '(^|\\s)\\$(agent-workflows:)?(loop|evidence-led-unattended-campaign)\\M')
                THEN 'loop_root'
            WHEN EXISTS (SELECT 1 FROM orch_tree t JOIN ah.tool_call c ON c.session_id = t.session_id
                         WHERE t.root_id = r.root_id AND c.tool_name = 'Workflow')
                THEN 'workflow_root'
            WHEN EXISTS (SELECT 1 FROM orch_tree t JOIN ah.subagent_spawn sp ON sp.parent_session_id = t.session_id
                         WHERE t.root_id = r.root_id AND sp.requested_type ~* '{FANOUT_TYPES}')
                THEN 'fanout_root'
            WHEN (SELECT count(*) FROM orch_tree t JOIN ah.subagent_spawn sp ON sp.parent_session_id = t.session_id
                  WHERE t.root_id = r.root_id) >= {FANOUT_MIN_SPAWNS}
                THEN 'fanout_root'
            ELSE 'none' END AS kind,
        CASE
            WHEN EXISTS (SELECT 1 FROM ah.loop_run l WHERE l.root_session_id = r.root_id) THEN 'loop_run'
            WHEN EXISTS (SELECT 1 FROM ah.message m WHERE m.session_id = r.root_id
                         AND m.message_class IN ('human_prompt', 'queued_prompt') AND m.prompt_origin = 'launch_message')
                THEN 'launch_message'
            WHEN EXISTS (SELECT 1 FROM ah.session_event e WHERE e.session_id = r.root_id
                         AND e.kind IN ('skill_invoke', 'slash_command')
                         AND (e.value ~* '{WAVE_SKILL}' OR e.value ~* '{LOOP_SKILL}'))
                THEN 'skill'
            WHEN EXISTS (SELECT 1 FROM ah.message m WHERE m.session_id = r.root_id
                         AND m.message_class IN ('human_prompt', 'queued_prompt')
                         AND m.text ~* '(^|\\s)\\$(agent-workflows:)?(loop|evidence-led-unattended-campaign)\\M')
                THEN 'codex_skill'
            WHEN EXISTS (SELECT 1 FROM orch_tree t JOIN ah.tool_call c ON c.session_id = t.session_id
                         WHERE t.root_id = r.root_id AND c.tool_name = 'Workflow') THEN 'workflow_tool'
            WHEN EXISTS (SELECT 1 FROM orch_tree t JOIN ah.subagent_spawn sp ON sp.parent_session_id = t.session_id
                         WHERE t.root_id = r.root_id AND sp.requested_type ~* '{FANOUT_TYPES}') THEN 'lane_agent_types'
            WHEN (SELECT count(*) FROM orch_tree t JOIN ah.subagent_spawn sp ON sp.parent_session_id = t.session_id
                  WHERE t.root_id = r.root_id) >= {FANOUT_MIN_SPAWNS} THEN 'spawn_count'
        END AS evidence
    ) k
    """,
    f"""
    CREATE TEMP TABLE orch_new ON COMMIT DROP AS
    SELECT t.session_id, rk.root_id,
           CASE WHEN t.session_id = rk.root_id THEN rk.kind
                WHEN role.r ~* '{POLLER_ROLE}' THEN 'poller'
                WHEN role.r ~* '{REVIEWER_ROLE}' THEN 'reviewer'
                WHEN role.r ~* '{LANE_ROLE}' OR EXISTS (SELECT 1 FROM ah.lane ln WHERE ln.session_id = t.session_id)
                    THEN 'lane'
                WHEN s.is_subagent OR s.parent_session_id IS NOT NULL THEN 'subagent'
                ELSE 'other' END AS kind,
           CASE WHEN rk.kind <> 'none' THEN rk.root_id END AS orch_root,
           (rk.kind <> 'none' AND t.session_id <> rk.root_id) AS descendant,
           CASE WHEN t.session_id = rk.root_id THEN rk.evidence
                WHEN t.via = 1 THEN 'loop_heuristic' ELSE 'lineage' END AS evidence
    FROM orch_tree t
    JOIN orch_root_kind rk ON rk.root_id = t.root_id
    JOIN ah.session s ON s.id = t.session_id
    LEFT JOIN LATERAL (
        SELECT concat_ws(' ', s.agent_type, s.agent_role, s.agent_nickname,
                         (SELECT string_agg(sp.requested_type || ' ' || COALESCE(sp.name, ''), ' ')
                          FROM ah.subagent_spawn sp WHERE sp.child_session_id = s.id)) AS r) role ON true
    """,
]

ORCH_APPLY_SQL = """
UPDATE ah.session s SET orchestration_kind = n.kind, orchestration_root_session_id = n.orch_root,
       is_orchestration_descendant = n.descendant, orchestration_evidence = n.evidence
FROM orch_new n
WHERE s.id = n.session_id
  AND (s.orchestration_kind IS DISTINCT FROM n.kind OR s.orchestration_root_session_id IS DISTINCT FROM n.orch_root
       OR s.is_orchestration_descendant IS DISTINCT FROM n.descendant
       OR s.orchestration_evidence IS DISTINCT FROM n.evidence)
RETURNING s.id
"""

# --- change feed -----------------------------------------------------------------------------

CHANGE_SQL = [
    """
    UPDATE ah.session s SET content_changed_at = now()
    FROM dirty_now d WHERE s.id = d.session_id AND NOT s.is_stub
    """,
    """
    INSERT INTO ah.change_log (refresh_id, agent, session_uid, agent_id, kind)
    SELECT %(rid)s, s.agent, s.session_uid, s.agent_id, 'content'
    FROM dirty_now d JOIN ah.session s ON s.id = d.session_id
    WHERE NOT s.is_stub
    ORDER BY s.last_event_at NULLS FIRST, s.id
    """,
    """
    INSERT INTO ah.change_log (refresh_id, agent, session_uid, agent_id, kind)
    SELECT %(rid)s, s.agent, s.session_uid, s.agent_id, 'orchestration'
    FROM ah.session s
    WHERE s.id = ANY(%(orch)s) AND NOT s.is_stub AND NOT EXISTS (SELECT 1 FROM dirty_now d WHERE d.session_id = s.id)
    """,
    "DELETE FROM ah.change_log WHERE at < now() - interval '90 days'",
]

# --- session embeddings ----------------------------------------------------------------------

SESSION_EMBED_SQL = """
WITH active AS (SELECT value AS model FROM ah.meta WHERE key = 'embedding_model'),
stats AS (
    SELECT c.session_id, count(*) AS n, max(c.id) AS mx FROM ah.chunk c, active a
    WHERE c.model = a.model AND c.message_id IS NOT NULL AND c.session_id IS NOT NULL GROUP BY 1),
todo AS (
    SELECT st.* FROM stats st CROSS JOIN active a
    LEFT JOIN ah.session_embedding se ON se.session_id = st.session_id
    WHERE se.session_id IS NULL OR se.chunk_count <> st.n OR se.chunk_max_id <> st.mx OR se.model <> a.model
    ORDER BY st.session_id LIMIT %(batch)s)
INSERT INTO ah.session_embedding AS se (session_id, model, embedding, chunk_count, chunk_max_id, computed_at)
SELECT t.session_id, a.model, l2_normalize(avg(e.embedding::vector))::halfvec(1024), t.n, t.mx, now()
FROM todo t CROSS JOIN active a
JOIN ah.chunk c ON c.session_id = t.session_id AND c.model = a.model AND c.message_id IS NOT NULL
JOIN ah.embedding e ON e.model = c.model AND e.input_sha256 = c.input_sha256
GROUP BY t.session_id, a.model, t.n, t.mx
ON CONFLICT (session_id) DO UPDATE SET model = EXCLUDED.model, embedding = EXCLUDED.embedding,
    chunk_count = EXCLUDED.chunk_count, chunk_max_id = EXCLUDED.chunk_max_id, computed_at = EXCLUDED.computed_at
"""
SESSION_EMBED_BATCH = 20_000


def session_embeddings(conn: psycopg.Connection) -> dict[str, int]:
    """Refresh session vectors whose chunk set changed (the embedder adds chunks between refreshes)."""
    cur = conn.execute(SESSION_EMBED_SQL, {"batch": SESSION_EMBED_BATCH})
    return {"session_embeddings": cur.rowcount or 0}


def run(conn: psycopg.Connection, refresh_id: int) -> dict[str, int]:
    for statement in REPO_SQL + CONTINUATION_SQL + SEQ_SQL:
        conn.execute(statement)
    conn.execute(FIRST_PROMPT_SQL)
    for statement in ERROR_SQL:
        conn.execute(statement)
    conn.execute(ROLLUP_V4_SQL)
    for statement in ORCH_SQL:
        conn.execute(statement)
    changed = [row[0] for row in conn.execute(ORCH_APPLY_SQL)]
    for statement in CHANGE_SQL:
        conn.execute(statement, {"rid": refresh_id, "orch": changed})
    result = {"orchestration_changed": len(changed)}
    result.update(session_embeddings(conn))
    return result

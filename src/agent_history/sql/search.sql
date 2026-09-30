-- Hybrid (BM25 + vector) search functions. Reapplied after analytics.sql on schema changes.
--
-- Vectors live in ah.embedding (cache keyed by input hash); ah.chunk maps them to messages and
-- summaries by offsets. The active model is meta.embedding_model; qvec must come from that model
-- with kind 'query'. Reciprocal rank fusion, k = 60 (ParadeDB hybrid guidance).
-- Vector branch: narrow filters (< 50k candidate chunks) use an exact scan over the filtered chunks;
-- broad ones use the HNSW index with iterative scan so post-join filters still fill the list.

SET search_path = ah, public, paradedb;

DROP FUNCTION IF EXISTS ah.hybrid_search(text, halfvec, text[], timestamptz, integer, real, text[]),
    ah.hybrid_search_summaries(text, halfvec, text[], timestamptz, integer, real),
    ah.similar_messages(halfvec, text[], timestamptz, timestamptz, integer, real, text[]),
    ah.vector_candidates(halfvec, text[], timestamptz, timestamptz, text[], integer, boolean) CASCADE;

-- Nearest chunks for a query vector, collapsed to one row per message (or summary) by best
-- distance. Internal building block; output: (message_id, summary_id, distance, char_start, char_end).
-- summaries = true searches summary chunks instead of message chunks.
CREATE OR REPLACE FUNCTION ah.vector_candidates(qvec halfvec, namespaces text[], since timestamptz,
                                                until timestamptz, classes text[], lim integer,
                                                summaries boolean DEFAULT false)
RETURNS TABLE (message_id bigint, summary_id bigint, distance real, char_start integer, char_end integer)
LANGUAGE plpgsql STABLE AS $$
DECLARE
    active text := (SELECT value FROM ah.meta WHERE key = 'embedding_model');
    filters text := '';
    n bigint;
BEGIN
    IF qvec IS NULL OR active IS NULL THEN RETURN; END IF;
    filters := CASE WHEN summaries THEN ' AND c.summary_id IS NOT NULL' ELSE ' AND c.message_id IS NOT NULL' END;
    IF namespaces IS NOT NULL THEN filters := filters || ' AND c.namespace = ANY($3)'; END IF;
    IF since IS NOT NULL THEN filters := filters || ' AND c.ts >= $4'; END IF;
    IF until IS NOT NULL THEN filters := filters || ' AND c.ts < $5'; END IF;
    IF classes IS NOT NULL AND NOT summaries THEN
        filters := filters || ' AND EXISTS (SELECT 1 FROM ah.message m WHERE m.id = c.message_id'
                           || ' AND m.message_class = ANY($6))';
    END IF;
    EXECUTE format('SELECT count(*) FROM (SELECT 1 FROM ah.chunk c WHERE c.model = $2 %s LIMIT 50001) x', filters)
        INTO n USING qvec, active, namespaces, since, until, classes;
    IF n <= 50000 THEN
        -- Exact: distances over the filtered chunks only (always complete for narrow filters).
        RETURN QUERY EXECUTE format($q$
            SELECT DISTINCT ON (k) c.message_id, c.summary_id, (e.embedding <=> $1)::real, c.char_start, c.char_end
            FROM ah.chunk c
            JOIN ah.embedding e ON e.model = c.model AND e.input_sha256 = c.input_sha256
            CROSS JOIN LATERAL (SELECT COALESCE(c.message_id, -c.summary_id) AS k) kk
            WHERE c.model = $2 %s
            ORDER BY k, e.embedding <=> $1 $q$, filters)
        USING qvec, active, namespaces, since, until, classes;
        RETURN;
    END IF;
    -- Broad: HNSW over ah.embedding; iterative scan keeps going until enough rows pass the filters.
    PERFORM set_config('hnsw.ef_search', '200', true);
    PERFORM set_config('hnsw.iterative_scan', 'relaxed_order', true);
    PERFORM set_config('hnsw.max_scan_tuples', '100000', true);
    RETURN QUERY EXECUTE format($q$
        WITH v AS MATERIALIZED (
            SELECT c.message_id, c.summary_id, (e.embedding <=> $1)::real AS d, c.char_start, c.char_end
            FROM ah.embedding e
            JOIN ah.chunk c ON c.model = e.model AND c.input_sha256 = e.input_sha256
            WHERE e.model = $2 %s
            ORDER BY e.embedding <=> $1
            LIMIT $7 * 3)
        SELECT DISTINCT ON (COALESCE(v.message_id, -v.summary_id)) v.message_id, v.summary_id, v.d,
               v.char_start, v.char_end
        FROM v ORDER BY COALESCE(v.message_id, -v.summary_id), v.d $q$, filters)
    USING qvec, active, namespaces, since, until, classes, lim;
END $$;

-- Hybrid message search: RRF of BM25 (ah.search, any-term) and vector ranks. qvec NULL = BM25 only.
-- Output columns match ah.search plus bm25_rank / vec_rank (NULL when that branch missed).
CREATE OR REPLACE FUNCTION ah.hybrid_search(q text, qvec halfvec, namespaces text[] DEFAULT NULL,
                                            since timestamptz DEFAULT NULL, lim integer DEFAULT 20,
                                            w_vec real DEFAULT 0.7, classes text[] DEFAULT NULL)
RETURNS TABLE (message_id bigint, session_id bigint, agent text, namespace text, session_uid text,
               agent_id text, ts timestamptz, role text, message_class text, score real,
               bm25_rank integer, vec_rank integer, snippet text, cwd text, title text)
LANGUAGE sql STABLE AS $$
    WITH b AS (
        SELECT h.message_id, h.snippet, row_number() OVER (ORDER BY h.score DESC, h.message_id)::int AS r
        FROM ah.search(q, namespaces, since, 100, 'any', classes) h),
    v AS (
        SELECT x.message_id, x.char_start, x.char_end,
               row_number() OVER (ORDER BY x.distance, x.message_id)::int AS r
        FROM (SELECT * FROM ah.vector_candidates(qvec, namespaces, since, NULL, classes, 200)
              ORDER BY distance LIMIT 200) x),
    f AS (
        SELECT COALESCE(b.message_id, v.message_id) AS message_id, b.r AS br, v.r AS vr, b.snippet,
               v.char_start, v.char_end,
               (COALESCE(1.0 / (60 + b.r), 0) + COALESCE(w_vec / (60 + v.r), 0))::real AS score
        FROM b FULL JOIN v ON v.message_id = b.message_id)
    SELECT m.id, m.session_id, m.agent, m.namespace, s.session_uid, s.agent_id, m.ts, m.role, m.message_class,
           f.score, f.br, f.vr,
           COALESCE(f.snippet, left(substr(m.text, f.char_start + 1, f.char_end - f.char_start), 240)),
           s.cwd, COALESCE(s.custom_title, s.title)
    FROM f
    JOIN ah.message m ON m.id = f.message_id
    JOIN ah.session s ON s.id = m.session_id
    ORDER BY f.score DESC, m.id
    LIMIT lim
$$;

-- Hybrid search over journal session summaries (ah.search_summaries + summary chunks).
CREATE OR REPLACE FUNCTION ah.hybrid_search_summaries(q text, qvec halfvec, namespaces text[] DEFAULT NULL,
                                                      since timestamptz DEFAULT NULL, lim integer DEFAULT 20,
                                                      w_vec real DEFAULT 0.7)
RETURNS TABLE (summary_id bigint, session_id bigint, namespace text, agent text, session_uid text,
               agent_id text, analysed_at timestamptz, classification text, project text, title text,
               objective text, snippet text, score real, bm25_rank integer, vec_rank integer, cwd text)
LANGUAGE sql STABLE AS $$
    WITH b AS (
        SELECT h.summary_id, h.snippet, row_number() OVER (ORDER BY h.score DESC, h.summary_id)::int AS r
        FROM ah.search_summaries(q, namespaces, since, 100) h),
    v AS (
        SELECT x.summary_id, row_number() OVER (ORDER BY x.distance, x.summary_id)::int AS r
        FROM (SELECT * FROM ah.vector_candidates(qvec, namespaces, since, NULL, NULL, 200, true)
              ORDER BY distance LIMIT 200) x),
    f AS (
        SELECT COALESCE(b.summary_id, v.summary_id) AS summary_id, b.r AS br, v.r AS vr, b.snippet,
               (COALESCE(1.0 / (60 + b.r), 0) + COALESCE(w_vec / (60 + v.r), 0))::real AS score
        FROM b FULL JOIN v ON v.summary_id = b.summary_id)
    SELECT ss.id, ss.session_id, ss.namespace, s.agent, s.session_uid, s.agent_id, ss.analysed_at,
           ss.classification, ss.project, ss.title, left(ss.objective, 300),
           COALESCE(f.snippet, left(COALESCE(ss.narrative, ss.objective), 240)), f.score, f.br, f.vr, s.cwd
    FROM f
    JOIN ah.session_summary ss ON ss.id = f.summary_id
    LEFT JOIN ah.session s ON s.id = ss.session_id
    ORDER BY f.score DESC, ss.id
    LIMIT lim
$$;

-- Messages semantically close to a query vector (e.g. a rule's text), for rule_effect narrowing.
-- max_distance is cosine distance (0 identical .. 2 opposite).
CREATE OR REPLACE FUNCTION ah.similar_messages(qvec halfvec, namespaces text[] DEFAULT NULL,
                                               since timestamptz DEFAULT NULL, until timestamptz DEFAULT NULL,
                                               lim integer DEFAULT 50, max_distance real DEFAULT 0.6,
                                               classes text[] DEFAULT ARRAY['human_prompt', 'queued_prompt'])
RETURNS TABLE (message_id bigint, session_id bigint, namespace text, ts timestamptz, distance real,
               snippet text)
LANGUAGE sql STABLE AS $$
    SELECT m.id, m.session_id, m.namespace, m.ts, x.distance,
           left(substr(m.text, x.char_start + 1, x.char_end - x.char_start), 240)
    FROM (SELECT * FROM ah.vector_candidates(qvec, namespaces, since, until, classes, lim)
          WHERE distance <= max_distance ORDER BY distance LIMIT lim) x
    JOIN ah.message m ON m.id = x.message_id
    ORDER BY x.distance, m.id
$$;

-- ---------------------------------------------------------------------------------------------
-- Vector search accepts qvec generated by the caller; database functions do not call embeddings
-- providers. It must match `ah.meta.embedding_model`; NULL qvec selects BM25-only search.

DROP FUNCTION IF EXISTS ah.search_tool_io(text, text[], timestamptz, integer, text),
    ah.search_hybrid(text, text[], timestamptz, integer, real, halfvec, text[], boolean),
    ah.similar_sessions(text, text, text, integer, text[]) CASCADE;

-- BM25 over tool input/output/stdout/stderr/result (ah.tool_io_search_idx). mode: all | any | phrase.
CREATE OR REPLACE FUNCTION ah.search_tool_io(q text, namespaces text[] DEFAULT NULL,
                                             since timestamptz DEFAULT NULL, lim integer DEFAULT 20,
                                             mode text DEFAULT 'all')
RETURNS TABLE (tool_io_id bigint, session_id bigint, agent text, namespace text, io_uid text, call_uid text,
               item_uid text, tool_name text, ts timestamptz, score real, snippet text)
LANGUAGE plpgsql STABLE AS $$
DECLARE
    op text := CASE mode WHEN 'any' THEN '|||' WHEN 'phrase' THEN '###' ELSE '&&&' END;
    filters text := '';
BEGIN
    -- The BM25 index is created after bulk loads (rebuild): without it there is no tool I/O text branch.
    IF to_regclass('ah.tool_io_search_idx') IS NULL THEN RETURN; END IF;
    IF namespaces IS NOT NULL THEN filters := filters || ' AND i.namespace = ANY($2)'; END IF;
    IF since IS NOT NULL THEN filters := filters || ' AND i.ts >= $3'; END IF;
    RETURN QUERY EXECUTE format($q$
        SELECT i.id, i.session_id, i.agent, i.namespace, i.io_uid, i.call_uid, i.item_uid, i.tool_name, i.ts,
               pdb.score(i.id)::real,
               COALESCE(NULLIF(pdb.snippet(i.output_text, max_num_chars => 240), ''),
                        NULLIF(pdb.snippet(i.stderr_text, max_num_chars => 240), ''),
                        NULLIF(pdb.snippet(i.stdout_text, max_num_chars => 240), ''),
                        NULLIF(pdb.snippet(i.input_text, max_num_chars => 240), ''),
                        NULLIF(pdb.snippet(i.result_json, max_num_chars => 240), ''))
        FROM ah.tool_io i
        WHERE (i.input_text %1$s $1 OR i.output_text %1$s $1 OR i.stdout_text %1$s $1 OR i.stderr_text %1$s $1
               OR i.result_json %1$s $1) %2$s
        ORDER BY pdb.score(i.id) DESC, i.id LIMIT $4 $q$, op, filters)
    USING q, namespaces, since, lim;
END $$;

-- Hybrid search across messages (every class, or `classes`) and tool I/O: reciprocal rank fusion
-- (k = 60) of message BM25, tool I/O BM25, message-chunk vectors and tool-error-excerpt vectors
-- (both vector lists weighted w_vec).
-- hit_kind 'message' carries event_uid; 'tool_io' carries call_uid / io_uid (message_class 'tool_io').
CREATE OR REPLACE FUNCTION ah.search_hybrid(q text, namespaces text[] DEFAULT NULL, since timestamptz DEFAULT NULL,
                                            lim integer DEFAULT 20, w_vec real DEFAULT 0.7, qvec halfvec DEFAULT NULL,
                                            classes text[] DEFAULT NULL, include_tool_io boolean DEFAULT true)
RETURNS TABLE (agent text, session_uid text, agent_id text, namespace text, hit_kind text, event_uid text,
               call_uid text, io_uid text, message_class text, tool_name text, ts timestamptz, seq bigint,
               score real, bm25_rank integer, vec_rank integer, snippet text, session_id bigint)
LANGUAGE sql STABLE AS $$
    WITH bm AS (
        SELECT h.message_id, h.snippet, row_number() OVER (ORDER BY h.score DESC, h.message_id)::int AS r
        FROM ah.search(q, namespaces, since, 100, 'any', classes) h),
    bt AS (
        SELECT t.tool_io_id, t.snippet, row_number() OVER (ORDER BY t.score DESC, t.tool_io_id)::int AS r
        FROM ah.search_tool_io(q, namespaces, since, 100, 'any') t WHERE include_tool_io),
    v AS (
        SELECT x.message_id, x.char_start, x.char_end,
               row_number() OVER (ORDER BY x.distance, x.message_id)::int AS r
        FROM (SELECT * FROM ah.vector_candidates(qvec, namespaces, since, NULL, classes, 200)
              ORDER BY distance LIMIT 200) x),
    ve AS (
        SELECT x.io_uid, x.agent, row_number() OVER (ORDER BY x.d, x.io_uid)::int AS r
        FROM (SELECT DISTINCT ON (k.io_uid) k.io_uid, k.agent, (e.embedding <=> qvec)::real AS d
              FROM ah.chunk c
              JOIN ah.embedding e ON e.model = c.model AND e.input_sha256 = c.input_sha256
              LEFT JOIN ah.tool_call tc ON tc.id = c.tool_call_id
              LEFT JOIN ah.tool_op op ON op.id = c.tool_op_id
              CROSS JOIN LATERAL (SELECT COALESCE(tc.call_uid, 'item:' || op.item_uid) AS io_uid,
                                         COALESCE(tc.agent, op.agent) AS agent) k
              WHERE qvec IS NOT NULL AND include_tool_io AND (c.tool_call_id IS NOT NULL OR c.tool_op_id IS NOT NULL)
                AND c.model = (SELECT value FROM ah.meta WHERE key = 'embedding_model')
                AND (namespaces IS NULL OR c.namespace = ANY(namespaces)) AND (since IS NULL OR c.ts >= since)
              ORDER BY k.io_uid, e.embedding <=> qvec) x
        ORDER BY x.d LIMIT 200),
    vei AS (SELECT i.id, ve.r FROM ve JOIN ah.tool_io i ON i.agent = ve.agent AND i.io_uid = ve.io_uid),
    ft AS (
        SELECT COALESCE(bt.tool_io_id, vei.id) AS id, bt.r AS br, vei.r AS vr, bt.snippet,
               (COALESCE(1.0 / (60 + bt.r), 0) + COALESCE(w_vec / (60 + vei.r), 0))::real AS score
        FROM bt FULL JOIN vei ON vei.id = bt.tool_io_id),
    fm AS (
        SELECT COALESCE(bm.message_id, v.message_id) AS id, bm.r AS br, v.r AS vr, bm.snippet, v.char_start, v.char_end,
               (COALESCE(1.0 / (60 + bm.r), 0) + COALESCE(w_vec / (60 + v.r), 0))::real AS score
        FROM bm FULL JOIN v ON v.message_id = bm.message_id),
    hits AS (
        SELECT s.agent, s.session_uid, s.agent_id, m.namespace, 'message'::text AS hit_kind, m.event_uid,
               NULL::text AS call_uid, NULL::text AS io_uid, m.message_class, NULL::text AS tool_name, m.ts, m.seq,
               fm.score, fm.br, fm.vr,
               COALESCE(fm.snippet, left(substr(m.text, fm.char_start + 1, fm.char_end - fm.char_start), 240)) AS snippet,
               m.session_id
        FROM fm JOIN ah.message m ON m.id = fm.id JOIN ah.session s ON s.id = m.session_id
        UNION ALL
        SELECT s.agent, s.session_uid, s.agent_id, i.namespace, 'tool_io', NULL, i.call_uid, i.io_uid, 'tool_io',
               i.tool_name, i.ts, i.seq, ft.score, ft.br, ft.vr,
               COALESCE(ft.snippet, (SELECT COALESCE(c.error_excerpt, o.error_excerpt)
                                     FROM (SELECT 1) one
                                     LEFT JOIN ah.tool_call c ON c.agent = i.agent AND c.call_uid = i.call_uid
                                     LEFT JOIN ah.tool_op o ON o.agent = i.agent AND o.item_uid = i.item_uid)),
               i.session_id
        FROM ft JOIN ah.tool_io i ON i.id = ft.id JOIN ah.session s ON s.id = i.session_id)
    SELECT * FROM hits ORDER BY score DESC, ts DESC, COALESCE(event_uid, io_uid) LIMIT lim
$$;

-- Sessions nearest to one session by session vector (mean of its message-chunk embeddings).
CREATE OR REPLACE FUNCTION ah.similar_sessions(p_agent text, p_session_uid text, p_agent_id text DEFAULT '',
                                               lim integer DEFAULT 10, namespaces text[] DEFAULT NULL)
RETURNS TABLE (agent text, session_uid text, agent_id text, namespace text, distance real, title text,
               first_event_at timestamptz, session_id bigint)
LANGUAGE plpgsql STABLE AS $$
DECLARE
    q halfvec;
    me bigint;
BEGIN
    SELECT se.embedding, se.session_id INTO q, me FROM ah.session_embedding se JOIN ah.session s ON s.id = se.session_id
    WHERE s.agent = p_agent AND s.session_uid = p_session_uid AND s.agent_id = COALESCE(p_agent_id, '');
    IF q IS NULL THEN RETURN; END IF;
    PERFORM set_config('hnsw.ef_search', '200', true);
    PERFORM set_config('hnsw.iterative_scan', 'relaxed_order', true);
    RETURN QUERY
    SELECT s.agent, s.session_uid, s.agent_id, s.namespace, x.d, COALESCE(s.custom_title, s.title), s.first_event_at, s.id
    FROM (SELECT se.session_id, (se.embedding <=> q)::real AS d FROM ah.session_embedding se
          WHERE se.session_id <> me ORDER BY se.embedding <=> q LIMIT lim * 4 + 20) x
    JOIN ah.session s ON s.id = x.session_id
    WHERE namespaces IS NULL OR s.namespace = ANY(namespaces)
    ORDER BY x.d, s.id
    LIMIT lim;
END $$;

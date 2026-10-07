# Catalogue comparison queries

These PostgreSQL queries are single read-only SELECT statements for an `ah_reader`
connection. They require migration `033_nullable_telemetry.sql` and the current
analytics functions. They return metadata, not transcript text. Protect their output:
recorded identifiers and provenance can still be sensitive.

Each `params` CTE selects the last seven days, with an inclusive lower bound and
exclusive upper bound. Replace both timestamps with explicit `timestamptz` literals
for a reproducible readback. Set `namespace` to an exact configured namespace to
narrow the sample; NULL includes all namespaces for the selected harness. No LIMIT
is applied, so the returned row count is the selected cohort size, not a page size.
Missing call timestamps cannot occur; turns without `started_at` are outside these
samples. No rows is not proof that telemetry is supported or absent everywhere.
Applying the migration alone does not enrich historical rows: retained transcripts
must be re-parsed with the corresponding parser upgrade or rebuilt.

## 1. pi recorded per-call cost versus catalogue model pricing

One row per pi model call. `cost_usd` is explicitly recorded USD cost, not an invoice
or an allocation of session cost. `seed_priced_cost_usd` uses the catalogue's current
`model_pricing` rows through `ah.priced_usd`, selecting the latest effective price
on or before the call's UTC day. It reflects installed seed prices and any operator
updates, not a frozen copy of the seed or provider-specific billing. Exact model
names are used, without alias inference. Reasoning tokens are not added to output.

The function normally treats missing token inputs as zero; this query deliberately
requires all five token components to be known before calling it. Missing tokens,
model or applicable price leave the comparison unknown. Recorded zero is known.
`recorded_minus_priced_usd` is NULL unless both costs are known. A difference can
reflect discounts, service tiers, provider accounting or price changes; it is not
by itself a parser defect. API-error rows are retained and labelled.

```sql
WITH params AS (
    SELECT current_timestamp - interval '7 days' AS since,
           current_timestamp AS until,
           NULL::text AS namespace
), priced AS (
    SELECT s.namespace, s.session_uid, s.agent_id,
           c.response_id, c.ts, c.model, c.provider, c.api,
           c.service_tier, c.is_api_error, c.cost_usd,
           c.input_uncached, c.cache_read, c.cache_write_5m,
           c.cache_write_1h, c.output,
           CASE WHEN c.input_uncached IS NOT NULL
                     AND c.cache_read IS NOT NULL
                     AND c.cache_write_5m IS NOT NULL
                     AND c.cache_write_1h IS NOT NULL
                     AND c.output IS NOT NULL
                THEN ah.priced_usd(c.model, (c.ts AT TIME ZONE 'UTC')::date,
                                   c.input_uncached, c.cache_read,
                                   c.cache_write_5m, c.cache_write_1h, c.output)
           END AS seed_priced_cost_usd
    FROM ah.llm_call c
    JOIN ah.session s ON s.id = c.session_id AND s.agent = c.agent
    CROSS JOIN params p
    WHERE c.agent = 'pi' AND c.ts >= p.since AND c.ts < p.until
      AND (p.namespace IS NULL OR s.namespace = p.namespace)
)
SELECT *, cost_usd - seed_priced_cost_usd AS recorded_minus_priced_usd,
       CASE WHEN cost_usd IS NULL THEN 'recorded_cost_unknown'
            WHEN seed_priced_cost_usd IS NULL THEN 'catalogue_cost_unknown'
            ELSE 'comparable' END AS comparison_status
FROM priced
ORDER BY ts, session_uid, agent_id, response_id;
```

## 2. Codex subagent turn to recorded root turn

One row per sampled subagent turn, including unresolved references. Codex records
`task_started.root_turn_id` as `turn.root_turn_key` and `task_started.trace_id` as
`turn.trace_id`. The parser records `session_meta.session_id` as the root session
UID; the loader resolves it to `session.root_session_id`. The join below uses only
that resolved session and the exact recorded turn key. Turn keys are session-scoped:
a key match in another session, a shared trace, timing or a path is not a substitute.
The root turn need not start within the sampled period; only child starts are filtered.

NULL trace IDs are unknown, not a mismatch. `trace_comparison` describes independent
corroboration, not a join criterion: differing traces do not silently discard the
explicit root reference. A NULL root reference, unresolved session, or missing root
turn yields no asserted link. `root_turn_not_indexed` can also mean incomplete retained
history. A linked row demonstrates a catalogue reference, not process liveness.

```sql
WITH params AS (
    SELECT current_timestamp - interval '7 days' AS since,
           current_timestamp AS until,
           NULL::text AS namespace
)
SELECT s.namespace, s.session_uid AS child_session_uid,
       s.agent_id AS child_agent_id, t.turn_key AS child_turn_key,
       t.started_at AS child_started_at, t.trace_id AS child_trace_id,
       s.root_session_uid AS recorded_root_session_uid,
       t.root_turn_key AS recorded_root_turn_key,
       r.session_uid AS resolved_root_session_uid,
       r.agent_id AS root_agent_id, rt.turn_key AS matched_root_turn_key,
       rt.started_at AS root_started_at, rt.trace_id AS root_trace_id,
       CASE WHEN t.root_turn_key IS NULL THEN 'root_turn_reference_unknown'
            WHEN r.id IS NULL THEN 'root_session_unresolved'
            WHEN rt.id IS NULL THEN 'root_turn_not_indexed'
            ELSE 'linked' END AS link_status,
       CASE WHEN t.trace_id IS NULL OR rt.trace_id IS NULL THEN 'unknown'
            WHEN t.trace_id = rt.trace_id THEN 'equal'
            ELSE 'different' END AS trace_comparison
FROM ah.turn t
JOIN ah.session s ON s.id = t.session_id
CROSS JOIN params p
LEFT JOIN ah.session r ON r.id = s.root_session_id AND r.agent = 'codex'
LEFT JOIN ah.turn rt ON rt.session_id = r.id AND rt.turn_key = t.root_turn_key
WHERE s.agent = 'codex' AND s.is_subagent
  AND t.started_at >= p.since AND t.started_at < p.until
  AND (p.namespace IS NULL OR s.namespace = p.namespace)
ORDER BY t.started_at, s.session_uid, s.agent_id, t.turn_key;
```

## 3. Claude recorded origin hint versus catalogue turn origin

One row per sampled Claude turn. `origin_hint` comes from explicit retained user
record `turnOrigin`; `origin` is the catalogue's existing turn classification.
They describe different provenance surfaces and need not share a vocabulary.
Compare exact strings, without normalising or treating the hint as authoritative
replacement classification. NULL on either side is unknown; empty text, if recorded,
is a value rather than NULL. A difference invites inspection of retained evidence,
not automatic relabelling. No message content is needed for this comparison.

```sql
WITH params AS (
    SELECT current_timestamp - interval '7 days' AS since,
           current_timestamp AS until,
           NULL::text AS namespace
)
SELECT s.namespace, s.session_uid, s.agent_id, s.is_subagent,
       t.turn_key, t.started_at, t.origin_hint, t.origin AS catalogue_origin,
       t.prompt_index, t.turn_index,
       CASE WHEN t.origin_hint IS NULL OR t.origin IS NULL THEN 'unknown'
            WHEN t.origin_hint = t.origin THEN 'equal'
            ELSE 'different' END AS origin_comparison
FROM ah.turn t
JOIN ah.session s ON s.id = t.session_id
CROSS JOIN params p
WHERE s.agent = 'claude'
  AND t.started_at >= p.since AND t.started_at < p.until
  AND (p.namespace IS NULL OR s.namespace = p.namespace)
ORDER BY t.started_at, s.session_uid, s.agent_id, t.turn_key;
```

## Readback evidence

The operator should execute all three statements on the deployed catalogue using
fixed period bounds, record those bounds and namespace scope, and retain each returned
row count plus counts by comparison/link status in private evidence. Do not copy
production rows into public documentation. Local schema inspection and repository
checks do not establish production coverage or successful historical enrichment.

The definitions above derive from the catalogue contract, the baseline session,
turn and call definitions, migration 033, `analytics.sql` (`ah.priced_usd`), the
Codex and Claude parsers, and the loader's Codex root-session resolution. Session
identity is `(agent, session_uid, agent_id)`; a turn additionally requires `turn_key`
and a call uses `(agent, response_id)`. Surrogate IDs used internally in joins are
not stable across rebuilds.

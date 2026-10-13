-- Content-free pi request evidence, additive in schema_version 1. No backfill or grants.
-- Request UUID, response id and recovery attempt UUID are distinct identities. Only the final
-- assistant envelope supplies a request/response link. Never join by ordinal, pid or proximity.
-- Source ownership is provenance, not deletion authority: source rewrites cannot purge newer
-- independent checkpoint observations. Rebuild explicitly truncates these derived tables.
SET search_path = ah, public;

ALTER TABLE ah.source_file DROP CONSTRAINT source_file_file_role_check;
ALTER TABLE ah.source_file ADD CONSTRAINT source_file_file_role_check CHECK
    (file_role IN ('main','subagent','workflow_agent','workflow_journal','pi_artifact','pi_progress'));

CREATE TABLE ah.request_progress (
    session_id bigint NOT NULL REFERENCES ah.session(id) ON DELETE CASCADE,
    request_uid text NOT NULL,
    source_id bigint REFERENCES ah.source_file(id) ON DELETE SET NULL,
    byte_offset bigint NOT NULL,
    namespace text NOT NULL,
    started_at timestamptz NOT NULL,
    written_at timestamptz NOT NULL,
    state text NOT NULL CHECK (state IN ('active','ended','abandoned')),
    producer_version integer NOT NULL CHECK (producer_version = 1),
    provenance text NOT NULL CHECK (provenance = 'loopPiProgress.v1'),
    ended_at timestamptz,
    response_id text,
    turn_ordinal integer CHECK (turn_ordinal >= 0),
    pid integer CHECK (pid >= 0),
    provider text,
    model text,
    api text,
    effort text,
    stop_reason text,
    age_ms bigint CHECK (age_ms BETWEEN 0 AND 9007199254740991),
    provider_responses bigint CHECK (provider_responses BETWEEN 0 AND 9007199254740991),
    first_response_at timestamptz,
    provider_request_id text,
    model_events bigint CHECK (model_events BETWEEN 0 AND 9007199254740991),
    first_model_event_at timestamptz,
    last_model_event_at timestamptz,
    reasoning_started bigint CHECK (reasoning_started BETWEEN 0 AND 9007199254740991),
    reasoning_deltas bigint CHECK (reasoning_deltas BETWEEN 0 AND 9007199254740991),
    reasoning_completed bigint CHECK (reasoning_completed BETWEEN 0 AND 9007199254740991),
    reasoning_first_at timestamptz,
    reasoning_last_at timestamptz,
    text_started bigint CHECK (text_started BETWEEN 0 AND 9007199254740991),
    text_deltas bigint CHECK (text_deltas BETWEEN 0 AND 9007199254740991),
    text_completed bigint CHECK (text_completed BETWEEN 0 AND 9007199254740991),
    text_first_at timestamptz,
    text_last_at timestamptz,
    tool_calls_started bigint CHECK (tool_calls_started BETWEEN 0 AND 9007199254740991),
    tool_calls_deltas bigint CHECK (tool_calls_deltas BETWEEN 0 AND 9007199254740991),
    tool_calls_completed bigint CHECK (tool_calls_completed BETWEEN 0 AND 9007199254740991),
    tool_calls_first_at timestamptz,
    tool_calls_last_at timestamptz,
    provider_events bigint CHECK (provider_events BETWEEN 0 AND 9007199254740991),
    last_provider_event_at timestamptz,
    keepalives bigint CHECK (keepalives BETWEEN 0 AND 9007199254740991),
    last_keepalive_at timestamptz,
    PRIMARY KEY (session_id, request_uid),
    CHECK (written_at >= started_at),
    CHECK ((state = 'active' AND ended_at IS NULL) OR
           (state IN ('ended','abandoned') AND ended_at BETWEEN started_at AND written_at))
);
CREATE INDEX request_progress_namespace_time_idx ON ah.request_progress(namespace, started_at DESC);
COMMENT ON TABLE ah.request_progress IS
    'loopPiProgress v1: local epoch-ms observation times in UTC; age_ms milliseconds. Counters are events/items/deltas, never tokens. written_at is snapshot freshness, not activity/liveness. NULL means independently unsupported/invalid/unobserved. Stream/header counts are not SDK attempts.';

CREATE TABLE ah.request_recovery (
    session_id bigint NOT NULL REFERENCES ah.session(id) ON DELETE CASCADE,
    response_id text NOT NULL,
    source_id bigint REFERENCES ah.source_file(id) ON DELETE SET NULL,
    byte_offset bigint NOT NULL,
    namespace text NOT NULL,
    observed_at timestamptz NOT NULL,
    provenance text NOT NULL CHECK (provenance = 'loopPiRecovery'),
    attempt_uid text,
    code text,
    status integer CHECK (status BETWEEN 100 AND 599),
    disposition text CHECK (disposition IN ('stop','retry','unchanged')),
    reason text,
    output_observed boolean,
    request_portable boolean,
    tools_executed boolean,
    timeout_source text CHECK (timeout_source = 'recovery-episode'),
    episode_limit_ms bigint CHECK (episode_limit_ms BETWEEN 0 AND 9007199254740991),
    attempt_count bigint CHECK (attempt_count BETWEEN 0 AND 9007199254740991),
    PRIMARY KEY (session_id, response_id)
);
CREATE INDEX request_recovery_namespace_time_idx ON ah.request_recovery(namespace, observed_at DESC);
COMMENT ON TABLE ah.request_recovery IS
    'Explicit unversioned loopPiRecovery assistant metadata. response_id is the enclosing retained assistant identity. attempt_uid is NOT a progress request UUID. code/reason are fixed producer allowlists, never error text. Current producer does not record episode_limit_ms or attempt_count here; they stay NULL.';

CREATE TABLE ah.request_incident (
    session_id bigint NOT NULL REFERENCES ah.session(id) ON DELETE CASCADE,
    event_uid text NOT NULL,
    source_id bigint REFERENCES ah.source_file(id) ON DELETE SET NULL,
    byte_offset bigint NOT NULL,
    namespace text NOT NULL,
    observed_at timestamptz NOT NULL,
    kind text NOT NULL CHECK (kind IN ('wall-clock','empty-length','episode-exhausted')),
    provenance text NOT NULL CHECK (provenance IN ('loop-request-incident.details','loop-recovery-exhausted.details')),
    request_uid text,
    timeout_source text CHECK (timeout_source IN ('request-wall-clock-ceiling','recovery-episode')),
    follow_up_count integer CHECK (follow_up_count >= 0),
    exhausted boolean,
    elapsed_ms bigint CHECK (elapsed_ms BETWEEN 0 AND 9007199254740991),
    wall_clock_limit_ms bigint CHECK (wall_clock_limit_ms BETWEEN 0 AND 9007199254740991),
    follow_up_limit integer CHECK (follow_up_limit >= 0),
    output_token_limit bigint CHECK (output_token_limit BETWEEN 0 AND 9007199254740991),
    episode_limit_ms bigint CHECK (episode_limit_ms BETWEEN 0 AND 9007199254740991),
    PRIMARY KEY (session_id, event_uid)
);
CREATE INDEX request_incident_namespace_time_idx ON ah.request_incident(namespace, observed_at DESC);
COMMENT ON TABLE ah.request_incident IS
    'Structured custom-message observations only. No incident-to-request correlation in current producer: request_uid NULL. follow_up_count is consecutive incidents, NOT request/provider attempts. elapsed_ms and *_limit_ms are milliseconds, follow_up_limit is continuations, output_token_limit tokens. Only episode exhaustion records its explicit limit. Other limits remain NULL: defaults/settings/text are not authoritative measurements.';

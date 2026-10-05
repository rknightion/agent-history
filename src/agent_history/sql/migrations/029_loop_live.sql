-- Additive nullable live phase and summary fields. Existing ah.loops SELECT grants suffice.
-- Replay on fresh init. The paid queue/cache/budget are independent of rebuild's derived tables.
ALTER TABLE ah.loops
    ADD COLUMN live_phase text CHECK (live_phase IN ('preparing','working','reviewing','gating','landing','waiting','closing')),
    ADD COLUMN phase_since timestamptz,
    ADD COLUMN jev_phase text CHECK (jev_phase IN ('preparing','working','reviewing','gating','landing','waiting','closing')),
    ADD COLUMN jev_phase_probs jsonb,
    ADD COLUMN active_lanes jsonb,
    ADD COLUMN last_gate jsonb,
    ADD COLUMN parks_total bigint,
    ADD COLUMN last_park jsonb,
    ADD COLUMN tasks_admitted bigint,
    ADD COLUMN tasks_landed bigint,
    ADD COLUMN last_judgement text CHECK (char_length(last_judgement) <= 500),
    ADD COLUMN last_judgement_at timestamptz,
    ADD COLUMN ops_state jsonb,
    ADD COLUMN headline text CHECK (char_length(headline) <= 120),
    ADD COLUMN summary text CHECK (char_length(summary) <= 600),
    ADD COLUMN summary_generated_at timestamptz,
    ADD COLUMN summary_model text,
    ADD COLUMN summary_error text,
    ADD COLUMN final_summary boolean;

CREATE TABLE ah.loop_phase_event (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    launch_uid text NOT NULL REFERENCES ah.loops(launch_uid) ON DELETE CASCADE,
    at timestamptz NOT NULL,
    phase text CHECK (phase IN ('preparing','working','reviewing','gating','landing','waiting','closing')),
    source text NOT NULL CHECK (source IN ('structure','hybrid','summary')),
    headline text CHECK (char_length(headline) <= 120)
);
CREATE INDEX loop_phase_event_launch_at_idx ON ah.loop_phase_event(launch_uid, at, id);

-- No launch/session foreign keys here: TRUNCATE derived tables CASCADE must not erase paid work.
CREATE TABLE ah.loop_live_job (
    launch_uid text PRIMARY KEY,
    digest_sha256 text NOT NULL,
    state jsonb NOT NULL,
    close_requested boolean NOT NULL,
    queued_at timestamptz NOT NULL DEFAULT now(),
    claimed_at timestamptz,
    finished_at timestamptz,
    applied_at timestamptz,
    error text
);
CREATE INDEX loop_live_job_pending_idx ON ah.loop_live_job(queued_at) WHERE claimed_at IS NULL;

CREATE TABLE ah.loop_live_cache (
    launch_uid text NOT NULL,
    digest_sha256 text NOT NULL,
    kind text NOT NULL CHECK (kind IN ('jev','summary')),
    response jsonb,
    error text,
    model text NOT NULL,
    generated_at timestamptz NOT NULL DEFAULT now(),
    requested_at timestamptz NOT NULL,
    final_summary boolean NOT NULL DEFAULT false,
    PRIMARY KEY (launch_uid, digest_sha256, kind)
);
CREATE INDEX loop_live_cache_latest_idx ON ah.loop_live_cache(launch_uid, kind, generated_at DESC);

CREATE TABLE ah.loop_live_budget (
    day date PRIMARY KEY,
    reserved_usd numeric NOT NULL CHECK (reserved_usd >= 0 AND reserved_usd <= 5)
);
CREATE TABLE ah.loop_live_reservation (
    launch_uid text NOT NULL,
    digest_sha256 text NOT NULL,
    kind text NOT NULL CHECK (kind IN ('jev','summary')),
    day date NOT NULL,
    reserved_usd numeric NOT NULL CHECK (reserved_usd > 0),
    at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (launch_uid, digest_sha256, kind)
);
-- roles.sql grants SELECT through the writer's default ACL. These internal paid tables must
-- explicitly counter that inheritance, without touching grants on loops or any existing table.
REVOKE ALL ON TABLE ah.loop_live_job, ah.loop_live_cache, ah.loop_live_budget,
    ah.loop_live_reservation FROM PUBLIC;
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ah_reader') THEN
        REVOKE ALL ON TABLE ah.loop_live_job, ah.loop_live_cache, ah.loop_live_budget,
            ah.loop_live_reservation FROM ah_reader;
    END IF;
END $$;

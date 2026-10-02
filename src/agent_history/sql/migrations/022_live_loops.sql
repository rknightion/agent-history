-- Stable live-loop relation: consumer grants survive analytics re-application and rebuild.
-- Parse quality remains in loop_run.status; this table records observed lifecycle state.
CREATE TABLE ah.loops (
    launch_uid text PRIMARY KEY REFERENCES ah.loop_run (launch_uid) ON DELETE CASCADE,
    status text NOT NULL CHECK (status IN ('running', 'finished', 'stale')),
    launch_ts timestamptz NOT NULL,
    end_ts timestamptz,
    observed_at timestamptz NOT NULL,
    CHECK ((status = 'running') = (end_ts IS NULL))
);

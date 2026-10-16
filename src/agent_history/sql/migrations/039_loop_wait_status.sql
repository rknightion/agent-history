-- Observed v3 waiting/paused states remain open; terminal lifecycle is unchanged.
ALTER TABLE ah.loops ADD COLUMN IF NOT EXISTS waiting_until timestamptz;

ALTER TABLE ah.loops
    DROP CONSTRAINT loops_status_check,
    ADD CONSTRAINT loops_status_check
        CHECK (status IN ('running', 'finished', 'stale', 'waiting', 'paused')),
    DROP CONSTRAINT loops_check,
    ADD CONSTRAINT loops_check
        CHECK ((status IN ('running', 'waiting', 'paused')) = (end_ts IS NULL));

-- Upgrade the open-state selection without dropping the historical running-only index.
CREATE INDEX IF NOT EXISTS loops_open_progress_repo_idx
    ON ah.loops (repo, launch_uid) WHERE status IN ('running', 'waiting', 'paused');

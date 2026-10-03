-- Additive consumer projection; nullable means no captured evidence, not a fabricated zero.
ALTER TABLE ah.loops
    ADD COLUMN IF NOT EXISTS lanes_total bigint,
    ADD COLUMN IF NOT EXISTS lanes_returned bigint,
    ADD COLUMN IF NOT EXISTS last_activity_at timestamptz,
    ADD COLUMN IF NOT EXISTS llm_calls bigint,
    ADD COLUMN IF NOT EXISTS input_uncached bigint,
    ADD COLUMN IF NOT EXISTS cache_read bigint,
    ADD COLUMN IF NOT EXISTS cache_write bigint,
    ADD COLUMN IF NOT EXISTS output bigint,
    ADD COLUMN IF NOT EXISTS priced_cost_usd numeric,
    ADD COLUMN IF NOT EXISTS tool_errors bigint,
    ADD COLUMN IF NOT EXISTS api_errors bigint,
    ADD COLUMN IF NOT EXISTS commits bigint,
    ADD COLUMN IF NOT EXISTS pushes bigint,
    ADD COLUMN IF NOT EXISTS root_agent text;

-- Reverse ownership lookup starts from changed roots, not every historical loop.
CREATE INDEX IF NOT EXISTS loop_run_progress_owner_idx
    ON ah.loop_run (root_session_id) INCLUDE (launch_uid);
-- Receiver queries start with exact repository identity; global refresh scans only running rows.
CREATE INDEX IF NOT EXISTS loops_progress_repo_idx
    ON ah.loops (repo, launch_uid) WHERE status = 'running';

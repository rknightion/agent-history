-- Drop journal FKs: TRUNCATE session CASCADE must not delete these collector-owned rows.
-- Preserve the natural key on each row so a rebuild can resume relinking after interruption.
ALTER TABLE ah.session_summary DROP CONSTRAINT IF EXISTS session_summary_session_id_fkey;
ALTER TABLE ah.session_topic DROP CONSTRAINT IF EXISTS session_topic_session_id_fkey;
ALTER TABLE ah.session_topic ADD COLUMN IF NOT EXISTS session_agent text;
ALTER TABLE ah.session_topic ADD COLUMN IF NOT EXISTS session_uid text;
ALTER TABLE ah.session_topic ADD COLUMN IF NOT EXISTS session_agent_id text;

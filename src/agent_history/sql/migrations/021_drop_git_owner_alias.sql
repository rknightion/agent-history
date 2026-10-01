-- Readers and writers must use author_is_owner before this migration is deployed.
-- These views depend on the compatibility column through SELECT c.*; apply_schema
-- recreates them from analytics.sql, with reader grants, in this same transaction.
DROP VIEW IF EXISTS ah.v_spawn_outcome;
DROP VIEW IF EXISTS ah.v_agent_commit_quality_weekly;
DROP VIEW IF EXISTS ah.v_agent_commit_quality;
DROP TRIGGER IF EXISTS git_commit_owner_sync ON ah.git_commit;
DROP FUNCTION IF EXISTS ah.sync_git_commit_owner();
ALTER TABLE ah.git_commit DROP COLUMN IF EXISTS author_is_rob;

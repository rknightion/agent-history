-- Loop measurement in schema_version 1: a protocol per run, the run-folder dialect's collected
-- documents and a separate count of client-generated model calls. Additive and nullable: existing
-- rows stay NULL until they are projected, collected or rolled up again. No grant changes.
SET search_path = ah, public;

-- The one token of a collected document's `protocol: <token>` line (the first non-empty line after
-- line 1), and two more document kinds: a run folder's AUTHORITY.md and CLOSEOUT.md.
ALTER TABLE ah.loop_run_metadata ADD COLUMN IF NOT EXISTS protocol text;
ALTER TABLE ah.loop_run_metadata DROP CONSTRAINT IF EXISTS loop_run_metadata_kind_check;
ALTER TABLE ah.loop_run_metadata ADD CONSTRAINT loop_run_metadata_kind_check
    CHECK (kind IN ('charter', 'closeout', 'authority', 'run_closeout'));

-- The protocol a run declares, separate from `mode` (the dialect of its terminal document).
ALTER TABLE ah.loop_run ADD COLUMN IF NOT EXISTS protocol text;

-- Assistant records whose model is Claude Code's `<synthetic>` marker, beside llm_calls.
ALTER TABLE ah.session_rollup ADD COLUMN IF NOT EXISTS synthetic_calls integer;

-- Roll up again every session holding such a record, and tag again every root that wrote a run
-- folder's AUTHORITY.md before this migration, so both are projected without a rebuild.
INSERT INTO ah.dirty_session (session_id)
SELECT DISTINCT session_id FROM ah.llm_call WHERE model = '<synthetic>'
ON CONFLICT DO NOTHING;
INSERT INTO ah.dirty_session (session_id)
SELECT DISTINCT COALESCE(s.root_session_id, s.id)
FROM ah.file_touch f JOIN ah.session s ON s.id = f.session_id
WHERE f.path ~ '(^|/)codex/run-[0-9]{8}-[1-9][0-9]{0,3}/AUTHORITY\.md$'
ON CONFLICT DO NOTHING;

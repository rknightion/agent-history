-- Distinct task IDs under a qualified closeout's Landed and Parked headings. Integers only: the
-- collector still stores no closeout body. Existing rows stay NULL until they are collected again.
ALTER TABLE ah.loop_run_metadata ADD COLUMN IF NOT EXISTS landed_count integer;
ALTER TABLE ah.loop_run_metadata ADD COLUMN IF NOT EXISTS parked_count integer;

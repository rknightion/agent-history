-- Collector evidence: one row per commit that moved a backlog task to Done, taken from the repo's
-- default-branch history. Not derived from transcripts, so rebuild and analytics keep it.
CREATE TABLE IF NOT EXISTS ah.backlog_done_event (
    repo_slug text NOT NULL,
    task_key text NOT NULL,
    sha text NOT NULL,
    context text,
    done_at timestamptz NOT NULL,
    from_status text,
    seen_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (repo_slug, task_key, sha)
);
CREATE INDEX IF NOT EXISTS backlog_done_event_repo_time_idx ON ah.backlog_done_event (repo_slug, done_at);

-- A repo's first Done scan covers its whole stored commit window; this marks it done so a repo with no
-- flips is not re-widened every run.
CREATE TABLE IF NOT EXISTS ah.backlog_done_scan (
    repo_slug text PRIMARY KEY,
    widened_at timestamptz NOT NULL
);

-- Additive loop projections; NULL means no captured evidence, not a fabricated zero.
ALTER TABLE ah.loops
    ADD COLUMN IF NOT EXISTS tasks_done bigint,
    ADD COLUMN IF NOT EXISTS lanes_accepted bigint,
    ADD COLUMN IF NOT EXISTS lanes_reported bigint;

-- Readers of the loop relations read the new table too. Grants are copied, never widened.
DO $$
DECLARE reader record;
BEGIN
    FOR reader IN
        SELECT DISTINCT acl.grantee
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        CROSS JOIN LATERAL aclexplode(c.relacl) acl
        WHERE n.nspname = 'ah' AND c.relname = 'loop_run'
          AND acl.privilege_type = 'SELECT' AND acl.grantee <> c.relowner
    LOOP
        IF reader.grantee = 0 THEN
            GRANT SELECT ON ah.backlog_done_event TO PUBLIC;
            GRANT SELECT ON ah.backlog_done_scan TO PUBLIC;
        ELSE
            EXECUTE format('GRANT SELECT ON ah.backlog_done_event TO %I', pg_get_userbyid(reader.grantee));
            EXECUTE format('GRANT SELECT ON ah.backlog_done_scan TO %I', pg_get_userbyid(reader.grantee));
        END IF;
    END LOOP;
    -- The hourly collector upserts rows, and deletes those whose commit left the default branch.
    -- INSERT .. ON CONFLICT needs SELECT on the key columns, so SELECT is part of the write grant.
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ah_ingest') THEN
        GRANT SELECT, INSERT, UPDATE, DELETE ON ah.backlog_done_event TO ah_ingest;
        GRANT SELECT, INSERT ON ah.backlog_done_scan TO ah_ingest;
    END IF;
END $$;

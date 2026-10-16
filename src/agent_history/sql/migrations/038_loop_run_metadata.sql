-- Metadata-only charter/closeout observations, preserved across derived-data rebuilds.
CREATE TABLE ah.loop_run_metadata (
    machine text NOT NULL,
    path text NOT NULL,
    kind text NOT NULL CHECK (kind IN ('charter', 'closeout')),
    sha256 text NOT NULL,
    line1 text,
    file_mtime timestamptz NOT NULL,
    repo_origin text,
    seen_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (machine, kind, path)
);
CREATE INDEX loop_run_metadata_path_idx ON ah.loop_run_metadata (kind, path);
CREATE INDEX loop_run_metadata_seen_idx ON ah.loop_run_metadata (kind, seen_at) INCLUDE (path);
-- Exact window probes and incremental owner selection do not walk retained launch history.
CREATE INDEX loop_metadata_terminal_idx ON ah.loop_run (report_path, launch_ts) WHERE launch_ts IS NOT NULL;
CREATE INDEX loop_metadata_owner_idx ON ah.loop_run (root_session_id, launch_ts) WHERE launch_ts IS NOT NULL;
CREATE INDEX loop_metadata_charter_idx ON ah.loop_run (goal_path);
CREATE INDEX loop_metadata_history_idx ON ah.loop_run (launch_uid) WHERE mode = 'v3' AND launch_ts IS NOT NULL;
CREATE INDEX loops_metadata_active_idx ON ah.loops (launch_uid) WHERE status IN ('running', 'waiting', 'paused');

-- Match the retained-state migration's receipt reader grants, without widening old grants.
DO $$
DECLARE reader record;
BEGIN
    FOR reader IN
        SELECT DISTINCT acl.grantee
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        CROSS JOIN LATERAL aclexplode(c.relacl) acl
        WHERE n.nspname = 'ah' AND c.relname = 'loop_receipt'
          AND acl.privilege_type = 'SELECT' AND acl.grantee <> c.relowner
    LOOP
        IF reader.grantee = 0 THEN
            GRANT SELECT ON ah.loop_run_metadata TO PUBLIC;
        ELSE
            EXECUTE format('GRANT SELECT ON ah.loop_run_metadata TO %I', pg_get_userbyid(reader.grantee));
        END IF;
    END LOOP;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ah_ingest') THEN
        GRANT SELECT, INSERT, UPDATE ON ah.loop_run_metadata TO ah_ingest;
    END IF;
END $$;

-- Retained state snapshots from explicitly configured, reconciled repository copies.
-- Collector evidence survives rebuild; paths describe the copy, not transcript paths.
CREATE TABLE ah.loop_state (
    machine text NOT NULL,
    path text NOT NULL,
    loop text NOT NULL,
    repo_origin text,
    content text NOT NULL,
    state_mtime timestamptz NOT NULL,
    seen_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (machine, path)
);
CREATE INDEX loop_state_identity_idx ON ah.loop_state (lower(repo_origin), loop);

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
            GRANT SELECT ON ah.loop_state TO PUBLIC;
        ELSE
            EXECUTE format('GRANT SELECT ON ah.loop_state TO %I', pg_get_userbyid(reader.grantee));
        END IF;
    END LOOP;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ah_ingest') THEN
        GRANT SELECT, INSERT, UPDATE ON ah.loop_state TO ah_ingest;
    END IF;
END $$;

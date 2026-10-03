-- Collector evidence from wave-notify receipt files: metadata about a receipt and its target, never
-- the report or goal body. Not derived from transcripts, so rebuild and analytics keep it.
CREATE TABLE ah.loop_receipt (
    machine text NOT NULL,
    path text NOT NULL,
    kind text NOT NULL CHECK (kind IN ('notified', 'started')),
    content text NOT NULL,
    receipt_mtime timestamptz NOT NULL,
    target_exists boolean NOT NULL,
    target_sha256 text,
    target_line1 text,
    repo_origin text,
    seen_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (machine, kind, path)
);
CREATE INDEX loop_receipt_kind_path_idx ON ah.loop_receipt (kind, path);

-- Readers of the loop relations read receipts too. Grants are copied, never widened.
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
            GRANT SELECT ON ah.loop_receipt TO PUBLIC;
        ELSE
            EXECUTE format('GRANT SELECT ON ah.loop_receipt TO %I', pg_get_userbyid(reader.grantee));
        END IF;
    END LOOP;
    -- The hourly collector runs as the dedicated ah_ingest role when it exists. It upserts
    -- receipts and never deletes them. INSERT .. ON CONFLICT needs SELECT on the key columns even
    -- with INSERT and UPDATE granted, so SELECT is part of the write grant.
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ah_ingest') THEN
        GRANT SELECT, INSERT, UPDATE ON ah.loop_receipt TO ah_ingest;
    END IF;
END $$;

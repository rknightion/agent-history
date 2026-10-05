-- Append evidence before reconciling off-default collector rows, in the mutation's transaction.
-- off_default retains git_commit and its file rows; delete removes a backlog_done_event.
-- No foreign keys: collector history and its natural keys survive reconciliation and rebuild.
CREATE TABLE ah.collector_mutation_audit (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    table_name text NOT NULL,
    operation text NOT NULL,
    row_key jsonb NOT NULL,
    repo_slug text NOT NULL,
    ref text NOT NULL,
    ref_sha text NOT NULL,
    deleted_at timestamptz NOT NULL DEFAULT now(),
    CHECK ((table_name = 'git_commit' AND operation = 'off_default')
        OR (table_name = 'backlog_done_event' AND operation = 'delete'))
);
CREATE INDEX collector_mutation_audit_repo_time_idx
    ON ah.collector_mutation_audit (repo_slug, deleted_at);

-- The dedicated collector can append evidence, never update or delete it.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ah_ingest') THEN
        GRANT SELECT, INSERT ON ah.collector_mutation_audit TO ah_ingest;
        GRANT USAGE ON SEQUENCE ah.collector_mutation_audit_id_seq TO ah_ingest;
    END IF;
END $$;

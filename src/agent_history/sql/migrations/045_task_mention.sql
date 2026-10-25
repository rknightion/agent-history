-- Every tracker-id mention in a session's messages, additive in schema_version 1. No backfill or
-- grants: the next index pass fills it with one full rescan, because its prefix hash is absent.
-- ah.task_ref is the subset that names a known, unremoved task. Keeping every mention lets a task
-- created or removed on a board re-derive task_ref without re-reading every message; only a change
-- to the known prefixes rescans message text. Derived data: session rebuild truncates it through
-- the session foreign key.
SET search_path = ah, public;

CREATE TABLE IF NOT EXISTS ah.task_mention (
    session_id bigint NOT NULL REFERENCES ah.session(id) ON DELETE CASCADE,
    task_key text NOT NULL,
    first_ts timestamptz NOT NULL,
    last_ts timestamptz NOT NULL,
    mentions integer NOT NULL,
    in_human boolean NOT NULL DEFAULT false,
    in_brief boolean NOT NULL DEFAULT false,
    PRIMARY KEY (session_id, task_key)
);
CREATE INDEX IF NOT EXISTS task_mention_key_idx ON ah.task_mention (task_key);

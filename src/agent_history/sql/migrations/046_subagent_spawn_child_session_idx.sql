-- Index the child side of ah.subagent_spawn, additive in schema_version 1. The session link pass
-- counts spawns per child session (WHERE child_session_id = c.id) for every candidate child; without
-- an index each probe was a sequential scan of the whole table. No CONCURRENTLY: migrations run in
-- a transaction, and the table is small enough for a plain build. Derived data: a btree survives a
-- rebuild, so this is not a post-load index.
SET search_path = ah, public;

CREATE INDEX IF NOT EXISTS subagent_spawn_child_session_idx ON ah.subagent_spawn (child_session_id);

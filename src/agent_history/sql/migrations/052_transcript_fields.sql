-- Additive transcript fields in schema_version 1. Recorded values only: absent or invalid values
-- remain NULL, including on existing rows until their transcripts are re-read. No defaults beyond
-- the new table's bookkeeping, no backfill, and no change to existing column semantics or grants.
SET search_path = ah, public;

-- Claude workflow journal: the phase of each agent step (`started.phase`).
ALTER TABLE ah.subagent_spawn
    ADD COLUMN IF NOT EXISTS workflow_phase text;

-- Claude assistant records: the model as requested (verbatim, including a `[1m]` suffix) and the
-- recorded tool input streaming time in milliseconds.
ALTER TABLE ah.llm_call
    ADD COLUMN IF NOT EXISTS requested_model text,
    ADD COLUMN IF NOT EXISTS tool_input_stream_ms integer;

-- Codex task_started.turn_attribution: the parent thread's turn id (a turn_key of the parent
-- session) and the path of the agent that initiated the turn.
ALTER TABLE ah.turn
    ADD COLUMN IF NOT EXISTS parent_turn_key text,
    ADD COLUMN IF NOT EXISTS initiating_agent_path text;

-- Claude subagent `*.meta.json` sidecars: whole-file snapshots the harness rewrites in place.
ALTER TABLE ah.source_file DROP CONSTRAINT source_file_file_role_check;
ALTER TABLE ah.source_file ADD CONSTRAINT source_file_file_role_check CHECK
    (file_role IN ('main','subagent','workflow_agent','workflow_journal','pi_artifact','pi_progress',
                   'claude_subagent_meta'));

CREATE TABLE IF NOT EXISTS ah.subagent_meta (
    session_id bigint PRIMARY KEY REFERENCES ah.session(id) ON DELETE CASCADE,
    source_id bigint REFERENCES ah.source_file(id) ON DELETE SET NULL,
    namespace text NOT NULL,
    agent_type text,
    model text,
    picked_effort_model text,
    effort text,
    spawn_depth integer,
    request_shape text,
    request_non_interactive boolean,
    workflow_phase text,
    tool_use_id text,
    parent_agent_id text,
    finished boolean,
    stopped_by_user boolean,
    launched_with_defaults boolean,
    is_fork boolean,
    unknown_keys text[] NOT NULL DEFAULT '{}',
    observed_at timestamptz NOT NULL DEFAULT now()
);
COMMENT ON TABLE ah.subagent_meta IS
    'Latest snapshot of a Claude subagent *.meta.json sidecar; a rewrite replaces the row. Scalars only: no description, worktree paths or other text. unknown_keys names unrecognised keys, never values.';

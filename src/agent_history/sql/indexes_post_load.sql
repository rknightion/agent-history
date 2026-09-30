-- ParadeDB (BM25) index on the text surface. Created AFTER the bulk load (ParadeDB guidance), by
-- `agent-history create-indexes` (or the first `agent-history index`). One ParadeDB index per table; changing its fields or
-- tokenizers needs CREATE INDEX CONCURRENTLY of a replacement, then DROP of the old one.
-- source_code tokenizer: splits camelCase/snake_case/paths; no stemming (identifier fidelity).
-- literal fields are filter/facet columns so namespace/agent/class/time filters push down.
CREATE INDEX IF NOT EXISTS message_search_idx ON ah.message USING paradedb (
    id,
    (text::pdb.source_code),
    (namespace::pdb.literal),
    (profile::pdb.literal),
    (agent::pdb.literal),
    (role::pdb.literal),
    (message_class::pdb.literal),
    session_id,
    ts
) WITH (key_field = 'id');

-- Journal analyses: prose, so English stemming; one ParadeDB index for the table.
CREATE INDEX IF NOT EXISTS session_summary_search_idx ON ah.session_summary USING paradedb (
    id,
    (title::pdb.simple('stemmer=english', 'alias=title_en')),
    (objective::pdb.simple('stemmer=english', 'alias=objective_en')),
    (narrative::pdb.simple('stemmer=english', 'alias=narrative_en')),
    (namespace::pdb.literal),
    (classification::pdb.literal),
    session_id,
    analysed_at
) WITH (key_field = 'id');

-- Tool input/output (parser v4). source_code tokenizer like message text; filter fields literal.
CREATE INDEX IF NOT EXISTS tool_io_search_idx ON ah.tool_io USING paradedb (
    id,
    (input_text::pdb.source_code),
    (output_text::pdb.source_code),
    (stdout_text::pdb.source_code),
    (stderr_text::pdb.source_code),
    (result_json::pdb.source_code),
    (namespace::pdb.literal),
    (agent::pdb.literal),
    (kind::pdb.literal),
    (tool_name::pdb.literal),
    session_id,
    ts
) WITH (key_field = 'id');

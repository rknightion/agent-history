-- opencode-* namespaces reuse the Claude parser but label their rows with the agent 'opencode'.
-- Widens the two agent checks only; no data changes, defaults, backfill or grants.
ALTER TABLE ah.session DROP CONSTRAINT IF EXISTS session_agent_check;
ALTER TABLE ah.session
    ADD CONSTRAINT session_agent_check CHECK (agent = ANY (ARRAY['claude'::text, 'codex'::text, 'pi'::text, 'opencode'::text]));
ALTER TABLE ah.source_file DROP CONSTRAINT IF EXISTS source_file_agent_check;
ALTER TABLE ah.source_file
    ADD CONSTRAINT source_file_agent_check CHECK (agent = ANY (ARRAY['claude'::text, 'codex'::text, 'pi'::text, 'opencode'::text]));

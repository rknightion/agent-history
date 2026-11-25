-- Claude Code's Code Mode writes each tool a REPL script ran as a client-written assistant message
-- (model '<synthetic>', zero tokens, a tool use whose id starts 'repl_'). The parser no longer makes an
-- llm_call row for it; this removes the rows it made before, without a reparse of every transcript.
-- Other '<synthetic>' records (API errors, interruption notices) are not touched: the rows removed are
-- those whose own tool_call is a Code Mode inner call and whose record carries no text or thinking: the
-- parser keeps a record with real content as a model call unless the client flagged it virtual, and a
-- stored row cannot tell the two apart, so a record with message rows is kept. Idempotent.
SET search_path = ah, public;

CREATE TEMP TABLE code_mode_call ON COMMIT DROP AS
SELECT l.id, l.session_id
FROM ah.llm_call l
WHERE l.model = '<synthetic>' AND NOT l.is_api_error
  AND EXISTS (SELECT 1 FROM ah.tool_call c
              WHERE c.session_id = l.session_id AND c.response_id = l.response_id
                AND c.call_uid LIKE 'repl\_%')
  AND NOT EXISTS (SELECT 1 FROM ah.message m
                  WHERE m.session_id = l.session_id AND m.byte_offset = l.byte_offset);

INSERT INTO ah.dirty_session (session_id)
SELECT DISTINCT session_id FROM code_mode_call
ON CONFLICT DO NOTHING;

DELETE FROM ah.llm_call l USING code_mode_call d WHERE l.id = d.id;

-- The parser leaves the response empty for these calls: they answer no model response.
UPDATE ah.tool_call SET response_id = NULL WHERE call_uid LIKE 'repl\_%' AND response_id IS NOT NULL;

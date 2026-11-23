-- Roll up again every session holding a Claude Code '<synthetic>' record. Migration 053 queued them
-- once, but a catalogue that applied 053 before its rollup wrote synthetic_calls consumed those marks
-- with the old rollup: synthetic_calls stayed NULL and llm_calls kept counting the records. The same
-- statement as 053; idempotent, so a catalogue that applied both together only recomputes again.
SET search_path = ah, public;

INSERT INTO ah.dirty_session (session_id)
SELECT DISTINCT session_id FROM ah.llm_call WHERE model = '<synthetic>'
ON CONFLICT DO NOTHING;

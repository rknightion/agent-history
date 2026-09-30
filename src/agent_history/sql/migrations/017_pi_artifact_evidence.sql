-- One response can have multiple artifact copies with conflicting agent types.
ALTER TABLE ah.pi_run_response DROP CONSTRAINT IF EXISTS pi_run_response_pkey;
ALTER TABLE ah.pi_run_response ADD CONSTRAINT pi_run_response_pkey PRIMARY KEY (run_id, response_id, source_id);

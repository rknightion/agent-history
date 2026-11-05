-- pi cost provenance (pi-ai 1.5.0 usage.cost.totalFromProvider) beside pi's catalogue estimate.
-- Recorded telemetry only: absent values remain NULL, including on existing rows. No defaults,
-- backfill, constraints or changes to existing column semantics or grants.
ALTER TABLE ah.llm_call
    ADD COLUMN IF NOT EXISTS cost_from_provider boolean,
    ADD COLUMN IF NOT EXISTS cost_estimate_usd numeric;

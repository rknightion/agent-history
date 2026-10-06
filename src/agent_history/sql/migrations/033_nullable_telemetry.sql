-- Recorded telemetry only: absent measurements remain NULL, including on existing rows.
-- No defaults, backfill, constraints or changes to existing column semantics or grants.
ALTER TABLE ah.llm_call
    ADD COLUMN duration_ms int,
    ADD COLUMN latency_basis text,
    ADD COLUMN cost_usd numeric,
    ADD COLUMN thinking_ms int,
    ADD COLUMN raw_stop_reason text,
    ADD COLUMN api text,
    ADD COLUMN provider text,
    ADD COLUMN cache_miss_type text,
    ADD COLUMN cache_missed_tokens bigint,
    ADD COLUMN input_transform_types text[],
    ADD COLUMN advisor_model text,
    ADD COLUMN inference_geo text,
    ADD COLUMN iterations int,
    ADD COLUMN ttft_ms int,
    ADD COLUMN attempts int,
    ADD COLUMN processing_ms int;

ALTER TABLE ah.cost_state ADD COLUMN has_unknown_model_cost bool;
ALTER TABLE ah.message ADD COLUMN phase text;

ALTER TABLE ah.turn
    ADD COLUMN reasoning_summary text,
    ADD COLUMN trace_id text,
    ADD COLUMN root_turn_key text,
    ADD COLUMN origin_hint text,
    ADD COLUMN prompt_index int,
    ADD COLUMN turn_index int,
    ADD COLUMN pending_bg_agents int,
    ADD COLUMN pending_workflows int;

ALTER TABLE ah.tool_call ADD COLUMN deadline_hit bool;

ALTER TABLE ah.subagent_spawn
    ADD COLUMN timeout_ms bigint,
    ADD COLUMN deadline_at timestamptz,
    ADD COLUMN run_fanout_budget int,
    ADD COLUMN spawn_budget int,
    ADD COLUMN active_async_capacity int,
    ADD COLUMN lifecycle_status text;

ALTER TABLE ah.tool_op
    ADD COLUMN mcp_plugin_id text,
    ADD COLUMN mcp_read_only bool;

-- Migration 020 (additive): gpt-6.1-sol list prices in USD per million tokens.
-- Source: OpenAI pricing page https://developers.openai.com/api/docs/pricing, 2026-09-29.
-- effective_from 2000-01-01 applies this list price to all history, as migration 002 does.
-- Long-context surcharges (>272K input) and fast mode are NOT modelled: very long
-- requests remain under-priced. No separate 1h cache-write price is published.

INSERT INTO ah.model_pricing (model, effective_from, input_per_mtok, cached_input_per_mtok,
                             cache_write_per_mtok, cache_write_1h_per_mtok, output_per_mtok, source)
VALUES ('gpt-6.1-sol', '2000-01-01', 2.00, 0.10, 2.50, NULL, 10.00,
        'OpenAI https://developers.openai.com/api/docs/pricing 2026-09-29')
ON CONFLICT (model, effective_from) DO NOTHING;

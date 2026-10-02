-- Optional seed for ah.model_pricing (USD per million tokens): list prices as published on the
-- date in each row's source, applied to all history (effective_from 2000-01-01). Add a newer
-- effective_from row when a price changes. Long-context surcharges and fast modes are not modelled.
-- Load with `agent-history seed-pricing`. A row that already exists is left as it is, so an
-- operator's own price for a model is never replaced.

SET search_path = ah, public;

INSERT INTO model_pricing (model, effective_from, input_per_mtok, cached_input_per_mtok, cache_write_per_mtok,
                           cache_write_1h_per_mtok, output_per_mtok, source) VALUES
    ('gpt-6-astra',              '2000-01-01', 10.00, 1.00,  12.50, NULL, 50.00, 'openai pricing 2026-09-25'),
    ('gpt-6.1-sol',              '2000-01-01',  2.00, 0.10,   2.50, NULL, 10.00, 'openai pricing 2026-09-29'),
    ('gpt-6-sol',                '2000-01-01',  2.00, 0.20,   2.50, NULL, 10.00, 'openai pricing 2026-09-25'),
    ('gpt-6-luna',               '2000-01-01',  0.10, 0.01,  0.125, NULL,  0.50, 'openai pricing 2026-09-25'),
    ('gpt-5.6-sol',              '2000-01-01',  4.00, 0.40,   5.00, NULL, 20.00, 'openai pricing 2026-09-25'),
    ('gpt-daybreak-blue-latest', '2000-01-01',  4.00, 0.40,   5.00, NULL, 20.00, 'openai pricing 2026-09-25 (alias of gpt-5.6-sol)'),
    ('gpt-5.6-terra',            '2000-01-01',  2.00, 0.20,   2.50, NULL, 12.00, 'openai model page 2026-09-25'),
    ('gpt-5.6-luna',             '2000-01-01',  0.20, 0.02,   0.25, NULL,  1.20, 'openai model page 2026-09-25'),
    ('gpt-5.5',                  '2000-01-01',  5.00, 0.50,   NULL, NULL, 30.00, 'openai model page 2026-09-25'),
    ('claude-opus-5-5',          '2000-01-01',  4.00, 0.20,   5.00, 8.00, 20.00, 'anthropic pricing 2026-09-25'),
    ('claude-opus-5',            '2000-01-01',  5.00, 0.50,   6.25, 10.00, 25.00, 'anthropic pricing 2026-09-25'),
    ('claude-opus-4-8',          '2000-01-01',  5.00, 0.50,   6.25, 10.00, 25.00, 'anthropic pricing 2026-09-25'),
    ('claude-opus-4-7',          '2000-01-01',  5.00, 0.50,   6.25, 10.00, 25.00, 'anthropic pricing 2026-09-25'),
    ('claude-sonnet-5',          '2000-01-01',  2.00, 0.20,   2.50, 4.00, 10.00, 'anthropic pricing 2026-09-25'),
    ('claude-sonnet-4-6',        '2000-01-01',  3.00, 0.30,   3.75, 6.00, 15.00, 'anthropic pricing 2026-09-25'),
    ('claude-fable-5-1',         '2000-01-01', 10.00, 0.25,  12.50, 20.00, 50.00, 'anthropic pricing 2026-09-25'),
    ('claude-fable-5',           '2000-01-01', 10.00, 1.00,  12.50, 20.00, 50.00, 'anthropic pricing 2026-09-25'),
    ('claude-haiku-4-5-20251001','2000-01-01',  1.00, 0.10,   1.25, 2.00,  5.00, 'anthropic pricing 2026-09-25 (Haiku 4.5)')
ON CONFLICT (model, effective_from) DO NOTHING;

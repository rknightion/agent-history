-- Preserve Claude's top-level JSONL record type separately from the semantic message class.
-- Existing byte_offset/source_id already identify physical position within the source file.
-- Nullable for existing rows and for deployed writers that do not supply this column.
ALTER TABLE ah.message ADD COLUMN IF NOT EXISTS raw_record_origin text;

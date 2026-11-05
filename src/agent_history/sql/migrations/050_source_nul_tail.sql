-- A current bounded observation of invalid source bytes, not consumed transcript content.
-- Raw size/offset lag remains authoritative; unresolved or changing tails are never subtracted.
ALTER TABLE ah.source_file ADD COLUMN nul_tail_bytes bigint NOT NULL DEFAULT 0
    CHECK (nul_tail_bytes >= 0);

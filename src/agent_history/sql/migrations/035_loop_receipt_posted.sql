-- wave-notify now writes `<report>.posted` beside a loop report once the receiver accepts it, in
-- place of the retired `.notified` receipt. Widen the receipt kinds; existing rows stay valid and
-- grants are unchanged.
ALTER TABLE ah.loop_receipt
    DROP CONSTRAINT loop_receipt_kind_check,
    ADD CONSTRAINT loop_receipt_kind_check CHECK (kind IN ('notified', 'started', 'posted'));

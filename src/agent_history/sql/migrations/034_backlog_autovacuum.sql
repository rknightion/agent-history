-- Lower the vacuum trigger for frequently updated collector tables.
-- Table-local SET preserves any unrelated storage parameters.
ALTER TABLE ah.backlog_task SET (
    autovacuum_vacuum_threshold = 25,
    autovacuum_vacuum_scale_factor = 0.05
);

ALTER TABLE ah.backlog_done_event SET (
    autovacuum_vacuum_threshold = 25,
    autovacuum_vacuum_scale_factor = 0.05
);

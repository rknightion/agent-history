-- Derived classifier work. A generation identifies each publication so consuming an older
-- snapshot cannot erase a late source/I/O change. Session cascade also clears rebuild state.
CREATE SEQUENCE ah.error_classification_generation;
CREATE TABLE ah.error_classification_pending (
    row_type smallint NOT NULL CHECK (row_type IN (1, 2)),
    row_id bigint NOT NULL,
    session_id bigint NOT NULL REFERENCES ah.session(id) ON DELETE CASCADE,
    generation bigint NOT NULL DEFAULT nextval('ah.error_classification_generation'),
    PRIMARY KEY (row_type, row_id)
);
CREATE INDEX error_classification_pending_session_idx ON ah.error_classification_pending(session_id);

CREATE FUNCTION ah.queue_error_classification(typ smallint, rid bigint, sid bigint) RETURNS void
LANGUAGE plpgsql AS $$
BEGIN
    INSERT INTO ah.error_classification_pending(row_type,row_id,session_id)
    SELECT typ,rid,id FROM ah.session WHERE id=sid
    ON CONFLICT (row_type,row_id) DO UPDATE SET
        session_id=EXCLUDED.session_id,generation=EXCLUDED.generation;
END $$;

CREATE FUNCTION ah.error_cell_id(body text) RETURNS text
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    SELECT substring(left(body,80) FROM '^Script running with cell ID ([^[:space:]]+)')
$$;

CREATE FUNCTION ah.queue_error_waits(sid bigint) RETURNS void
LANGUAGE plpgsql AS $$
BEGIN
    -- A cell owner can arrive after its wait, or be renamed/deleted. Reclassify waits only,
    -- not every successful exec in the session. Rows in other sessions never own this cell.
    INSERT INTO ah.error_classification_pending(row_type,row_id,session_id)
    SELECT 1,id,session_id FROM ah.tool_call
    WHERE session_id=sid AND agent='codex' AND tool_name='wait'
    ON CONFLICT (row_type,row_id) DO UPDATE SET generation=EXCLUDED.generation;
END $$;

CREATE FUNCTION ah.error_tool_changed() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    typ smallint := TG_ARGV[0]::smallint;
    changed boolean;
BEGIN
    IF TG_OP='UPDATE' THEN
        IF typ=1 THEN
            changed := (OLD.agent,OLD.call_uid,OLD.session_id,OLD.tool_name,OLD.denial_kind,
                        OLD.timed_out,OLD.interrupted,OLD.exit_code,OLD.is_error,OLD.outcome)
                       IS DISTINCT FROM
                       (NEW.agent,NEW.call_uid,NEW.session_id,NEW.tool_name,NEW.denial_kind,
                        NEW.timed_out,NEW.interrupted,NEW.exit_code,NEW.is_error,NEW.outcome);
        ELSE
            changed := (OLD.agent,OLD.item_uid,OLD.session_id,OLD.status,OLD.exit_code,OLD.is_error)
                       IS DISTINCT FROM
                       (NEW.agent,NEW.item_uid,NEW.session_id,NEW.status,NEW.exit_code,NEW.is_error);
        END IF;
        IF NOT changed THEN RETURN NULL; END IF;
    END IF;
    IF TG_OP='DELETE' THEN
        DELETE FROM ah.error_classification_pending WHERE row_type=typ AND row_id=OLD.id;
    ELSE
        PERFORM ah.queue_error_classification(typ,NEW.id,NEW.session_id);
    END IF;
    IF typ=1 THEN
        IF TG_OP='UPDATE' THEN
            IF (OLD.agent,OLD.call_uid,OLD.session_id,OLD.tool_name) IS NOT DISTINCT FROM
               (NEW.agent,NEW.call_uid,NEW.session_id,NEW.tool_name) THEN RETURN NULL; END IF;
        END IF;
        IF TG_OP<>'INSERT' AND OLD.agent='codex' AND OLD.tool_name IN ('exec','wait')
           AND EXISTS (SELECT 1 FROM ah.tool_io WHERE agent=OLD.agent AND io_uid=OLD.call_uid
                       AND ah.error_cell_id(output_text) IS NOT NULL) THEN
            PERFORM ah.queue_error_waits(OLD.session_id);
        END IF;
        IF TG_OP<>'DELETE' AND NEW.agent='codex' AND NEW.tool_name IN ('exec','wait')
           AND EXISTS (SELECT 1 FROM ah.tool_io WHERE agent=NEW.agent AND io_uid=NEW.call_uid
                       AND ah.error_cell_id(output_text) IS NOT NULL) THEN
            PERFORM ah.queue_error_waits(NEW.session_id);
        END IF;
    END IF;
    RETURN NULL;
END $$;

CREATE TRIGGER tool_call_error_changed
AFTER INSERT OR DELETE OR UPDATE OF agent,call_uid,session_id,tool_name,denial_kind,timed_out,
    interrupted,exit_code,is_error,outcome ON ah.tool_call
FOR EACH ROW EXECUTE FUNCTION ah.error_tool_changed('1');
CREATE TRIGGER tool_op_error_changed
AFTER INSERT OR DELETE OR UPDATE OF agent,item_uid,session_id,status,exit_code,is_error ON ah.tool_op
FOR EACH ROW EXECUTE FUNCTION ah.error_tool_changed('2');

CREATE FUNCTION ah.queue_error_io(io_agent text,uid text,cell_changed boolean) RETURNS void
LANGUAGE plpgsql AS $$
DECLARE target record;
BEGIN
    FOR target IN SELECT id,session_id,tool_name FROM ah.tool_call
                  WHERE agent=io_agent AND call_uid=uid LOOP
        PERFORM ah.queue_error_classification(1::smallint,target.id,target.session_id);
        IF cell_changed AND io_agent='codex' AND target.tool_name IN ('exec','wait') THEN
            PERFORM ah.queue_error_waits(target.session_id);
        END IF;
    END LOOP;
    FOR target IN SELECT id,session_id FROM ah.tool_op
                  WHERE agent=io_agent AND item_uid=substring(uid FROM 6) AND starts_with(uid,'item:') LOOP
        PERFORM ah.queue_error_classification(2::smallint,target.id,target.session_id);
    END LOOP;
END $$;

CREATE FUNCTION ah.error_io_changed() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE cell_changed boolean := true;
BEGIN
    IF TG_OP='UPDATE' THEN
        IF (OLD.agent,OLD.io_uid,OLD.output_text,OLD.stderr_text) IS NOT DISTINCT FROM
           (NEW.agent,NEW.io_uid,NEW.output_text,NEW.stderr_text) THEN RETURN NULL; END IF;
        cell_changed := (OLD.agent,OLD.io_uid,ah.error_cell_id(OLD.output_text)) IS DISTINCT FROM
                        (NEW.agent,NEW.io_uid,ah.error_cell_id(NEW.output_text));
    END IF;
    IF TG_OP<>'INSERT' THEN
        PERFORM ah.queue_error_io(OLD.agent,OLD.io_uid,cell_changed AND ah.error_cell_id(OLD.output_text) IS NOT NULL);
    END IF;
    IF TG_OP<>'DELETE' THEN
        PERFORM ah.queue_error_io(NEW.agent,NEW.io_uid,cell_changed AND ah.error_cell_id(NEW.output_text) IS NOT NULL);
    END IF;
    RETURN NULL;
END $$;
CREATE TRIGGER tool_io_error_changed
AFTER INSERT OR DELETE OR UPDATE OF agent,io_uid,output_text,stderr_text ON ah.tool_io
FOR EACH ROW EXECUTE FUNCTION ah.error_io_changed();

-- Classifier semantics are unchanged at adoption. Preserve completed historical work and seed
-- only sessions which still owed a post-pass before the triggers were installed.
INSERT INTO ah.error_classification_pending(row_type,row_id,session_id)
SELECT 1,c.id,c.session_id FROM ah.tool_call c JOIN ah.dirty_session d USING(session_id)
UNION ALL
SELECT 2,o.id,o.session_id FROM ah.tool_op o JOIN ah.dirty_session d USING(session_id)
ON CONFLICT DO NOTHING;

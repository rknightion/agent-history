-- Transactional work queue for deterministic source ordering. Derived state is
-- cleared by the session FK during rebuild; retained catalogues derive once lazily.
CREATE TABLE ah.session_sequence_state (
    session_id bigint PRIMARY KEY REFERENCES ah.session(id) ON DELETE CASCADE,
    needs_full boolean NOT NULL DEFAULT true,
    last_seq bigint NOT NULL DEFAULT 0,
    last_path text,
    last_offset bigint,
    last_rank integer,
    last_uid text,
    last_type integer
);
CREATE TABLE ah.session_sequence_pending (
    session_id bigint NOT NULL REFERENCES ah.session(id) ON DELETE CASCADE,
    row_type integer NOT NULL CHECK (row_type BETWEEN 1 AND 4),
    row_id bigint NOT NULL,
    PRIMARY KEY (session_id, row_type, row_id)
);

CREATE FUNCTION ah.invalidate_session_sequence(sid bigint) RETURNS void
LANGUAGE plpgsql AS $$
BEGIN
    -- A session being cascade-deleted no longer needs derived state.
    INSERT INTO ah.session_sequence_state(session_id)
    SELECT id FROM ah.session WHERE id = sid
    ON CONFLICT (session_id) DO UPDATE SET needs_full = true;
    INSERT INTO ah.dirty_session(session_id)
    SELECT id FROM ah.session WHERE id = sid ON CONFLICT DO NOTHING;
END
$$;

CREATE FUNCTION ah.session_sequence_changed() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    typ integer := TG_ARGV[0]::integer;
    changed boolean := true;
    linked_sid bigint;
BEGIN
    IF TG_OP = 'UPDATE' THEN
        changed := (OLD.session_id, OLD.source_id, OLD.byte_offset, OLD.agent)
                   IS DISTINCT FROM (NEW.session_id, NEW.source_id, NEW.byte_offset, NEW.agent);
        IF typ = 1 THEN
            changed := changed OR (OLD.event_uid, OLD.message_class = 'reasoning')
                       IS DISTINCT FROM (NEW.event_uid, NEW.message_class = 'reasoning');
        ELSIF typ = 2 THEN
            changed := changed OR OLD.call_uid IS DISTINCT FROM NEW.call_uid;
        ELSIF typ = 3 THEN
            changed := changed OR OLD.item_uid IS DISTINCT FROM NEW.item_uid;
        ELSE
            changed := changed OR (OLD.io_uid, OLD.kind, OLD.call_uid, OLD.item_uid)
                       IS DISTINCT FROM (NEW.io_uid, NEW.kind, NEW.call_uid, NEW.item_uid);
        END IF;
        IF NOT changed THEN RETURN NULL; END IF;
    END IF;
    IF TG_OP <> 'INSERT' THEN
        PERFORM ah.invalidate_session_sequence(OLD.session_id);
    END IF;
    IF TG_OP <> 'DELETE' THEN
        INSERT INTO ah.session_sequence_state(session_id) VALUES (NEW.session_id)
        ON CONFLICT DO NOTHING;
        -- Serialise queue publication with ordering and clearing its snapshot.
        PERFORM 1 FROM ah.session_sequence_state WHERE session_id = NEW.session_id FOR UPDATE;
        IF TG_OP = 'UPDATE' THEN
            PERFORM ah.invalidate_session_sequence(NEW.session_id);
        END IF;
        INSERT INTO ah.session_sequence_pending VALUES (NEW.session_id, typ, NEW.id)
        ON CONFLICT DO NOTHING;
        INSERT INTO ah.dirty_session VALUES (NEW.session_id) ON CONFLICT DO NOTHING;
    END IF;
    -- A newly resolved orphan stops consuming its own sequence position. Likewise,
    -- removing or changing the target gives the I/O row its own position again.
    IF typ = 2 THEN
        FOR linked_sid IN
            SELECT DISTINCT i.session_id FROM ah.tool_io i
            WHERE i.kind = 'call' AND i.seq IS NOT NULL AND
                ((TG_OP <> 'INSERT' AND i.agent = OLD.agent AND i.call_uid = OLD.call_uid)
                 OR (TG_OP <> 'DELETE' AND i.agent = NEW.agent AND i.call_uid = NEW.call_uid))
        LOOP
            PERFORM ah.invalidate_session_sequence(linked_sid);
        END LOOP;
    ELSIF typ = 3 THEN
        FOR linked_sid IN
            SELECT DISTINCT i.session_id FROM ah.tool_io i
            WHERE i.kind = 'op' AND i.seq IS NOT NULL AND
                ((TG_OP <> 'INSERT' AND i.agent = OLD.agent AND i.item_uid = OLD.item_uid)
                 OR (TG_OP <> 'DELETE' AND i.agent = NEW.agent AND i.item_uid = NEW.item_uid))
        LOOP
            PERFORM ah.invalidate_session_sequence(linked_sid);
        END LOOP;
    END IF;
    RETURN NULL;
END
$$;

CREATE TRIGGER message_sequence_changed
AFTER INSERT OR DELETE OR UPDATE OF session_id, source_id, byte_offset, agent, event_uid, message_class ON ah.message
FOR EACH ROW EXECUTE FUNCTION ah.session_sequence_changed('1');
CREATE TRIGGER tool_call_sequence_changed
AFTER INSERT OR DELETE OR UPDATE OF session_id, source_id, byte_offset, agent, call_uid ON ah.tool_call
FOR EACH ROW EXECUTE FUNCTION ah.session_sequence_changed('2');
CREATE TRIGGER tool_op_sequence_changed
AFTER INSERT OR DELETE OR UPDATE OF session_id, source_id, byte_offset, agent, item_uid ON ah.tool_op
FOR EACH ROW EXECUTE FUNCTION ah.session_sequence_changed('3');
CREATE TRIGGER tool_io_sequence_changed
AFTER INSERT OR DELETE OR UPDATE OF session_id, source_id, byte_offset, agent, io_uid, kind, call_uid, item_uid ON ah.tool_io
FOR EACH ROW EXECUTE FUNCTION ah.session_sequence_changed('4');

CREATE FUNCTION ah.source_sequence_changed() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE sid bigint;
BEGIN
    IF OLD.rel_path IS NOT DISTINCT FROM NEW.rel_path THEN RETURN NULL; END IF;
    FOR sid IN
        SELECT session_id FROM ah.message WHERE source_id = NEW.id
        UNION SELECT session_id FROM ah.tool_call WHERE source_id = NEW.id
        UNION SELECT session_id FROM ah.tool_op WHERE source_id = NEW.id
        UNION SELECT session_id FROM ah.tool_io WHERE source_id = NEW.id
    LOOP
        PERFORM ah.invalidate_session_sequence(sid);
    END LOOP;
    RETURN NULL;
END
$$;
CREATE TRIGGER source_sequence_changed AFTER UPDATE OF rel_path ON ah.source_file
FOR EACH ROW EXECUTE FUNCTION ah.source_sequence_changed();

CREATE FUNCTION ah.session_sequence_truncated() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    UPDATE ah.session_sequence_state SET needs_full = true WHERE NOT needs_full;
    INSERT INTO ah.dirty_session SELECT session_id FROM ah.session_sequence_state ON CONFLICT DO NOTHING;
    RETURN NULL;
END
$$;
CREATE TRIGGER message_sequence_truncated AFTER TRUNCATE ON ah.message
FOR EACH STATEMENT EXECUTE FUNCTION ah.session_sequence_truncated();
CREATE TRIGGER tool_call_sequence_truncated AFTER TRUNCATE ON ah.tool_call
FOR EACH STATEMENT EXECUTE FUNCTION ah.session_sequence_truncated();
CREATE TRIGGER tool_op_sequence_truncated AFTER TRUNCATE ON ah.tool_op
FOR EACH STATEMENT EXECUTE FUNCTION ah.session_sequence_truncated();
CREATE TRIGGER tool_io_sequence_truncated AFTER TRUNCATE ON ah.tool_io
FOR EACH STATEMENT EXECUTE FUNCTION ah.session_sequence_truncated();

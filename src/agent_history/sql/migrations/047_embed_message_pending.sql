-- A derived per-model work queue avoids re-reading covered message history on an empty batch.
-- Model registration seeds existing rows once; row triggers keep inserts, edits and chunk resets
-- visible. Rebuild truncates the queue through its message foreign key and new rows refill it.
SET search_path = ah, public;

CREATE TABLE ah.embed_message_model (
    model text PRIMARY KEY
);
CREATE TABLE ah.embed_message_pending (
    model text NOT NULL REFERENCES ah.embed_message_model(model) ON DELETE CASCADE,
    message_id bigint NOT NULL REFERENCES ah.message(id) ON DELETE CASCADE,
    PRIMARY KEY (model, message_id)
);
CREATE INDEX embed_message_pending_message_idx ON ah.embed_message_pending (message_id);

CREATE FUNCTION ah.embed_message_eligible(message_class text, body text) RETURNS boolean
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    SELECT message_class IN ('human_prompt', 'queued_prompt', 'assistant_text',
                             'subagent_brief', 'subagent_report', 'compaction_summary')
       AND (octet_length(body) >= 120 OR length(body) >= 30)
$$;

CREATE FUNCTION ah.sync_embed_message_pending(mid bigint) RETURNS void
LANGUAGE plpgsql AS $$
DECLARE
    m ah.message%ROWTYPE;
BEGIN
    -- Serialize chunk completion with a concurrent message edit. Always lock the parent before
    -- touching queue rows, and read chunk coverage after obtaining that lock.
    SELECT * INTO m FROM ah.message WHERE id = mid FOR UPDATE;
    IF NOT FOUND THEN
        RETURN;
    END IF;
    DELETE FROM ah.embed_message_pending p WHERE p.message_id = mid;
    IF ah.embed_message_eligible(m.message_class, m.text) THEN
        INSERT INTO ah.embed_message_pending (model, message_id)
        SELECT models.model, mid FROM ah.embed_message_model models
        WHERE NOT EXISTS (
            SELECT 1 FROM ah.chunk c WHERE c.message_id = mid AND c.model = models.model
              AND c.source_sha256 = m.content_sha256)
        ON CONFLICT DO NOTHING;
    END IF;
END
$$;

CREATE FUNCTION ah.message_embed_pending_changed() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    PERFORM ah.sync_embed_message_pending(NEW.id);
    RETURN NULL;
END
$$;
CREATE TRIGGER message_embed_pending_insert AFTER INSERT ON ah.message
FOR EACH ROW EXECUTE FUNCTION ah.message_embed_pending_changed();
CREATE TRIGGER message_embed_pending_update AFTER UPDATE OF message_class, text, content_sha256 ON ah.message
FOR EACH ROW WHEN (OLD.message_class IS DISTINCT FROM NEW.message_class
                  OR OLD.text IS DISTINCT FROM NEW.text
                  OR OLD.content_sha256 IS DISTINCT FROM NEW.content_sha256)
EXECUTE FUNCTION ah.message_embed_pending_changed();

CREATE FUNCTION ah.chunk_embed_pending_changed() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP IN ('DELETE', 'UPDATE') AND OLD.message_id IS NOT NULL THEN
        PERFORM ah.sync_embed_message_pending(OLD.message_id);
    END IF;
    IF TG_OP IN ('INSERT', 'UPDATE') AND NEW.message_id IS NOT NULL THEN
        PERFORM ah.sync_embed_message_pending(NEW.message_id);
    END IF;
    RETURN NULL;
END
$$;
CREATE TRIGGER chunk_embed_pending_changed
AFTER INSERT OR DELETE OR UPDATE OF message_id, model, source_sha256 ON ah.chunk
FOR EACH ROW EXECUTE FUNCTION ah.chunk_embed_pending_changed();

CREATE FUNCTION ah.chunk_embed_pending_truncated() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    INSERT INTO ah.embed_message_pending (model, message_id)
    SELECT models.model, m.id FROM ah.embed_message_model models CROSS JOIN ah.message m
    WHERE ah.embed_message_eligible(m.message_class, m.text)
    ON CONFLICT DO NOTHING;
    RETURN NULL;
END
$$;
CREATE TRIGGER chunk_embed_pending_truncated AFTER TRUNCATE ON ah.chunk
FOR EACH STATEMENT EXECUTE FUNCTION ah.chunk_embed_pending_truncated();

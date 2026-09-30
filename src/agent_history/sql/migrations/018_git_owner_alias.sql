-- Compatibility: old installations continue reading and writing author_is_rob.
ALTER TABLE ah.git_commit ADD COLUMN IF NOT EXISTS author_is_owner boolean;
UPDATE ah.git_commit SET author_is_owner = author_is_rob
WHERE author_is_owner IS DISTINCT FROM author_is_rob;
CREATE OR REPLACE FUNCTION ah.sync_git_commit_owner() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NEW.author_is_owner IS NOT NULL AND NEW.author_is_rob IS NOT NULL
           AND NEW.author_is_owner IS DISTINCT FROM NEW.author_is_rob THEN
            RAISE EXCEPTION 'conflicting owner flags';
        END IF;
        NEW.author_is_owner := COALESCE(NEW.author_is_owner, NEW.author_is_rob);
        NEW.author_is_rob := NEW.author_is_owner;
    ELSIF NEW.author_is_owner IS DISTINCT FROM OLD.author_is_owner THEN
        NEW.author_is_rob := NEW.author_is_owner;
    ELSIF NEW.author_is_rob IS DISTINCT FROM OLD.author_is_rob THEN
        NEW.author_is_owner := NEW.author_is_rob;
    END IF;
    RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS git_commit_owner_sync ON ah.git_commit;
CREATE TRIGGER git_commit_owner_sync BEFORE INSERT OR UPDATE ON ah.git_commit
FOR EACH ROW EXECUTE FUNCTION ah.sync_git_commit_owner();

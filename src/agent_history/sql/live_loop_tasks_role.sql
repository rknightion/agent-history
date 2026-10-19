-- The narrow consumer role for ah.live_loop_tasks(text). It may connect, name schema `ah` and
-- execute that one function, which runs as its owner, for the repositories it is scoped to; it holds
-- no privilege on any table, view or sequence, so it cannot read a transcript. Run as a superuser in
-- the catalogue database after roles.sql. Re-running it is harmless.
--
--   psql -X -v ON_ERROR_STOP=1 -d agent_history -v repo=owner/repo -f live_loop_tasks_role.sql
--   psql -X -d agent_history -c '\password ah_loop_tasks'
--
-- -v repo=owner/repo adds that repository to the role's scope (ah.live_loop_tasks_scope, created by
-- migration 043, so the indexer must have applied it first); run once per repository. Without
-- -v repo the script grants no new scope and may run before or after migration 043.
-- The role is created without a password: set one as above, or admit it through pg_hba another way.
-- The final block fails if the role can reach any relation or column in `ah`, for example through a
-- grant to PUBLIC, so the script never leaves a wider role in place silently.

\if :{?repo}
SELECT set_config('ah_loop_tasks.repo', :'repo', false) AS ah_loop_tasks_repo \gset
\endif

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ah_loop_tasks') THEN
        CREATE ROLE ah_loop_tasks LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
    END IF;
END
$$;

ALTER ROLE ah_loop_tasks SET default_transaction_read_only = on;
ALTER ROLE ah_loop_tasks SET statement_timeout = '30s';

DO $$
BEGIN
    EXECUTE format('GRANT CONNECT ON DATABASE %I TO ah_loop_tasks', current_database());
END
$$;
GRANT USAGE ON SCHEMA ah TO ah_loop_tasks;

DO $$
BEGIN
    IF to_regprocedure('ah.live_loop_tasks(text)') IS NOT NULL THEN
        GRANT EXECUTE ON FUNCTION ah.live_loop_tasks(text) TO ah_loop_tasks;
    END IF;
END
$$;

DO $$
DECLARE
    repo text := lower(COALESCE(current_setting('ah_loop_tasks.repo', true), ''));
BEGIN
    IF repo = '' THEN
        RETURN;
    END IF;
    IF repo !~ '^[a-z0-9_.-]+/[a-z0-9_.-]+$' THEN
        RAISE EXCEPTION 'repo must be an owner/repo slug';
    END IF;
    IF to_regclass('ah.live_loop_tasks_scope') IS NULL THEN
        RAISE EXCEPTION 'apply migration 043 (agent-history index or init) before scoping a repository';
    END IF;
    INSERT INTO ah.live_loop_tasks_scope (role_name, repo) VALUES ('ah_loop_tasks', repo)
    ON CONFLICT DO NOTHING;
END
$$;

DO $$
DECLARE
    reachable text;
BEGIN
    SELECT string_agg(c.relname, ', ' ORDER BY c.relname) INTO reachable
    FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'ah'
      AND ((c.relkind IN ('r', 'v', 'm', 'p', 'f')
            AND (has_table_privilege('ah_loop_tasks', c.oid, 'SELECT, INSERT, UPDATE, DELETE, TRUNCATE')
                 OR has_any_column_privilege('ah_loop_tasks', c.oid, 'SELECT, INSERT, UPDATE')))
           OR (c.relkind = 'S' AND has_sequence_privilege('ah_loop_tasks', c.oid, 'USAGE, SELECT, UPDATE')));
    IF reachable IS NOT NULL THEN
        RAISE EXCEPTION 'ah_loop_tasks can reach ah relations directly: %', reachable;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_auth_members WHERE member = 'ah_loop_tasks'::regrole) THEN
        RAISE EXCEPTION 'ah_loop_tasks must not be a member of another role';
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ah_loop_tasks'
               AND (rolsuper OR rolcreaterole OR rolcreatedb OR rolreplication OR rolbypassrls)) THEN
        RAISE EXCEPTION 'ah_loop_tasks must hold no administrative attribute';
    END IF;
END
$$;

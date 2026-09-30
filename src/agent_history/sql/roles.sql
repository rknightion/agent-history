-- Writer and reader roles for one agent-history database. Run once as a superuser, in the target
-- database, with psql variables for the passwords:
--
--   psql -d agent_history -v writer_password="$AH_WRITER_PASSWORD" -v reader_password="$AH_READER_PASSWORD" \
--        -f roles.sql
--
-- ah_writer owns schema `ah` and runs the indexer (`agent-history init`, `index`, ...).
-- ah_reader is what the MCP server and read-only clients use: SELECT only, every transaction
-- read-only by default, a statement timeout. The MCP server refuses any role that could write.

\set ON_ERROR_STOP on

SELECT format('CREATE ROLE ah_writer LOGIN PASSWORD %L', :'writer_password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ah_writer') \gexec
SELECT format('CREATE ROLE ah_reader LOGIN PASSWORD %L', :'reader_password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ah_reader') \gexec

ALTER ROLE ah_reader SET default_transaction_read_only = on;
ALTER ROLE ah_reader SET statement_timeout = '60s';

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_search;
CREATE SCHEMA IF NOT EXISTS ah AUTHORIZATION ah_writer;
ALTER SCHEMA ah OWNER TO ah_writer;
GRANT USAGE ON SCHEMA ah TO ah_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA ah TO ah_reader;
ALTER DEFAULT PRIVILEGES FOR ROLE ah_writer IN SCHEMA ah GRANT SELECT ON TABLES TO ah_reader;
-- The BM25 search functions read ParadeDB's own schema.
GRANT USAGE ON SCHEMA paradedb TO ah_reader;

#!/bin/sh
# First start of the ParadeDB container only (docker-entrypoint-initdb.d): create ah_writer and
# ah_reader in $POSTGRES_DB with the passwords from the environment.
set -eu
: "${AH_WRITER_PASSWORD:?set AH_WRITER_PASSWORD}"
: "${AH_READER_PASSWORD:?set AH_READER_PASSWORD}"
psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
    -v writer_password="$AH_WRITER_PASSWORD" -v reader_password="$AH_READER_PASSWORD" \
    -f /agent-history/roles.sql

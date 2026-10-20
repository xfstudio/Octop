#!/usr/bin/env bash
set -euo pipefail

# This runs only for a fresh PostgreSQL data directory. Credentials stay in
# environment/psql variables; SQL literal quoting handles arbitrary passwords.
if [ -n "${OCTOP_DATABASE_PASSWORD_FILE:-}" ]; then
    OCTOP_DATABASE_PASSWORD="$(cat "$OCTOP_DATABASE_PASSWORD_FILE")"
    export OCTOP_DATABASE_PASSWORD
fi
: "${OCTOP_DATABASE_PASSWORD:?OCTOP_DATABASE_PASSWORD must be set}"
psql --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" --set=ON_ERROR_STOP=1 <<'SQL'
\getenv app_password OCTOP_DATABASE_PASSWORD
CREATE ROLE octop WITH LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOREPLICATION PASSWORD :'app_password';
GRANT CONNECT ON DATABASE octop TO octop;
GRANT USAGE, CREATE ON SCHEMA public TO octop;
SQL

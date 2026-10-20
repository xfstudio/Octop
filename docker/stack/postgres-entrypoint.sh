#!/usr/bin/env bash
set -euo pipefail
# Read the application role secret before upstream drops to the postgres user.
export OCTOP_DATABASE_PASSWORD="$(cat /run/stack-secrets/database-password)"
exec /usr/local/bin/docker-entrypoint.sh "$@"

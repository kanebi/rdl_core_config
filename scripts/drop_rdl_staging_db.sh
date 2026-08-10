#!/usr/bin/env bash
# Drop rdl_staging (or named DB) — terminates sessions first.
set -euo pipefail
DB="${1:-rdl_staging}"
PGUSER="${PGUSER:-kane}"
PGPASSWORD="${PGPASSWORD:-kane24}"
export PGPASSWORD

if psql -h localhost -U "$PGUSER" -d postgres -At -c \
    "SELECT 1 FROM pg_database WHERE datname='${DB}'" | grep -q 1; then
  echo "Terminating sessions on ${DB}..."
  psql -h localhost -U "$PGUSER" -d postgres -c \
    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '${DB}' AND pid <> pg_backend_pid();" \
    || true
  sleep 1
  dropdb -h localhost -U "$PGUSER" "$DB"
  echo "Dropped database ${DB}"
else
  echo "Database ${DB} does not exist — nothing to drop"
fi

#!/usr/bin/env bash
# Full pipeline: export modules → create rdl_staging → migrate data from rdl_staging_dev
set -eu
cd /home/kane/odoo-18
source odoo-18env/bin/activate

DB="${1:-rdl_staging}"
SOURCE="${RDL_SOURCE_DB:-rdl_staging_dev}"
SCRIPTS="extra-addons/rdl_core_config/scripts"
PIPELINE_LOG="${RDL_PIPELINE_LOG:-/tmp/migration_run.log}"
PGUSER="${PGUSER:-kane}"
PGPASSWORD="${PGPASSWORD:-kane24}"
export PGPASSWORD

drop_db_if_exists() {
  local db=$1
  if psql -h localhost -U "$PGUSER" -d postgres -At -c \
      "SELECT 1 FROM pg_database WHERE datname='${db}'" | grep -q 1; then
    echo "--- Terminating sessions on $db ---"
    psql -h localhost -U "$PGUSER" -d postgres -c \
      "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '${db}' AND pid <> pg_backend_pid();" \
      || true
    sleep 1
    dropdb -h localhost -U "$PGUSER" --if-exists "$db"
  fi
}

pipeline() {
  echo "=== RDL staging pipeline: $SOURCE → $DB ==="

  echo "--- Upgrade rdl_core_config on source ---"
  ./odoo-source/odoo-bin -c odoo.conf -d "$SOURCE" -u rdl_core_config --stop-after-init

  if [[ "${RDL_FRESH:-1}" == "1" ]]; then
    echo "--- Dropping existing $DB for clean migration ---"
    drop_db_if_exists "$DB"
  fi

  bash "$SCRIPTS/export_source_modules.sh"
  bash "$SCRIPTS/setup_rdl_staging_db.sh" "$DB"

  echo "--- Running Odoo shell migration ---"
  set +e
  RDL_SOURCE_DB="$SOURCE" ./odoo-source/odoo-bin shell -d "$DB" -c odoo.conf \
    < "$SCRIPTS/migrate_rdl_staging.py"
  local mig_exit=$?
  set -e

  echo "--- Running DB comparison ---"
  bash "$SCRIPTS/compare_staging_dbs.sh" "$SOURCE" "$DB" || true

  echo "--- Done migration exit=${mig_exit}. Log: $SCRIPTS/migration_log.json ==="
  return "${mig_exit}"
}

set +e
pipeline 2>&1 | tee "${PIPELINE_LOG}"
PIPELINE_EXIT=${PIPESTATUS[0]}
set -e

echo "PIPELINE_EXIT=${PIPELINE_EXIT}" | tee -a "${PIPELINE_LOG}"
exit "${PIPELINE_EXIT}"

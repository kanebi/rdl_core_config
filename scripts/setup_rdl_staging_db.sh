#!/usr/bin/env bash
# Create fresh rdl_staging database and install RDL module stack — no demo data.
# Note: do not use pipefail with odoo | tee — docutils stderr can abort the pipeline.
set -eu
cd /home/kane/odoo-18
source odoo-18env/bin/activate

DB="${1:-rdl_staging}"
CONF="${ODOO_CONF:-/home/kane/odoo-18/odoo.conf}"
ODOO="./odoo-source/odoo-bin"
LOG="extra-addons/rdl_core_config/scripts/setup_${DB}.log"
MODULES_FILE="extra-addons/rdl_core_config/scripts/rdl_staging_dev_modules.txt"
DEMO_SKIP="theme_default,mass_mailing_themes,website_sale_comparison"

: > "${LOG}"

log() {
  echo "$@" | tee -a "${LOG}"
}

run_odoo_optional() {
  log "--- odoo optional $* ---"
  set +e
  "${ODOO}" -c "${CONF}" -d "${DB}" --without-demo=all --stop-after-init "$@" 2>&1 | tee -a "${LOG}"
  local rc=${PIPESTATUS[0]}
  set -e
  if [[ "${rc}" -ne 0 ]]; then
    log "WARN: optional odoo batch exited ${rc} for: $*"
  fi
}

run_odoo() {
  log "--- odoo $* ---"
  set +e
  "${ODOO}" -c "${CONF}" -d "${DB}" --without-demo=all --stop-after-init "$@" 2>&1 | tee -a "${LOG}"
  local rc=${PIPESTATUS[0]}
  set -e
  if [[ "${rc}" -ne 0 ]]; then
    log "ERROR: odoo exited ${rc} for: $*"
    exit "${rc}"
  fi
}

log "=== Setup ${DB} no-demo $(date -Iseconds) ==="

if psql -d postgres -At -c "SELECT 1 FROM pg_database WHERE datname='${DB}'" | grep -q 1; then
  log "Database ${DB} already exists — skipping createdb"
else
  createdb "${DB}"
  log "Created database ${DB}"
fi

run_odoo
run_odoo -i l10n_ng

# Mark NG chart loaded so later module installs do not reload duplicate accounts
log "--- Stamp company chart_template ng ---"
./odoo-source/odoo-bin shell -d "${DB}" -c "${CONF}" --stop-after-init <<'PYEOF'
for company in env['res.company'].search([]):
    n = env['account.account'].search_count([('company_ids', 'in', company.id)])
    if n and not company.chart_template:
        company.chart_template = 'ng'
env.cr.commit()
PYEOF

run_odoo -i uom,stock,stock_account,purchase,sale_management,account,point_of_sale
run_odoo -i rdl_core_config,pos_seerbit

if [[ -f "${MODULES_FILE}" ]] && [[ -s "${MODULES_FILE}" ]]; then
  log "--- Install remaining modules from source list no-demo ---"
  BATCH=""
  BATCH_COUNT=0
  while IFS= read -r mod || [[ -n "${mod}" ]]; do
    [[ -z "${mod}" ]] && continue
    [[ ",${DEMO_SKIP}," == *",${mod},"* ]] && continue
    if [[ -n "${BATCH}" ]]; then
      BATCH="${BATCH},${mod}"
    else
      BATCH="${mod}"
    fi
    BATCH_COUNT=$((BATCH_COUNT + 1))
    if [[ "${BATCH_COUNT}" -ge 20 ]]; then
      run_odoo_optional -i "${BATCH}"
      BATCH=""
      BATCH_COUNT=0
    fi
  done < "${MODULES_FILE}"
  if [[ -n "${BATCH}" ]]; then
    run_odoo_optional -i "${BATCH}"
  fi
else
  log "Run export_source_modules.sh first to mirror all source modules optional."
fi

run_odoo -u rdl_core_config

log "--- Done. Run migration: ---"
log "  ${ODOO} shell -d ${DB} -c ${CONF} < extra-addons/rdl_core_config/scripts/migrate_rdl_staging.py"
log "=== Setup complete $(date -Iseconds) ==="

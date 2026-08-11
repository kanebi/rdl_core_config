#!/usr/bin/env bash
# Create fresh rdl_live and migrate config from rdl_staging_dev (NOT a clone).
set -euo pipefail

ODOO_ROOT="${ODOO_ROOT:-/home/kane/odoo-18}"
SCRIPT_DIR="${ODOO_ROOT}/extra-addons/rdl_core_config/scripts"
SOURCE_DB="${SOURCE_DB:-rdl_staging_dev}"
TARGET_DB="${TARGET_DB:-rdl_live}"
VENV="${ODOO_VENV:-${ODOO_ROOT}/odoo-18env}"

export SOURCE_DB TARGET_DB ODOO_ROOT

if [[ -f "${VENV}/bin/activate" ]]; then
  # shellcheck disable=SC1091
  source "${VENV}/bin/activate"
  echo "Using venv: ${VENV}"
else
  echo "WARNING: venv not found at ${VENV} — set ODOO_VENV to your activate path" >&2
fi

ADDONS_PATH="${ODOO_ROOT}/odoo-source/odoo/addons,${ODOO_ROOT}/odoo-source/addons,${ODOO_ROOT}/labule-addons,${ODOO_ROOT}/extra-addons"
export VENV_PYTHON="${VENV}/bin/python3"

cd "$SCRIPT_DIR"

if [[ "${1:-}" == "--dry-run" ]]; then
  "${VENV_PYTHON}" create_rdl_live.py --dry-run
elif [[ "${1:-}" == "--recreate" ]]; then
  "${VENV_PYTHON}" create_rdl_live.py --recreate
elif [[ "${1:-}" == "--migrate-only" ]]; then
  "${VENV_PYTHON}" create_rdl_live.py --migrate-only
elif [[ "${1:-}" == "--resume-install" ]]; then
  "${VENV_PYTHON}" create_rdl_live.py --resume-install
elif [[ "${1:-}" == "--fix-labels-and-teams" ]]; then
  "${VENV_PYTHON}" fix_rdl_live_labels_and_teams.py
else
  "${VENV_PYTHON}" create_rdl_live.py "$@"
fi

if [[ "${1:-}" != "--dry-run" && "${SKIP_UPGRADE:-}" != "1" ]]; then
  echo "Upgrading rdl_core_config on ${TARGET_DB}..."
  PYTHONPATH="${ODOO_ROOT}/odoo-source" PYTHONNOUSERSITE=1 \
    "${VENV_PYTHON}" "${ODOO_ROOT}/odoo-source/odoo-bin" \
    -c "${ODOO_ROOT}/odoo.conf" \
    --addons-path="${ADDONS_PATH}" \
    -d "$TARGET_DB" -u rdl_core_config --stop-after-init --no-http
fi

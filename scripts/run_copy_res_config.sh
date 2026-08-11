#!/usr/bin/env bash
# Copy Seerbit + company settings from source DB into rdl_staging.
set -euo pipefail

ODOO_ROOT="${ODOO_ROOT:-/home/kane/odoo-18}"
SCRIPT="${ODOO_ROOT}/extra-addons/rdl_core_config/scripts/copy_res_config_to_staging.py"
SOURCE_DB="${SOURCE_DB:-braw-live}"
TARGET_DB="${TARGET_DB:-rdl_staging}"

cd "$ODOO_ROOT"

if [[ "${1:-}" == "--dry-run" ]]; then
  python3 "$SCRIPT" --source "$SOURCE_DB" --target "$TARGET_DB" --dry-run
else
  python3 "$SCRIPT" --source "$SOURCE_DB" --target "$TARGET_DB"
fi

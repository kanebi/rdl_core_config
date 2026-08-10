#!/usr/bin/env bash
# Dump installed module list from rdl_staging_dev for setup_rdl_staging_db.sh
set -euo pipefail
PGPASSWORD="${PGPASSWORD:-kane24}" psql -h localhost -U kane -d rdl_staging_dev -At -c \
  "SELECT name FROM ir_module_module WHERE state='installed' AND name NOT IN ('base') ORDER BY name;" \
  > /home/kane/odoo-18/extra-addons/rdl_core_config/scripts/rdl_staging_dev_modules.txt
echo "Wrote $(wc -l < /home/kane/odoo-18/extra-addons/rdl_core_config/scripts/rdl_staging_dev_modules.txt) modules"

#!/usr/bin/env bash
# Re-apply consolidated empties qty on existing rdl_staging without full migration.
set -eu
cd /home/kane/odoo-18
source odoo-18env/bin/activate
RDL_REAPPLY_EMPTIES_ONLY=1 RDL_SOURCE_DB="${RDL_SOURCE_DB:-rdl_staging_dev}" \
  ./odoo-source/odoo-bin shell -d rdl_staging -c odoo.conf \
  < extra-addons/rdl_core_config/scripts/migrate_rdl_staging.py

python3 -c "import json; d=json.load(open('extra-addons/rdl_core_config/scripts/migration_log.json')); print('Empties qty phase:', d.get('phases',{}).get('01-Consolidated Empties qty'))"

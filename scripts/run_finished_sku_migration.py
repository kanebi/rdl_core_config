# -*- coding: utf-8 -*-
"""
Manual valuation-safe finished-SKU migration for rdl_staging_dev / production.

Prerequisites (from DB inspection):
  1. Backup the database
  2. Close open POS sessions (e.g. Van POS Profile in opening_control)
  3. Clear draft inventory adjustments (inventory_quantity_set quants)

Run:

  cd /home/kane/odoo-18
  source odoo-18env/bin/activate
  ./odoo-bin shell -d rdl_staging_dev < extra-addons/rdl_core_config/scripts/run_finished_sku_migration.py

If staging table is missing (upgrade already ran without snapshot), rebuild it:

  From shell, only if still on old columns — otherwise restore DB backup and
  upgrade again so pre-migrate can snapshot.

Force past preflight (NOT recommended):

  FORCE=1 ./odoo-bin shell -d rdl_staging_dev < ...
"""
import os

from odoo.addons.rdl_core_config.migrate_finished_sku import (
    STAGING_TABLE,
    run_finished_sku_migration,
    _table_exists,
)

force = os.environ.get('FORCE') == '1'
if not _table_exists(env.cr, STAGING_TABLE):
    raise SystemExit(
        "Staging table %s not found. Re-upgrade from 18.0.1.0.0 so "
        "pre-migrate can snapshot component links/SVL, or restore backup."
        % STAGING_TABLE
    )

result = run_finished_sku_migration(env, skip_preflight=force)
env.cr.commit()
print("RDL finished-SKU migration result:", result)
print(
    "Verify: parent SVL remaining_value ~= prior component total (~13.8M); "
    "component internal qty/SVL = 0; phantom BOMs = 0."
)

#!/usr/bin/env bash
set -euo pipefail
cd /home/kane/odoo-18
LOG="/home/kane/odoo-18/extra-addons/rdl_core_config/scripts/migration_run.log"
exec > >(tee -a "$LOG") 2>&1

echo "=== RDL migration run $(date -Iseconds) ==="

source odoo-18env/bin/activate

echo "--- Pre-checks ---"
psql -d rdl_staging_dev -At -c "SELECT latest_version FROM ir_module_module WHERE name='rdl_core_config';"
psql -d rdl_staging_dev -At -c "SELECT COUNT(*) FROM information_schema.tables WHERE table_name='rdl_finished_sku_mig_staging';"
psql -d rdl_staging_dev -At -c "SELECT COUNT(*) FROM rdl_finished_sku_mig_staging;" 2>/dev/null || echo "staging table missing"

echo "--- POS sessions ---"
psql -d rdl_staging_dev -c "SELECT id, name, state FROM pos_session WHERE state NOT IN ('closed');"

echo "--- Close stuck POS opening_control sessions ---"
psql -d rdl_staging_dev -c "
UPDATE pos_session
   SET state = 'closed',
       stop_at = NOW()
 WHERE state IN ('opening_control', 'opened');
"

echo "--- Clear draft inventory flags ---"
psql -d rdl_staging_dev -c "
UPDATE stock_quant
   SET inventory_quantity_set = FALSE,
       inventory_quantity = 0,
       inventory_diff_quantity = 0
 WHERE inventory_quantity_set IS TRUE;
"

echo "--- Run odoo shell migration ---"
./odoo-source/odoo-bin shell -d rdl_staging_dev -c odoo.conf \
  < extra-addons/rdl_core_config/scripts/run_finished_sku_migration.py

echo "--- Post-verify ---"
psql -d rdl_staging_dev -c "SELECT COUNT(*) AS phantom_boms FROM mrp_bom WHERE type='phantom';" 2>/dev/null || echo "mrp not installed"
psql -d rdl_staging_dev -c "
SELECT
  (SELECT COALESCE(SUM(sq.quantity),0)
     FROM stock_quant sq
     JOIN stock_location sl ON sl.id = sq.location_id
     JOIN product_product pp ON pp.id = sq.product_id
     JOIN product_template pt ON pt.id = pp.product_tmpl_id
    WHERE sl.usage='internal' AND pt.is_brewery) AS brewery_parent_qty,
  (SELECT COALESCE(SUM(sq.quantity),0)
     FROM stock_quant sq
     JOIN stock_location sl ON sl.id = sq.location_id
     JOIN product_product pp ON pp.id = sq.product_id
     JOIN product_template pt ON pt.id = pp.product_tmpl_id
    WHERE sl.usage='internal'
      AND (pt.name::text ILIKE '%(Liquid)%'
        OR pt.name::text ILIKE '%(Empty Bottle)%'
        OR pt.name::text ILIKE '%(Empty Crate)%')) AS component_qty;
"
psql -d rdl_staging_dev -c "
SELECT COALESCE(SUM(remaining_value),0) AS svl_value
  FROM stock_valuation_layer svl
  JOIN product_product pp ON pp.id = svl.product_id
  JOIN product_template pt ON pt.id = pp.product_tmpl_id
 WHERE pt.is_brewery;
"

echo "=== DONE $(date -Iseconds) ==="

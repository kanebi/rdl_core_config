#!/usr/bin/env bash
# Phase-2: split already-migrated parent stock into drink + consolidated empties.
set -euo pipefail
cd /home/kane/odoo-18
LOG="/home/kane/odoo-18/extra-addons/rdl_core_config/scripts/empties_split_run.log"
exec > >(tee -a "$LOG") 2>&1

echo "=== Empties split phase2 $(date -Iseconds) ==="
source odoo-18env/bin/activate

# Backfill staging SVL split columns if missing (from archived component SVL history)
psql -d rdl_staging_dev -v ON_ERROR_STOP=1 <<'SQL'
ALTER TABLE rdl_finished_sku_mig_staging
  ADD COLUMN IF NOT EXISTS liquid_svl_value double precision,
  ADD COLUMN IF NOT EXISTS empties_svl_value double precision;

-- Estimate split from total using fixed ₦5000 empties per pack when not set
UPDATE rdl_finished_sku_mig_staging s
   SET liquid_svl_value = COALESCE(
         s.liquid_svl_value,
         GREATEST(0, s.svl_value - packs.total_packs * 5000)
       ),
       empties_svl_value = COALESCE(
         s.empties_svl_value,
         LEAST(s.svl_value, packs.total_packs * 5000)
       )
  FROM (
    SELECT parent_tmpl_id,
           COALESCE(SUM((value)::text::double precision), 0) AS total_packs
      FROM rdl_finished_sku_mig_staging,
           LATERAL jsonb_each_text(COALESCE(location_packs, '{}'::jsonb)) AS lp(key, value)
     GROUP BY parent_tmpl_id
  ) packs
 WHERE s.parent_tmpl_id = packs.parent_tmpl_id
   AND (s.liquid_svl_value IS NULL OR s.empties_svl_value IS NULL);
SQL

psql -d rdl_staging_dev -c "UPDATE pos_session SET state='closed', stop_at=NOW() WHERE state NOT IN ('closed');" || true

./odoo-source/odoo-bin shell -d rdl_staging_dev -c odoo.conf <<'PY'
from odoo.addons.rdl_core_config.migrate_finished_sku import run_finished_sku_migration
result = run_finished_sku_migration(env, skip_preflight=True, phase2_split=True)
env.cr.commit()
print("Phase2 result:", result)
print("\n--- UoM locked (could not change UoM due to stock moves) ---")
for item in result.get('uom_locked', []):
    print("[%s] %s: %s" % (item['id'], item['name'], '; '.join(item['reasons'])))
PY

echo "--- Verify ---"
psql -d rdl_staging_dev -c "
SELECT
  (SELECT COUNT(*) FROM product_template WHERE is_brewery AND empties_product_id IS NOT NULL) AS brewery_with_empties,
  (SELECT COALESCE(SUM(sq.quantity),0) FROM stock_quant sq
     JOIN stock_location sl ON sl.id=sq.location_id
     JOIN product_product pp ON pp.id=sq.product_id
     JOIN product_template pt ON pt.id=pp.product_tmpl_id
    WHERE sl.usage='internal' AND pt.is_brewery) AS brewery_parent_qty,
  (SELECT COALESCE(SUM(sq.quantity),0) FROM stock_quant sq
     JOIN stock_location sl ON sl.id=sq.location_id
     JOIN product_template pt ON pt.id=sq.product_id
    WHERE sl.usage='internal' AND pt.name::text ILIKE '%(Empties)%' AND pt.active) AS active_empties_qty,
  (SELECT COALESCE(SUM(remaining_value),0) FROM stock_valuation_layer svl
     JOIN product_template pt ON pt.id=(SELECT product_tmpl_id FROM product_product WHERE id=svl.product_id)
    WHERE pt.is_brewery OR pt.name::text ILIKE '%(Empties)%') AS combined_svl;
"
echo "=== DONE $(date -Iseconds) ==="

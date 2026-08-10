#!/usr/bin/env bash
# Compare rdl_staging vs rdl_staging_dev after migration
set -euo pipefail
SOURCE="${1:-rdl_staging_dev}"
TARGET="${2:-rdl_staging}"
PGPASSWORD="${PGPASSWORD:-kane24}"

psql_cmd() {
  local db=$1
  shift
  PGPASSWORD="$PGPASSWORD" psql -h localhost -U kane -d "$db" -At "$@"
}

echo "=== DB comparison: $SOURCE (source) vs $TARGET (target) ==="
echo

for db in "$SOURCE" "$TARGET"; do
  if ! psql_cmd postgres -c "SELECT 1 FROM pg_database WHERE datname='$db'" | grep -q 1; then
    echo "ERROR: database $db does not exist"
    exit 1
  fi
done

compare_count() {
  local label=$1
  local sql=$2
  local src=$(psql_cmd "$SOURCE" -c "$sql")
  local tgt=$(psql_cmd "$TARGET" -c "$sql")
  local match="OK"
  [[ "$src" != "$tgt" ]] && match="DIFF"
  printf "%-45s %10s %10s %s\n" "$label" "$src" "$tgt" "$match"
}

printf "%-45s %10s %10s %s\n" "METRIC" "SOURCE" "TARGET" "STATUS"
printf "%.0s-" {1..80}; echo

compare_count "Installed modules" \
  "SELECT COUNT(*) FROM ir_module_module WHERE state='installed';"

compare_count "Product templates (active)" \
  "SELECT COUNT(*) FROM product_template WHERE active IS TRUE;"

compare_count "Brewery products (active)" \
  "SELECT COUNT(*) FROM product_template WHERE active IS TRUE AND is_brewery IS TRUE;"

compare_count "Packaged drinks (active)" \
  "SELECT COUNT(*) FROM product_template WHERE active IS TRUE AND is_packaged_drinks IS TRUE;"

compare_count "RDL products (brewery|packaged|sku)" \
  "SELECT COUNT(*) FROM product_template WHERE active IS TRUE AND (is_brewery IS TRUE OR is_packaged_drinks IS TRUE OR (default_code IS NOT NULL AND default_code::text != ''));"

compare_count "Accounts (by code, excl empty)" \
  "SELECT COUNT(*) FROM account_account WHERE code_store IS NOT NULL AND code_store::text != '';"

compare_count "Product categories" \
  "SELECT COUNT(*) FROM product_category;"

compare_count "Accounts" \
  "SELECT COUNT(*) FROM account_account;"

compare_count "Partners (customer rank>0)" \
  "SELECT COUNT(*) FROM res_partner WHERE customer_rank > 0;"

compare_count "Partners (supplier rank>0)" \
  "SELECT COUNT(*) FROM res_partner WHERE supplier_rank > 0;"

compare_count "Stock pickings (incoming/outgoing)" \
  "SELECT COUNT(*) FROM stock_picking sp JOIN stock_picking_type spt ON spt.id = sp.picking_type_id WHERE spt.code IN ('incoming','outgoing');"

compare_count "Stock pickings done" \
  "SELECT COUNT(*) FROM stock_picking sp JOIN stock_picking_type spt ON spt.id = sp.picking_type_id WHERE spt.code IN ('incoming','outgoing') AND sp.state = 'done';"

compare_count "Consolidated empties SKU qty" \
  "SELECT ROUND(COALESCE(SUM(sq.quantity), 0)::numeric, 2) FROM stock_quant sq JOIN product_product pp ON pp.id = sq.product_id JOIN product_template pt ON pt.id = pp.product_tmpl_id JOIN stock_location sl ON sl.id = sq.location_id WHERE sl.usage = 'internal' AND pt.default_code::text = 'RDL-EMPTIES';"

compare_count "Empties products active name match" \
  "SELECT COUNT(*) FROM product_template WHERE active IS TRUE AND name::text ILIKE '%empties%';"

compare_count "Purchase orders" \
  "SELECT COUNT(*) FROM purchase_order;"

compare_count "Sale orders (all)" \
  "SELECT COUNT(*) FROM sale_order;"

compare_count "Sale orders (sale state)" \
  "SELECT COUNT(*) FROM sale_order WHERE state='sale';"

compare_count "Posted account moves" \
  "SELECT COUNT(*) FROM account_move WHERE state='posted';"

compare_count "Customer invoices (posted)" \
  "SELECT COUNT(*) FROM account_move WHERE state='posted' AND move_type IN ('out_invoice','out_refund');"

compare_count "Vendor bills (posted)" \
  "SELECT COUNT(*) FROM account_move WHERE state='posted' AND move_type IN ('in_invoice','in_refund');"

compare_count "Journal entries (posted)" \
  "SELECT COUNT(*) FROM account_move WHERE state='posted' AND move_type='entry';"

echo
echo "--- Stock quants (internal, qty>0) ---"
psql_cmd "$SOURCE" -c "
SELECT COALESCE(pt.default_code::text, pt.name::text), ROUND(SUM(sq.quantity)::numeric, 2)
FROM stock_quant sq
JOIN product_product pp ON pp.id = sq.product_id
JOIN product_template pt ON pt.id = pp.product_tmpl_id
JOIN stock_location sl ON sl.id = sq.location_id
WHERE sl.usage = 'internal' AND sq.quantity > 0
GROUP BY 1
ORDER BY 1;" > /tmp/src_quants.txt

psql_cmd "$TARGET" -c "
SELECT COALESCE(pt.default_code::text, pt.name::text), ROUND(SUM(sq.quantity)::numeric, 2)
FROM stock_quant sq
JOIN product_product pp ON pp.id = sq.product_id
JOIN product_template pt ON pt.id = pp.product_tmpl_id
JOIN stock_location sl ON sl.id = sq.location_id
WHERE sl.usage = 'internal' AND sq.quantity > 0
GROUP BY 1
ORDER BY 1;" > /tmp/tgt_quants.txt

echo "Source lines: $(wc -l < /tmp/src_quants.txt) | Target lines: $(wc -l < /tmp/tgt_quants.txt)"
diff -u /tmp/src_quants.txt /tmp/tgt_quants.txt || true

echo
echo "--- SVL total (parents with stock) ---"
for db in "$SOURCE" "$TARGET"; do
  val=$(psql_cmd "$db" -c "
    SELECT ROUND(COALESCE(SUM(svl.value), 0)::numeric, 2)
    FROM stock_valuation_layer svl
    JOIN product_product pp ON pp.id = svl.product_id
    JOIN product_template pt ON pt.id = pp.product_tmpl_id
    WHERE pt.is_brewery IS TRUE OR pt.is_packaged_drinks IS TRUE;")
  echo "$db SVL (brewery+packaged): $val"
done

echo
LOG="/home/kane/odoo-18/extra-addons/rdl_core_config/scripts/migration_log.json"
if [[ -f "$LOG" ]]; then
  echo "--- migration_log.json phases ---"
  python3 -c "import json; d=json.load(open('$LOG')); [print(k, v) for k,v in d.get('phases',{}).items()]"
fi

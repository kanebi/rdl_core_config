# -*- coding: utf-8 -*-
"""Print UoM-locked products and run phase2 empties split. For odoo shell."""
from odoo.addons.rdl_core_config.migrate_finished_sku import (
    report_uom_locked_products,
    run_finished_sku_migration,
)

locked = report_uom_locked_products(env)
print("\n=== UoM LOCKED (have stock moves — UoM not changed) ===")
for item in locked:
    print("[%s] %s | list=%s | %s" % (
        item['id'], item['name'], item['list_price'], '; '.join(item['reasons']),
    ))
if not locked:
    print("(none)")

result = run_finished_sku_migration(env, skip_preflight=True, phase2_split=True)
env.cr.commit()
print("\n=== MIGRATION RESULT ===")
print(result)

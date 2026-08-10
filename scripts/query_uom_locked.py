#!/usr/bin/env python3
"""Quick report: products whose UoM could not be changed (have stock moves)."""
import psycopg2

DB = 'rdl_staging_dev'
conn = psycopg2.connect(dbname=DB)
cr = conn.cursor()
cr.execute("""
    SELECT pt.id,
           pt.name->>'en_US' AS name,
           u.name AS uom,
           pt.is_brewery,
           pt.is_packaged_drinks,
           pt.list_price,
           (SELECT COUNT(*) FROM stock_move sm
             JOIN product_product pp ON pp.id = sm.product_id
            WHERE pp.product_tmpl_id = pt.id) AS moves
      FROM product_template pt
      JOIN uom_uom u ON u.id = pt.uom_id
     WHERE pt.active
       AND (pt.is_brewery OR pt.is_packaged_drinks)
     ORDER BY moves DESC, pt.id
""")
print("id|name|uom|brewery|packaged|list_price|moves")
for row in cr.fetchall():
    print("|".join(str(x) for x in row))
conn.close()

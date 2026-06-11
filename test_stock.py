import sys
sys.path.append('/home/kane/odoo-18/odoo-source')
import odoo

odoo.tools.config.parse_config(['-c', '/home/kane/odoo-18/odoo.conf'])
registry = odoo.registry('dev18')

with registry.cursor() as cr:
    env = odoo.api.Environment(cr, odoo.SUPERUSER_ID, {})
    product = env['product.product'].search([('default_code', '=', 'GNESS-001')], limit=1)
    if product:
        print('Kit Product:', product.name, 'Qty:', product.qty_available, 'Type:', product.type, 'Is Storable:', product.is_storable)
        print('Components:')
        if product.liquid_product_id:
            print(' - Liquid:', product.liquid_product_id.name, 'Qty:', product.liquid_product_id.qty_available)
        if product.bottle_product_id:
            print(' - Bottle:', product.bottle_product_id.name, 'Qty:', product.bottle_product_id.qty_available)
        if product.crate_product_id:
            print(' - Crate:', product.crate_product_id.name, 'Qty:', product.crate_product_id.qty_available)

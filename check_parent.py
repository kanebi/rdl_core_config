import sys
sys.path.append('/home/kane/odoo-18/odoo-source')
import odoo
odoo.tools.config.parse_config(['-c', '/home/kane/odoo-18/odoo.conf'])
registry = odoo.registry('dev18')
with registry.cursor() as cr:
    env = odoo.api.Environment(cr, odoo.SUPERUSER_ID, {})
    fields = env['account.account']._fields.keys()
    if 'parent_id' in fields:
        print("YES_PARENT_ID")
    else:
        print("NO_PARENT_ID")

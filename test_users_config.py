import sys
sys.path.insert(0, '/home/kane/odoo-18/odoo-source')
import odoo
import logging
logging.basicConfig(level=logging.INFO)
odoo.tools.config.parse_config(['-c', '/home/kane/odoo-18/odoo.conf', '--logfile='])
registry = odoo.registry('dev18')

print("Starting custom group and user configuration verification...")

with registry.cursor() as cr:
    env = odoo.api.Environment(cr, odoo.SUPERUSER_ID, {})
    
    print("Upgrading rdl_core_config module to load XML groups...")
    module = env['ir.module.module'].search([('name', '=', 'rdl_core_config')], limit=1)
    if module:
        module.button_immediate_upgrade()
    else:
        print("❌ rdl_core_config module not found in database module list!")
        sys.exit(1)

print("\nUpgraded module successfully. Now running post_init_hook and checking DB...")

with registry.cursor() as cr:
    env = odoo.api.Environment(cr, odoo.SUPERUSER_ID, {})
    from odoo.addons.rdl_core_config import post_init_hook
    
    # Manually execute the post_init_hook to create users
    print("Executing post_init_hook...")
    post_init_hook(env)
    
    # Verify groups exist
    groups_to_check = [
        ('rdl_core_config.group_finance_rdl', 'account.group_account_user'),
        ('rdl_core_config.group_inventory_manager_rdl', 'stock.group_stock_manager'),
        ('rdl_core_config.group_operations_manager_rdl', 'stock.group_stock_manager'),
        ('rdl_core_config.group_admin_rdl', 'base.group_system'),
        ('rdl_core_config.group_store_cashier', 'point_of_sale.group_pos_user'),
        ('rdl_core_config.group_vsr', 'point_of_sale.group_pos_user')
    ]
    
    for g_ref, implied_ref in groups_to_check:
        g = env.ref(g_ref, raise_if_not_found=False)
        if not g:
            print(f"❌ Custom group {g_ref} not found!")
            sys.exit(1)
        else:
            print(f"✅ Group {g_ref} found.")

    # Verify users exist and have correct logins
    users_to_check = [
        ('finance@rdltrading.com', 'rdl_core_config.group_finance_rdl'),
        ('inventory@rdltrading.com', 'rdl_core_config.group_inventory_manager_rdl'),
        ('operations@rdltrading.com', 'rdl_core_config.group_operations_manager_rdl'),
        ('storepos@rdltrading.com', 'rdl_core_config.group_store_cashier'),
        ('vanpos@rdltrading.com', 'rdl_core_config.group_vsr'),
        ('admin@rdltrading.com', 'rdl_core_config.group_admin_rdl')
    ]
    
    all_companies = env['res.company'].search([])
    
    for login, group_ref in users_to_check:
        user = env['res.users'].search([('login', '=', login)], limit=1)
        if not user:
            print(f"❌ User for {login} not found!")
            sys.exit(1)
        else:
            print(f"✅ User found: {user.name} ({user.login})")
            g = env.ref(group_ref)
            assert g in user.groups_id, f"User {login} is not member of group {group_ref}"
            print(f"   - Correctly linked to group {group_ref}")
            
            # Check company access
            assert all(c in user.company_ids for c in all_companies), f"User {login} is missing some company access rights"
            print(f"   - Has access to all {len(all_companies)} companies")

    # Commit to apply the user creations to the database
    cr.commit()

print("\nVerification completed successfully!")

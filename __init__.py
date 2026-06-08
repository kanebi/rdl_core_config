# -*- coding: utf-8 -*-
from . import models
from odoo import api, SUPERUSER_ID

def post_init_hook(env):
    # Ensure standard crate UoM has the requested name
    uom_crate = env.ref('rdl_core_config.uom_crate', raise_if_not_found=False)
    if uom_crate and uom_crate.name != 'Crate x24':
        uom_crate.write({'name': 'Crate x24'})

    # 1. First import all Seerbit banks into res.bank
    try:
        with env.cr.savepoint():
            banks = env['seerbit.payout'].get_seerbit_banks()
            # Get Nigeria country ID (163)
            country_ng = env['res.country'].search([('code', '=', 'NG')], limit=1)
            country_ng_id = country_ng.id if country_ng else False
            
            for b in banks:
                bank_name = b.get('bankName')
                if not bank_name:
                    continue
                bank_name = bank_name.strip()
                existing_bank = env['res.bank'].search([('name', '=ilike', bank_name)], limit=1)
                if not existing_bank:
                    env['res.bank'].create({
                        'name': bank_name,
                        'bic': b.get('bankCode') or False,
                        'country': country_ng_id,
                    })
    except Exception:
        pass

    # 2. Global permissions/groups setup (Storage Locations and Consignment)
    try:
        with env.cr.savepoint():
            group_user = env.ref('base.group_user')
            multi_loc_group = env.ref('stock.group_stock_multi_locations')
            consignment_group = env.ref('stock.group_tracking_owner')
            if multi_loc_group not in group_user.implied_ids:
                group_user.write({'implied_ids': [(4, multi_loc_group.id)]})
            if consignment_group not in group_user.implied_ids:
                group_user.write({'implied_ids': [(4, consignment_group.id)]})
    except Exception:
        pass

    # 3. Loop over all companies and set up warehouses, locations, POS profiles, journals, and opening balances
    for company in env['res.company'].search([]):
        # Skip companies without chart template (not accounting enabled)
        if not company.chart_template:
            continue
            
        try:
            with env.cr.savepoint():
                # Warehouse setup
                warehouse = env['stock.warehouse'].search([('company_id', '=', company.id)], limit=1)
                if not warehouse:
                    warehouse = env['stock.warehouse'].with_company(company).create({
                        'name': 'Main Warehouse',
                        'code': 'WH',
                        'company_id': company.id,
                    })
                if warehouse.lot_stock_id and warehouse.lot_stock_id.name == 'Stock':
                    warehouse.lot_stock_id.write({'name': 'Main'})
                    
                # Activate all archived internal transfer picking types for this warehouse
                internal_types = env['stock.picking.type'].with_context(active_test=False).search([
                    ('warehouse_id', '=', warehouse.id),
                    ('code', '=', 'internal'),
                    ('company_id', '=', company.id),
                ])
                for pt in internal_types:
                    if not pt.active:
                        pt.write({'active': True})
                    
                # Custom Locations setup (Vans, Van-001, Damaged_Scrap)
                vans_loc = env['stock.location'].search([('name', '=', 'Vans'), ('company_id', '=', company.id)], limit=1)
                if not vans_loc:
                    vans_loc = env['stock.location'].with_company(company).create({
                        'name': 'Vans',
                        'usage': 'view',
                        'location_id': warehouse.view_location_id.id,
                        'company_id': company.id,
                    })
                    
                van_loc = env['stock.location'].search([('name', '=', 'Van-001'), ('company_id', '=', company.id)], limit=1)
                if not van_loc:
                    van_loc = env['stock.location'].with_company(company).create({
                        'name': 'Van-001',
                        'usage': 'internal',
                        'location_id': vans_loc.id,
                        'company_id': company.id,
                    })
                    
                damaged_loc = env['stock.location'].search([('name', '=', 'Damaged_Scrap'), ('company_id', '=', company.id)], limit=1)
                if not damaged_loc:
                    damaged_loc = env['stock.location'].with_company(company).create({
                        'name': 'Damaged_Scrap',
                        'usage': 'internal',
                        'scrap_location': True,
                        'location_id': warehouse.view_location_id.id,
                        'company_id': company.id,
                    })
                    
                # Store & Van POS Profiles
                store_config = env['pos.config'].search([('name', '=', 'Store POS Profile'), ('company_id', '=', company.id)], limit=1)
                if not store_config:
                    store_config = env['pos.config'].with_company(company).create({
                        'name': 'Store POS Profile',
                        'company_id': company.id,
                    })
                    
                van_config = env['pos.config'].search([('name', '=', 'Van POS Profile'), ('company_id', '=', company.id)], limit=1)
                if not van_config:
                    van_config = env['pos.config'].with_company(company).create({
                        'name': 'Van POS Profile',
                        'company_id': company.id,
                    })
                    
                # Van picking type
                van_pos_type = env['stock.picking.type'].search([('name', '=', 'POS Van-001'), ('company_id', '=', company.id)], limit=1)
                if not van_pos_type:
                    van_pos_type = env['stock.picking.type'].with_company(company).create({
                        'name': 'POS Van-001',
                        'code': 'outgoing',
                        'warehouse_id': warehouse.id,
                        'default_location_src_id': van_loc.id,
                        'default_location_dest_id': env.ref('stock.stock_location_customers').id,
                        'sequence_code': 'POSVAN',
                        'company_id': company.id,
                    })
                    
                van_config.write({'picking_type_id': van_pos_type.id})
                
                default_pos_type = warehouse.pos_type_id or env['stock.picking.type'].search([
                    ('code', '=', 'outgoing'),
                    ('warehouse_id', '=', warehouse.id),
                    ('company_id', '=', company.id)
                ], limit=1)
                if default_pos_type:
                    store_config.write({'picking_type_id': default_pos_type.id})
                    
                # Assign Sale journal to POS configs
                sale_journal = env['account.journal'].search([
                    ('type', '=', 'sale'),
                    ('company_id', '=', company.id)
                ], limit=1)
                if sale_journal:
                    store_config.write({'journal_id': sale_journal.id})
                    van_config.write({'journal_id': sale_journal.id})
                    
                # Cash journals and payment methods
                default_cash_journal = env['account.journal'].search([
                    ('type', '=', 'cash'),
                    ('company_id', '=', company.id),
                    ('code', '!=', 'CSHV1')
                ], limit=1)
                if not default_cash_journal:
                    default_cash_journal = env['account.journal'].with_company(company).create({
                        'name': 'Cash',
                        'type': 'cash',
                        'code': 'CSH',
                        'company_id': company.id,
                    })
                    
                store_cash_pm = env['pos.payment.method'].search([
                    ('journal_id', '=', default_cash_journal.id),
                    ('company_id', '=', company.id)
                ], limit=1)
                if not store_cash_pm:
                    store_cash_pm = env['pos.payment.method'].with_company(company).create({
                        'name': 'Cash (Store)',
                        'journal_id': default_cash_journal.id,
                        'company_id': company.id,
                    })
                    
                if store_cash_pm not in store_config.payment_method_ids:
                    store_config.write({'payment_method_ids': [(4, store_cash_pm.id)]})
                    
                van_cash_journal = env['account.journal'].search([
                    ('code', '=', 'CSHV1'),
                    ('company_id', '=', company.id)
                ], limit=1)
                if not van_cash_journal:
                    van_cash_journal = env['account.journal'].with_company(company).create({
                        'name': 'Cash Van-001',
                        'type': 'cash',
                        'code': 'CSHV1',
                        'company_id': company.id,
                    })
                    
                van_cash_pm = env['pos.payment.method'].search([
                    ('journal_id', '=', van_cash_journal.id),
                    ('company_id', '=', company.id)
                ], limit=1)
                if not van_cash_pm:
                    van_cash_pm = env['pos.payment.method'].with_company(company).create({
                        'name': 'Cash Van-001',
                        'journal_id': van_cash_journal.id,
                        'company_id': company.id,
                    })
                    
                if van_cash_pm not in van_config.payment_method_ids:
                    van_config.write({'payment_method_ids': [(4, van_cash_pm.id)]})
                    
                # Customer Account payment method and journal
                cust_pm = env['pos.payment.method'].search([
                    ('name', '=', 'Customer Account'),
                    ('company_id', '=', company.id)
                ], limit=1)
                if not cust_pm:
                    cust_pm = env['pos.payment.method'].with_company(company).create({
                        'name': 'Customer Account',
                        'split_transactions': True,
                        'sequence': 2,
                        'company_id': company.id,
                    })
                    
                if cust_pm not in store_config.payment_method_ids:
                    store_config.write({'payment_method_ids': [(4, cust_pm.id)]})
                if cust_pm not in van_config.payment_method_ids:
                    van_config.write({'payment_method_ids': [(4, cust_pm.id)]})
                    
                cust_journal = env['account.journal'].search([
                    ('name', '=', 'Customer Account'),
                    ('type', '=', 'bank'),
                    ('company_id', '=', company.id)
                ], limit=1)
                if not cust_journal:
                    cust_code = 'CUST'
                    if env['account.journal'].search([('code', '=', cust_code), ('company_id', '=', company.id)]):
                        cust_code = 'CUSTA'
                    cust_journal = env['account.journal'].with_company(company).create({
                        'name': 'Customer Account',
                        'type': 'bank',
                        'code': cust_code,
                        'company_id': company.id,
                    })
                    
                # Zenith Bank Setup
                zenith_bank = env['res.bank'].search([('name', '=', 'Zenith Bank PLC')], limit=1)
                if not zenith_bank:
                    zenith_bank = env['res.bank'].create({
                        'name': 'Zenith Bank PLC',
                        'bic': 'ZENITHNG',
                        'country': country_ng_id,
                    })
                    
                zen_partner_bank = env['res.partner.bank'].search([
                    ('acc_number', '=', '1011234567'),
                    ('partner_id', '=', company.partner_id.id)
                ], limit=1)
                if not zen_partner_bank:
                    zen_partner_bank = env['res.partner.bank'].with_company(company).create({
                        'acc_number': '1011234567',
                        'bank_id': zenith_bank.id,
                        'partner_id': company.partner_id.id,
                        'company_id': company.id,
                    })
                    
                zenith_journal = env['account.journal'].search([
                    ('code', '=', 'ZEN'),
                    ('company_id', '=', company.id)
                ], limit=1)
                if not zenith_journal:
                    zenith_journal = env['account.journal'].with_company(company).create({
                        'name': 'Zenith Bank',
                        'type': 'bank',
                        'code': 'ZEN',
                        'bank_account_id': zen_partner_bank.id,
                        'company_id': company.id,
                    })
                    
                # GTBank Setup
                gtb_bank = env['res.bank'].search([('name', '=', 'Guaranty Trust Bank PLC')], limit=1)
                if not gtb_bank:
                    gtb_bank = env['res.bank'].create({
                        'name': 'Guaranty Trust Bank PLC',
                        'bic': 'GTBNKNG',
                        'country': country_ng_id,
                    })
                    
                gtb_partner_bank = env['res.partner.bank'].search([
                    ('acc_number', '=', '0112345678'),
                    ('partner_id', '=', company.partner_id.id)
                ], limit=1)
                if not gtb_partner_bank:
                    gtb_partner_bank = env['res.partner.bank'].with_company(company).create({
                        'acc_number': '0112345678',
                        'bank_id': gtb_bank.id,
                        'partner_id': company.partner_id.id,
                        'company_id': company.id,
                    })
                    
                gtb_journal = env['account.journal'].search([
                    ('code', '=', 'GTB'),
                    ('company_id', '=', company.id)
                ], limit=1)
                if not gtb_journal:
                    gtb_journal = env['account.journal'].with_company(company).create({
                        'name': 'GTBank',
                        'type': 'bank',
                        'code': 'GTB',
                        'bank_account_id': gtb_partner_bank.id,
                        'company_id': company.id,
                    })
                    
                # Flush changes to make sure default accounts are created and linked to the journals
                env.flush_all()
                
                # Opening balances updates
                zen_acc = zenith_journal.default_account_id
                gtb_acc = gtb_journal.default_account_id
                updates = {}
                if zen_acc:
                    updates[zen_acc] = (500000.0, 0.0)
                if gtb_acc:
                    updates[gtb_acc] = (250000.0, 0.0)
                    
                if updates:
                    company.with_company(company)._update_opening_move(updates)
                    
                # Replenishment routes
                route_name = 'Replenish Van-001 from WH/Main'
                route = env['stock.route'].search([
                    ('name', '=', route_name),
                    ('company_id', '=', company.id)
                ], limit=1)
                if not route:
                    route = env['stock.route'].with_company(company).create({
                        'name': route_name,
                        'sequence': 10,
                        'company_id': company.id,
                        'product_selectable': True,
                        'product_categ_selectable': True,
                        'rule_ids': [(0, 0, {
                            'name': 'Pull from WH/Main to Van-001',
                            'action': 'pull',
                            'picking_type_id': warehouse.int_type_id.id,
                            'location_src_id': warehouse.lot_stock_id.id,
                            'location_dest_id': van_loc.id,
                            'procure_method': 'make_to_stock',
                            'company_id': company.id,
                        })]
                    })
                    
                guinness_liquid = env.ref('rdl_core_config.product_guinness_liquid_single', raise_if_not_found=False)
                if guinness_liquid:
                    existing_op = env['stock.warehouse.orderpoint'].search([
                        ('product_id', '=', guinness_liquid.id),
                        ('location_id', '=', van_loc.id),
                        ('company_id', '=', company.id)
                    ], limit=1)
                    if not existing_op:
                        env['stock.warehouse.orderpoint'].with_company(company).create({
                            'name': 'OP/Van-001/Guinness',
                            'product_id': guinness_liquid.id,
                            'location_id': van_loc.id,
                            'route_id': route.id,
                            'product_min_qty': 0.0,
                            'product_max_qty': 0.0,
                            'qty_multiple': 1.0,
                            'trigger': 'manual',
                            'company_id': company.id,
                        })
                        
                # Currency configuration & Credit Limits
                ngn_currency = env['res.currency'].search([('name', '=', 'NGN')], limit=1)
                if ngn_currency:
                    if not ngn_currency.active:
                        ngn_currency.write({'active': True})
                    if company.currency_id != ngn_currency:
                        company.write({'currency_id': ngn_currency.id})
                        
                company.write({'account_use_credit_limit': True})
                env['ir.default'].set(
                    'res.partner',
                    'credit_limit',
                    5000000.0,
                    company_id=company.id
                )
        except Exception:
            pass

    # 4. Configure 0% Default Taxes for all companies (Root configuration)
    try:
        for company in env['res.company'].search([]):
            with env.cr.savepoint():
                country = company.account_fiscal_country_id or company.country_id
                tax_group = env['account.tax.group'].search([
                    ('company_id', '=', company.id),
                ], limit=1)
                if not tax_group and country:
                    tax_group = env['account.tax.group'].search([
                        ('country_id', '=', country.id)
                    ], limit=1)
                if not tax_group:
                    tax_group = env['account.tax.group'].create({
                        'name': 'Default Tax Group',
                        'company_id': company.id,
                        'country_id': country.id if country else False,
                    })

                tax_sale = env['account.tax'].search([
                    ('company_id', '=', company.id),
                    ('type_tax_use', '=', 'sale'),
                    ('amount', '=', 0.0),
                ], limit=1)
                if not tax_sale:
                    xml_tax = env.ref('rdl_core_config.tax_sale_0', raise_if_not_found=False)
                    if xml_tax and xml_tax.company_id == company:
                        tax_sale = xml_tax
                    else:
                        tax_sale = env['account.tax'].create({
                            'name': '0% Sales Tax',
                            'amount': 0.0,
                            'amount_type': 'percent',
                            'type_tax_use': 'sale',
                            'description': '0% Sale',
                            'company_id': company.id,
                            'tax_group_id': tax_group.id,
                        })
                
                tax_purchase = env['account.tax'].search([
                    ('company_id', '=', company.id),
                    ('type_tax_use', '=', 'purchase'),
                    ('amount', '=', 0.0),
                ], limit=1)
                if not tax_purchase:
                    xml_tax = env.ref('rdl_core_config.tax_purchase_0', raise_if_not_found=False)
                    if xml_tax and xml_tax.company_id == company:
                        tax_purchase = xml_tax
                    else:
                        tax_purchase = env['account.tax'].create({
                            'name': '0% Purchase Tax',
                            'amount': 0.0,
                            'amount_type': 'percent',
                            'type_tax_use': 'purchase',
                            'description': '0% Purchase',
                            'company_id': company.id,
                            'tax_group_id': tax_group.id,
                        })
                
                company.write({
                    'account_sale_tax_id': tax_sale.id,
                    'account_purchase_tax_id': tax_purchase.id,
                })
    except Exception:
        pass




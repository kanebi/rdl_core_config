# -*- coding: utf-8 -*-
import logging
from . import models
from . import wizard
from odoo import api, SUPERUSER_ID

_logger = logging.getLogger(__name__)

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

    # 2. Global permissions/groups setup (Storage Locations, Consignment, Multi-currency, Lots/Serial Numbers)
    try:
        with env.cr.savepoint():
            group_user = env.ref('base.group_user')
            multi_loc_group = env.ref('stock.group_stock_multi_locations')
            consignment_group = env.ref('stock.group_tracking_owner')
            multi_curr_group = env.ref('base.group_multi_currency')
            lot_group = env.ref('stock.group_production_lot')
            if multi_loc_group not in group_user.implied_ids:
                group_user.write({'implied_ids': [(4, multi_loc_group.id)]})
            if consignment_group not in group_user.implied_ids:
                group_user.write({'implied_ids': [(4, consignment_group.id)]})
            if multi_curr_group and multi_curr_group in group_user.implied_ids:
                group_user.write({'implied_ids': [(3, multi_curr_group.id)]})
            if lot_group and lot_group not in group_user.implied_ids:
                group_user.write({'implied_ids': [(4, lot_group.id)]})
    except Exception:
        pass

    # 3. Loop over all companies and set up warehouses, locations, POS profiles, journals, and opening balances
    for company in env['res.company'].search([]):
        # If no chart of accounts is configured, install the Nigerian accounting template
        if not company.chart_template:
            country_ng = env['res.country'].search([('code', '=', 'NG')], limit=1)
            if country_ng:
                company.write({'country_id': country_ng.id})
            try:
                env['account.chart.template'].try_loading('ng', company)
            except Exception:
                pass

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

                # 1. Seerbit Store Journal and Payment Method Setup
                seerbit_store_journal = env['account.journal'].search([
                    ('code', '=', 'SEER'),
                    ('company_id', '=', company.id)
                ], limit=1)
                if not seerbit_store_journal:
                    seerbit_store_journal = env['account.journal'].search([
                        ('name', '=', 'Seerbit'),
                        ('company_id', '=', company.id)
                    ], limit=1)
                if not seerbit_store_journal:
                    seerbit_store_journal = env['account.journal'].with_company(company).create({
                        'name': 'Seerbit',
                        'type': 'bank',
                        'code': 'SEER',
                        'company_id': company.id,
                    })

                seerbit_store_pm = env['pos.payment.method'].search([
                    ('name', '=', 'Seerbit POS'),
                    ('company_id', '=', company.id)
                ], limit=1)
                store_term_id = f"STORE001_{company.id}"
                if not seerbit_store_pm:
                    seerbit_store_pm = env['pos.payment.method'].with_company(company).create({
                        'name': 'Seerbit POS',
                        'journal_id': seerbit_store_journal.id,
                        'company_id': company.id,
                        'use_payment_terminal': 'seerbit',
                        'seerbit_terminal_id': store_term_id,
                    })
                else:
                    store_vals = {}
                    if seerbit_store_pm.use_payment_terminal != 'seerbit':
                        store_vals['use_payment_terminal'] = 'seerbit'
                    if not seerbit_store_pm.seerbit_terminal_id:
                        store_vals['seerbit_terminal_id'] = store_term_id
                    if store_vals:
                        seerbit_store_pm.write(store_vals)

                # 2. Seerbit Van Journal and Payment Method Setup
                seerbit_van_journal = env['account.journal'].search([
                    ('code', '=', 'SEERV'),
                    ('company_id', '=', company.id)
                ], limit=1)
                if not seerbit_van_journal:
                    seerbit_van_journal = env['account.journal'].search([
                        ('name', '=', 'Seerbit Van-001'),
                        ('company_id', '=', company.id)
                    ], limit=1)
                if not seerbit_van_journal:
                    seerbit_van_journal = env['account.journal'].with_company(company).create({
                        'name': 'Seerbit Van-001',
                        'type': 'bank',
                        'code': 'SEERV',
                        'company_id': company.id,
                    })

                seerbit_van_pm = env['pos.payment.method'].search([
                    ('name', '=', 'Seerbit Van-001'),
                    ('company_id', '=', company.id)
                ], limit=1)
                van_term_id = f"VAN001_{company.id}"
                if not seerbit_van_pm:
                    seerbit_van_pm = env['pos.payment.method'].with_company(company).create({
                        'name': 'Seerbit Van-001',
                        'journal_id': seerbit_van_journal.id,
                        'company_id': company.id,
                        'use_payment_terminal': 'seerbit',
                        'seerbit_terminal_id': van_term_id,
                    })
                else:
                    van_vals = {}
                    if seerbit_van_pm.use_payment_terminal != 'seerbit':
                        van_vals['use_payment_terminal'] = 'seerbit'
                    if not seerbit_van_pm.seerbit_terminal_id:
                        van_vals['seerbit_terminal_id'] = van_term_id
                    if van_vals:
                        seerbit_van_pm.write(van_vals)

                # Set exactly Cash, Customer Account, and Seerbit payment methods
                if store_cash_pm and cust_pm and seerbit_store_pm:
                    store_config.write({
                        'payment_method_ids': [(6, 0, [store_cash_pm.id, cust_pm.id, seerbit_store_pm.id])]
                    })
                if van_cash_pm and cust_pm and seerbit_van_pm:
                    van_config.write({
                        'payment_method_ids': [(6, 0, [van_cash_pm.id, cust_pm.id, seerbit_van_pm.id])]
                    })

                # Create walk-in customers for the two POS profiles
                walkin_store_partner = env['res.partner'].search([
                    ('name', '=', 'Walk-in Customer (Store)'),
                    ('company_id', '=', company.id)
                ], limit=1)
                if not walkin_store_partner:
                    walkin_store_partner = env['res.partner'].with_company(company).create({
                        'name': 'Walk-in Customer (Store)',
                        'company_id': company.id,
                    })

                walkin_van_partner = env['res.partner'].search([
                    ('name', '=', 'Walk-in Customer (Van)'),
                    ('company_id', '=', company.id)
                ], limit=1)
                if not walkin_van_partner:
                    walkin_van_partner = env['res.partner'].with_company(company).create({
                        'name': 'Walk-in Customer (Van)',
                        'company_id': company.id,
                    })
                    
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
                    
                zen_partner_bank = env['res.partner.bank'].with_context(active_test=False).search([
                    ('acc_number', '=', '1311743396'),
                    ('partner_id', '=', company.partner_id.id)
                ], limit=1)
                if not zen_partner_bank:
                    zen_partner_bank = env['res.partner.bank'].with_company(company).create({
                        'acc_number': '1311743396',
                        'bank_id': zenith_bank.id,
                        'partner_id': company.partner_id.id,
                        'company_id': company.id,
                    })
                if zen_partner_bank and not zen_partner_bank.active:
                    zen_partner_bank.write({'active': True})
                    
                zenith_journal = env['account.journal'].search([
                    ('code', '=', 'ZEN'),
                    ('company_id', '=', company.id)
                ], limit=1)
                if not zenith_journal:
                    zenith_journal = env['account.journal'].with_company(company).create({
                        'name': 'RDL Trading Ltd — Current',
                        'type': 'bank',
                        'code': 'ZEN',
                        'bank_account_id': zen_partner_bank.id,
                        'company_id': company.id,
                    })
                else:
                    journal_vals = {}
                    if zenith_journal.name != 'RDL Trading Ltd — Current':
                        journal_vals['name'] = 'RDL Trading Ltd — Current'
                    if zenith_journal.bank_account_id != zen_partner_bank:
                        journal_vals['bank_account_id'] = zen_partner_bank.id
                    if journal_vals:
                        zenith_journal.write(journal_vals)
                    
                # Paralex Bank Setup
                paralex_bank = env['res.bank'].search([('name', '=', 'Paralex Bank')], limit=1)
                if not paralex_bank:
                    paralex_bank = env['res.bank'].create({
                        'name': 'Paralex Bank',
                        'bic': 'PRLXNG',
                        'country': country_ng_id,
                    })
                    
                par_partner_bank = env['res.partner.bank'].with_context(active_test=False).search([
                    ('acc_number', '=', '1000349758'),
                    ('partner_id', '=', company.partner_id.id)
                ], limit=1)
                if not par_partner_bank:
                    par_partner_bank = env['res.partner.bank'].with_company(company).create({
                        'acc_number': '1000349758',
                        'bank_id': paralex_bank.id,
                        'partner_id': company.partner_id.id,
                        'company_id': company.id,
                    })
                if par_partner_bank and not par_partner_bank.active:
                    par_partner_bank.write({'active': True})
                    
                paralex_journal = env['account.journal'].search([
                    ('code', '=', 'PRL'),
                    ('company_id', '=', company.id)
                ], limit=1)
                if not paralex_journal:
                    old_gtb_journal = env['account.journal'].search([
                        ('code', '=', 'GTB'),
                        ('company_id', '=', company.id)
                    ], limit=1)
                    if old_gtb_journal:
                        old_gtb_journal.write({
                            'name': 'RDL Trading Ltd — Operations',
                            'code': 'PRL',
                            'bank_account_id': par_partner_bank.id,
                        })
                        paralex_journal = old_gtb_journal
                    else:
                        paralex_journal = env['account.journal'].with_company(company).create({
                            'name': 'RDL Trading Ltd — Operations',
                            'type': 'bank',
                            'code': 'PRL',
                            'bank_account_id': par_partner_bank.id,
                            'company_id': company.id,
                        })
                else:
                    journal_vals = {}
                    if paralex_journal.name != 'RDL Trading Ltd — Operations':
                        journal_vals['name'] = 'RDL Trading Ltd — Operations'
                    if paralex_journal.bank_account_id != par_partner_bank:
                        journal_vals['bank_account_id'] = par_partner_bank.id
                    if journal_vals:
                        paralex_journal.write(journal_vals)
                    
                # Flush changes to make sure default accounts are created and linked to the journals
                env.flush_all()
                
                # Opening balances updates
                zen_acc = zenith_journal.default_account_id
                prl_acc = paralex_journal.default_account_id
                updates = {}
                if zen_acc:
                    updates[zen_acc] = (500000.0, 0.0)
                if prl_acc:
                    updates[prl_acc] = (250000.0, 0.0)
                    
                if updates:
                    try:
                        company.with_company(company)._update_opening_move(updates)
                    except Exception as e:
                        _logger.warning("Failed to update opening balances for company %s: %s", company.name, str(e))
                    
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
                        
                company.write({
                    'account_use_credit_limit': True,
                    'anglo_saxon_accounting': True,
                })
                env['ir.default'].set(
                    'res.partner',
                    'credit_limit',
                    5000000.0,
                    company_id=company.id
                )

                # Assign stock locations to POS profiles
                if store_config and warehouse.lot_stock_id:
                    store_config.write({'stock_location_id': warehouse.lot_stock_id.id})
                if van_config and van_loc:
                    van_config.write({'stock_location_id': van_loc.id})

                # Discover stock valuation, input/output accounts, and journal for this company
                acc_valuation = env['account.account'].search([('company_ids', 'in', company.id), ('code', '=', '110100')], limit=1)
                acc_input = env['account.account'].search([('company_ids', 'in', company.id), ('code', '=', '110200')], limit=1)
                acc_output = env['account.account'].search([('company_ids', 'in', company.id), ('code', '=', '110300')], limit=1)
                
                if not acc_valuation:
                    acc_valuation = env['account.account'].search([('company_ids', 'in', company.id), ('name', 'ilike', 'Stock Valuation')], limit=1)
                if not acc_input:
                    acc_input = env['account.account'].search([('company_ids', 'in', company.id), ('name', 'ilike', 'Interim (Received)')], limit=1)
                if not acc_output:
                    acc_output = env['account.account'].search([('company_ids', 'in', company.id), ('name', 'ilike', 'Interim (Delivered)')], limit=1)
                    
                journal = env['account.journal'].search([('company_id', '=', company.id), ('code', '=', 'STJ')], limit=1)
                if not journal:
                    journal = env['account.journal'].search([('company_id', '=', company.id), ('type', '=', 'general')], limit=1)

                if acc_valuation and acc_input and acc_output and journal:
                    for cat_ref in [
                        'rdl_core_config.product_category_liquid',
                        'rdl_core_config.product_category_empties',
                        'rdl_core_config.product_category_kits',
                        'product.product_category_all'
                    ]:
                        cat = env.ref(cat_ref, raise_if_not_found=False)
                        if cat:
                            cat.with_company(company).write({
                                'property_stock_valuation_account_id': acc_valuation.id,
                                'property_stock_account_input_categ_id': acc_input.id,
                                'property_stock_account_output_categ_id': acc_output.id,
                                'property_stock_journal': journal.id,
                                'property_valuation': 'real_time'
                            })
        except Exception:
            pass

    # 4. Configure Default Taxes for all companies (Root configuration)
    try:
        for company in env['res.company'].search([]):
            if not company.chart_template:
                continue
            with env.cr.savepoint():
                # Ensure 0% sales tax and 0% purchase tax groups exist
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

                # Find or create 0% Sales Tax
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

                # Find 7.5% Purchase VAT
                vat_purchase = env['account.tax'].search([
                    ('company_id', '=', company.id),
                    ('type_tax_use', '=', 'purchase'),
                    ('amount', '=', 7.5),
                ], limit=1)
                
                # If not found, fall back to 0% Purchase Tax
                if not vat_purchase:
                    vat_purchase = env['account.tax'].search([
                        ('company_id', '=', company.id),
                        ('type_tax_use', '=', 'purchase'),
                        ('amount', '=', 0.0),
                    ], limit=1)
                if not vat_purchase:
                    xml_tax = env.ref('rdl_core_config.tax_purchase_0', raise_if_not_found=False)
                    if xml_tax and xml_tax.company_id == company:
                        vat_purchase = xml_tax
                    else:
                        vat_purchase = env['account.tax'].create({
                            'name': '0% Purchase Tax',
                            'amount': 0.0,
                            'amount_type': 'percent',
                            'type_tax_use': 'purchase',
                            'description': '0% Purchase',
                            'company_id': company.id,
                            'tax_group_id': tax_group.id,
                        })

                # Write defaults: 0% VAT on Sales, 7.5% VAT on Purchases (POs)
                company.write({
                    'account_sale_tax_id': tax_sale.id,
                    'account_purchase_tax_id': vat_purchase.id,
                })
    except Exception as e:
        _logger.exception("Error in post_init_hook for company: %s", company.name)

    # 5. Ensure all existing brewery products are synced to create the new Empties kit component and BOM
    try:
        with env.cr.savepoint():
            brewery_templates = env['product.template'].search([('is_brewery', '=', True)])
            for template in brewery_templates:
                template._sync_brewery_components()
    except Exception:
        pass





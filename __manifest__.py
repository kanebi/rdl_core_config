# -*- coding: utf-8 -*-
{
    'name': 'RDL Core Config',
    'version': '18.0.1.0.0',
    'category': 'Operations/Inventory',
    'summary': 'Centralized configurations for warehouse, purchase, and POS channels.',
    'description': """
This module configures centralized main warehouse structure, default vendor initializations,
standard pricing mechanics, multi-channel customer workflows, and double-layered POS return logic
for Odoo 18 Community.
""",
    'depends': [
        'purchase',
        'stock_account',
        'stock',
        'point_of_sale',
        'mrp',
    ],
    'data': [
        'security/security_groups.xml',
        'security/ir.model.access.csv',
        'data/partner_data.xml',
        'data/warehouse_data.xml',
        'data/product_category_data.xml',
        'data/tax_data.xml',
        'data/bank_journal_data.xml',
        'data/pos_config_data.xml',
        'views/purchase_order_views.xml',
        'views/product_template_views.xml',
    ],
    'post_init_hook': 'post_init_hook',
    'installable': True,
    'application': False,
    'license': 'LGPL-3',
}

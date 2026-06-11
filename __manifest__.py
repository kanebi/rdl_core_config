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
        'l10n_ng',
        'product_expiry',
    ],
    'external_dependencies': {
        'python': ['pandas'],
    },
    'data': [
        'security/security_groups.xml',
        'security/ir.model.access.csv',
        'data/stock_package_type_data.xml',
        'data/partner_data.xml',
        'data/warehouse_data.xml',
        'data/product_category_data.xml',
        'data/tax_data.xml',
        'data/bank_journal_data.xml',
        'data/pos_config_data.xml',
        'views/purchase_order_views.xml',
        'views/product_template_views.xml',
        'views/pos_config_views.xml',
        'wizard/bank_statement_import_wizard_views.xml',
        'wizard/product_import_wizard_views.xml',
        'views/inventory_import_wizard_views.xml',
        'views/category_import_wizard_views.xml',
        'views/account_import_wizard_views.xml',
    ],
    'assets': {
        'point_of_sale._assets_pos': [
            'rdl_core_config/static/src/app/product_screen_patch.js',
        ],
    },
    'post_init_hook': 'post_init_hook',
    'installable': True,
    'application': False,
    'license': 'LGPL-3',
}

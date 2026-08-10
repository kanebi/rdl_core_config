# -*- coding: utf-8 -*-
from odoo import api, fields, models


class ProductProduct(models.Model):
    _inherit = 'product.product'

    rdl_whole_valuation = fields.Float(
        string="Valuation",
        compute="_compute_rdl_whole_valuation",
        help="Valuation calculated as Quantity On Hand * Unit Cost (Standard Price).",
    )

    @api.depends('qty_available', 'standard_price')
    def _compute_rdl_whole_valuation(self):
        for product in self:
            product.rdl_whole_valuation = product.qty_available * product.standard_price

    def _process_pos_ui_product_product(self, products, config_id):
        super()._process_pos_ui_product_product(products, config_id)

        location = config_id._get_pos_stock_location()
        if not location:
            return

        product_ids = [p['id'] for p in products]
        product_records = self.env['product.product'].browse(product_ids)
        storable = product_records.filtered('is_storable')
        quantities = storable.with_context(location=location.id)._compute_quantities_dict(
            lot_id=False, owner_id=False, package_id=False, from_date=False, to_date=False
        )

        qty_map = {}
        for product in product_records:
            if not product.is_storable:
                qty_map[product.id] = 9999.0
            else:
                qty_map[product.id] = quantities.get(product.id, {}).get('qty_available', 0.0)

        for product_data in products:
            product_data['pos_qty_available'] = qty_map.get(product_data['id'], 0.0)

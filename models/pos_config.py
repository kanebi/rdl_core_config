# -*- coding: utf-8 -*-
from odoo import fields, models


class PosConfig(models.Model):
    _inherit = 'pos.config'

    stock_location_id = fields.Many2one(
        'stock.location',
        string='POS Stock Location',
        domain="[('usage', '=', 'internal')]",
        help="Explicitly filter POS products to show only those present in this location. If empty, falls back to the picking type's default source location.",
    )

    negative_stock_alert = fields.Boolean(
        string="Negative Stock Alert",
        default=False,
        help="Warn if the cashier adds a product with zero or negative stock.",
    )

    def _get_pos_stock_location(self):
        self.ensure_one()
        return self.stock_location_id or self.picking_type_id.default_location_src_id

    def _get_available_product_domain(self):
        domain = super()._get_available_product_domain()

        if self.negative_stock_alert:
            return domain

        filter_location = self.stock_location_id
        if not filter_location:
            return domain

        all_pos_products = self.env['product.product'].search(domain)
        storable = all_pos_products.filtered('is_storable')
        non_storable_ids = (all_pos_products - storable).ids

        quantities = storable.with_context(location=filter_location.id)._compute_quantities_dict(
            lot_id=False, owner_id=False, package_id=False, from_date=False, to_date=False
        )
        allowed_ids = list(non_storable_ids)
        for product in storable:
            qty = quantities.get(product.id, {}).get('qty_available', 0.0)
            if qty > 0:
                allowed_ids.append(product.id)

        domain.append(('id', 'in', allowed_ids))
        return domain

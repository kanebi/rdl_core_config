# -*- coding: utf-8 -*-
from odoo import api, fields, models

class ProductProduct(models.Model):
    _inherit = 'product.product'

    rdl_whole_valuation = fields.Float(
        string="Valuation",
        compute="_compute_rdl_whole_valuation",
        help="Valuation calculated as Quantity On Hand * Unit Cost (Standard Price)."
    )

    @api.depends('qty_available', 'standard_price')
    def _compute_rdl_whole_valuation(self):
        for product in self:
            product.rdl_whole_valuation = product.qty_available * product.standard_price

    def _process_pos_ui_product_product(self, products, config_id):
        super()._process_pos_ui_product_product(products, config_id)
        
        # Get resolved location for stock level
        location = config_id._get_pos_stock_location()
        if not location:
            return
            
        # Get all product IDs from the loaded list
        product_ids = [p['id'] for p in products]
        product_records = self.env['product.product'].browse(product_ids)
        
        # Fetch phantom BOMs for these products
        boms = self.env['mrp.bom'].search([
            '|',
            ('product_id', 'in', product_ids),
            ('product_tmpl_id', 'in', product_records.mapped('product_tmpl_id').ids),
            ('type', '=', 'phantom')
        ])
        bom_by_prod = {}
        bom_by_tmpl = {}
        for b in boms:
            if b.product_id:
                bom_by_prod[b.product_id.id] = b
            else:
                bom_by_tmpl[b.product_tmpl_id.id] = b
                
        # Batch fetch stock quantities of components & products
        comp_product_ids = set()
        for p in product_records:
            bom = bom_by_prod.get(p.id) or bom_by_tmpl.get(p.product_tmpl_id.id)
            if bom:
                for line in bom.bom_line_ids:
                    if line.product_id and line.product_id.is_storable:
                        comp_product_ids.add(line.product_id.id)
            elif p.is_storable:
                comp_product_ids.add(p.id)
                
        comp_products = self.env['product.product'].browse(list(comp_product_ids))
        quantities = comp_products.with_context(location=location.id)._compute_quantities_dict(
            lot_id=False, owner_id=False, package_id=False, from_date=False, to_date=False
        )
        
        # Build a map of product_id -> qty_available
        qty_map = {}
        for p in product_records:
            if not p.is_storable:
                qty_map[p.id] = 9999.0 # service or consumable has infinite stock
                continue
                
            bom = bom_by_prod.get(p.id) or bom_by_tmpl.get(p.product_tmpl_id.id)
            if bom:
                has_storable_components = False
                possible_kits = []
                for line in bom.bom_line_ids:
                    if line.product_id and line.product_id.is_storable:
                        has_storable_components = True
                        qty_required = line.product_qty
                        if qty_required <= 0:
                            continue
                        comp_qty = quantities.get(line.product_id.id, {}).get('qty_available', 0.0)
                        possible_kits.append(comp_qty / qty_required)
                if has_storable_components:
                    qty = min(possible_kits) if possible_kits else 0.0
                else:
                    qty = 1.0 # default to allowed if no storable components exist
                qty_map[p.id] = qty
            else:
                qty_map[p.id] = quantities.get(p.id, {}).get('qty_available', 0.0)
                
        # Inject pos_qty_available in the products JSON data
        for p in products:
            p['pos_qty_available'] = qty_map.get(p['id'], 0.0)

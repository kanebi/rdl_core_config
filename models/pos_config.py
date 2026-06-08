# -*- coding: utf-8 -*-
from odoo import api, fields, models

class PosConfig(models.Model):
    _inherit = 'pos.config'

    stock_location_id = fields.Many2one(
        'stock.location', 
        string='POS Stock Location',
        domain="[('usage', '=', 'internal')]",
        help="Explicitly filter POS products to show only those present in this location. If empty, falls back to the picking type's default source location."
    )

    negative_stock_alert = fields.Boolean(
        string="Negative Stock Alert",
        default=False,
        help="Warn if the cashier adds a product with zero or negative stock."
    )

    def _get_pos_stock_location(self):
        self.ensure_one()
        return self.stock_location_id or self.picking_type_id.default_location_src_id

    def _get_available_product_domain(self):
        domain = super(PosConfig, self)._get_available_product_domain()
        
        if self.negative_stock_alert:
            return domain
            
        # Determine stock location to filter by
        filter_location = False
        if self.stock_location_id:
            filter_location = self.stock_location_id
        else:
            is_van_pos = False
            if self.name and 'Van' in self.name:
                is_van_pos = True
            else:
                src_loc = self.picking_type_id.default_location_src_id
                if src_loc:
                    parent = src_loc
                    while parent:
                        if parent.name == 'Vans':
                            is_van_pos = True
                            break
                        parent = parent.location_id
            if is_van_pos:
                filter_location = self.picking_type_id.default_location_src_id
                    
        if filter_location:
            # Find all POS-enabled products using the base domain (including global products)
            all_pos_products = self.env['product.product'].search(domain)
            
            allowed_product_ids = []
            
            # Fetch all phantom BOMs for these products
            boms = self.env['mrp.bom'].search([
                '|',
                ('product_id', 'in', all_pos_products.ids),
                ('product_tmpl_id', 'in', all_pos_products.mapped('product_tmpl_id').ids),
                ('type', '=', 'phantom')
            ])
            bom_by_prod = {}
            bom_by_tmpl = {}
            for b in boms:
                if b.product_id:
                    bom_by_prod[b.product_id.id] = b
                else:
                    bom_by_tmpl[b.product_tmpl_id.id] = b
                    
            # Batch fetch stock quantities of components
            comp_product_ids = set()
            for p in all_pos_products:
                bom = bom_by_prod.get(p.id) or bom_by_tmpl.get(p.product_tmpl_id.id)
                if bom:
                    for line in bom.bom_line_ids:
                        if line.product_id and line.product_id.is_storable:
                            comp_product_ids.add(line.product_id.id)
                elif p.is_storable:
                    comp_product_ids.add(p.id)
                    
            comp_products = self.env['product.product'].browse(list(comp_product_ids))
            quantities = comp_products.with_context(location=filter_location.id)._compute_quantities_dict(
                lot_id=False, owner_id=False, package_id=False, from_date=False, to_date=False
            )
            
            for p in all_pos_products:
                if not p.is_storable:
                    allowed_product_ids.append(p.id)
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
                    if qty > 0:
                        allowed_product_ids.append(p.id)
                else:
                    qty = quantities.get(p.id, {}).get('qty_available', 0.0)
                    if qty > 0:
                        allowed_product_ids.append(p.id)
                        
            domain.append(('id', 'in', allowed_product_ids))
            
        return domain

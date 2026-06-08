# -*- coding: utf-8 -*-
from odoo import api, fields, models

class ProductProduct(models.Model):
    _inherit = 'product.product'

    @api.model
    def _load_pos_data(self, data):
        res = super(ProductProduct, self)._load_pos_data(data)
        config = self.env['pos.config'].browse(data['pos.config']['data'][0]['id'])
        
        # Check if this is a Van POS
        is_van_pos = False
        if config.name and 'Van' in config.name:
            is_van_pos = True
        else:
            src_loc = config.picking_type_id.default_location_src_id
            if src_loc:
                parent = src_loc
                while parent:
                    if parent.name == 'Vans':
                        is_van_pos = True
                        break
                    parent = parent.location_id
                    
        if is_van_pos:
            src_location = config.picking_type_id.default_location_src_id
            if src_location:
                product_ids = [p['id'] for p in res['data']]
                products_obj = self.browse(product_ids)
                
                # Fetch all phantom BOMs for these products
                boms = self.env['mrp.bom'].search([
                    '|',
                    ('product_id', 'in', product_ids),
                    ('product_tmpl_id', 'in', products_obj.mapped('product_tmpl_id').ids),
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
                for p in products_obj:
                    bom = bom_by_prod.get(p.id) or bom_by_tmpl.get(p.product_tmpl_id.id)
                    if bom:
                        for line in bom.bom_line_ids:
                            if line.product_id and line.product_id.is_storable:
                                comp_product_ids.add(line.product_id.id)
                    elif p.is_storable:
                        comp_product_ids.add(p.id)
                        
                comp_products = self.env['product.product'].browse(list(comp_product_ids))
                quantities = comp_products.with_context(location=src_location.id)._compute_quantities_dict(
                    lot_id=False, owner_id=False, package_id=False, from_date=False, to_date=False
                )
                
                filtered_data = []
                for p in res['data']:
                    p_obj = products_obj.filtered(lambda x: x.id == p['id'])
                    if not p_obj:
                        continue
                    # Services and other non-storable products are always allowed
                    if not p_obj.is_storable:
                        filtered_data.append(p)
                        continue
                        
                    bom = bom_by_prod.get(p['id']) or bom_by_tmpl.get(p_obj.product_tmpl_id.id)
                    if bom:
                        possible_kits = []
                        for line in bom.bom_line_ids:
                            if line.product_id and line.product_id.is_storable:
                                qty_required = line.product_qty
                                if qty_required <= 0:
                                    continue
                                comp_qty = quantities.get(line.product_id.id, {}).get('qty_available', 0.0)
                                possible_kits.append(comp_qty / qty_required)
                        qty = min(possible_kits) if possible_kits else 0.0
                        if qty > 0:
                            filtered_data.append(p)
                    else:
                        qty = quantities.get(p['id'], {}).get('qty_available', 0.0)
                        if qty > 0:
                            filtered_data.append(p)
                            
                res['data'] = filtered_data
        return res

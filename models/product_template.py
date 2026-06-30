# -*- coding: utf-8 -*-
from odoo import api, fields, models

class ProductTemplate(models.Model):
    _inherit = 'product.template'

    is_brewery = fields.Boolean("Bottled Component", default=False)
    is_packaged_drinks = fields.Boolean("Packaged Drinks (No Deposit)", default=False)

    type = fields.Selection(default='consu')
    is_storable = fields.Boolean(default=True)
    available_in_pos = fields.Boolean(default=True)

    @api.onchange('is_brewery', 'list_price', 'standard_price', 'brewery_liquid_qty')
    def _onchange_is_brewery(self):
        if self.is_brewery:
            self.is_packaged_drinks = False
            ICPSudo = self.env['ir.config_parameter'].sudo()
            default_crate = float(ICPSudo.get_param('rdl_core_config.brewery_default_crate_deposit', 2300.0) or 2300.0)
            default_bottle_total = float(ICPSudo.get_param('rdl_core_config.brewery_default_bottle_deposit', 2700.0) or 2700.0)
            
            qty = self.brewery_liquid_qty or 24.0
            if qty <= 0.0:
                qty = 24.0
            
            if not self.brewery_crate_price:
                self.brewery_crate_price = default_crate
            if not self.brewery_crate_cost:
                self.brewery_crate_cost = default_crate
                
            if not self.brewery_bottle_price:
                self.brewery_bottle_price = default_bottle_total / qty
            if not self.brewery_bottle_cost:
                self.brewery_bottle_cost = default_bottle_total / qty
                
            empties_price_total = self.brewery_crate_price + (self.brewery_bottle_price * qty)
            empties_cost_total = self.brewery_crate_cost + (self.brewery_bottle_cost * qty)
            
            self.brewery_liquid_price = abs((self.list_price - empties_price_total) / qty)
            self.brewery_liquid_cost = abs((self.standard_price - empties_cost_total) / qty)
            self.brewery_bottle_qty = qty

    @api.onchange('is_packaged_drinks')
    def _onchange_is_packaged_drinks(self):
        if self.is_packaged_drinks:
            self.is_brewery = False

    def _default_route_ids(self):
        routes = self.env['stock.route'].search([
            ('name', 'in', ['Buy', 'Replenish Van-001 from WH/Main']),
            '|', ('company_id', '=', self.env.company.id), ('company_id', '=', False)
        ])
        if routes:
            return [(6, 0, routes.ids)]
        return False

    route_ids = fields.Many2many(default=_default_route_ids)

    @api.model
    def default_get(self, fields_list):
        res = super(ProductTemplate, self).default_get(fields_list)
        if 'is_brewery' in fields_list:
            if not self.env.context.get('install_mode') and not self.env.context.get('module') and self.env.registry.ready:
                res['is_brewery'] = True
            else:
                res['is_brewery'] = False
        return res

    # Liquid Component
    brewery_liquid_price = fields.Float("Liquid Sales Price", default=0.0)
    brewery_liquid_cost = fields.Float("Liquid Cost", default=0.0)
    brewery_liquid_qty = fields.Float("Liquid Qty in Crate", default=24.0)

    # Empty Bottle Component
    brewery_bottle_price = fields.Float("Empty Bottle Sales Price", default=0.0)
    brewery_bottle_cost = fields.Float("Empty Bottle Cost", default=0.0)
    brewery_bottle_qty = fields.Float("Empty Bottle Qty in Crate", default=24.0)

    # Empty Crate Component
    brewery_crate_price = fields.Float("Empty Crate Sales Price", default=0.0)
    brewery_crate_cost = fields.Float("Empty Crate Cost", default=0.0)

    # Component links
    liquid_product_id = fields.Many2one('product.product', "Liquid Product", readonly=True, ondelete='set null')
    bottle_product_id = fields.Many2one('product.product', "Empty Bottle Product", readonly=True, ondelete='set null')
    crate_product_id = fields.Many2one('product.product', "Empty Crate Product", readonly=True, ondelete='set null')
    full_bottle_product_id = fields.Many2one('product.product', "Full Bottle Product", readonly=True, ondelete='set null')
    empties_product_id = fields.Many2one('product.product', "Empties Kit Product", readonly=True, ondelete='set null')


    @api.onchange('brewery_liquid_qty')
    def _onchange_liquid_qty(self):
        self.brewery_bottle_qty = self.brewery_liquid_qty

    @api.onchange('brewery_bottle_qty')
    def _onchange_bottle_qty(self):
        self.brewery_liquid_qty = self.brewery_bottle_qty

    def _get_brewery_pricing_values(self, vals, template=None):
        ICPSudo = self.env['ir.config_parameter'].sudo()
        default_crate = float(ICPSudo.get_param('rdl_core_config.brewery_default_crate_deposit', 2300.0) or 2300.0)
        default_bottle_total = float(ICPSudo.get_param('rdl_core_config.brewery_default_bottle_deposit', 2700.0) or 2700.0)
        
        qty = vals.get('brewery_liquid_qty')
        if qty is None:
            qty = template.brewery_liquid_qty if template else 24.0
        if not qty or qty <= 0.0:
            qty = 24.0
            
        list_price = vals.get('list_price')
        if list_price is None:
            list_price = template.list_price if template else 0.0
            
        standard_price = vals.get('standard_price')
        if standard_price is None:
            standard_price = template.standard_price if template else 0.0
            
        # Crate price & cost (default if 0 or empty/None)
        crate_price = vals.get('brewery_crate_price')
        if crate_price is None:
            crate_price = template.brewery_crate_price if template else 0.0
        if not crate_price:
            crate_price = default_crate
            
        crate_cost = vals.get('brewery_crate_cost')
        if crate_cost is None:
            crate_cost = template.brewery_crate_cost if template else 0.0
        if not crate_cost:
            crate_cost = default_crate
            
        # Bottle price & cost (default if 0 or empty/None)
        bottle_price = vals.get('brewery_bottle_price')
        if bottle_price is None:
            bottle_price = template.brewery_bottle_price if template else 0.0
        if not bottle_price:
            bottle_price = default_bottle_total / qty
            
        bottle_cost = vals.get('brewery_bottle_cost')
        if bottle_cost is None:
            bottle_cost = template.brewery_bottle_cost if template else 0.0
        if not bottle_cost:
            bottle_cost = default_bottle_total / qty
            
        empties_price_total = crate_price + (bottle_price * qty)
        empties_cost_total = crate_cost + (bottle_cost * qty)
        
        liquid_price = abs((list_price - empties_price_total) / qty)
        liquid_cost = abs((standard_price - empties_cost_total) / qty)
        
        return {
            'brewery_crate_price': crate_price,
            'brewery_crate_cost': crate_cost,
            'brewery_bottle_price': bottle_price,
            'brewery_bottle_cost': bottle_cost,
            'brewery_liquid_price': liquid_price,
            'brewery_liquid_cost': liquid_cost,
            'brewery_liquid_qty': qty,
            'brewery_bottle_qty': qty,
        }

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            # Sync qty fields if one is provided
            if vals.get('brewery_liquid_qty'):
                vals['brewery_bottle_qty'] = vals['brewery_liquid_qty']
            elif vals.get('brewery_bottle_qty'):
                vals['brewery_liquid_qty'] = vals['brewery_bottle_qty']
                
            if vals.get('is_brewery'):
                calc_vals = self._get_brewery_pricing_values(vals)
                vals.update(calc_vals)
        
        templates = super(ProductTemplate, self).create(vals_list)
        for template in templates:
            if template.is_brewery:
                template._sync_brewery_components()
        return templates

    def write(self, vals):
        # Sync qty fields if one is provided
        if vals.get('brewery_liquid_qty'):
            vals['brewery_bottle_qty'] = vals['brewery_liquid_qty']
        elif vals.get('brewery_bottle_qty'):
            vals['brewery_liquid_qty'] = vals['brewery_bottle_qty']

        # If trigger fields are changed, update the pricing values first
        recalc_fields = {'is_brewery', 'list_price', 'standard_price', 'brewery_liquid_qty', 'brewery_crate_price', 'brewery_crate_cost', 'brewery_bottle_price', 'brewery_bottle_cost'}
        if any(f in vals for f in recalc_fields):
            for template in self:
                is_brewery = vals.get('is_brewery', template.is_brewery)
                if is_brewery:
                    calc_vals = self._get_brewery_pricing_values(vals, template=template)
                    super(ProductTemplate, template).write(calc_vals)

        res = super(ProductTemplate, self).write(vals)

        # 1. If parent brewery fields are edited, sync child products and pricing
        brewery_fields = {
            'name', 'is_brewery',
            'brewery_liquid_price', 'brewery_liquid_cost', 'brewery_liquid_qty',
            'brewery_bottle_price', 'brewery_bottle_cost', 'brewery_bottle_qty',
            'brewery_crate_price', 'brewery_crate_cost',
            'list_price', 'standard_price'
        }
        if any(f in vals for f in brewery_fields):
            for template in self.filtered(lambda t: t.is_brewery):
                template._sync_brewery_components()

        # 2. If a child's price/cost changes, update the parent pricing
        if 'list_price' in vals or 'standard_price' in vals:
            variants = self.mapped('product_variant_ids')
            if variants:
                parents = self.env['product.template'].search([
                    '|', '|',
                    ('liquid_product_id', 'in', variants.ids),
                    ('bottle_product_id', 'in', variants.ids),
                    ('crate_product_id', 'in', variants.ids)
                ])
                for parent in parents:
                    parent._sync_from_child_components()

        return res

    def _sync_brewery_components(self):
        self.ensure_one()
        # Calculate totals (statically using 1.0 for empty crate quantity)
        total_price = (
            (self.brewery_liquid_price * self.brewery_liquid_qty) +
            (self.brewery_bottle_price * self.brewery_bottle_qty) +
            (self.brewery_crate_price * 1.0)
        )
        total_cost = (
            (self.brewery_liquid_cost * self.brewery_liquid_qty) +
            (self.brewery_bottle_cost * self.brewery_bottle_qty) +
            (self.brewery_crate_cost * 1.0)
        )

        # Determine the target UoM based on the liquid/bottle quantity (e.g. Crate x24)
        qty = self.brewery_liquid_qty
        categ = self.env.ref('rdl_core_config.uom_categ_brewery', raise_if_not_found=False)
        
        target_uom = False
        if qty == 24.0:
            target_uom = self.env.ref('rdl_core_config.uom_crate', raise_if_not_found=False)
        elif categ:
            uom_name = f"Crate x{int(qty)}"
            target_uom = self.env['uom.uom'].search([
                ('name', '=', uom_name),
                ('category_id', '=', categ.id)
            ], limit=1)
            if not target_uom:
                target_uom = self.env['uom.uom'].create({
                    'name': uom_name,
                    'category_id': categ.id,
                    'uom_type': 'bigger',
                    'factor_inv': qty,
                    'rounding': 1.0,
                })

        uom_vals = {}
        if target_uom and self.uom_id != target_uom:
            uom_vals = {
                'uom_id': target_uom.id,
                'uom_po_id': target_uom.id
            }

        super(ProductTemplate, self).write({
            'list_price': total_price,
            'standard_price': total_cost,
            **uom_vals
        })

        # Get standard Buy route and assign to routes list
        buy_route = self.env['stock.route'].search([('name', '=', 'Buy')], limit=1)
        routes_to_set = self.route_ids.ids
        if buy_route and buy_route.id not in routes_to_set:
            routes_to_set.append(buy_route.id)

        # Build list of suppliers for components based on parent's suppliers
        # Map price to the respective component cost
        liquid_sellers = [(0, 0, {
            'partner_id': s.partner_id.id,
            'price': self.brewery_liquid_cost,
            'min_qty': s.min_qty,
            'delay': s.delay,
            'currency_id': s.currency_id.id,
        }) for s in self.seller_ids]

        bottle_sellers = [(0, 0, {
            'partner_id': s.partner_id.id,
            'price': self.brewery_bottle_cost,
            'min_qty': s.min_qty,
            'delay': s.delay,
            'currency_id': s.currency_id.id,
        }) for s in self.seller_ids]

        crate_sellers = [(0, 0, {
            'partner_id': s.partner_id.id,
            'price': self.brewery_crate_cost,
            'min_qty': s.min_qty,
            'delay': s.delay,
            'currency_id': s.currency_id.id,
        }) for s in self.seller_ids]

        full_bottle_sellers = [(0, 0, {
            'partner_id': s.partner_id.id,
            'price': self.brewery_liquid_cost + self.brewery_bottle_cost,
            'min_qty': s.min_qty,
            'delay': s.delay,
            'currency_id': s.currency_id.id,
        }) for s in self.seller_ids]

        empties_sellers = [(0, 0, {
            'partner_id': s.partner_id.id,
            'price': (self.brewery_bottle_cost * self.brewery_bottle_qty) + self.brewery_crate_cost,
            'min_qty': s.min_qty,
            'delay': s.delay,
            'currency_id': s.currency_id.id,
        }) for s in self.seller_ids]

        # 1. Sync/Create Liquid Template (available_in_pos = False)
        base_name = self.name.split('(')[0].strip() if self.name else ""
        if not self.liquid_product_id:
            liquid_tmpl = self.env['product.template'].create({
                'name': f"{base_name} (Liquid)",
                'categ_id': self.env.ref('rdl_core_config.product_category_liquid').id,
                'list_price': self.brewery_liquid_price,
                'standard_price': self.brewery_liquid_cost,
                'uom_id': self.env.ref('rdl_core_config.uom_bottle').id,
                'uom_po_id': self.env.ref('rdl_core_config.uom_bottle').id,
                'type': 'consu',
                'is_storable': True,
                'available_in_pos': False,
                'is_brewery': False,
                'route_ids': [(6, 0, routes_to_set)],
                'seller_ids': liquid_sellers,
            })
            super(ProductTemplate, self).write({
                'liquid_product_id': liquid_tmpl.product_variant_id.id
            })
        else:
            self.liquid_product_id.product_tmpl_id.write({
                'name': f"{base_name} (Liquid)",
                'list_price': self.brewery_liquid_price,
                'standard_price': self.brewery_liquid_cost,
                'available_in_pos': False,
                'is_brewery': False,
                'route_ids': [(6, 0, routes_to_set)],
                'seller_ids': [(5, 0, 0)] + liquid_sellers,
            })

        # 2. Sync/Create Bottle Template (available_in_pos = True)
        if not self.bottle_product_id:
            bottle_tmpl = self.env['product.template'].create({
                'name': f"{base_name} (Empty Bottle)",
                'categ_id': self.env.ref('rdl_core_config.product_category_empties').id,
                'list_price': self.brewery_bottle_price,
                'standard_price': self.brewery_bottle_cost,
                'uom_id': self.env.ref('rdl_core_config.uom_bottle').id,
                'uom_po_id': self.env.ref('rdl_core_config.uom_bottle').id,
                'type': 'consu',
                'is_storable': True,
                'available_in_pos': True,
                'is_brewery': False,
                'route_ids': [(6, 0, routes_to_set)],
                'seller_ids': bottle_sellers,
            })
            super(ProductTemplate, self).write({
                'bottle_product_id': bottle_tmpl.product_variant_id.id
            })
        else:
            self.bottle_product_id.product_tmpl_id.write({
                'name': f"{base_name} (Empty Bottle)",
                'list_price': self.brewery_bottle_price,
                'standard_price': self.brewery_bottle_cost,
                'available_in_pos': True,
                'is_brewery': False,
                'route_ids': [(6, 0, routes_to_set)],
                'seller_ids': [(5, 0, 0)] + bottle_sellers,
            })

        # 3. Sync/Create Crate Template (available_in_pos = True, using Crate xN name format)
        if not self.crate_product_id:
            crate_tmpl = self.env['product.template'].create({
                'name': f"{base_name} (Empty Crate x{int(qty)})",
                'categ_id': self.env.ref('rdl_core_config.product_category_empties').id,
                'list_price': self.brewery_crate_price,
                'standard_price': self.brewery_crate_cost,
                'uom_id': self.env.ref('uom.product_uom_unit').id,
                'uom_po_id': self.env.ref('uom.product_uom_unit').id,
                'type': 'consu',
                'is_storable': True,
                'available_in_pos': True,
                'is_brewery': False,
                'route_ids': [(6, 0, routes_to_set)],
                'seller_ids': crate_sellers,
            })
            super(ProductTemplate, self).write({
                'crate_product_id': crate_tmpl.product_variant_id.id
            })
        else:
            self.crate_product_id.product_tmpl_id.write({
                'name': f"{base_name} (Empty Crate x{int(qty)})",
                'list_price': self.brewery_crate_price,
                'standard_price': self.brewery_crate_cost,
                'available_in_pos': True,
                'is_brewery': False,
                'route_ids': [(6, 0, routes_to_set)],
                'seller_ids': [(5, 0, 0)] + crate_sellers,
            })

        # 3.5 Sync/Create Full Bottle Template (Liquid + Bottle Only)
        full_bottle_price = self.brewery_liquid_price + self.brewery_bottle_price
        full_bottle_cost = self.brewery_liquid_cost + self.brewery_bottle_cost
        if not self.full_bottle_product_id:
            full_bottle_tmpl = self.env['product.template'].create({
                'name': f"{base_name} (Full Bottle)",
                'categ_id': self.env.ref('rdl_core_config.product_category_kits').id,
                'list_price': full_bottle_price,
                'standard_price': full_bottle_cost,
                'uom_id': self.env.ref('rdl_core_config.uom_bottle').id,
                'uom_po_id': self.env.ref('rdl_core_config.uom_bottle').id,
                'type': 'consu',
                'is_storable': True,
                'available_in_pos': True,
                'is_brewery': False,
                'route_ids': [(6, 0, routes_to_set)],
                'seller_ids': full_bottle_sellers,
            })
            super(ProductTemplate, self).write({
                'full_bottle_product_id': full_bottle_tmpl.product_variant_id.id
            })
        else:
            self.full_bottle_product_id.product_tmpl_id.write({
                'name': f"{base_name} (Full Bottle)",
                'list_price': full_bottle_price,
                'standard_price': full_bottle_cost,
                'available_in_pos': True,
                'is_brewery': False,
                'route_ids': [(6, 0, routes_to_set)],
                'seller_ids': [(5, 0, 0)] + full_bottle_sellers,
            })

        # Synchronize Phantom BOM for Full Bottle (1x Liquid + 1x Bottle)
        full_bottle_bom = self.env['mrp.bom'].search([('product_tmpl_id', '=', self.full_bottle_product_id.product_tmpl_id.id)], limit=1)
        full_bottle_bom_lines = [
            (0, 0, {
                'product_id': self.liquid_product_id.id,
                'product_qty': 1.0,
                'product_uom_id': self.env.ref('rdl_core_config.uom_bottle').id,
            }),
            (0, 0, {
                'product_id': self.bottle_product_id.id,
                'product_qty': 1.0,
                'product_uom_id': self.env.ref('rdl_core_config.uom_bottle').id,
            })
        ]
        if not full_bottle_bom:
            self.env['mrp.bom'].create({
                'product_tmpl_id': self.full_bottle_product_id.product_tmpl_id.id,
                'product_uom_id': self.env.ref('rdl_core_config.uom_bottle').id,
                'type': 'phantom',
                'bom_line_ids': full_bottle_bom_lines,
            })
        else:
            full_bottle_bom.write({
                'product_uom_id': self.env.ref('rdl_core_config.uom_bottle').id,
                'type': 'phantom',
                'bom_line_ids': [(5, 0, 0)] + full_bottle_bom_lines,
            })

        # 3.7 Sync/Create Empties Template (Empty Bottle xN + Empty Crate x1)
        empties_price = (self.brewery_bottle_price * self.brewery_bottle_qty) + self.brewery_crate_price
        empties_cost = (self.brewery_bottle_cost * self.brewery_bottle_qty) + self.brewery_crate_cost
        if not self.empties_product_id:
            empties_tmpl = self.env['product.template'].create({
                'name': f"{base_name} (Empties)",
                'categ_id': self.env.ref('rdl_core_config.product_category_empties').id,
                'list_price': empties_price,
                'standard_price': empties_cost,
                'uom_id': self.env.ref('uom.product_uom_unit').id,
                'uom_po_id': self.env.ref('uom.product_uom_unit').id,
                'type': 'consu',
                'is_storable': True,
                'available_in_pos': True,
                'is_brewery': False,
                'route_ids': [(6, 0, routes_to_set)],
                'seller_ids': empties_sellers,
            })
            super(ProductTemplate, self).write({
                'empties_product_id': empties_tmpl.product_variant_id.id
            })
        else:
            self.empties_product_id.product_tmpl_id.write({
                'name': f"{base_name} (Empties)",
                'list_price': empties_price,
                'standard_price': empties_cost,
                'available_in_pos': True,
                'is_brewery': False,
                'route_ids': [(6, 0, routes_to_set)],
                'seller_ids': [(5, 0, 0)] + empties_sellers,
            })

        # Synchronize Phantom BOM for Empties (N x Empty Bottle + 1 x Empty Crate)
        empties_bom = self.env['mrp.bom'].search([('product_tmpl_id', '=', self.empties_product_id.product_tmpl_id.id)], limit=1)
        empties_bom_lines = [
            (0, 0, {
                'product_id': self.bottle_product_id.id,
                'product_qty': self.brewery_bottle_qty,
                'product_uom_id': self.env.ref('rdl_core_config.uom_bottle').id,
            }),
            (0, 0, {
                'product_id': self.crate_product_id.id,
                'product_qty': 1.0,
                'product_uom_id': self.env.ref('uom.product_uom_unit').id,
            })
        ]
        if not empties_bom:
            self.env['mrp.bom'].create({
                'product_tmpl_id': self.empties_product_id.product_tmpl_id.id,
                'product_uom_id': self.env.ref('uom.product_uom_unit').id,
                'type': 'phantom',
                'bom_line_ids': empties_bom_lines,
            })
        else:
            empties_bom.write({
                'product_uom_id': self.env.ref('uom.product_uom_unit').id,
                'type': 'phantom',
                'bom_line_ids': [(5, 0, 0)] + empties_bom_lines,
            })

        # 4. Synchronize Phantom BOM for Crate Bundle (statically using 1.0 for empty crate quantity)
        bom = self.env['mrp.bom'].search([('product_tmpl_id', '=', self.id)], limit=1)
        bom_line_vals = [
            (0, 0, {
                'product_id': self.liquid_product_id.id,
                'product_qty': self.brewery_liquid_qty,
                'product_uom_id': self.env.ref('rdl_core_config.uom_bottle').id,
            }),
            (0, 0, {
                'product_id': self.bottle_product_id.id,
                'product_qty': self.brewery_bottle_qty,
                'product_uom_id': self.env.ref('rdl_core_config.uom_bottle').id,
            }),
            (0, 0, {
                'product_id': self.crate_product_id.id,
                'product_qty': 1.0,
                'product_uom_id': self.env.ref('uom.product_uom_unit').id,
            })
        ]
        
        bom_uom = self.uom_id or target_uom
        
        if not bom:
            self.env['mrp.bom'].create({
                'product_tmpl_id': self.id,
                'product_uom_id': bom_uom.id if bom_uom else False,
                'type': 'phantom',
                'bom_line_ids': bom_line_vals,
            })
        else:
            bom.write({
                'product_uom_id': bom_uom.id if bom_uom else False,
                'type': 'phantom',
                'bom_line_ids': [(5, 0, 0)] + bom_line_vals,
            })

    def _sync_from_child_components(self):
        self.ensure_one()
        # Read current child prices/costs
        liquid_price = self.liquid_product_id.list_price or 0.0
        liquid_cost = self.liquid_product_id.standard_price or 0.0
        bottle_price = self.bottle_product_id.list_price or 0.0
        bottle_cost = self.bottle_product_id.standard_price or 0.0
        crate_price = self.crate_product_id.list_price or 0.0
        crate_cost = self.crate_product_id.standard_price or 0.0

        # Calculate totals (statically using 1.0 for empty crate quantity)
        total_price = (
            (liquid_price * self.brewery_liquid_qty) +
            (bottle_price * self.brewery_bottle_qty) +
            (crate_price * 1.0)
        )
        total_cost = (
            (liquid_cost * self.brewery_liquid_qty) +
            (bottle_cost * self.brewery_bottle_qty) +
            (crate_cost * 1.0)
        )

        super(ProductTemplate, self).write({
            'brewery_liquid_price': liquid_price,
            'brewery_liquid_cost': liquid_cost,
            'brewery_bottle_price': bottle_price,
            'brewery_bottle_cost': bottle_cost,
            'brewery_crate_price': crate_price,
            'brewery_crate_cost': crate_cost,
            'list_price': total_price,
            'standard_price': total_cost,
        })

        # Also update Full Bottle price/cost if linked
        if self.full_bottle_product_id:
            self.full_bottle_product_id.product_tmpl_id.write({
                'list_price': liquid_price + bottle_price,
                'standard_price': liquid_cost + bottle_cost,
                'is_brewery': False,
            })

        # Also update Empties price/cost if linked
        if self.empties_product_id:
            self.empties_product_id.product_tmpl_id.write({
                'list_price': (bottle_price * self.brewery_bottle_qty) + crate_price,
                'standard_price': (bottle_cost * self.brewery_bottle_qty) + crate_cost,
                'is_brewery': False,
            })

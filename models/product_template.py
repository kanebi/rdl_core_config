# -*- coding: utf-8 -*-
from odoo import api, fields, models


class ProductTemplate(models.Model):
    _inherit = 'product.template'

    type = fields.Selection(default='consu')
    is_storable = fields.Boolean(default=True)
    available_in_pos = fields.Boolean(default=True)

    pack_qty = fields.Float(
        "Units per Pack",
        default=0.0,
        help="When set (>1), the main UoM and list price use the full pack (e.g. Crate x24). "
             "Bottle/Can in the same UoM category converts price/qty on order lines.",
    )
    pack_uom_type = fields.Selection(
        [
            ('crate', 'Crate'),
            ('carton', 'Carton'),
            ('case', 'Case'),
        ],
        string="Pack Type",
        default='crate',
        help="Larger purchase/pack UoM label (e.g. Crate x24).",
    )

    def _uses_pack_uom(self):
        self.ensure_one()
        return bool(self.pack_qty and self.pack_qty > 1)

    def _default_route_ids(self):
        routes = self.env['stock.route'].search([
            ('name', 'in', ['Buy', 'Replenish Van-001 from WH/Main']),
            '|', ('company_id', '=', self.env.company.id), ('company_id', '=', False),
        ])
        if routes:
            return [(6, 0, routes.ids)]
        return False

    route_ids = fields.Many2many(default=_default_route_ids)

    def _get_unit_uom(self):
        """Reference unit (Bottle or Can) for pack conversion."""
        self.ensure_one()
        if self.pack_uom_type == 'carton':
            return self.env.ref('rdl_core_config.uom_can', raise_if_not_found=False) or self.env.ref(
                'rdl_core_config.uom_bottle', raise_if_not_found=False
            )
        return self.env.ref('rdl_core_config.uom_bottle', raise_if_not_found=False)

    def _get_or_create_pack_uom(self, qty, pack_type, category):
        """Bigger pack UoM in the same category as the unit (e.g. Carton x24 = 24 bottles)."""
        qty = int(qty or 24)
        if qty <= 0:
            qty = 24
        label = {
            'crate': 'Crate',
            'carton': 'Carton',
            'case': 'Case',
        }.get(pack_type or 'crate', 'Crate')
        uom_name = f"{label} x{qty}"

        if pack_type == 'carton':
            xml_map = {
                ('carton', 12): 'rdl_core_config.uom_can_carton_12',
                ('carton', 24): 'rdl_core_config.uom_can_carton_24',
                ('case', 12): 'rdl_core_config.uom_can_case_12',
                ('case', 24): 'rdl_core_config.uom_can_case_24',
            }
        else:
            xml_map = {
                ('crate', 12): 'rdl_core_config.uom_crate_12',
                ('crate', 24): 'rdl_core_config.uom_crate',
                ('case', 12): 'rdl_core_config.uom_case_12',
                ('case', 24): 'rdl_core_config.uom_case_24',
                ('carton', 12): 'rdl_core_config.uom_carton_12',
                ('carton', 24): 'rdl_core_config.uom_carton_24',
            }

        xmlid = xml_map.get((pack_type or 'crate', qty))
        if xmlid:
            uom = self.env.ref(xmlid, raise_if_not_found=False)
            if uom:
                if uom.rounding > 0.0001:
                    uom.sudo().write({'rounding': 0.0001})
                return uom

        UoM = self.env['uom.uom']
        uom = UoM.search([
            ('name', '=', uom_name),
            ('category_id', '=', category.id),
        ], limit=1)
        if uom:
            if uom.rounding > 0.0001:
                uom.sudo().write({'rounding': 0.0001})
            return uom

        return UoM.create({
            'name': uom_name,
            'category_id': category.id,
            'uom_type': 'bigger',
            'factor_inv': float(qty),
            'rounding': 0.0001,
        })

    def _has_stock_moves(self):
        self.ensure_one()
        return bool(self.env['stock.move'].search_count([
            ('product_id', 'in', self.product_variant_ids.ids),
        ], limit=1))

    def _configure_pack_uoms(self):
        """
        When pack_qty > 1, set main UoM to the full pack (Crate/Carton xN).
        Bottle/Can stays as the reference unit in the same category.
        """
        for template in self:
            if not template._uses_pack_uom():
                continue
            unit_uom = template._get_unit_uom()
            if not unit_uom:
                continue
            pack_uom = template._get_or_create_pack_uom(
                template.pack_qty,
                template.pack_uom_type,
                unit_uom.category_id,
            )
            vals = {}
            locked = template._has_stock_moves()
            if template.uom_id != pack_uom and not locked:
                vals['uom_id'] = pack_uom.id
            if template.uom_po_id != pack_uom and not locked:
                vals['uom_po_id'] = pack_uom.id
            if vals:
                super(ProductTemplate, template).write(vals)

    def _clear_phantom_boms(self):
        if 'mrp.bom' not in self.env:
            return
        boms = self.env['mrp.bom'].sudo().search([
            ('product_tmpl_id', 'in', self.ids),
            ('type', '=', 'phantom'),
        ])
        if boms:
            boms.unlink()

    @api.model_create_multi
    def create(self, vals_list):
        templates = super().create(vals_list)
        to_configure = templates.filtered(lambda t: t._uses_pack_uom())
        if to_configure:
            to_configure._configure_pack_uoms()
            to_configure._clear_phantom_boms()
        return templates

    def write(self, vals):
        res = super().write(vals)
        if {'pack_qty', 'pack_uom_type'} & set(vals):
            to_configure = self.filtered(lambda t: t._uses_pack_uom())
            if to_configure:
                to_configure._configure_pack_uoms()
                to_configure._clear_phantom_boms()
        return res

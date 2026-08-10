# -*- coding: utf-8 -*-
from odoo import api, fields, models


class ProductTemplate(models.Model):
    _inherit = 'product.template'

    is_brewery = fields.Boolean(
        "Bottled Product",
        default=False,
        help="Bottled drink tracked as one SKU. Main UoM/list price is the full pack (e.g. Crate x24); select Bottle on the order line for unit price via standard UoM conversion.",
    )
    is_packaged_drinks = fields.Boolean(
        "Packaged Drinks",
        default=False,
        help="Packaged drinks (cans, PET, etc.) tracked as one SKU with pack UoM.",
    )

    type = fields.Selection(default='consu')
    is_storable = fields.Boolean(default=True)
    available_in_pos = fields.Boolean(default=True)

    pack_qty = fields.Float(
        "Units per Pack",
        default=24.0,
        help="Number of bottles/cans in one crate, carton, or case. Used for UoM conversion so selling 1 bottle keeps inventory balanced.",
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
    empties_product_id = fields.Many2one(
        'product.product',
        string="Empties Product",
        ondelete='set null',
        help="Optional consolidated empties SKU (crate + bottles deposit) for purchase/sale.",
    )

    def _default_route_ids(self):
        routes = self.env['stock.route'].search([
            ('name', 'in', ['Buy', 'Replenish Van-001 from WH/Main']),
            '|', ('company_id', '=', self.env.company.id), ('company_id', '=', False)
        ])
        if routes:
            return [(6, 0, routes.ids)]
        return False

    route_ids = fields.Many2many(default=_default_route_ids)

    @api.onchange('is_brewery')
    def _onchange_is_brewery(self):
        if self.is_brewery:
            self.is_packaged_drinks = False
            if not self.pack_qty:
                self.pack_qty = 24.0
            if not self.pack_uom_type:
                self.pack_uom_type = 'crate'

    @api.onchange('is_packaged_drinks')
    def _onchange_is_packaged_drinks(self):
        if self.is_packaged_drinks:
            self.is_brewery = False
            if not self.pack_qty:
                self.pack_qty = 24.0
            if not self.pack_uom_type:
                self.pack_uom_type = 'carton'

    def _get_unit_uom(self):
        """Category reference unit (Bottle or Can) used when selling single units."""
        self.ensure_one()
        if self.is_packaged_drinks:
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

        # Prefer XML data for common sizes
        xml_map = {
            ('crate', 12): 'rdl_core_config.uom_crate_12',
            ('crate', 24): 'rdl_core_config.uom_crate',
            ('case', 12): 'rdl_core_config.uom_case_12',
            ('case', 24): 'rdl_core_config.uom_case_24',
            ('carton', 12): 'rdl_core_config.uom_carton_12',
            ('carton', 24): 'rdl_core_config.uom_carton_24',
        }
        if self.is_packaged_drinks:
            xml_map = {
                ('carton', 12): 'rdl_core_config.uom_can_carton_12',
                ('carton', 24): 'rdl_core_config.uom_can_carton_24',
                ('case', 12): 'rdl_core_config.uom_can_case_12',
                ('case', 24): 'rdl_core_config.uom_can_case_24',
            }

        xmlid = xml_map.get((pack_type or 'crate', qty))
        if xmlid:
            uom = self.env.ref(xmlid, raise_if_not_found=False)
            if uom:
                # Fine rounding so 1 bottle = 1/24 pack does not collapse to 0
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
        Main product UoM (and list/cost price) = full pack (Crate/Carton xN).
        Bottle/Can stays in the same UoM category as the reference unit.
        On SO/PO, selecting Bottle converts price and qty via standard Odoo UoM math
        (carton_price / 24) — no custom pricing override.
        """
        for template in self:
            if not (template.is_brewery or template.is_packaged_drinks):
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
        """Remove kit/phantom BOMs so deliveries ship the finished SKU only."""
        if 'mrp.bom' not in self.env:
            return
        Bom = self.env['mrp.bom'].sudo()
        boms = Bom.search([
            ('product_tmpl_id', 'in', self.ids),
            ('type', '=', 'phantom'),
        ])
        if boms:
            boms.unlink()

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('is_brewery'):
                vals['is_packaged_drinks'] = False
                vals.setdefault('pack_qty', 24.0)
                vals.setdefault('pack_uom_type', 'crate')
            elif vals.get('is_packaged_drinks'):
                vals['is_brewery'] = False
                vals.setdefault('pack_qty', 24.0)
                vals.setdefault('pack_uom_type', 'carton')
        templates = super().create(vals_list)
        to_configure = templates.filtered(lambda t: t.is_brewery or t.is_packaged_drinks)
        if to_configure:
            to_configure._configure_pack_uoms()
            to_configure._clear_phantom_boms()
        return templates

    def write(self, vals):
        res = super().write(vals)
        uom_triggers = {'is_brewery', 'is_packaged_drinks', 'pack_qty', 'pack_uom_type'}
        if uom_triggers & set(vals):
            to_configure = self.filtered(lambda t: t.is_brewery or t.is_packaged_drinks)
            if to_configure:
                to_configure._configure_pack_uoms()
                to_configure._clear_phantom_boms()
        return res

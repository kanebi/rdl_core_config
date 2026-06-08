# -*- coding: utf-8 -*-
from odoo import api, fields, models

class PurchaseOrder(models.Model):
    _inherit = 'purchase.order'

    @api.model
    def default_get(self, fields_list):
        res = super(PurchaseOrder, self).default_get(fields_list)
        if 'partner_id' in fields_list and not res.get('partner_id'):
            default_partner = self.env.ref('rdl_core_config.res_partner_guinness_default', raise_if_not_found=False)
            if default_partner:
                res['partner_id'] = default_partner.id
        return res

    @api.model_create_multi
    def create(self, vals_list):
        default_partner = self.env.ref('rdl_core_config.res_partner_guinness_default', raise_if_not_found=False)
        if default_partner:
            for vals in vals_list:
                if not vals.get('partner_id'):
                    vals['partner_id'] = default_partner.id
        return super(PurchaseOrder, self).create(vals_list)

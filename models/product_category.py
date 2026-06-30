# -*- coding: utf-8 -*-
from odoo import api, fields, models

class ProductCategory(models.Model):
    _inherit = 'product.category'

    property_cost_method = fields.Selection(
        default='fifo'
    )

    @api.model_create_multi
    def create(self, vals_list):
        records = super(ProductCategory, self).create(vals_list)
        # Ensure property_cost_method defaults to fifo for all companies
        for record in records:
            for company in self.env['res.company'].search([]):
                if not record.with_company(company).property_cost_method or record.with_company(company).property_cost_method == 'standard':
                    record.with_company(company).write({'property_cost_method': 'fifo'})
        return records

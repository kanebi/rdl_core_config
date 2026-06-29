# -*- coding: utf-8 -*-
from odoo import fields, models

class ResCompany(models.Model):
    _inherit = 'res.company'

    brewery_default_crate_deposit = fields.Float("Default Crate Deposit", default=2300.0)
    brewery_default_bottle_deposit = fields.Float("Default Bottle Deposit", default=2700.0)

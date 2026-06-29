# -*- coding: utf-8 -*-
from odoo import fields, models

class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    pos_negative_stock_alert = fields.Boolean(
        related='pos_config_id.negative_stock_alert',
        readonly=False,
        string="Negative Stock Alert",
        help="Alert if product has negative or zero stock in the active location when clicked in POS."
    )

    brewery_default_crate_deposit = fields.Float(
        related='company_id.brewery_default_crate_deposit',
        readonly=False,
        string="Default Crate Deposit",
    )
    brewery_default_bottle_deposit = fields.Float(
        related='company_id.brewery_default_bottle_deposit',
        readonly=False,
        string="Default Bottle Deposit",
    )

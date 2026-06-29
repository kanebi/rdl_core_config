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
        config_parameter='rdl_core_config.brewery_default_crate_deposit',
        string="Default Crate Deposit",
        default=2300.0,
    )
    brewery_default_bottle_deposit = fields.Float(
        config_parameter='rdl_core_config.brewery_default_bottle_deposit',
        string="Default Bottle Deposit",
        default=2700.0,
    )

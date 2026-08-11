# -*- coding: utf-8 -*-
import base64
from odoo import fields, models, _
from odoo.exceptions import UserError


class InventoryImportWizard(models.TransientModel):
    _name = 'inventory.import.wizard'
    _description = 'Import Opening Inventory from Excel'

    excel_file = fields.Binary(string='Excel File', required=True)
    file_name = fields.Char(string='File Name')
    sheet_index = fields.Integer(
        string='Sheet Index',
        default=3,
        required=True,
        help='0-based index (default: 3 = Opening Inventory).',
    )
    header_row_index = fields.Integer(
        string='Header Row',
        default=4,
        required=True,
        help='1-based header row number (default: 4).',
    )

    def action_import_inventory(self):
        self.ensure_one()
        if not self.excel_file:
            raise UserError(_('Please upload an Excel file.'))

        from odoo.addons.rdl_core_config.utils import excel_template as rdl_xl

        stats = rdl_xl.import_opening_inventory(
            self.env,
            base64.b64decode(self.excel_file),
            sheet_index=self.sheet_index,
            header_row=self.header_row_index,
        )

        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Import Successful'),
                'message': _(
                    '%(applied)d stock lines applied. %(missing)d SKUs not found. %(skipped)d rows skipped.'
                ) % stats,
                'sticky': False,
                'type': 'success',
                'next': {'type': 'ir.actions.act_window_close'},
            },
        }

# -*- coding: utf-8 -*-
import base64
from odoo import fields, models, _
from odoo.exceptions import UserError


class ProductImportWizard(models.TransientModel):
    _name = 'product.import.wizard'
    _description = 'Product Import Wizard'

    file = fields.Binary(string='Excel File', required=True)
    filename = fields.Char(string='File Name')
    sheet_name = fields.Char(
        string='Sheet Name or Index',
        default='1',
        help='0-based index or sheet name (default: 1 = Product Master).',
    )
    start_row = fields.Integer(
        string='Header Row',
        default=4,
        help='1-based row number where headers are located.',
    )

    def action_import(self):
        self.ensure_one()
        if not self.file:
            raise UserError(_('Please upload an Excel file.'))

        from odoo.addons.rdl_core_config.utils import excel_template as rdl_xl

        file_bytes = base64.b64decode(self.file)
        sheet_name = (self.sheet_name or '1').strip()
        if sheet_name.isdigit():
            sheet_index = int(sheet_name)
        else:
            workbook = rdl_xl.load_workbook(file_bytes)
            sheet = rdl_xl.resolve_sheet(workbook, sheet_name=sheet_name)
            sheet_index = workbook.worksheets.index(sheet)

        stats = rdl_xl.import_products(
            self.env,
            file_bytes,
            sheet_index=sheet_index,
            header_row=self.start_row,
        )

        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Import Successful'),
                'message': _(
                    '%(created)d created, %(updated)d updated, %(skipped)d skipped.'
                ) % stats,
                'type': 'success',
                'sticky': False,
                'next': {'type': 'ir.actions.act_window_close'},
            },
        }

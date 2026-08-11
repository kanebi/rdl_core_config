# -*- coding: utf-8 -*-
from odoo import fields, models, _


class RdlTemplateImportWizard(models.TransientModel):
    _name = 'rdl.template.import.wizard'
    _description = 'Import Products and Opening Stock from RDL Excel Template'

    excel_file = fields.Binary(string='RDL Excel File', required=True)
    file_name = fields.Char(string='File Name')
    import_products = fields.Boolean(string='Import Products', default=True)
    import_inventory = fields.Boolean(string='Load Opening Stock', default=True)
    product_sheet_index = fields.Integer(
        string='Product Sheet Index',
        default=1,
        help='0-based sheet index for 01-Product Master (default: 1).',
    )
    inventory_sheet_index = fields.Integer(
        string='Inventory Sheet Index',
        default=3,
        help='0-based sheet index for 03-Opening Inventory (default: 3).',
    )
    header_row = fields.Integer(
        string='Header Row',
        default=4,
        help='1-based row number containing column headers (default: 4).',
    )

    def action_import(self):
        self.ensure_one()
        if not self.excel_file:
            from odoo.exceptions import UserError
            raise UserError(_('Please upload the RDL Excel template file.'))
        if not self.import_products and not self.import_inventory:
            from odoo.exceptions import UserError
            raise UserError(_('Select at least one of Import Products or Load Opening Stock.'))

        import base64
        from odoo.addons.rdl_core_config.utils import excel_template as rdl_xl

        file_bytes = base64.b64decode(self.excel_file)
        messages = []

        if self.import_products:
            product_stats = rdl_xl.import_products(
                self.env,
                file_bytes,
                sheet_index=self.product_sheet_index,
                header_row=self.header_row,
            )
            messages.append(
                _('Products: %(created)d created, %(updated)d updated, %(skipped)d skipped.') % product_stats
            )

        if self.import_inventory:
            if not self.import_products:
                pass  # products must already exist
            inv_stats = rdl_xl.import_opening_inventory(
                self.env,
                file_bytes,
                sheet_index=self.inventory_sheet_index,
                header_row=self.header_row,
            )
            messages.append(
                _('Stock: %(applied)d locations updated, %(missing)d SKUs missing, %(skipped)d rows skipped.')
                % {
                    'applied': inv_stats.get('applied', 0),
                    'missing': inv_stats.get('missing', inv_stats.get('missing_product', 0)),
                    'skipped': inv_stats.get('skipped', 0),
                }
            )

        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('RDL Template Import Complete'),
                'message': '\n'.join(messages),
                'type': 'success',
                'sticky': False,
                'next': {'type': 'ir.actions.act_window_close'},
            },
        }

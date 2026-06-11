# -*- coding: utf-8 -*-
import base64
import io
from odoo import api, fields, models, _
from odoo.exceptions import UserError
import openpyxl

class ProductImportWizard(models.TransientModel):
    _name = 'product.import.wizard'
    _description = 'Product Import Wizard'

    file = fields.Binary(string='Excel File', required=True)
    filename = fields.Char(string='File Name')
    
    sheet_name = fields.Char(string='Sheet Name or Index', default="1", help="Enter sheet name or 0-based index (e.g. 1 for second sheet)")
    start_row = fields.Integer(string='Header Row', default=4, help="Row number where the headers are located (1-indexed, e.g., 4)")

    def action_import(self):
        self.ensure_one()
        if not self.file:
            raise UserError(_("Please upload an Excel file."))
            
        try:
            file_content = base64.b64decode(self.file)
            wb = openpyxl.load_workbook(io.BytesIO(file_content), data_only=True)
            
            sheet_identifier = self.sheet_name.strip() if self.sheet_name else '0'
            if sheet_identifier.isdigit():
                sheet_idx = int(sheet_identifier)
                if sheet_idx < len(wb.worksheets):
                    sheet = wb.worksheets[sheet_idx]
                else:
                    raise UserError(_("Sheet index out of bounds."))
            else:
                if sheet_identifier in wb.sheetnames:
                    sheet = wb[sheet_identifier]
                else:
                    raise UserError(_("Sheet name '%s' not found in workbook.") % sheet_identifier)
                    
        except Exception as e:
            raise UserError(_("Error reading the Excel file: %s") % str(e))

        Product = self.env['product.template']
        Category = self.env['product.category']
        Packaging = self.env['product.packaging']
        PackageType = self.env['stock.package.type']

        headers = []
        rows = list(sheet.iter_rows(values_only=True))
        
        header_row_idx = self.start_row - 1
        if header_row_idx < 0 or header_row_idx >= len(rows):
            raise UserError(_("Header row is out of bounds."))
            
        headers = [str(h).strip() if h else '' for h in rows[header_row_idx]]
        
        def safe_float(val):
            if val is None or str(val).strip() == '' or str(val).lower() == 'nan':
                return 0.0
            try:
                return float(val)
            except ValueError:
                return 0.0

        for r_idx in range(header_row_idx + 1, len(rows)):
            row_data = rows[r_idx]
            row = dict(zip(headers, row_data))
            
            sku_val = row.get('SKU')
            if sku_val is None or str(sku_val).strip() == '' or str(sku_val).lower() == 'nan':
                continue

            sku = str(sku_val).strip()

            name = str(row.get('Product Name', '')).strip()
            unit_packaging = str(row.get('Unit Packaging', '')).strip()
            categ_name = str(row.get('Category', '')).strip()
            
            categ = False
            if categ_name and str(categ_name).lower() != 'nan':
                categ = Category.search([('name', '=ilike', categ_name)], limit=1)
                if not categ:
                    categ = Category.create({'name': categ_name})

            existing_product = Product.search([('default_code', '=', sku)], limit=1)
            
            product_vals = {
                'name': name,
                'default_code': sku,
                'type': 'consu',
                'is_storable': True,
            }
            if categ:
                product_vals['categ_id'] = categ.id
                
            if unit_packaging.lower() == 'bottle':
                product_vals.update({
                    'is_brewery': True,
                    'is_packaged_drinks': False,
                    'brewery_liquid_price': safe_float(row.get('Liquid Unit Sales Price')),
                    'brewery_liquid_cost': safe_float(row.get('Liquid Unit Cost')),
                    'brewery_liquid_qty': safe_float(row.get('Bottles in a Crate')),
                    'brewery_bottle_price': safe_float(row.get('Empty Bottle Unit Sales Price')),
                    'brewery_bottle_cost': safe_float(row.get('Empty Bottle Unit Cost')),
                    'brewery_bottle_qty': safe_float(row.get('Bottles in a Crate')),
                    'brewery_crate_price': safe_float(row.get('Empty Crate Sales Price')),
                    'brewery_crate_cost': safe_float(row.get('Empty Crate Cost')),
                })
                
                if existing_product:
                    existing_product.write(product_vals)
                else:
                    existing_product = Product.create(product_vals)
            
            elif unit_packaging.lower() in ['can', 'plastic', 'pet', 'carton']:
                carton_price = safe_float(row.get('Sales Price (₦)'))
                carton_cost = safe_float(row.get('Cost Price (₦)'))
                if not carton_cost:
                    carton_cost = safe_float(row.get('Full Crate Cost Price'))
                    
                qty_in_crate = safe_float(row.get('Bottles in a Crate'))
                
                pkg_type = PackageType.search([('name', '=ilike', unit_packaging)], limit=1)
                if not pkg_type and 'plastic' in unit_packaging.lower():
                    pkg_type = PackageType.search([('name', '=ilike', '%Plastic%')], limit=1)

                product_vals.update({
                    'is_brewery': False,
                    'is_packaged_drinks': True,
                    'list_price': carton_price,
                    'standard_price': carton_cost,
                })
                
                carton_uom = False
                if qty_in_crate > 0:
                    UoM = self.env['uom.uom']
                    carton_uom = UoM.search([
                        ('category_id.name', 'ilike', 'can'),
                        ('name', 'ilike', f'Carton x{int(qty_in_crate)}')
                    ], limit=1)
                    if not carton_uom:
                        carton_uom = UoM.search([('name', 'ilike', f'Carton x{int(qty_in_crate)}')], limit=1)
                        
                if carton_uom:
                    product_vals['uom_id'] = carton_uom.id
                    product_vals['uom_po_id'] = carton_uom.id

                if existing_product:
                    # Update uom_id first before price to avoid Odoo auto-scaling the price incorrectly
                    if 'uom_id' in product_vals and existing_product.uom_id.id != product_vals['uom_id']:
                        existing_product.write({'uom_id': product_vals['uom_id'], 'uom_po_id': product_vals['uom_po_id']})
                    existing_product.write(product_vals)
                else:
                    existing_product = Product.create(product_vals)

                if qty_in_crate > 0 and existing_product.product_variant_id:
                    pack_name = f"Case of {int(qty_in_crate)}"
                    existing_pack = Packaging.search([
                        ('product_id', '=', existing_product.product_variant_id.id),
                        ('name', '=', pack_name)
                    ], limit=1)
                    if not existing_pack:
                        Packaging.create({
                            'name': pack_name,
                            'product_id': existing_product.product_variant_id.id,
                            'qty': qty_in_crate,
                            'package_type_id': pkg_type.id if pkg_type else False,
                        })
            else:
                # Catch-all for other simple products
                full_price = safe_float(row.get('Sales Price (₦)'))
                full_cost = safe_float(row.get('Cost Price (₦)'))
                
                if full_price or full_cost:
                    product_vals.update({
                        'list_price': full_price,
                        'standard_price': full_cost,
                    })
                if existing_product:
                    existing_product.write(product_vals)
                else:
                    existing_product = Product.create(product_vals)

        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Import Successful'),
                'message': _('Products have been imported successfully.'),
                'type': 'success',
                'sticky': False,
                'next': {'type': 'ir.actions.act_window_close'},
            }
        }

import openpyxl
wb = openpyxl.load_workbook('/home/kane/odoo-18/extra-addons/rdl_core_config/RDL_Trading_Odoo.xlsx', data_only=True)
sheet = wb.worksheets[0]
rows = list(sheet.iter_rows(values_only=True))
for i in range(6):
    print(f'Row {i}:', rows[i])

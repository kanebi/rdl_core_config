import pandas as pd

try:
    df = pd.read_excel('RDL_Trading_Odoo.xlsx', sheet_name=1)
    print("COLUMNS:")
    print(df.columns.tolist())
    print("\nHEAD:")
    print(df.head(15).to_string())
except Exception as e:
    print(f"Error: {e}")

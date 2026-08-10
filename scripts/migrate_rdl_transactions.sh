#!/usr/bin/env bash
# Migrate POs → receipts → SOs → deliveries on existing rdl_staging (products must exist).
set -eu
cd /home/kane/odoo-18
source odoo-18env/bin/activate
export RDL_MIGRATE_TRANSACTIONS_ONLY=1
export RDL_SOURCE_DB="${RDL_SOURCE_DB:-rdl_staging_dev}"
./odoo-source/odoo-bin shell -d rdl_staging -c odoo.conf \
  < extra-addons/rdl_core_config/scripts/migrate_rdl_staging.py

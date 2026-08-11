# -*- coding: utf-8 -*-
"""
Odoo shell script — purge products and inventory from rdl_live.

Pipe into:
    ./odoo-source/odoo-bin shell -c odoo.conf -d rdl_live --no-http \\
        < extra-addons/rdl_core_config/scripts/purge_products_and_inventory.py
"""
import logging

_logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")


def _unlink_all(model_name, domain=None, batch=500):
    Model = env[model_name].sudo()
    domain = domain or []
    total = 0
    while True:
        records = Model.search(domain, limit=batch)
        if not records:
            break
        names = records[:5].mapped('display_name')
        _logger.info("Deleting %s x%d (e.g. %s)", model_name, len(records), names)
        records.unlink()
        total += len(records)
        env.cr.commit()
    return total


def _cancel_pickings():
    pickings = env['stock.picking'].sudo().search([])
    for picking in pickings:
        try:
            if picking.state not in ('done', 'cancel'):
                picking.action_cancel()
        except Exception as exc:
            _logger.warning("Could not cancel picking %s: %s", picking.name, exc)
    env.cr.commit()
    return len(pickings)


def _reset_draft_inventory():
    quants = env['stock.quant'].sudo().search([('inventory_quantity_set', '=', True)])
    if quants and hasattr(quants, 'action_clear_inventory_quantity'):
        quants.action_clear_inventory_quantity()
    env.cr.commit()
    return len(quants)


_logger.info("Starting product/inventory purge on database %s", env.cr.dbname)

# Transactional documents that reference products
for model in (
    'pos.order.line',
    'pos.order',
    'pos.payment',
    'pos.session',
    'sale.order.line',
    'sale.order',
    'purchase.order.line',
    'purchase.order',
):
    if model in env:
        _unlink_all(model)

_cancel_pickings()
_reset_draft_inventory()

for model in (
    'stock.valuation.layer',
    'stock.move.line',
    'stock.move',
    'stock.scrap',
    'stock.picking',
    'stock.quant',
    'stock.lot',
    'mrp.bom.line',
    'mrp.bom',
    'product.supplierinfo',
    'product.pricelist.item',
):
    if model in env:
        _unlink_all(model)

# Remove staging table from finished-SKU migration if present
env.cr.execute("""
    SELECT 1 FROM information_schema.tables
     WHERE table_name = 'rdl_finished_sku_mig_staging'
""")
if env.cr.fetchone():
    env.cr.execute("DROP TABLE rdl_finished_sku_mig_staging")
    _logger.info("Dropped rdl_finished_sku_mig_staging table")
    env.cr.commit()

# Delete all products (including archived module seed SKUs)
if 'product.template' in env:
    templates = env['product.template'].sudo().with_context(active_test=False).search([])
    _logger.info("Deleting %d product templates", len(templates))
    templates.unlink()
    env.cr.commit()

# Clear product-linked orderpoints
if 'stock.warehouse.orderpoint' in env:
    _unlink_all('stock.warehouse.orderpoint')

_logger.info("Purge complete on %s", env.cr.dbname)

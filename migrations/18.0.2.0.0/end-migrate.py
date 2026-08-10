# -*- coding: utf-8 -*-
import logging

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    """Lightweight step only; full migration runs in 18.0.2.2.0."""
    env = api.Environment(cr, SUPERUSER_ID, {})
    try:
        from odoo.addons.rdl_core_config.migrate_finished_sku import (
            enable_uom_group,
            remove_phantom_boms,
            tighten_pack_uom_rounding,
        )
        enable_uom_group(env)
        tighten_pack_uom_rounding(env)
        remove_phantom_boms(env)
        products = env['product.template'].search([
            '|', ('is_brewery', '=', True), ('is_packaged_drinks', '=', True),
        ])
        if products and hasattr(products, '_configure_pack_uoms'):
            products._configure_pack_uoms()
            products._clear_phantom_boms()
        _logger.info("18.0.2.0.0 light migrate done for %s templates", len(products))
    except Exception:
        _logger.exception("18.0.2.0.0 migrate failed")
        raise

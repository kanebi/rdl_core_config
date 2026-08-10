# -*- coding: utf-8 -*-
import logging

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    """
    Apply finished-SKU migration using pre-migrate staging snapshot.

    If POS is open, UoM/BOM cleanup still runs but stock/SVL transfer is
    deferred — run scripts/run_finished_sku_migration.py after closing POS.
    """
    env = api.Environment(cr, SUPERUSER_ID, {})
    from odoo.addons.rdl_core_config.migrate_finished_sku import (
        archive_orphan_kit_products,
        configure_packaged_drinks,
        configure_parents_from_staging,
        enable_uom_group,
        preflight_checks,
        remove_phantom_boms,
        run_finished_sku_migration,
        tighten_pack_uom_rounding,
        _load_staging_rows,
    )

    enable_uom_group(env)
    tighten_pack_uom_rounding(env)
    remove_phantom_boms(env)

    rows = _load_staging_rows(cr)
    if rows:
        configure_parents_from_staging(env, rows)
    configure_packaged_drinks(env)

    issues = preflight_checks(env)
    if issues:
        _logger.warning(
            "end-migrate: blockers present — stock/SVL transfer NOT run.\n"
            "Close POS / clear adjustments, then:\n"
            "  ./odoo-bin shell -d rdl_staging_dev < "
            "extra-addons/rdl_core_config/scripts/run_finished_sku_migration.py\n"
            "Blockers:\n%s",
            '\n'.join(issues),
        )
        archive_orphan_kit_products(env)
        remove_phantom_boms(env)
        return

    result = run_finished_sku_migration(env, skip_preflight=True)
    _logger.info("end-migrate 18.0.2.2.0 result: %s", result)

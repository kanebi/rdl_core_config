# -*- coding: utf-8 -*-
"""Repair parent_path / complete_name after raw SQL copies (e.g. DB migration)."""
from __future__ import annotations

import logging

_logger = logging.getLogger(__name__)


def fix_hierarchy_paths(env):
    """Rebuild hierarchy fields on models using _parent_store."""
    ProductCategory = env['product.category']
    if ProductCategory._parent_store:
        _logger.info('Recomputing product.category parent_path and complete_name')
        ProductCategory._parent_store_compute()
        ProductCategory.search([])._compute_complete_name()

    Location = env['stock.location']
    if Location._parent_store:
        _logger.info('Recomputing stock.location parent_path and complete_name')
        Location._parent_store_compute()
        Location.search([])._compute_complete_name()

    env.flush_all()

# -*- coding: utf-8 -*-
from odoo import api, SUPERUSER_ID
from odoo.addons.rdl_core_config.utils.hierarchy_fix import fix_hierarchy_paths


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    fix_hierarchy_paths(env)

# -*- coding: utf-8 -*-
"""
RDL finished-SKU migration (LEGACY — pre 18.0.2.4.0 empties split workflow).

Superseded by Excel product/inventory import. Kept for reference on old databases only.
"""
import json
import logging

from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

STAGING_TABLE = 'rdl_finished_sku_mig_staging'
EMPTIES_FIXED_PRICE = 5000.0


def _table_exists(cr, table):
    cr.execute("""
        SELECT 1 FROM information_schema.tables WHERE table_name = %s
    """, (table,))
    return bool(cr.fetchone())


def _staging_columns(cr):
    cr.execute("""
        SELECT column_name FROM information_schema.columns
         WHERE table_name = %s
    """, (STAGING_TABLE,))
    return {row[0] for row in cr.fetchall()}


def enable_uom_group(env):
    group_uom = env.ref('uom.group_uom', raise_if_not_found=False)
    group_user = env.ref('base.group_user', raise_if_not_found=False)
    if group_uom and group_user and group_uom not in group_user.implied_ids:
        group_user.write({'implied_ids': [(4, group_uom.id)]})


def tighten_pack_uom_rounding(env):
    for xmlid in ('rdl_core_config.uom_categ_brewery', 'rdl_core_config.uom_categ_can'):
        categ = env.ref(xmlid, raise_if_not_found=False)
        if not categ:
            continue
        for uom in env['uom.uom'].search([
            ('category_id', '=', categ.id),
            ('uom_type', '=', 'bigger'),
        ]):
            if uom.rounding > 0.0001:
                uom.write({'rounding': 0.0001})


def remove_phantom_boms(env):
    if 'mrp.bom' not in env:
        return 0
    phantoms = env['mrp.bom'].sudo().with_context(active_test=False).search([
        ('type', '=', 'phantom'),
    ])
    count = len(phantoms)
    if phantoms:
        phantoms.unlink()
    return count


def clear_draft_inventory_adjustments(env):
    dirty = env['stock.quant'].sudo().search([('inventory_quantity_set', '=', True)])
    if not dirty:
        return 0
    if hasattr(dirty, 'action_clear_inventory_quantity'):
        dirty.action_clear_inventory_quantity()
    else:
        dirty.write({'inventory_quantity': 0.0, 'inventory_diff_quantity': 0.0})
    return len(dirty)


def preflight_checks(env, auto_clear_draft_inventory=True):
    issues = []
    if auto_clear_draft_inventory:
        clear_draft_inventory_adjustments(env)
    open_pos = env['pos.session'].sudo().search([
        ('state', 'in', ('opening_control', 'opened')),
    ])
    if open_pos:
        issues.append(
            "Open POS session(s): %s" % ', '.join(open_pos.mapped('name'))
        )
    dirty = env['stock.quant'].sudo().search([('inventory_quantity_set', '=', True)])
    if dirty:
        issues.append("%s quants still have inventory_quantity_set" % len(dirty))
    return issues


def report_uom_locked_products(env):
    """
    Products that already have stock moves and therefore cannot have UoM changed.
    Returns list of dicts for logging/report file.
    """
    Template = env['product.template'].sudo()
    locked = []
    candidates = Template.search([
        ('pack_qty', '>', 1),
        ('active', '=', True),
    ])
    for tmpl in candidates:
        if not tmpl._has_stock_moves():
            continue
        unit_uom = tmpl._get_unit_uom()
        if not unit_uom:
            continue
        pack_uom = tmpl._get_or_create_pack_uom(
            tmpl.pack_qty or 24.0,
            tmpl.pack_uom_type or 'crate',
            unit_uom.category_id,
        )
        reasons = []
        if tmpl.uom_id != pack_uom:
            reasons.append(
                "uom_id=%s (target %s)" % (tmpl.uom_id.name, pack_uom.name)
            )
        if tmpl.uom_po_id != pack_uom:
            reasons.append(
                "uom_po_id=%s (target %s)" % (tmpl.uom_po_id.name, pack_uom.name)
            )
        if reasons:
            locked.append({
                'id': tmpl.id,
                'name': tmpl.display_name,
                'pack_qty': tmpl.pack_qty,
                'list_price': tmpl.list_price,
                'reasons': reasons,
            })
    return locked


def _set_quant_on_hand(env, product, location, qty, unit_cost=None):
    """Apply absolute on-hand qty via inventory adjustment."""
    if unit_cost is not None and unit_cost > 0:
        product.with_context(disable_auto_svl=True).write({
            'standard_price': unit_cost,
        })
    Quant = env['stock.quant'].sudo().with_context(inventory_mode=True)
    quant = Quant.search([
        ('product_id', '=', product.id),
        ('location_id', '=', location.id),
    ], limit=1)
    if not quant:
        if not qty:
            return
        quant = Quant.create({
            'product_id': product.id,
            'location_id': location.id,
        })
    quant.inventory_quantity = qty
    quant.action_apply_inventory()


def _component_remaining_value(env, product_ids):
    if not product_ids:
        return 0.0
    layers = env['stock.valuation.layer'].sudo().search([
        ('product_id', 'in', product_ids),
    ])
    return sum(layers.mapped('remaining_value'))


def _reprice_drink_minus_empties(tmpl, full_list_price=None, full_standard_price=None):
    """Commercial drink price = full pack price − fixed empties deposit."""
    full_list = full_list_price if full_list_price is not None else tmpl.list_price
    full_std = full_standard_price if full_standard_price is not None else tmpl.standard_price
    drink_list = max(0.0, float(full_list or 0.0) - EMPTIES_FIXED_PRICE)
    drink_std = max(0.0, float(full_std or 0.0) - EMPTIES_FIXED_PRICE)
    tmpl.write({
        'list_price': drink_list,
        'standard_price': drink_std,
    })
    return drink_list, drink_std


def get_or_create_consolidated_empties(env, parent_tmpl, old_empties_product_id=None):
    """
    One empties SKU per drink: Units UoM, fixed ₦5,000 list/cost.
    Reuses linked/archived empties template when present.
    """
    Template = env['product.template'].sudo()
    Product = env['product.product'].sudo()
    unit_uom = env.ref('uom.product_uom_unit', raise_if_not_found=False)
    categ = env.ref('rdl_core_config.product_category_empties', raise_if_not_found=False)

    if parent_tmpl.empties_product_id:
        empties = parent_tmpl.empties_product_id
        empties.product_tmpl_id.write({
            'active': True,
            'sale_ok': True,
            'purchase_ok': True,
            'available_in_pos': True,
            'uom_id': unit_uom.id if unit_uom else empties.uom_id.id,
            'uom_po_id': unit_uom.id if unit_uom else empties.uom_id.id,
            'list_price': EMPTIES_FIXED_PRICE,
            'standard_price': EMPTIES_FIXED_PRICE,
        })
        return empties

    if old_empties_product_id:
        old = Product.browse(old_empties_product_id).exists()
        if old:
            old.product_tmpl_id.write({
                'name': "%s (Empties)" % parent_tmpl.name.split('(')[0].strip(),
                'active': True,
                'sale_ok': True,
                'purchase_ok': True,
                'available_in_pos': True,
                'is_brewery': False,
                'is_packaged_drinks': False,
                'categ_id': categ.id if categ else old.categ_id.id,
                'uom_id': unit_uom.id if unit_uom else old.uom_id.id,
                'uom_po_id': unit_uom.id if unit_uom else old.uom_id.id,
                'list_price': EMPTIES_FIXED_PRICE,
                'standard_price': EMPTIES_FIXED_PRICE,
            })
            parent_tmpl.write({'empties_product_id': old.id})
            return old

    base = parent_tmpl.name.split('(')[0].strip()
    code_suffix = (parent_tmpl.default_code or str(parent_tmpl.id))[:20]
    empties_tmpl = Template.create({
        'name': "%s (Empties)" % base,
        'default_code': "%s-EMPT" % code_suffix,
        'categ_id': categ.id if categ else parent_tmpl.categ_id.id,
        'type': 'consu',
        'is_storable': True,
        'available_in_pos': True,
        'sale_ok': True,
        'purchase_ok': True,
        'is_brewery': False,
        'is_packaged_drinks': False,
        'uom_id': unit_uom.id if unit_uom else parent_tmpl.uom_id.id,
        'uom_po_id': unit_uom.id if unit_uom else parent_tmpl.uom_id.id,
        'list_price': EMPTIES_FIXED_PRICE,
        'standard_price': EMPTIES_FIXED_PRICE,
    })
    parent_tmpl.write({'empties_product_id': empties_tmpl.product_variant_id.id})
    return empties_tmpl.product_variant_id


def transfer_family_stock(env, row):
    """
    Move liquid SVL → drink parent (pack UoM).
    Move bottle+crate SVL → consolidated empties (Units UoM, qty = packs).
    """
    Product = env['product.product'].sudo()
    Location = env['stock.location'].sudo()

    parent = Product.browse(row['parent_product_id']).exists()
    if not parent:
        return 0.0
    parent_tmpl = parent.product_tmpl_id

    liquid = Product.browse(row['liquid_product_id']).exists() if row.get('liquid_product_id') else Product.browse()
    bottle = Product.browse(row['bottle_product_id']).exists() if row.get('bottle_product_id') else Product.browse()
    crate = Product.browse(row['crate_product_id']).exists() if row.get('crate_product_id') else Product.browse()
    components = liquid | bottle | crate

    location_packs = row.get('location_packs') or {}
    if isinstance(location_packs, str):
        location_packs = json.loads(location_packs)

    total_packs = sum(float(v) for v in location_packs.values())

    # SVL split (prefer live component SVL, fall back to staging columns)
    liquid_svl = _component_remaining_value(env, liquid.ids)
    empties_svl = _component_remaining_value(env, (bottle | crate).ids)
    if not liquid_svl and not empties_svl:
        liquid_svl = float(row.get('liquid_svl_value') or 0.0)
        empties_svl = float(row.get('empties_svl_value') or 0.0)
    total_svl = liquid_svl + empties_svl
    if not total_svl:
        total_svl = float(row.get('svl_value') or 0.0)
        # Fallback split using fixed empties commercial amount
        if total_packs and total_svl:
            empties_svl = min(total_svl, total_packs * EMPTIES_FIXED_PRICE)
            liquid_svl = total_svl - empties_svl

    drink_unit_cost = (liquid_svl / total_packs) if total_packs else 0.0
    empties_unit_cost = (empties_svl / total_packs) if total_packs else EMPTIES_FIXED_PRICE

    empties = get_or_create_consolidated_empties(
        env, parent_tmpl, row.get('empties_product_id'),
    )

    _logger.info(
        "%s: packs=%.2f liquid_svl=%.2f empties_svl=%.2f drink_cost=%.4f empties_cost=%.4f",
        parent.display_name, total_packs, liquid_svl, empties_svl,
        drink_unit_cost, empties_unit_cost,
    )

    # Zero legacy components
    for comp in components:
        for quant in env['stock.quant'].sudo().search([
            ('product_id', '=', comp.id),
            ('location_id.usage', '=', 'internal'),
        ]):
            if quant.quantity or quant.inventory_quantity_set:
                _set_quant_on_hand(env, comp, quant.location_id, 0.0)

    if total_packs <= 0:
        _reprice_drink_minus_empties(
            parent_tmpl,
            full_list_price=row.get('parent_list_price'),
            full_standard_price=row.get('parent_standard_price'),
        )
        return 0.0

    moved = 0.0
    for loc_id, packs in location_packs.items():
        packs = float(packs)
        if packs <= 0:
            continue
        location = Location.browse(int(loc_id)).exists()
        if not location:
            continue
        _set_quant_on_hand(env, parent, location, packs, unit_cost=drink_unit_cost)
        _set_quant_on_hand(env, empties, location, packs, unit_cost=empties_unit_cost)
        moved += packs

    _reprice_drink_minus_empties(
        parent_tmpl,
        full_list_price=row.get('parent_list_price'),
        full_standard_price=row.get('parent_standard_price'),
    )

    leftover = _component_remaining_value(env, components.ids)
    if abs(leftover) > 1.0:
        _logger.warning("Component SVL leftover %.2f on %s", leftover, parent.display_name)

    return moved


def split_existing_parent_to_empties(env, row):
    """
    Phase-2 for DB already migrated (all SVL on parent, no component stock).
    Splits parent SVL into drink + consolidated empties using staging ratios.
    """
    Product = env['product.product'].sudo()
    Location = env['stock.location'].sudo()

    parent = Product.browse(row['parent_product_id']).exists()
    if not parent:
        return 0.0
    parent_tmpl = parent.product_tmpl_id

    location_packs = row.get('location_packs') or {}
    if isinstance(location_packs, str):
        location_packs = json.loads(location_packs)

    # Build location packs from current parent stock if staging empty
    if not location_packs:
        for quant in env['stock.quant'].sudo().search([
            ('product_id', '=', parent.id),
            ('location_id.usage', '=', 'internal'),
        ]):
            if quant.quantity:
                location_packs[str(quant.location_id.id)] = float(quant.quantity)

    total_packs = sum(float(v) for v in location_packs.values())
    if total_packs <= 0:
        return 0.0

    parent_svl = _component_remaining_value(env, parent.ids)
    liquid_svl = float(row.get('liquid_svl_value') or 0.0)
    empties_svl = float(row.get('empties_svl_value') or 0.0)
    staged_total = float(row.get('svl_value') or 0.0)

    if liquid_svl or empties_svl:
        ratio_drink = liquid_svl / (liquid_svl + empties_svl) if (liquid_svl + empties_svl) else 1.0
    elif staged_total:
        ratio_drink = max(0.0, (staged_total - total_packs * EMPTIES_FIXED_PRICE) / staged_total)
    else:
        ratio_drink = max(0.0, (parent_svl - total_packs * EMPTIES_FIXED_PRICE) / parent_svl) if parent_svl else 1.0

    drink_svl = parent_svl * ratio_drink
    empties_svl_target = parent_svl - drink_svl
    drink_unit_cost = drink_svl / total_packs if total_packs else 0.0
    empties_unit_cost = empties_svl_target / total_packs if total_packs else EMPTIES_FIXED_PRICE

    empties = get_or_create_consolidated_empties(
        env, parent_tmpl, row.get('empties_product_id'),
    )

    _logger.info(
        "Phase2 split %s: parent_svl=%.2f → drink=%.2f empties=%.2f",
        parent.display_name, parent_svl, drink_svl, empties_svl_target,
    )

    # Re-apply inventory at split costs (zeros then re-sets — preserves total SVL)
    for loc_id, packs in location_packs.items():
        packs = float(packs)
        location = Location.browse(int(loc_id)).exists()
        if not location or packs <= 0:
            continue
        _set_quant_on_hand(env, parent, location, 0.0)
        _set_quant_on_hand(env, empties, location, 0.0)
        _set_quant_on_hand(env, parent, location, packs, unit_cost=drink_unit_cost)
        _set_quant_on_hand(env, empties, location, packs, unit_cost=empties_unit_cost)

    full_list = float(row.get('parent_list_price') or parent_tmpl.list_price + EMPTIES_FIXED_PRICE)
    full_std = float(row.get('parent_standard_price') or 0.0)
    if not full_std:
        full_std = parent.standard_price + EMPTIES_FIXED_PRICE
    _reprice_drink_minus_empties(parent_tmpl, full_list_price=full_list, full_standard_price=full_std)

    return total_packs


def archive_components(env, row):
    Product = env['product.product'].sudo()
    Template = env['product.template'].sudo()
    parent_tmpl = Template.browse(row.get('parent_tmpl_id')).exists()

    ids = [
        row.get('liquid_product_id'),
        row.get('bottle_product_id'),
        row.get('crate_product_id'),
        row.get('full_bottle_product_id'),
        row.get('empties_product_id'),
    ]
    archived = 0
    for pid in filter(None, ids):
        product = Product.browse(pid).exists()
        if not product:
            continue
        tmpl = product.product_tmpl_id
        if tmpl.pack_qty > 1:
            continue
        if tmpl.active:
            tmpl.write({
                'active': False,
                'available_in_pos': False,
                'sale_ok': False,
                'purchase_ok': False,
            })
            archived += 1
    return archived


def archive_orphan_kit_products(env):
    Template = env['product.template'].sudo()
    orphans = Template.with_context(active_test=False).search([
        '|', '|', '|',
        ('name', 'ilike', '(Full Bottle)'),
        ('name', 'ilike', '(Empties)'),
        ('name', 'ilike', '(Liquid)'),
        '|',
        ('name', 'ilike', '(Empty Bottle)'),
        ('name', 'ilike', '(Empty Crate'),
    ])
    count = 0
    for tmpl in orphans:
        if sum(tmpl.product_variant_ids.mapped('qty_available')):
            continue
        if tmpl.active:
            tmpl.write({
                'active': False,
                'available_in_pos': False,
                'sale_ok': False,
                'purchase_ok': False,
            })
            count += 1
    return count


def configure_parents_from_staging(env, rows):
    Template = env['product.template'].sudo()
    for row in rows:
        tmpl = Template.browse(row['parent_tmpl_id']).exists()
        if not tmpl:
            continue
        pack_qty = float(row.get('pack_qty') or 24) or 24.0
        uom_name = (tmpl.uom_id.name or '').lower()
        pack_type = 'carton' if 'carton' in uom_name else 'case' if 'case' in uom_name else 'crate'
        tmpl.write({
            'pack_qty': pack_qty,
            'pack_uom_type': pack_type,
        })
        tmpl._clear_phantom_boms()


def configure_packaged_drinks(env):
    templates = env['product.template'].sudo().search([('pack_uom_type', '=', 'carton'), ('pack_qty', '>', 1)])
    for tmpl in templates:
        vals = {}
        if not tmpl.pack_qty:
            factor = getattr(tmpl.uom_id, 'factor_inv', None) or getattr(tmpl.uom_id, 'ratio', None)
            vals['pack_qty'] = float(factor) if factor and factor > 1 else 24.0
        if not tmpl.pack_uom_type:
            vals['pack_uom_type'] = 'carton'
        if vals:
            tmpl.write(vals)


def _load_staging_rows(cr):
    if not _table_exists(cr, STAGING_TABLE):
        return []
    cols = _staging_columns(cr)
    extra = ''
    if 'liquid_svl_value' in cols:
        extra += ', liquid_svl_value'
    else:
        extra += ', NULL::double precision AS liquid_svl_value'
    if 'empties_svl_value' in cols:
        extra += ', empties_svl_value'
    else:
        extra += ', NULL::double precision AS empties_svl_value'

    cr.execute(f"""
        SELECT parent_tmpl_id, parent_product_id, pack_qty,
               liquid_product_id, bottle_product_id, crate_product_id,
               full_bottle_product_id, empties_product_id,
               parent_list_price, parent_standard_price,
               svl_value, location_packs
               {extra}
          FROM {STAGING_TABLE}
         ORDER BY parent_tmpl_id
    """)
    keys = [
        'parent_tmpl_id', 'parent_product_id', 'pack_qty',
        'liquid_product_id', 'bottle_product_id', 'crate_product_id',
        'full_bottle_product_id', 'empties_product_id',
        'parent_list_price', 'parent_standard_price',
        'svl_value', 'location_packs',
        'liquid_svl_value', 'empties_svl_value',
    ]
    return [dict(zip(keys, row)) for row in cr.fetchall()]


def run_finished_sku_migration(env, skip_preflight=False, phase2_split=False):
    """Full migration. phase2_split=True for DB already migrated to single parent SKU."""
    _logger.info("=== RDL finished-SKU migration starting (phase2=%s) ===", phase2_split)
    enable_uom_group(env)
    tighten_pack_uom_rounding(env)

    issues = preflight_checks(env)
    if issues and not skip_preflight:
        raise UserError("Blockers:\n- " + "\n- ".join(issues))

    uom_locked = report_uom_locked_products(env)
    if uom_locked:
        _logger.warning("UoM locked products (%s):", len(uom_locked))
        for item in uom_locked:
            _logger.warning("  [%s] %s — %s", item['id'], item['name'], '; '.join(item['reasons']))

    remove_phantom_boms(env)
    rows = _load_staging_rows(env.cr)

    if not rows:
        _logger.warning("No staging rows — metadata-only pass")
        configure_packaged_drinks(env)
        pack_products = env['product.template'].search([('pack_qty', '>', 1)])
        if pack_products:
            pack_products._configure_pack_uoms()
            pack_products._clear_phantom_boms()
        return {'staged': 0, 'uom_locked': uom_locked}

    configure_parents_from_staging(env, rows)
    configure_packaged_drinks(env)

    moved_total = 0.0
    archived_total = 0
    for row in rows:
        if phase2_split:
            moved_total += split_existing_parent_to_empties(env, row)
        else:
            moved_total += transfer_family_stock(env, row)
        archived_total += archive_components(env, row)

    archived_total += archive_orphan_kit_products(env)
    remove_phantom_boms(env)

    result = {
        'staged': len(rows),
        'moved_packs': moved_total,
        'archived_components': archived_total,
        'target_svl_value': sum(float(r.get('svl_value') or 0) for r in rows),
        'uom_locked_count': len(uom_locked),
        'uom_locked': uom_locked,
        'phase2': phase2_split,
    }
    _logger.info("=== RDL finished-SKU migration complete: %s ===", result)
    return result

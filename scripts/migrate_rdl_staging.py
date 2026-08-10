#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Migrate data rdl_staging_dev → rdl_staging using Excel sheet names as phase guide.

Run (target DB must exist and have base + l10n_ng + rdl_core_config installed):
  cd /home/kane/odoo-18
  source odoo-18env/bin/activate
  ./odoo-source/odoo-bin shell -d rdl_staging -c odoo.conf \\
      < extra-addons/rdl_core_config/scripts/migrate_rdl_staging.py

Data is read from the SOURCE Odoo database (not Excel cell values).
Sheet names mirror RDL_Trading_Odoo.xlsx for traceability in migration_log.json.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime

import odoo
from odoo import SUPERUSER_ID, api

SOURCE_DB = os.environ.get('RDL_SOURCE_DB', 'rdl_staging_dev')
LOG_PATH = os.environ.get(
    'RDL_MIGRATION_LOG',
    '/home/kane/odoo-18/extra-addons/rdl_core_config/scripts/migration_log.json',
)
EMPTIES_SKU = 'RDL-EMPTIES'
EMPTIES_NAME = 'Consolidated Empties (Crate + Bottles)'
EMPTIES_UNIT_PRICE = 5000.0

_logger = logging.getLogger(__name__)

# Excel workbook sheet guide (0-based index → phase label)
SHEET_GUIDE = {
    5: '05-Product Categories',
    1: '01-Product Master',
    6: '06-Chart of Accounts',
    4: '04-Stock Pickings',
    3: '03-Opening Inventory',
    2: '02-Vendor Master',
    8: '08-Customer Master',
    9: '09-Sale Orders',
    10: '10-Customer Invoices',
    11: '11-Vendor Bills',
    12: '12-Journal Entries',
}


class MigrationLog:
    def __init__(self):
        self.data = {
            'source_db': SOURCE_DB,
            'target_db': None,
            'started': datetime.now().isoformat(),
            'sheet_guide': SHEET_GUIDE,
            'phases': {},
        }

    def phase(self, sheet_key, **kwargs):
        label = SHEET_GUIDE.get(sheet_key, str(sheet_key))
        self.data['phases'][label] = kwargs
        _logger.info('Phase %s: %s', label, kwargs)

    def save(self, path):
        self.data['finished'] = datetime.now().isoformat()
        with open(path, 'w', encoding='utf-8') as fh:
            json.dump(self.data, fh, indent=2, default=str)
        _logger.info('Migration log written to %s', path)


class IdMaps:
    def __init__(self):
        self.categ = {}
        self.product_tmpl = {}
        self.product = {}
        self.partner = {}
        self.account = {}
        self.journal = {}
        self.uom = {}
        self.tax = {}
        self.location = {}
        self.picking_type = {}
        self.source_empties_pp_ids = set()
        self.consolidated_empties_pp = None
        self.po = {}
        self.so = {}
        self.picking = {}


def _open_source():
    registry = odoo.registry(SOURCE_DB)
    cr = registry.cursor()
    return api.Environment(cr, SUPERUSER_ID, {}), cr, registry


def _db_column_exists(cr, table, column):
    cr.execute("""
        SELECT 1 FROM information_schema.columns
        WHERE table_name = %s AND column_name = %s
    """, (table, column))
    return bool(cr.fetchone())


def _field_readable(env, model_name, field_name):
    """True if field can be read without SQL error on this database."""
    field = env[model_name]._fields.get(field_name)
    if not field:
        return False
    if not field.store:
        return True
    return _db_column_exists(env.cr, env[model_name]._table, field.name)


def _json_text(val):
    if isinstance(val, dict):
        return val.get('en_US') or val.get('en') or next(iter(val.values()), '')
    return val or ''


def _company(env):
    return env.company


def _map_by_field(src_records, tgt_env, model, field, maps, src_field='id', create_fn=None):
    """Build id map using a unique business key (name, code, default_code)."""
    Model = tgt_env[model]
    for rec in src_records:
        key = getattr(rec, field if src_field == 'id' else src_field)
        if not key:
            continue
        key = str(key).strip()
        existing = Model.search([(field, '=', key)], limit=1)
        if existing:
            maps[src_field if src_field != 'id' else rec.id] = existing.id
        elif create_fn:
            new = create_fn(rec)
            if new:
                maps[rec.id] = new.id


def migrate_categories(src, tgt, maps, log):
    """Sheet 05-Product Categories — mirror category.import.wizard hierarchy."""
    sheet = 5
    ProductCategory = tgt['product.category']
    PosCategory = tgt['pos.category']
    created = updated = 0

    categories = src['product.category'].search([], order='parent_path')
    for cat in categories:
        if cat.name in ('All', 'Expenses', 'Saleable', 'Internal'):
            continue
        parent_tgt = maps.categ.get(cat.parent_id.id) if cat.parent_id else False
        existing = ProductCategory.search([('name', '=ilike', cat.name)], limit=1)
        if existing:
            if parent_tgt and not existing.parent_id:
                existing.parent_id = parent_tgt
            maps.categ[cat.id] = existing.id
            updated += 1
        else:
            new = ProductCategory.create({
                'name': cat.name,
                'parent_id': parent_tgt or False,
            })
            maps.categ[cat.id] = new.id
            created += 1

        pos_parent = False
        if parent_tgt:
            parent_cat = ProductCategory.browse(parent_tgt)
            pos_parent_rec = PosCategory.search([('name', '=ilike', parent_cat.name)], limit=1)
            pos_parent = pos_parent_rec.id if pos_parent_rec else False
        pos_existing = PosCategory.search([('name', '=ilike', cat.name)], limit=1)
        if not pos_existing:
            PosCategory.create({'name': cat.name, 'parent_id': pos_parent or False})

    log.phase(sheet, created=created, updated=updated, total=len(maps.categ))


def _standard_price_float(product):
    sp = product.standard_price if hasattr(product, 'standard_price') else product.get('standard_price')
    if isinstance(sp, dict):
        return float(next(iter(sp.values()), 0.0) or 0.0)
    return float(sp or 0.0)


def _is_empties_name(name):
    name = (name or '').lower()
    return (
        'empties' in name
        or 'empty crate' in name
        or 'empty bottle' in name
        or 'empty can' in name
    )


def _source_internal_location_ids(src):
    return src['stock.location'].search([('usage', '=', 'internal')]).ids


def _source_empties_pp_ids(src):
    """All source product.product records that represent per-drink empties stock."""
    pp_ids = set()
    if _db_column_exists(src.cr, 'product_template', 'empties_product_id'):
        src.cr.execute("""
            SELECT DISTINCT empties_product_id
            FROM product_template
            WHERE active IS TRUE AND empties_product_id IS NOT NULL
        """)
        pp_ids.update(r[0] for r in src.cr.fetchall() if r[0])

    src.cr.execute("""
        SELECT pp.id
        FROM product_product pp
        JOIN product_template pt ON pt.id = pp.product_tmpl_id
        WHERE pt.name::text ILIKE '%empt%'
           OR pt.name::text ILIKE '%empty%'
           OR pt.categ_id IN (
               SELECT id FROM product_category WHERE name ILIKE '%empt%'
           )
    """)
    pp_ids.update(r[0] for r in src.cr.fetchall())
    return pp_ids


def _total_source_empties_qty(src, empties_pp_ids):
    """Sum on-hand for all empties SKUs in source (active + archived), no drink fallback."""
    src.cr.execute("""
        SELECT COALESCE(SUM(sq.quantity), 0)
        FROM stock_quant sq
        JOIN product_product pp ON pp.id = sq.product_id
        JOIN product_template pt ON pt.id = pp.product_tmpl_id
        JOIN stock_location sl ON sl.id = sq.location_id
        LEFT JOIN product_category pc ON pc.id = pt.categ_id
        WHERE sl.usage = 'internal'
          AND (
            pt.name::text ILIKE '%%empt%%'
            OR pt.name::text ILIKE '%%empty%%'
            OR pc.name::text ILIKE '%%empt%%'
            OR pp.id = ANY(%s)
          )
    """, (list(empties_pp_ids) or [0],))
    qty = float(src.cr.fetchone()[0] or 0)
    return qty, 'empties_skus_sum'


def _collect_source_product_ids(src):
    """Product templates to migrate — drinks and SKUs, excluding per-drink empties."""
    ids = set()
    read_fields = [
        f for f in (
            'name', 'default_code', 'is_brewery', 'is_packaged_drinks',
        )
        if _field_readable(src, 'product.template', f)
    ]
    rows = src['product.template'].search([('active', '=', True)]).read(read_fields)
    for row in rows:
        name = _json_text(row.get('name'))
        sku = _json_text(row.get('default_code')).strip()
        is_drink = row.get('is_brewery') or row.get('is_packaged_drinks')
        if _is_empties_name(name) and not is_drink:
            continue
        if sku or is_drink:
            ids.add(row['id'])
    return ids


def create_consolidated_empties(src, tgt, maps, log):
    """
    One empties SKU at ₦5,000 — on-hand = sum of all empties quants in source internal locs.
    All brewery/packaged drinks link to this single product.
    """
    maps.source_empties_pp_ids = _source_empties_pp_ids(src)

    ProductTmpl = tgt['product.template']
    unit_uom = tgt.ref('uom.product_uom_unit')

    existing = ProductTmpl.search([('default_code', '=', EMPTIES_SKU)], limit=1)
    vals = {
        'name': EMPTIES_NAME,
        'default_code': EMPTIES_SKU,
        'type': 'consu',
        'is_storable': True,
        'is_brewery': False,
        'is_packaged_drinks': False,
        'available_in_pos': False,
        'list_price': EMPTIES_UNIT_PRICE,
        'uom_id': unit_uom.id,
        'uom_po_id': unit_uom.id,
        'active': True,
    }
    tmpl = existing if existing else ProductTmpl.create(vals)
    if existing:
        existing.write(vals)
    pp = tmpl.product_variant_ids[:1]
    pp.standard_price = EMPTIES_UNIT_PRICE
    maps.consolidated_empties_pp = pp.id

    # Deactivate archived empties stub products (keep single RDL-EMPTIES)
    stubs = ProductTmpl.search([
        ('default_code', '!=', EMPTIES_SKU),
        '|', '|',
        ('name', 'ilike', 'empties'),
        ('name', 'ilike', 'empty crate'),
        ('name', 'ilike', 'empty bottle'),
    ])
    if stubs:
        stubs.write({'active': False})

    drinks_linked = 0
    if _field_readable(tgt, 'product.template', 'empties_product_id'):
        drinks = ProductTmpl.search([
            '|', ('is_brewery', '=', True), ('is_packaged_drinks', '=', True),
        ])
        drinks.write({'empties_product_id': pp.id})
        drinks_linked = len(drinks)

    log.phase(
        '01-Consolidated Empties',
        sku=EMPTIES_SKU,
        source_empties_products=len(maps.source_empties_pp_ids),
        drinks_linked=drinks_linked,
        cost=EMPTIES_UNIT_PRICE,
        sales_price=EMPTIES_UNIT_PRICE,
    )
    return pp


def apply_consolidated_empties_qty(src, tgt, maps, log):
    """Set consolidated empties on-hand = total source empties (after pickings reconcile)."""
    if not maps.consolidated_empties_pp:
        consolidated = tgt['product.template'].search([('default_code', '=', EMPTIES_SKU)], limit=1)
        if consolidated:
            maps.consolidated_empties_pp = consolidated.product_variant_ids[:1].id
    if not maps.consolidated_empties_pp:
        return

    total_qty, qty_source = _total_source_empties_qty(src, maps.source_empties_pp_ids)
    wh = tgt['stock.warehouse'].search([('company_id', '=', tgt.company.id)], limit=1)
    if not wh:
        return
    Quant = tgt['stock.quant']
    quant = Quant.search([
        ('product_id', '=', maps.consolidated_empties_pp),
        ('location_id', '=', wh.lot_stock_id.id),
    ], limit=1)
    if not quant:
        quant = Quant.create({
            'product_id': maps.consolidated_empties_pp,
            'location_id': wh.lot_stock_id.id,
        })
    quant.inventory_quantity = total_qty
    try:
        quant.with_context(inventory_mode=True).action_apply_inventory()
    except Exception as exc:
        _logger.warning('Consolidated empties final qty apply failed: %s', exc)
        try:
            tgt.cr.execute(
                "UPDATE stock_quant SET quantity = %s, inventory_quantity = %s "
                "WHERE id = %s",
                (total_qty, total_qty, quant.id),
            )
        except Exception as exc2:
            _logger.warning('Consolidated empties SQL qty set failed: %s', exc2)
    log.phase('01-Consolidated Empties qty', qty_on_hand=total_qty, sku=EMPTIES_SKU, qty_source=qty_source)


def _migrate_one_product(row, src, tgt, maps, ProductTmpl, variant_by_tmpl):
    """Create or update a single product.template on target from a read() dict."""
    name = _json_text(row.get('name'))
    sku = _json_text(row.get('default_code')).strip()
    is_drink = row.get('is_brewery') or row.get('is_packaged_drinks')
    is_empties_sku = 'empties' in name.lower() or 'empty crate' in name.lower()

    categ_id = maps.categ.get(row['categ_id'][0]) if row.get('categ_id') else False
    vals = {
        'name': name,
        'default_code': sku or False,
        'barcode': row.get('barcode') or False,
        'type': row.get('type') or 'consu',
        'is_storable': row.get('is_storable', True),
        'available_in_pos': row.get('available_in_pos', True) if is_drink else False,
        'list_price': row.get('list_price') or 0.0,
        'is_brewery': bool(row.get('is_brewery')),
        'is_packaged_drinks': bool(row.get('is_packaged_drinks')),
        'pack_qty': row.get('pack_qty') or 24.0,
        'pack_uom_type': row.get('pack_uom_type') or 'crate',
        'active': True,
    }
    if is_empties_sku and not is_drink:
        vals.update({'is_brewery': False, 'is_packaged_drinks': False})
    if categ_id:
        vals['categ_id'] = categ_id

    existing = ProductTmpl.search([('default_code', '=', sku)], limit=1) if sku else False
    if not existing:
        existing = ProductTmpl.search([('name', '=', name)], limit=1)

    if existing:
        existing.write(vals)
        tmpl = existing
        action = 'updated'
    else:
        tmpl = ProductTmpl.create(vals)
        action = 'created'

    maps.product_tmpl[row['id']] = tmpl.id
    src_variant = variant_by_tmpl.get(row['id'])
    tgt_variant = tmpl.product_variant_ids[:1]
    if src_variant and tgt_variant:
        maps.product[src_variant['id']] = tgt_variant.id
        cost = _standard_price_float(src_variant)
        if cost:
            tgt_variant.standard_price = cost

    if is_drink:
        try:
            tmpl._configure_pack_uoms()
        except Exception as exc:
            _logger.warning('UoM config skipped for %s: %s', name, exc)

    return action


def migrate_products(src, tgt, maps, log):
    """Sheet 01-Product Master — drinks/SKUs only; consolidated empties created separately."""
    sheet = 1
    ProductTmpl = tgt['product.template']
    ProductProduct = src['product.product']
    created = updated = skipped = 0

    read_fields = [
        f for f in (
            'name', 'default_code', 'barcode', 'type', 'is_storable', 'available_in_pos',
            'list_price', 'is_brewery', 'is_packaged_drinks', 'pack_qty', 'pack_uom_type',
            'categ_id',
        )
        if _field_readable(src, 'product.template', f)
    ]

    required_ids = _collect_source_product_ids(src)
    products_data = src['product.template'].browse(list(required_ids)).read(read_fields)
    by_id = {row['id']: row for row in products_data}

    tmpl_ids = list(by_id.keys())
    variants_data = ProductProduct.search([('product_tmpl_id', 'in', tmpl_ids)]).read(
        ['id', 'product_tmpl_id', 'standard_price']
    )
    variant_by_tmpl = {}
    for vd in variants_data:
        tmpl_id = vd['product_tmpl_id'][0] if vd.get('product_tmpl_id') else None
        if tmpl_id and tmpl_id not in variant_by_tmpl:
            variant_by_tmpl[tmpl_id] = vd

    for pid in sorted(by_id.keys()):
        row = by_id[pid]
        name = _json_text(row.get('name'))
        sku = _json_text(row.get('default_code')).strip()
        is_drink = row.get('is_brewery') or row.get('is_packaged_drinks')
        if _is_empties_name(name) and not is_drink:
            skipped += 1
            continue
        if not sku and not is_drink:
            skipped += 1
            continue

        action = _migrate_one_product(row, src, tgt, maps, ProductTmpl, variant_by_tmpl)
        if action == 'created':
            created += 1
        else:
            updated += 1

    create_consolidated_empties(src, tgt, maps, log)
    log.phase(sheet, created=created, updated=updated, skipped=skipped,
              total=len(maps.product_tmpl), consolidated_empties_sku=EMPTIES_SKU)


def migrate_accounts(src, tgt, maps, log):
    """Sheet 06-Chart of Accounts — update names/types only; never duplicate NG codes."""
    sheet = 6
    Account = tgt['account.account']
    company = _company(tgt)
    updated = skipped = 0

    accounts = src['account.account'].search([
        ('company_ids', 'in', src.company.id),
    ], order='code')

    for acc in accounts:
        if not acc.code:
            continue
        existing = Account.search([
            ('code', '=', acc.code),
            ('company_ids', 'in', company.id),
        ], limit=1)
        if existing:
            existing.write({
                'name': acc.name,
                'account_type': acc.account_type,
            })
            maps.account[acc.id] = existing.id
            updated += 1
        else:
            skipped += 1

    log.phase(sheet, updated=updated, skipped=skipped, total=len(maps.account))


def _map_location(src_loc, tgt, maps):
    if not src_loc:
        return False
    if src_loc.id in maps.location:
        return maps.location[src_loc.id]
    loc = tgt['stock.location'].search([
        ('complete_name', '=', src_loc.complete_name),
    ], limit=1)
    if not loc:
        loc = tgt['stock.location'].search([
            ('name', '=', src_loc.name),
            ('usage', '=', src_loc.usage),
        ], limit=1)
    if loc:
        maps.location[src_loc.id] = loc.id
        return loc.id
    return False


def _map_picking_type(src_pt, tgt, maps):
    if src_pt.id in maps.picking_type:
        return maps.picking_type[src_pt.id]
    pt = tgt['stock.picking.type'].search([
        ('sequence_code', '=', src_pt.sequence_code),
        ('warehouse_id.company_id', '=', tgt.company.id),
    ], limit=1)
    if not pt:
        pt = tgt['stock.picking.type'].search([
            ('code', '=', src_pt.code),
            ('warehouse_id.company_id', '=', tgt.company.id),
        ], limit=1)
    if pt:
        maps.picking_type[src_pt.id] = pt.id
        return pt.id
    return False


def _map_product_id(src_pp_id, maps):
    if src_pp_id in maps.product:
        return maps.product[src_pp_id]
    if src_pp_id in maps.source_empties_pp_ids and maps.consolidated_empties_pp:
        return maps.consolidated_empties_pp
    return False


def _migrate_one_picking(sp, src, tgt, maps, extra_vals=None):
    """Create and optionally validate one stock.picking on target."""
    Picking = tgt['stock.picking']
    picking_type_id = _map_picking_type(sp.picking_type_id, tgt, maps)
    location_id = _map_location(sp.location_id, tgt, maps)
    location_dest_id = _map_location(sp.location_dest_id, tgt, maps)
    if not picking_type_id or not location_id or not location_dest_id:
        return None, 'locations'

    partner_id = maps.partner.get(sp.partner_id.id) if sp.partner_id else False
    move_cmds = []
    for move in sp.move_ids:
        if move.state == 'cancel':
            continue
        product_id = _map_product_id(move.product_id.id, maps)
        if not product_id:
            return None, 'product'
        move_cmds.append((0, 0, {
            'name': move.name or move.product_id.display_name,
            'product_id': product_id,
            'product_uom_qty': move.product_uom_qty,
            'product_uom': _map_uom(move.product_uom, tgt),
            'location_id': location_id,
            'location_dest_id': location_dest_id,
        }))
    if not move_cmds:
        return None, 'no_moves'

    vals = {
        'picking_type_id': picking_type_id,
        'location_id': location_id,
        'location_dest_id': location_dest_id,
        'partner_id': partner_id or False,
        'origin': sp.origin or sp.name,
        'scheduled_date': sp.scheduled_date,
        'move_ids': move_cmds,
    }
    if extra_vals:
        vals.update(extra_vals)

    new_picking = Picking.create(vals)
    maps.picking[sp.id] = new_picking.id

    if sp.state == 'done':
        try:
            new_picking.action_confirm()
            new_picking.action_assign()
            for move in new_picking.move_ids:
                move.quantity = move.product_uom_qty
            res = new_picking.button_validate()
            if isinstance(res, dict) and res.get('res_model'):
                for move in new_picking.move_ids:
                    move.quantity = move.product_uom_qty
                new_picking.button_validate()
        except Exception as exc:
            _logger.warning('Picking validate failed %s: %s', sp.name, exc)
            return new_picking, 'validate_failed'
    elif sp.state == 'cancel':
        new_picking.action_cancel()
    return new_picking, 'ok'


def migrate_purchase_orders(src, tgt, maps, log):
    """Purchase orders before receipts."""
    PO = tgt['purchase.order']
    created = skipped = 0
    orders = src['purchase.order'].search([], order='date_order, id')
    for po in orders:
        partner_id = maps.partner.get(po.partner_id.id)
        if not partner_id:
            skipped += 1
            continue
        line_cmds = []
        skip = False
        for line in po.order_line:
            if line.display_type:
                line_cmds.append((0, 0, {
                    'display_type': line.display_type,
                    'name': line.name,
                }))
                continue
            product_id = _map_product_id(line.product_id.id, maps)
            if not product_id:
                skip = True
                break
            line_vals = {
                'product_id': product_id,
                'name': line.name,
                'product_qty': line.product_qty,
                'price_unit': line.price_unit,
            }
            uom_id = _map_uom(line.product_uom, tgt)
            if uom_id:
                line_vals['product_uom'] = uom_id
            line_cmds.append((0, 0, line_vals))
        if skip or not line_cmds:
            skipped += 1
            continue
        new_po = PO.create({
            'partner_id': partner_id,
            'date_order': po.date_order,
            'origin': po.origin,
            'order_line': line_cmds,
        })
        maps.po[po.id] = new_po.id
        if po.state in ('purchase', 'done'):
            try:
                new_po.button_confirm()
            except Exception as exc:
                _logger.warning('PO confirm failed %s: %s', po.name, exc)
        elif po.state == 'cancel':
            new_po.button_cancel()
        created += 1
    log.phase('07-Purchase Orders', created=created, skipped=skipped, source_total=len(orders))


def migrate_receipts(src, tgt, maps, log):
    """Incoming pickings / receipts linked to POs where possible."""
    sheet = 4
    created = skipped = validated = 0
    pickings = src['stock.picking'].search([
        ('picking_type_code', '=', 'incoming'),
        ('state', 'in', ('done', 'assigned', 'confirmed', 'waiting')),
    ], order='scheduled_date, id')

    for sp in pickings:
        extra = {}
        if sp.purchase_id and sp.purchase_id.id in maps.po:
            extra['purchase_id'] = maps.po[sp.purchase_id.id]
        result, status = _migrate_one_picking(sp, src, tgt, maps, extra)
        if not result:
            skipped += 1
            continue
        created += 1
        if sp.state == 'done' and status == 'ok':
            validated += 1
    log.phase(sheet, phase='receipts', created=created, validated=validated,
              skipped=skipped, source_total=len(pickings))


def migrate_deliveries(src, tgt, maps, log):
    """Outgoing pickings / deliveries linked to SOs where possible."""
    pickings = src['stock.picking'].search([
        ('picking_type_code', '=', 'outgoing'),
        ('state', 'in', ('done', 'assigned', 'confirmed', 'waiting')),
    ], order='scheduled_date, id')
    created = skipped = validated = 0

    for sp in pickings:
        extra = {}
        if sp.sale_id and sp.sale_id.id in maps.so:
            extra['sale_id'] = maps.so[sp.sale_id.id]
        result, status = _migrate_one_picking(sp, src, tgt, maps, extra)
        if not result:
            skipped += 1
            continue
        created += 1
        if sp.state == 'done' and status == 'ok':
            validated += 1
    log.phase('04-Stock Pickings', phase='deliveries', created=created,
              validated=validated, skipped=skipped, source_total=len(pickings))


def migrate_opening_inventory(src, tgt, maps, log):
    """Sheet 03-Opening Inventory — reconcile quants to match source (after pickings)."""
    sheet = 3
    Quant = tgt['stock.quant']
    wh = tgt['stock.warehouse'].search([('company_id', '=', tgt.company.id)], limit=1)
    if not wh:
        log.phase(sheet, error='no_warehouse')
        return

    location = wh.lot_stock_id
    src_wh = src['stock.warehouse'].search([('company_id', '=', src.company.id)], limit=1)
    src_location_ids = src_wh.view_location_id.child_ids.filtered(
        lambda l: l.usage == 'internal'
    ).ids if src_wh else []

    applied = skipped = 0
    Product = src['product.product']

    for src_pp_id, tgt_pp_id in maps.product.items():
        if tgt_pp_id == maps.consolidated_empties_pp:
            continue
        if src_pp_id in maps.source_empties_pp_ids:
            continue
        src_pp = Product.browse(src_pp_id)
        quants = src['stock.quant'].search([
            ('product_id', '=', src_pp_id),
            ('location_id', 'in', src_location_ids),
        ])
        qty = sum(quants.mapped('quantity'))
        if qty <= 0:
            continue

        quant = Quant.search([
            ('product_id', '=', tgt_pp_id),
            ('location_id', '=', location.id),
        ], limit=1)
        if not quant:
            quant = Quant.create({
                'product_id': tgt_pp_id,
                'location_id': location.id,
            })
        quant.inventory_quantity = qty
        try:
            quant.action_apply_inventory()
            applied += 1
        except Exception as exc:
            _logger.warning('Inventory apply failed SKU %s: %s', src_pp.default_code, exc)
            skipped += 1

    log.phase(sheet, applied=applied, skipped=skipped, location=location.display_name)


def migrate_partners(src, tgt, maps, log, supplier_sheet=2, customer_sheet=8):
    """Sheets 02-Vendor Master + 08-Customer Master."""
    Partner = tgt['res.partner']
    vendors = src['res.partner'].search([('supplier_rank', '>', 0)])
    customers = src['res.partner'].search([('customer_rank', '>', 0)])
    all_partners = (vendors | customers).sorted('id')

    v_created = c_created = 0
    for p in all_partners:
        ref = (p.ref or '').strip()
        domain = [('ref', '=', ref)] if ref else [('name', '=', p.name), ('vat', '=', p.vat or False)]
        existing = Partner.search(domain, limit=1)
        vals = {
            'name': p.name,
            'ref': ref or False,
            'vat': p.vat or False,
            'email': p.email or False,
            'phone': p.phone or False,
            'street': p.street or False,
            'city': p.city or False,
            'supplier_rank': p.supplier_rank,
            'customer_rank': p.customer_rank,
            'company_type': p.company_type,
        }
        if existing:
            existing.write(vals)
            maps.partner[p.id] = existing.id
        else:
            new = Partner.create(vals)
            maps.partner[p.id] = new.id
            if p.supplier_rank:
                v_created += 1
            if p.customer_rank:
                c_created += 1

    log.phase(supplier_sheet, partners=len(vendors), created=v_created)
    log.phase(customer_sheet, partners=len(customers), created=c_created)


def _map_journal(src_journal, src, tgt, maps):
    if src_journal.id in maps.journal:
        return maps.journal[src_journal.id]
    existing = tgt['account.journal'].search([
        ('code', '=', src_journal.code),
        ('company_id', '=', tgt.company.id),
    ], limit=1)
    if existing:
        maps.journal[src_journal.id] = existing.id
        return existing.id
    return False


def _map_uom(src_uom, tgt):
    if not src_uom:
        return False
    existing = tgt['uom.uom'].search([('name', '=', src_uom.name)], limit=1)
    return existing.id if existing else False


def migrate_sale_orders(src, tgt, maps, log):
    """Sheet 09-Sale Orders."""
    sheet = 9
    SaleOrder = tgt['sale.order']
    created = skipped = 0

    orders = src['sale.order'].search([], order='date_order, id')
    for so in orders:
        partner_id = maps.partner.get(so.partner_id.id)
        if not partner_id:
            skipped += 1
            continue

        line_cmds = []
        skip_order = False
        for line in so.order_line:
            if line.display_type:
                line_cmds.append((0, 0, {
                    'display_type': line.display_type,
                    'name': line.name,
                }))
                continue
            product_id = maps.product.get(line.product_id.id)
            if not product_id:
                skip_order = True
                break
            line_cmds.append((0, 0, {
                'product_id': product_id,
                'name': line.name,
                'product_uom_qty': line.product_uom_qty,
                'price_unit': line.price_unit,
                'product_uom': _map_uom(line.product_uom, tgt),
            }))

        if skip_order or not line_cmds:
            skipped += 1
            continue

        new_so = SaleOrder.create({
            'partner_id': partner_id,
            'date_order': so.date_order,
            'client_order_ref': so.client_order_ref,
            'order_line': line_cmds,
        })
        maps.so[so.id] = new_so.id
        if so.state == 'sale':
            try:
                new_so.action_confirm()
            except Exception as exc:
                _logger.warning('SO confirm failed %s: %s', so.name, exc)
        elif so.state == 'cancel':
            new_so.action_cancel()
        created += 1

    log.phase(sheet, created=created, skipped=skipped, source_total=len(orders))


def _copy_account_move(src_move, src, tgt, maps, move_type_label):
    """Create move on target from source."""
    journal_id = _map_journal(src_move.journal_id, src, tgt, maps)
    if not journal_id:
        return None

    partner_id = maps.partner.get(src_move.partner_id.id) if src_move.partner_id else False

    if src_move.move_type in ('out_invoice', 'out_refund', 'in_invoice', 'in_refund'):
        inv_line_cmds = []
        for line in src_move.invoice_line_ids:
            product_id = maps.product.get(line.product_id.id) if line.product_id else False
            inv_line_cmds.append((0, 0, {
                'name': line.name,
                'product_id': product_id or False,
                'quantity': line.quantity,
                'price_unit': line.price_unit,
                'product_uom_id': _map_uom(line.product_uom_id, tgt),
            }))
        if not inv_line_cmds:
            return None
        move = tgt['account.move'].create({
            'move_type': src_move.move_type,
            'journal_id': journal_id,
            'date': src_move.date,
            'invoice_date': src_move.invoice_date,
            'ref': src_move.ref,
            'partner_id': partner_id or False,
            'invoice_line_ids': inv_line_cmds,
        })
    else:
        line_cmds = []
        for line in src_move.line_ids:
            account_id = maps.account.get(line.account_id.id)
            if not account_id and line.account_id.code:
                existing = tgt['account.account'].search([
                    ('code', '=', line.account_id.code),
                    ('company_ids', 'in', tgt.company.id),
                ], limit=1)
                if existing:
                    account_id = existing.id
                    maps.account[line.account_id.id] = account_id
            if not account_id:
                continue
            line_vals = {
                'name': line.name or '/',
                'account_id': account_id,
                'debit': line.debit,
                'credit': line.credit,
            }
            if line.partner_id and line.partner_id.id in maps.partner:
                line_vals['partner_id'] = maps.partner[line.partner_id.id]
            line_cmds.append((0, 0, line_vals))
        if not line_cmds:
            return None
        move = tgt['account.move'].create({
            'move_type': src_move.move_type,
            'journal_id': journal_id,
            'date': src_move.date,
            'ref': src_move.ref,
            'partner_id': partner_id or False,
            'line_ids': line_cmds,
        })

    if src_move.state == 'posted':
        try:
            move.action_post()
        except Exception as exc:
            _logger.warning('%s post failed %s: %s', move_type_label, src_move.name, exc)
            move.unlink()
            return None
    return move


def migrate_invoices_and_entries(src, tgt, maps, log):
    """Sheets 10-Customer Invoices, 11-Vendor Bills, 12-Journal Entries."""
    inv_created = bill_created = entry_created = skipped = 0

    for move in src['account.move'].search([
        ('state', '=', 'posted'),
        ('move_type', 'in', ('out_invoice', 'out_refund', 'in_invoice', 'in_refund')),
    ], order='date, id'):
        result = _copy_account_move(move, src, tgt, maps, move.move_type)
        if result:
            if move.move_type.startswith('out_'):
                inv_created += 1
            else:
                bill_created += 1
        else:
            skipped += 1

    for move in src['account.move'].search([
        ('state', '=', 'posted'),
        ('move_type', '=', 'entry'),
    ], order='date, id'):
        result = _copy_account_move(move, src, tgt, maps, 'entry')
        if result:
            entry_created += 1
        else:
            skipped += 1

    log.phase(10, created=inv_created)
    log.phase(11, created=bill_created)
    log.phase(12, created=entry_created, skipped=skipped)


def purge_target_before_import(tgt, log):
    """Remove demo/seed records so only migrated RDL data remains."""
    tgt.cr.execute("UPDATE ir_module_module SET demo = false WHERE demo = true")

    rdl_tmpl_ids = tgt['ir.model.data'].search([
        ('module', '=', 'rdl_core_config'),
        ('model', '=', 'product.template'),
    ]).mapped('res_id')

    # Remove demo transactional data
    tgt['stock.picking'].search([]).unlink()
    tgt['sale.order'].search([]).unlink()
    tgt['purchase.order'].search([]).unlink()
    draft_moves = tgt['account.move'].search([('state', '=', 'draft')])
    if draft_moves:
        draft_moves.button_cancel()
        draft_moves.unlink()

    # Deactivate non-RDL products (demo catalog items)
    demo_products = tgt['product.template'].search([
        ('id', 'not in', rdl_tmpl_ids or [0]),
        ('is_brewery', '=', False),
        ('is_packaged_drinks', '=', False),
    ])
    if demo_products:
        demo_products.write({'active': False})

    # Remove demo partners (keep company contact)
    company_partner_id = tgt.company.partner_id.id
    demo_partners = tgt['res.partner'].search([
        ('id', '!=', company_partner_id),
        ('is_company', '=', True),
        ('customer_rank', '=', 0),
        ('supplier_rank', '=', 0),
        ('employee', '=', False),
    ])
    if demo_partners:
        demo_partners.write({'active': False})

    log.phase('prepare', deactivated_products=len(demo_products),
              deactivated_partners=len(demo_partners))


def install_modules_from_source(src, tgt, log):
    """Install modules present on source but missing on target."""
    src_mods = src['ir.module.module'].search([('state', '=', 'installed')]).mapped('name')
    tgt_mods = set(tgt['ir.module.module'].search([('state', '=', 'installed')]).mapped('name'))
    missing = [m for m in src_mods if m not in tgt_mods and m != 'base']
    batch_size = 15
    installed = []
    Module = tgt['ir.module.module']
    for i in range(0, len(missing), batch_size):
        batch = missing[i:i + batch_size]
        mods = Module.search([('name', 'in', batch), ('state', '!=', 'installed')])
        if mods:
            try:
                mods.button_immediate_install()
                tgt.cr.commit()
                installed.extend(mods.mapped('name'))
            except Exception as exc:
                _logger.warning('Module batch install failed (%s): %s', batch, exc)
                tgt.cr.rollback()
    log.phase('modules', installed=len(installed), missing_attempted=len(missing))


def run_migrate_transactions_only(env):
    """PO → receipts → SO → deliveries only."""
    log = MigrationLog()
    log.data['target_db'] = env.cr.dbname
    maps = IdMaps()
    src_env, src_cr, src_reg = _open_source()
    try:
        _bootstrap_maps_from_target(src_env, env, maps)
        env['stock.picking'].search([]).unlink()
        env['purchase.order'].search([]).unlink()
        env['sale.order'].search([]).unlink()
        env.cr.commit()
        migrate_purchase_orders(src_env, env, maps, log)
        env.cr.commit()
        migrate_receipts(src_env, env, maps, log)
        env.cr.commit()
        migrate_sale_orders(src_env, env, maps, log)
        env.cr.commit()
        migrate_deliveries(src_env, env, maps, log)
        env.cr.commit()
        apply_consolidated_empties_qty(src_env, env, maps, log)
        env.cr.commit()
        log.save(LOG_PATH)
    finally:
        src_cr.close()
        src_reg.reset_changes()


def _bootstrap_maps_from_target(src, tgt, maps):
    """Rebuild product/partner maps from SKU/name for transaction-only runs."""
    for pt in src['product.template'].search([('active', '=', True)]):
        sku = _json_text(pt.default_code).strip()
        if not sku:
            continue
        tgt_pt = tgt['product.template'].search([('default_code', '=', sku)], limit=1)
        if tgt_pt:
            maps.product_tmpl[pt.id] = tgt_pt.id
            if pt.product_variant_ids and tgt_pt.product_variant_ids:
                maps.product[pt.product_variant_ids[0].id] = tgt_pt.product_variant_ids[0].id
    maps.source_empties_pp_ids = _source_empties_pp_ids(src)
    consolidated = tgt['product.template'].search([('default_code', '=', EMPTIES_SKU)], limit=1)
    if consolidated:
        maps.consolidated_empties_pp = consolidated.product_variant_ids[:1].id
    for p in src['res.partner'].search([]):
        existing = tgt['res.partner'].search([
            ('name', '=', p.name),
            ('vat', '=', p.vat or False),
        ], limit=1)
        if existing:
            maps.partner[p.id] = existing.id


def run_migration(env):
    """Entry point when executed via odoo shell (env = target)."""
    log = MigrationLog()
    log.data['target_db'] = env.cr.dbname
    maps = IdMaps()

    src_env, src_cr, src_reg = _open_source()
    try:
        _logger.info('=== RDL migration %s → %s ===', SOURCE_DB, env.cr.dbname)

        install_modules_from_source(src_env, env, log)
        env.cr.commit()

        purge_target_before_import(env, log)
        env.cr.commit()

        migrate_categories(src_env, env, maps, log)
        env.cr.commit()

        migrate_products(src_env, env, maps, log)
        env.cr.commit()

        migrate_accounts(src_env, env, maps, log)
        env.cr.commit()

        migrate_partners(src_env, env, maps, log)
        env.cr.commit()

        migrate_purchase_orders(src_env, env, maps, log)
        env.cr.commit()

        migrate_receipts(src_env, env, maps, log)
        env.cr.commit()

        migrate_sale_orders(src_env, env, maps, log)
        env.cr.commit()

        migrate_deliveries(src_env, env, maps, log)
        env.cr.commit()

        migrate_opening_inventory(src_env, env, maps, log)
        env.cr.commit()

        migrate_invoices_and_entries(src_env, env, maps, log)
        env.cr.commit()

        apply_consolidated_empties_qty(src_env, env, maps, log)
        env.cr.commit()

        log.save(LOG_PATH)
        _logger.info('=== Migration complete ===')
    finally:
        src_cr.close()
        src_reg.reset_changes()


def run_reapply_empties_only(env):
    log = MigrationLog()
    log.data['target_db'] = env.cr.dbname
    maps = IdMaps()
    src_env, src_cr, src_reg = _open_source()
    try:
        maps.source_empties_pp_ids = _source_empties_pp_ids(src_env)
        apply_consolidated_empties_qty(src_env, env, maps, log)
        env.cr.commit()
        log.save(LOG_PATH)
        _logger.info('Reapplied consolidated empties qty')
    finally:
        src_cr.close()
        src_reg.reset_changes()


# odoo shell provides `env`
if os.environ.get('RDL_REAPPLY_EMPTIES_ONLY') == '1':
    run_reapply_empties_only(env)
elif os.environ.get('RDL_MIGRATE_TRANSACTIONS_ONLY') == '1':
    run_migrate_transactions_only(env)
else:
    run_migration(env)

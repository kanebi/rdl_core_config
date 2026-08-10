# -*- coding: utf-8 -*-
"""
Snapshot brewery kit links + stock/SVL BEFORE ORM drops legacy columns,
and clear stale settings views that reference removed deposit fields.
"""
import logging
import json

_logger = logging.getLogger(__name__)

STAGING_TABLE = 'rdl_finished_sku_mig_staging'


def migrate(cr, version):
    _clear_stale_deposit_views(cr)
    _snapshot_brewery_state(cr)


def _jsonb_company_float(raw, company_ids, default=0.0):
    """Extract numeric cost from Odoo 18 company-keyed jsonb standard_price."""
    if raw is None:
        return float(default or 0.0)
    if isinstance(raw, (int, float)):
        return float(raw)
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            return float(default or 0.0)
    if isinstance(raw, dict):
        for cid in company_ids:
            if cid in raw and raw[cid] not in (None, ''):
                try:
                    return float(raw[cid])
                except (TypeError, ValueError):
                    continue
        for val in raw.values():
            if val not in (None, ''):
                try:
                    return float(val)
                except (TypeError, ValueError):
                    continue
    return float(default or 0.0)


def _clear_stale_deposit_views(cr):
    cr.execute("""
        SELECT res_id FROM ir_model_data
         WHERE module = 'rdl_core_config'
           AND name = 'res_config_settings_view_form'
           AND model = 'ir.ui.view'
    """)
    view_ids = [row[0] for row in cr.fetchall()]

    cr.execute("""
        SELECT id FROM ir_ui_view
         WHERE model = 'res.config.settings'
           AND (
                arch_db::text LIKE %s
             OR arch_db::text LIKE %s
           )
    """, ('%brewery_default_crate_deposit%', '%brewery_default_bottle_deposit%'))
    view_ids.extend(row[0] for row in cr.fetchall())
    view_ids = list({vid for vid in view_ids if vid})

    if not view_ids:
        _logger.info("pre-migrate: no stale deposit settings views")
        return

    cr.execute(
        "DELETE FROM ir_model_data WHERE model = 'ir.ui.view' AND res_id IN %s",
        (tuple(view_ids),),
    )
    cr.execute("DELETE FROM ir_ui_view WHERE id IN %s", (tuple(view_ids),))
    _logger.info("pre-migrate: deleted stale settings views %s", view_ids)


def _column_exists(cr, table, column):
    cr.execute("""
        SELECT 1 FROM information_schema.columns
         WHERE table_name = %s AND column_name = %s
    """, (table, column))
    return bool(cr.fetchone())


def _snapshot_brewery_state(cr):
    """Persist everything end-migrate needs after legacy columns are dropped."""
    required = [
        'is_brewery', 'liquid_product_id', 'bottle_product_id', 'crate_product_id',
        'full_bottle_product_id', 'empties_product_id', 'brewery_liquid_qty',
    ]
    if not all(_column_exists(cr, 'product_template', c) for c in required):
        _logger.warning(
            "pre-migrate: legacy brewery columns missing — skip snapshot "
            "(already migrated or fresh DB)"
        )
        return

    cr.execute(f"DROP TABLE IF EXISTS {STAGING_TABLE}")
    cr.execute(f"""
        CREATE TABLE {STAGING_TABLE} (
            parent_tmpl_id integer PRIMARY KEY,
            parent_product_id integer,
            pack_qty double precision,
            liquid_product_id integer,
            bottle_product_id integer,
            crate_product_id integer,
            full_bottle_product_id integer,
            empties_product_id integer,
            parent_list_price double precision,
            parent_standard_price double precision,
            svl_value double precision,
            liquid_svl_value double precision,
            empties_svl_value double precision,
            location_packs jsonb,
            notes text
        )
    """)

    # Internal on-hand by (product, location)
    cr.execute("""
        SELECT sq.product_id, sq.location_id, SUM(sq.quantity) AS qty
          FROM stock_quant sq
          JOIN stock_location sl ON sl.id = sq.location_id
         WHERE sl.usage = 'internal'
         GROUP BY sq.product_id, sq.location_id
        HAVING SUM(sq.quantity) != 0
    """)
    qty_map = {(pid, lid): float(qty) for pid, lid, qty in cr.fetchall()}

    # SVL remaining value by product
    cr.execute("""
        SELECT product_id,
               COALESCE(SUM(remaining_qty), 0),
               COALESCE(SUM(remaining_value), 0)
          FROM stock_valuation_layer
         GROUP BY product_id
    """)
    svl_map = {
        pid: {'qty': float(q), 'value': float(v)}
        for pid, q, v in cr.fetchall()
    }

    # Odoo 18: product_product.standard_price is company-keyed jsonb, e.g. {"1": 14213.96}
    cr.execute("SELECT id FROM res_company ORDER BY id")
    company_ids = [str(row[0]) for row in cr.fetchall()] or ['1']

    cr.execute("""
        SELECT pt.id,
               pp.id,
               COALESCE(pt.brewery_liquid_qty, pt.brewery_bottle_qty, 24),
               pt.liquid_product_id,
               pt.bottle_product_id,
               pt.crate_product_id,
               pt.full_bottle_product_id,
               pt.empties_product_id,
               pt.list_price,
               pp.standard_price
          FROM product_template pt
          JOIN product_product pp ON pp.product_tmpl_id = pt.id AND pp.active
         WHERE pt.is_brewery IS TRUE
           AND pt.active IS TRUE
    """)
    parents = cr.fetchall()
    _logger.info("pre-migrate: snapshotting %s brewery parents", len(parents))

    for (
        tmpl_id, product_id, pack_qty, liquid_id, bottle_id, crate_id,
        full_id, empties_id, list_price, std_price_raw,
    ) in parents:
        pack_qty = float(pack_qty or 24) or 24.0
        list_price = float(list_price or 0.0)
        std_price = _jsonb_company_float(std_price_raw, company_ids, default=list_price)
        component_ids = [i for i in (liquid_id, bottle_id, crate_id) if i]

        # Packs per location from crate qty (validated against liquid/bottle)
        location_ids = set()
        for pid in component_ids:
            for (p, loc), _qty in qty_map.items():
                if p == pid:
                    location_ids.add(loc)

        location_packs = {}
        for loc in location_ids:
            crate_qty = qty_map.get((crate_id, loc), 0.0) if crate_id else 0.0
            liquid_qty = qty_map.get((liquid_id, loc), 0.0) if liquid_id else 0.0
            bottle_qty = qty_map.get((bottle_id, loc), 0.0) if bottle_id else 0.0
            candidates = []
            if crate_id:
                candidates.append(crate_qty)
            if liquid_id:
                candidates.append(liquid_qty / pack_qty)
            if bottle_id:
                candidates.append(bottle_qty / pack_qty)
            packs = max(0.0, min(candidates)) if candidates else 0.0
            # Prefer integer packs when components agree
            if abs(packs - round(packs)) < 1e-6:
                packs = float(round(packs))
            if packs > 0:
                location_packs[str(loc)] = packs

        svl_value = 0.0
        liquid_svl = svl_map.get(liquid_id, {}).get('value', 0.0) if liquid_id else 0.0
        bottle_svl = svl_map.get(bottle_id, {}).get('value', 0.0) if bottle_id else 0.0
        crate_svl = svl_map.get(crate_id, {}).get('value', 0.0) if crate_id else 0.0
        empties_svl = bottle_svl + crate_svl
        for pid in component_ids:
            svl_value += svl_map.get(pid, {}).get('value', 0.0)

        cr.execute(f"""
            INSERT INTO {STAGING_TABLE} (
                parent_tmpl_id, parent_product_id, pack_qty,
                liquid_product_id, bottle_product_id, crate_product_id,
                full_bottle_product_id, empties_product_id,
                parent_list_price, parent_standard_price,
                svl_value, liquid_svl_value, empties_svl_value,
                location_packs, notes
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
            )
        """, (
            tmpl_id, product_id, pack_qty,
            liquid_id, bottle_id, crate_id, full_id, empties_id,
            list_price, std_price, svl_value, liquid_svl, empties_svl,
            json.dumps(location_packs),
            None,
        ))

    cr.execute(f"SELECT COUNT(*), COALESCE(SUM(svl_value),0) FROM {STAGING_TABLE}")
    count, total_val = cr.fetchone()
    _logger.info(
        "pre-migrate: staged %s brewery families, component SVL value=%.2f",
        count, total_val,
    )

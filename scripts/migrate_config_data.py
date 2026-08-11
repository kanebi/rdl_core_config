#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Selective config/master-data migration between Odoo PostgreSQL databases.

Copies configuration from source -> target WITHOUT products, MRP, or stock
transactions. Includes categories, UoMs, company, accounting setup, warehouses,
POS/Seerbit config, partners, users, etc.
"""
from __future__ import annotations

import json
import logging
from typing import Any

try:
    from psycopg2.extras import Json
except ImportError:  # pragma: no cover
    Json = None  # type: ignore

_logger = logging.getLogger(__name__)

# Tables never copied (product / inventory / MRP / transactional data)
EXCLUDED_TABLE_PREFIXES = (
    "product_template",
    "product_product",
    "product_attribute",
    "product_tag",
    "product_pricelist_item",
    "product_supplierinfo",
    "mrp_",
    "stock_quant",
    "stock_move",
    "stock_picking",
    "stock_valuation",
    "stock_lot",
    "stock_scrap",
    "sale_order",
    "purchase_order",
    "pos_order",
    "pos_session",
    "account_move",
    "account_payment",
    "account_bank_statement",
    "account_analytic_line",
)

META_COLS = ("write_date", "create_date", "write_uid", "create_uid")


class ConfigMigrator:
    def __init__(self, src_conn, tgt_conn, *, dry_run=False):
        self.src = src_conn
        self.tgt = tgt_conn
        self.dry_run = dry_run
        self.maps: dict[str, dict[int, int]] = {}

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _localized_name(value):
        """Odoo 18 translated fields are stored as JSONB, e.g. {"en_US": "Name"}."""
        if value is None:
            return None
        if isinstance(value, dict):
            return value
        text = str(value).strip()
        return {"en_US": text} if text else None

    @staticmethod
    def _trans_str(value):
        """Normalize Odoo translated JSON/dict fields to a plain string for lookups."""
        if value is None:
            return None
        if isinstance(value, dict):
            for key in ("en_US", "en_GB", "en"):
                if value.get(key):
                    return value[key]
            for v in value.values():
                if v:
                    return v
            return ""
        return value

    @staticmethod
    def _adapt_value(value):
        """Make values safe for psycopg2 (JSONB dicts, lists, nested structures)."""
        if isinstance(value, dict):
            if Json is not None:
                return Json(value)
            return json.dumps(value)
        if isinstance(value, list):
            # leave list-of-scalars for array columns; wrap list-of-dicts as JSON
            if value and isinstance(value[0], dict):
                if Json is not None:
                    return Json(value)
                return json.dumps(value)
            return value
        return value

    def _adapt_row(self, row: dict) -> dict:
        return {k: self._adapt_value(v) for k, v in row.items()}

    def _update_keys(self, row: dict, exclude=()):
        skip = set(exclude) | set(META_COLS)
        return [k for k in row.keys() if k not in skip]

    def _src_fetchall(self, query, params=None):
        with self.src.cursor() as cur:
            cur.execute(query, params or ())
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]

    def _src_fetchone(self, query, params=None):
        rows = self._src_fetchall(query, params)
        return rows[0] if rows else None

    def _table_exists(self, conn, table):
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT 1 FROM information_schema.tables
                 WHERE table_schema = 'public' AND table_name = %s
                """,
                (table,),
            )
            return bool(cur.fetchone())

    def _tgt_column_names(self, table):
        with self.tgt.cursor() as cur:
            cur.execute(
                """
                SELECT column_name FROM information_schema.columns
                 WHERE table_schema = 'public' AND table_name = %s
                 ORDER BY ordinal_position
                """,
                (table,),
            )
            return [r[0] for r in cur.fetchall()]

    def _map_id(self, table, old_id):
        if old_id is None:
            return None
        return self.maps.get(table, {}).get(old_id, old_id)

    def _set_map(self, table, old_id, new_id):
        self.maps.setdefault(table, {})[old_id] = new_id

    def _commit(self):
        if not self.dry_run:
            self.tgt.commit()

    def _upsert_row(self, table, row: dict, conflict_cols: list[str], update_cols: list[str] | None = None):
        row = self._adapt_row(row)
        cols = list(row.keys())
        if update_cols is None:
            update_cols = [
                c for c in cols if c not in conflict_cols + ["id", "create_uid", "write_uid", "create_date", "write_date"]
            ]

        placeholders = ", ".join(f"%({c})s" for c in cols)
        col_list = ", ".join(f'"{c}"' for c in cols)
        conflict = ", ".join(f'"{c}"' for c in conflict_cols)
        updates = ", ".join(f'"{c}" = EXCLUDED."{c}"' for c in update_cols)

        sql = f"""
            INSERT INTO "{table}" ({col_list})
            VALUES ({placeholders})
            ON CONFLICT ({conflict}) DO UPDATE SET {updates}
            RETURNING id
        """
        if self.dry_run:
            _logger.info("[dry-run] upsert %s %s", table, {k: row[k] for k in conflict_cols})
            return row.get("id")

        with self.tgt.cursor() as cur:
            cur.execute(sql, row)
            result = cur.fetchone()
            return result[0] if result else row.get("id")

    def _insert_or_update_by_id(self, table, row: dict, match_field=None):
        """Insert row preserving business data; map source id -> target id."""
        old_id = row.pop("id", None)
        row = self._adapt_row(row)
        if match_field and row.get(match_field):
            existing = self._tgt_fetchone(
                f'SELECT id FROM "{table}" WHERE "{match_field}" = %s LIMIT 1',
                (row[match_field],),
            )
            if existing:
                new_id = existing["id"]
                if not self.dry_run:
                    keys = self._update_keys(row)
                    if keys:
                        sets = ", ".join(f'"{k}" = %s' for k in keys)
                        with self.tgt.cursor() as cur:
                            cur.execute(
                                f'UPDATE "{table}" SET {sets}, write_date = NOW() WHERE id = %s',
                                [row[k] for k in keys] + [new_id],
                            )
                if old_id:
                    self._set_map(table, old_id, new_id)
                return new_id

        if self.dry_run:
            _logger.info("[dry-run] insert %s: %s", table, list(row.keys())[:6])
            if old_id:
                self._set_map(table, old_id, old_id)
            return old_id

        cols = list(row.keys())
        col_list = ", ".join(f'"{c}"' for c in cols)
        placeholders = ", ".join(f"%({c})s" for c in cols)
        with self.tgt.cursor() as cur:
            cur.execute(
                f'INSERT INTO "{table}" ({col_list}) VALUES ({placeholders}) RETURNING id',
                row,
            )
            new_id = cur.fetchone()[0]
        if old_id:
            self._set_map(table, old_id, new_id)
        return new_id

    def _tgt_fetchone(self, query, params=None):
        with self.tgt.cursor() as cur:
            # Adapt any dict params for JSON comparisons / lookups
            if params:
                params = tuple(self._adapt_value(p) if isinstance(p, dict) else p for p in params)
            cur.execute(query, params or ())
            if not cur.description:
                return None
            cols = [d[0] for d in cur.description]
            row = cur.fetchone()
            return dict(zip(cols, row)) if row else None

    def _remap_fk(self, row: dict, field_map: dict[str, str]):
        for field, table in field_map.items():
            if field in row and row[field]:
                if table == "res_currency":
                    row[field] = self._map_currency_id(row[field])
                elif table == "res_country":
                    row[field] = self._map_country_id(row[field])
                else:
                    row[field] = self._map_id(table, row[field])
        return row

    def _copy_table_rows(self, table, where="TRUE", fk_map=None, skip_cols=None):
        if not self._table_exists(self.src, table) or not self._table_exists(self.tgt, table):
            return 0
        tgt_cols = set(self._tgt_column_names(table))
        rows = self._src_fetchall(f'SELECT * FROM "{table}" WHERE {where}')
        count = 0
        for src_row in rows:
            old_id = src_row.get("id")
            row = {k: v for k, v in src_row.items() if k in tgt_cols and k not in (skip_cols or [])}
            if fk_map:
                self._remap_fk(row, fk_map)
                # Drop FKs that still point at missing target rows (unmapped source ids)
                for field, fk_table in fk_map.items():
                    if not row.get(field):
                        continue
                    if not self._tgt_fetchone(f'SELECT id FROM "{fk_table}" WHERE id = %s', (row[field],)):
                        row[field] = None
            try:
                new_id = self._insert_or_update_by_id(table, row)
            except Exception as exc:
                # Skip rows that still can't satisfy FKs; continue migration
                _logger.warning("Skip %s id=%s: %s", table, old_id, exc)
                try:
                    self.tgt.rollback()
                except Exception:
                    pass
                continue
            if old_id and new_id:
                self._set_map(table, old_id, new_id)
            count += 1
        self._commit()
        _logger.info("Migrated %d rows from %s", count, table)
        return count

    def _name_match_sql(self, table, name_value, extra_sql="", extra_params=()):
        """Match translated or plain name columns."""
        name_str = self._trans_str(name_value)
        # Prefer text cast so JSONB name columns compare cleanly
        query = (
            f'SELECT id FROM "{table}" '
            f"WHERE name::text = %s OR name::text LIKE %s {extra_sql} LIMIT 1"
        )
        like = f'%"{name_str}"%' if name_str else ""
        params = (name_str, like) + tuple(extra_params)
        return self._tgt_fetchone(query, params)

    def _map_currency_id(self, old_id):
        """Map currency by ISO name (IDs differ across DBs) and activate on target."""
        if not old_id:
            return None
        cached = self.maps.get("res_currency", {}).get(old_id)
        if cached:
            return cached
        src = self._src_fetchone("SELECT id, name FROM res_currency WHERE id = %s", (old_id,))
        if not src:
            return None
        tgt = self._tgt_fetchone("SELECT id FROM res_currency WHERE name = %s LIMIT 1", (src["name"],))
        if not tgt:
            _logger.warning("Target missing currency %s (source id=%s)", src["name"], old_id)
            return None
        new_id = tgt["id"]
        self._set_map("res_currency", old_id, new_id)
        if not self.dry_run:
            with self.tgt.cursor() as cur:
                cur.execute(
                    "UPDATE res_currency SET active = true, write_date = NOW() WHERE id = %s",
                    (new_id,),
                )
        return new_id

    def _map_country_id(self, old_id):
        if not old_id:
            return None
        cached = self.maps.get("res_country", {}).get(old_id)
        if cached:
            return cached
        src = self._src_fetchone("SELECT id, code FROM res_country WHERE id = %s", (old_id,))
        if not src:
            return None
        tgt = self._tgt_fetchone("SELECT id FROM res_country WHERE code = %s LIMIT 1", (src["code"],))
        if not tgt:
            return None
        self._set_map("res_country", old_id, tgt["id"])
        return tgt["id"]

    def _map_state_id(self, old_id):
        """Map or create res.country.state by country+code/name (custom states may be missing)."""
        if not old_id:
            return None
        cached = self.maps.get("res_country_state", {}).get(old_id)
        if cached:
            return cached
        src = self._src_fetchone("SELECT * FROM res_country_state WHERE id = %s", (old_id,))
        if not src:
            return None
        country_id = self._map_country_id(src.get("country_id"))
        if not country_id:
            return None
        name = self._trans_str(src.get("name"))
        existing = self._tgt_fetchone(
            """
            SELECT id FROM res_country_state
             WHERE country_id = %s
               AND (
                    code = %s
                    OR name::text = %s
                    OR name::text LIKE %s
               )
             LIMIT 1
            """,
            (country_id, src.get("code"), name, f'%"{name}"%' if name else ""),
        )
        if existing:
            self._set_map("res_country_state", old_id, existing["id"])
            return existing["id"]
        row = {k: v for k, v in src.items() if k != "id"}
        row["country_id"] = country_id
        new_id = self._insert_or_update_by_id("res_country_state", row)
        if new_id:
            self._set_map("res_country_state", old_id, new_id)
        return new_id

    def _tgt_row_exists(self, table, row_id):
        if not row_id:
            return False
        return bool(self._tgt_fetchone(f'SELECT id FROM "{table}" WHERE id = %s', (row_id,)))

    # -------------------------------------------------------------- migrations
    def migrate_ir_config_parameter(self):
        rows = self._src_fetchall("SELECT key, value FROM ir_config_parameter ORDER BY key")
        for row in rows:
            if self.dry_run:
                _logger.info("[dry-run] ir.config_parameter %s", row["key"])
                continue
            with self.tgt.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO ir_config_parameter (key, value, create_uid, write_uid, create_date, write_date)
                    VALUES (%(key)s, %(value)s, 1, 1, NOW(), NOW())
                    ON CONFLICT (key) DO UPDATE
                       SET value = EXCLUDED.value, write_date = NOW()
                    """,
                    row,
                )
        self._commit()
        _logger.info("Migrated %d ir.config_parameter entries", len(rows))

    def migrate_uom(self):
        categories = self._src_fetchall("SELECT * FROM uom_category ORDER BY id")
        for cat in categories:
            old_id = cat["id"]
            name = self._trans_str(cat["name"])
            existing = self._name_match_sql("uom_category", cat["name"])
            if existing:
                self._set_map("uom_category", old_id, existing["id"])
            else:
                cat_row = {k: v for k, v in cat.items() if k != "id"}
                new_id = self._insert_or_update_by_id("uom_category", cat_row)
                self._set_map("uom_category", old_id, new_id)

        uoms = self._src_fetchall("SELECT * FROM uom_uom ORDER BY id")
        for uom in uoms:
            old_id = uom["id"]
            cat_id = self._map_id("uom_category", uom.get("category_id"))
            name = self._trans_str(uom["name"])
            existing = self._tgt_fetchone(
                """
                SELECT id FROM uom_uom
                 WHERE (name::text = %s OR name::text LIKE %s)
                   AND category_id = %s
                 LIMIT 1
                """,
                (name, f'%"{name}"%' if name else "", cat_id),
            )
            row = {k: v for k, v in uom.items() if k != "id"}
            row["category_id"] = cat_id
            if existing:
                self._set_map("uom_uom", old_id, existing["id"])
                if not self.dry_run:
                    keys = self._update_keys(row, exclude=("name", "category_id"))
                    if keys:
                        sets = ", ".join(f'"{k}" = %s' for k in keys)
                        with self.tgt.cursor() as cur:
                            vals = [self._adapt_value(row[k]) for k in keys] + [existing["id"]]
                            cur.execute(
                                f'UPDATE uom_uom SET {sets}, write_date = NOW() WHERE id = %s',
                                vals,
                            )
            else:
                new_id = self._insert_or_update_by_id("uom_uom", row)
                self._set_map("uom_uom", old_id, new_id)
        self._commit()
        _logger.info("Migrated %d UoM categories, %d UoMs", len(categories), len(uoms))

    def migrate_product_categories(self):
        cats = self._src_fetchall("SELECT * FROM product_category ORDER BY parent_id NULLS FIRST, id")
        for cat in cats:
            old_id = cat["id"]
            parent_id = self._map_id("product_category", cat.get("parent_id")) if cat.get("parent_id") else None
            name = self._trans_str(cat["name"])
            existing = self._tgt_fetchone(
                """
                SELECT id FROM product_category
                 WHERE (name::text = %s OR name::text LIKE %s)
                   AND COALESCE(parent_id, 0) = COALESCE(%s, 0)
                 LIMIT 1
                """,
                (name, f'%"{name}"%' if name else "", parent_id),
            )
            row = {k: v for k, v in cat.items() if k not in ("id", "parent_path")}
            row["parent_id"] = parent_id
            if existing:
                self._set_map("product_category", old_id, existing["id"])
                if not self.dry_run:
                    keys = self._update_keys(row, exclude=("name",))
                    if keys:
                        sets = ", ".join(f'"{k}" = %s' for k in keys)
                        with self.tgt.cursor() as cur:
                            cur.execute(
                                f'UPDATE product_category SET {sets}, write_date = NOW() WHERE id = %s',
                                [self._adapt_value(row[k]) for k in keys] + [existing["id"]],
                            )
            else:
                new_id = self._insert_or_update_by_id("product_category", row)
                self._set_map("product_category", old_id, new_id)
        self._commit()

        # Category property fields (valuation accounts, cost method, etc.)
        # Odoo 18 may not have ir_property on some DBs — skip gracefully.
        migrated = 0
        if self._table_exists(self.src, "ir_property") and self._table_exists(self.tgt, "ir_property"):
            props = self._src_fetchall(
                """
                SELECT * FROM ir_property
                 WHERE res_id LIKE 'product.category,%%'
                """
            )
            for prop in props:
                res_id = prop.get("res_id") or ""
                if not res_id.startswith("product.category,"):
                    continue
                old_cat_id = int(res_id.split(",")[1])
                new_cat_id = self._map_id("product_category", old_cat_id)
                if not new_cat_id:
                    continue
                row = dict(prop)
                row["res_id"] = f"product.category,{new_cat_id}"
                row.pop("id", None)
                company_id = row.get("company_id")
                if company_id:
                    row["company_id"] = self._map_id("res_company", company_id) or company_id
                if self.dry_run:
                    migrated += 1
                    continue
                cols = [k for k in row if k != "id"]
                adapted = self._adapt_row(row)
                with self.tgt.cursor() as cur:
                    cur.execute(
                        """
                        DELETE FROM ir_property
                         WHERE fields_id = %(fields_id)s
                           AND res_id = %(res_id)s
                           AND COALESCE(company_id, 0) = COALESCE(%(company_id)s, 0)
                        """,
                        adapted,
                    )
                    col_list = ", ".join(f'"{c}"' for c in cols)
                    placeholders = ", ".join(f"%({c})s" for c in cols)
                    cur.execute(f'INSERT INTO ir_property ({col_list}) VALUES ({placeholders})', adapted)
                migrated += 1
            self._commit()
        else:
            _logger.info("Skipping ir_property (table missing on source/target)")
        _logger.info("Migrated %d product categories, %d category properties", len(cats), migrated)

    def migrate_companies(self):
        """Copy full res.company + company partner (address lives on partner in Odoo 18)."""
        companies = self._src_fetchall("SELECT * FROM res_company ORDER BY id")
        tgt_cols = set(self._tgt_column_names("res_company"))
        # Keep target partner_id; defer accounting FKs until charts/taxes exist
        skip_cols = set(META_COLS) | {
            "id",
            "partner_id",
            "account_opening_move_id",  # transactional
            "sale_discount_product_id",  # products excluded
        }
        defer_fk_tables = {
            "account_sale_tax_id": "account_tax",
            "account_purchase_tax_id": "account_tax",
            "account_cash_basis_base_account_id": "account_account",
            "account_default_pos_receivable_account_id": "account_account",
            "account_discount_expense_allocation_id": "account_account",
            "account_discount_income_allocation_id": "account_account",
            "account_journal_early_pay_discount_gain_account_id": "account_account",
            "account_journal_early_pay_discount_loss_account_id": "account_account",
            "account_journal_suspense_account_id": "account_account",
            "account_production_wip_account_id": "account_account",
            "account_production_wip_overhead_account_id": "account_account",
            "automatic_entry_default_journal_id": "account_journal",
            "currency_exchange_journal_id": "account_journal",
            "default_cash_difference_expense_account_id": "account_account",
            "default_cash_difference_income_account_id": "account_account",
            "expense_accrual_account_id": "account_account",
            "expense_currency_exchange_account_id": "account_account",
            "expense_journal_id": "account_journal",
            "expense_outstanding_account_id": "account_account",
            "income_currency_exchange_account_id": "account_account",
            "revenue_accrual_account_id": "account_account",
            "tax_cash_basis_journal_id": "account_journal",
            "transfer_account_id": "account_account",
            "internal_transit_location_id": "stock_location",
        }
        self._pending_company_fks = []

        for idx, comp in enumerate(companies):
            old_id = comp["id"]
            name = self._trans_str(comp["name"])
            target = self._tgt_fetchone(
                "SELECT id, partner_id FROM res_company WHERE id = %s OR name::text = %s OR name::text LIKE %s ORDER BY id LIMIT 1",
                (old_id, name, f'%"{name}"%' if name else ""),
            )
            if not target:
                target = self._tgt_fetchone(
                    "SELECT id, partner_id FROM res_company ORDER BY id OFFSET %s LIMIT 1",
                    (idx,),
                )
            if not target:
                _logger.warning("No target company match for %s", name)
                continue

            new_id = target["id"]
            self._set_map("res_company", old_id, new_id)
            self._set_map("res_partner", comp["partner_id"], target["partner_id"])

            pending_fks = {"company_id": new_id}
            for field in defer_fk_tables:
                if field in comp:
                    pending_fks[field] = comp.get(field)
            self._pending_company_fks.append(pending_fks)

            # Optional FKs that must exist on target or be cleared
            optional_fk = {
                "paperformat_id": "report_paperformat",
                "resource_calendar_id": "resource_calendar",
                "nomenclature_id": "barcode_nomenclature",
                "external_report_layout_id": "ir_ui_view",
                "alias_domain_id": "mail_alias_domain",
                "incoterm_id": "account_incoterms",
                "batch_payment_sequence_id": "ir_sequence",
                "stock_mail_confirmation_template_id": "mail_template",
                "stock_sms_confirmation_template_id": "sms_template",
                "sale_order_template_id": "sale_order_template",
            }

            comp_vals = {}
            for field, value in comp.items():
                if field in skip_cols or field not in tgt_cols or field in defer_fk_tables:
                    continue
                if field == "currency_id":
                    comp_vals[field] = self._map_currency_id(value)
                elif field == "account_fiscal_country_id":
                    comp_vals[field] = self._map_country_id(value)
                elif field == "parent_id":
                    comp_vals[field] = self._map_id("res_company", value) if value else None
                elif field in optional_fk:
                    mapped = self._map_id(optional_fk[field], value) if value else None
                    if mapped and self._tgt_row_exists(optional_fk[field], mapped):
                        comp_vals[field] = mapped
                    elif value and self._tgt_row_exists(optional_fk[field], value):
                        comp_vals[field] = value
                    else:
                        comp_vals[field] = None
                else:
                    comp_vals[field] = value

            if self.dry_run:
                _logger.info(
                    "[dry-run] update company %s -> id %s currency=%s fields=%d",
                    name,
                    new_id,
                    comp_vals.get("currency_id"),
                    len(comp_vals),
                )
            elif comp_vals:
                keys = list(comp_vals.keys())
                sets = ", ".join(f'"{k}" = %s' for k in keys)
                with self.tgt.cursor() as cur:
                    cur.execute(
                        f'UPDATE res_company SET {sets}, write_date = NOW() WHERE id = %s',
                        [self._adapt_value(comp_vals[k]) for k in keys] + [new_id],
                    )
                _logger.info(
                    "Updated company %s: currency_id=%s (%d fields)",
                    name,
                    comp_vals.get("currency_id"),
                    len(comp_vals),
                )

            # Company address/VAT/registry live on res.partner in Odoo 18
            src_partner = self._src_fetchone(
                "SELECT * FROM res_partner WHERE id = %s", (comp["partner_id"],)
            )
            if src_partner and not self.dry_run:
                partner_cols = set(self._tgt_column_names("res_partner"))
                partner_skip = set(META_COLS) | {
                    "id",
                    "commercial_partner_id",
                    "parent_id",
                    "company_id",
                    "user_id",
                    "message_main_attachment_id",
                }
                partner_vals = {}
                for field, value in src_partner.items():
                    if field in partner_skip or field not in partner_cols:
                        continue
                    if field == "country_id":
                        partner_vals[field] = self._map_country_id(value)
                    elif field == "state_id":
                        partner_vals[field] = self._map_state_id(value)
                    elif field == "currency_id":
                        partner_vals[field] = self._map_currency_id(value)
                    else:
                        partner_vals[field] = value
                if partner_vals:
                    keys = list(partner_vals.keys())
                    sets = ", ".join(f'"{k}" = %s' for k in keys)
                    with self.tgt.cursor() as cur:
                        cur.execute(
                            f'UPDATE res_partner SET {sets}, write_date = NOW() WHERE id = %s',
                            [self._adapt_value(partner_vals[k]) for k in keys] + [target["partner_id"]],
                        )
                    _logger.info(
                        "Updated company partner id=%s (%d fields, country=%s state=%s)",
                        target["partner_id"],
                        len(partner_vals),
                        partner_vals.get("country_id"),
                        partner_vals.get("state_id"),
                    )
                    # Staging may still have US Generic CoA while company address is NG.
                    # Prefer installed country localization (l10n_ng -> chart_template=ng).
                    self._apply_country_fiscal_localization(
                        new_id, partner_vals.get("country_id")
                    )
        self._commit()
        _logger.info("Migrated %d companies (accounting FKs deferred)", len(companies))

    def _apply_country_fiscal_localization(self, company_id, partner_country_id):
        """Set Fiscal Country + Localization Package from company address + installed l10n_*.

        Odoo stores Package in res_company.chart_template (e.g. 'ng', 'generic_coa') and
        Fiscal Country in account_fiscal_country_id. Fresh installs often keep generic_coa/US
        even when the company partner country is Nigeria and l10n_ng is installed.
        """
        if self.dry_run or not company_id or not partner_country_id:
            return
        country = self._tgt_fetchone(
            "SELECT id, code FROM res_country WHERE id = %s", (partner_country_id,)
        )
        if not country or not country.get("code"):
            return
        code = country["code"].lower()
        mod_name = f"l10n_{code}"
        installed = self._tgt_fetchone(
            """
            SELECT 1 FROM ir_module_module
             WHERE name = %s AND state = 'installed'
            """,
            (mod_name,),
        )
        chart_template = code if installed else None
        with self.tgt.cursor() as cur:
            if chart_template:
                cur.execute(
                    """
                    UPDATE res_company
                       SET account_fiscal_country_id = %s,
                           chart_template = %s,
                           write_date = NOW()
                     WHERE id = %s
                    """,
                    (country["id"], chart_template, company_id),
                )
            else:
                cur.execute(
                    """
                    UPDATE res_company
                       SET account_fiscal_country_id = %s,
                           write_date = NOW()
                     WHERE id = %s
                    """,
                    (country["id"], company_id),
                )
            # Align existing taxes/tax groups country with fiscal country when safe
            if self._table_exists(self.tgt, "account_tax"):
                cur.execute(
                    """
                    UPDATE account_tax
                       SET country_id = %s, write_date = NOW()
                     WHERE company_id = %s AND (country_id IS DISTINCT FROM %s)
                    """,
                    (country["id"], company_id, country["id"]),
                )
            if self._table_exists(self.tgt, "account_tax_group"):
                cur.execute(
                    """
                    UPDATE account_tax_group
                       SET country_id = %s, write_date = NOW()
                     WHERE country_id IS DISTINCT FROM %s
                       AND id IN (
                            SELECT DISTINCT tax_group_id FROM account_tax
                             WHERE company_id = %s AND tax_group_id IS NOT NULL
                       )
                    """,
                    (country["id"], country["id"], company_id),
                )
        _logger.info(
            "Set fiscal country=%s chart_template=%s on company id=%s (module %s %s)",
            country["code"],
            chart_template or "(unchanged)",
            company_id,
            mod_name,
            "installed" if installed else "missing",
        )

    def migrate_company_tax_fks(self):
        """Apply deferred company accounting FKs after taxes/accounts/journals exist."""
        pending = getattr(self, "_pending_company_fks", None)
        if pending is None:
            # backward compat if only tax keys were stored
            pending = getattr(self, "_pending_company_tax_fks", []) or []
        fk_table = {
            "account_sale_tax_id": "account_tax",
            "account_purchase_tax_id": "account_tax",
            "account_cash_basis_base_account_id": "account_account",
            "account_default_pos_receivable_account_id": "account_account",
            "account_discount_expense_allocation_id": "account_account",
            "account_discount_income_allocation_id": "account_account",
            "account_journal_early_pay_discount_gain_account_id": "account_account",
            "account_journal_early_pay_discount_loss_account_id": "account_account",
            "account_journal_suspense_account_id": "account_account",
            "account_production_wip_account_id": "account_account",
            "account_production_wip_overhead_account_id": "account_account",
            "automatic_entry_default_journal_id": "account_journal",
            "currency_exchange_journal_id": "account_journal",
            "default_cash_difference_expense_account_id": "account_account",
            "default_cash_difference_income_account_id": "account_account",
            "expense_accrual_account_id": "account_account",
            "expense_currency_exchange_account_id": "account_account",
            "expense_journal_id": "account_journal",
            "expense_outstanding_account_id": "account_account",
            "income_currency_exchange_account_id": "account_account",
            "revenue_accrual_account_id": "account_account",
            "tax_cash_basis_journal_id": "account_journal",
            "transfer_account_id": "account_account",
            "internal_transit_location_id": "stock_location",
        }
        for item in pending:
            updates = {}
            for field, table in fk_table.items():
                if field not in item:
                    continue
                mapped = self._map_id(table, item.get(field))
                if mapped and self._tgt_row_exists(table, mapped):
                    updates[field] = mapped
                else:
                    updates[field] = None
            if self.dry_run or not updates:
                continue
            keys = list(updates.keys())
            sets = ", ".join(f'"{k}" = %s' for k in keys)
            with self.tgt.cursor() as cur:
                cur.execute(
                    f'UPDATE res_company SET {sets}, write_date = NOW() WHERE id = %s',
                    [updates[k] for k in keys] + [item["company_id"]],
                )
        self._commit()
        _logger.info("Applied company accounting FKs for %d companies", len(pending))

    def migrate_res_bank_and_partner_banks(self):
        self._copy_table_rows("res_bank", fk_map={})
        banks = self._src_fetchall("SELECT * FROM res_partner_bank ORDER BY id")
        for bank in banks:
            row = {k: v for k, v in bank.items() if k != "id"}
            row["partner_id"] = self._map_id("res_partner", row.get("partner_id")) or row.get("partner_id")
            row["bank_id"] = self._map_id("res_bank", row.get("bank_id")) or row.get("bank_id")
            row["company_id"] = self._map_id("res_company", row.get("company_id")) or row.get("company_id")
            if self.dry_run:
                continue
            existing = self._tgt_fetchone(
                "SELECT id FROM res_partner_bank WHERE acc_number = %s AND partner_id = %s LIMIT 1",
                (row.get("acc_number"), row.get("partner_id")),
            )
            if existing:
                self._set_map("res_partner_bank", bank["id"], existing["id"])
            else:
                self._insert_or_update_by_id("res_partner_bank", row)
        self._commit()
        _logger.info("Migrated partner bank accounts")

    def migrate_partners(self):
        """Commercial partners (customers/vendors), excluding users and company partners."""
        partners = self._src_fetchall(
            """
            SELECT p.* FROM res_partner p
             WHERE p.active = true
               AND NOT EXISTS (SELECT 1 FROM res_users u WHERE u.partner_id = p.id)
               AND NOT EXISTS (SELECT 1 FROM res_company c WHERE c.partner_id = p.id)
             ORDER BY p.id
            """
        )
        deferred_links = []
        for partner in partners:
            old_id = partner["id"]
            ref = partner.get("ref")
            email = partner.get("email")
            name = self._trans_str(partner.get("name"))
            existing = None
            if ref:
                existing = self._tgt_fetchone("SELECT id FROM res_partner WHERE ref = %s LIMIT 1", (ref,))
            if not existing and email:
                existing = self._tgt_fetchone("SELECT id FROM res_partner WHERE email = %s LIMIT 1", (email,))
            if not existing:
                existing = self._tgt_fetchone(
                    """
                    SELECT id FROM res_partner
                     WHERE (name::text = %s OR name::text LIKE %s)
                       AND COALESCE(company_id, 0) = COALESCE(%s, 0)
                     LIMIT 1
                    """,
                    (
                        name,
                        f'%"{name}"%' if name else "",
                        self._map_id("res_company", partner.get("company_id")) or partner.get("company_id"),
                    ),
                )
            row = {k: v for k, v in partner.items() if k != "id"}
            row["company_id"] = self._map_id("res_company", row.get("company_id")) or row.get("company_id")
            # Defer self-FKs so inserts don't require partners that aren't present yet
            deferred_links.append(
                {
                    "old_id": old_id,
                    "parent_id": partner.get("parent_id"),
                    "commercial_partner_id": partner.get("commercial_partner_id"),
                }
            )
            row["parent_id"] = None
            row["commercial_partner_id"] = None

            if existing:
                self._set_map("res_partner", old_id, existing["id"])
            else:
                new_id = self._insert_or_update_by_id("res_partner", row)
                self._set_map("res_partner", old_id, new_id)
        self._commit()

        # Second pass: wire parent / commercial partner links
        for link in deferred_links:
            new_id = self._map_id("res_partner", link["old_id"])
            if not new_id:
                continue
            parent_id = self._map_id("res_partner", link["parent_id"]) if link["parent_id"] else None
            commercial_id = (
                self._map_id("res_partner", link["commercial_partner_id"])
                if link["commercial_partner_id"]
                else new_id
            )
            if self.dry_run:
                continue
            with self.tgt.cursor() as cur:
                cur.execute(
                    """
                    UPDATE res_partner
                       SET parent_id = %s,
                           commercial_partner_id = COALESCE(%s, id),
                           write_date = NOW()
                     WHERE id = %s
                    """,
                    (parent_id, commercial_id, new_id),
                )
        self._commit()
        _logger.info("Migrated %d commercial partners", len(partners))

    def migrate_accounting(self):
        fk = {"company_id": "res_company", "currency_id": "res_currency"}
        for table in (
            "account_tax_group",
            "account_tax",
            "account_account",
            "account_journal",
            "account_payment_term",
            "account_fiscal_position",
        ):
            if not self._table_exists(self.src, table):
                continue
            rows = self._src_fetchall(f'SELECT * FROM "{table}" ORDER BY id')
            for row in rows:
                old_id = row["id"]
                data = {k: v for k, v in row.items() if k != "id"}
                self._remap_fk(data, fk)
                name = self._trans_str(row.get("name"))
                if table == "account_account":
                    match = self._tgt_fetchone(
                        """
                        SELECT id FROM account_account
                         WHERE name::text = %s OR name::text LIKE %s
                         LIMIT 1
                        """,
                        (name, f'%"{name}"%' if name else ""),
                    )
                elif table == "account_journal":
                    match = self._tgt_fetchone(
                        "SELECT id FROM account_journal WHERE code = %s AND company_id = %s LIMIT 1",
                        (row.get("code"), data.get("company_id")),
                    )
                else:
                    match = self._tgt_fetchone(
                        f"""
                        SELECT id FROM "{table}"
                         WHERE name::text = %s OR name::text LIKE %s
                         LIMIT 1
                        """,
                        (name, f'%"{name}"%' if name else ""),
                    )
                if match:
                    self._set_map(table, old_id, match["id"])
                    if not self.dry_run:
                        keys = self._update_keys(data, exclude=("name", "code"))
                        if keys:
                            sets = ", ".join(f'"{k}" = %s' for k in keys)
                            with self.tgt.cursor() as cur:
                                cur.execute(
                                    f'UPDATE "{table}" SET {sets}, write_date = NOW() WHERE id = %s',
                                    [self._adapt_value(data[k]) for k in keys] + [match["id"]],
                                )
                else:
                    new_id = self._insert_or_update_by_id(table, data)
                    self._set_map(table, old_id, new_id)
            self._commit()
            _logger.info("Migrated %d rows from %s", len(rows), table)

    def migrate_stock_structure(self):
        fk_wh = {"company_id": "res_company", "partner_id": "res_partner"}
        warehouses = self._src_fetchall("SELECT * FROM stock_warehouse ORDER BY id")
        for wh in warehouses:
            old_id = wh["id"]
            data = {k: v for k, v in wh.items() if k != "id"}
            self._remap_fk(data, fk_wh)
            match = self._tgt_fetchone(
                "SELECT id FROM stock_warehouse WHERE code = %s AND company_id = %s LIMIT 1",
                (wh.get("code"), data.get("company_id")),
            )
            if match:
                self._set_map("stock_warehouse", old_id, match["id"])
                if not self.dry_run:
                    keys = self._update_keys(data, exclude=("code", "company_id"))
                    if keys:
                        sets = ", ".join(f'"{k}" = %s' for k in keys)
                        with self.tgt.cursor() as cur:
                            cur.execute(
                                f'UPDATE stock_warehouse SET {sets}, write_date = NOW() WHERE id = %s',
                                [self._adapt_value(data[k]) for k in keys] + [match["id"]],
                            )
            else:
                new_id = self._insert_or_update_by_id("stock_warehouse", data)
                self._set_map("stock_warehouse", old_id, new_id)
        self._commit()

        locations = self._src_fetchall(
            "SELECT * FROM stock_location WHERE usage IN ('view', 'internal', 'transit') ORDER BY id"
        )
        for loc in locations:
            old_id = loc["id"]
            data = {k: v for k, v in loc.items() if k not in ("id", "parent_path", "complete_name")}
            data["location_id"] = self._map_id("stock_location", data.get("location_id")) or data.get("location_id")
            data["company_id"] = self._map_id("res_company", data.get("company_id")) or data.get("company_id")
            name = self._trans_str(loc.get("name"))
            match = self._tgt_fetchone(
                """
                SELECT id FROM stock_location
                 WHERE (name::text = %s OR name::text LIKE %s)
                   AND COALESCE(location_id, 0) = COALESCE(%s, 0)
                   AND company_id = %s
                 LIMIT 1
                """,
                (name, f'%"{name}"%' if name else "", data.get("location_id"), data.get("company_id")),
            )
            # Also match by barcode to avoid unique (barcode, company_id) collisions
            if not match and data.get("barcode"):
                match = self._tgt_fetchone(
                    "SELECT id FROM stock_location WHERE barcode = %s AND company_id = %s LIMIT 1",
                    (data.get("barcode"), data.get("company_id")),
                )
            if match:
                self._set_map("stock_location", old_id, match["id"])
                if not self.dry_run:
                    keys = self._update_keys(data, exclude=("name", "barcode", "company_id", "location_id"))
                    if keys:
                        sets = ", ".join(f'"{k}" = %s' for k in keys)
                        with self.tgt.cursor() as cur:
                            cur.execute(
                                f'UPDATE stock_location SET {sets}, write_date = NOW() WHERE id = %s',
                                [self._adapt_value(data[k]) for k in keys] + [match["id"]],
                            )
                    # Refresh display name from source when target still has Odoo defaults
                    src_name = self._trans_str(loc.get("name"))
                    tgt_row = self._tgt_fetchone(
                        "SELECT name FROM stock_location WHERE id = %s", (match["id"],)
                    )
                    tgt_name = self._trans_str(tgt_row.get("name")) if tgt_row else ""
                    if src_name and tgt_name in ("My Company", "Physical Locations", "Partner Locations", "Virtual Locations"):
                        with self.tgt.cursor() as cur:
                            cur.execute(
                                "UPDATE stock_location SET name = %s, write_date = NOW() WHERE id = %s",
                                (self._adapt_value(loc.get("name")), match["id"]),
                            )
            else:
                # If barcode would collide for another reason, clear barcode on insert
                try:
                    new_id = self._insert_or_update_by_id("stock_location", data)
                except Exception as exc:
                    if "stock_location_barcode_company_uniq" in str(exc):
                        self.tgt.rollback()
                        data = dict(data)
                        data["barcode"] = None
                        new_id = self._insert_or_update_by_id("stock_location", data)
                    else:
                        raise
                self._set_map("stock_location", old_id, new_id)
        self._commit()

        # Routes first
        if self._table_exists(self.src, "stock_route"):
            self._copy_table_rows(
                "stock_route",
                fk_map={"company_id": "res_company", "warehouse_id": "stock_warehouse"},
            )

        # Picking types: drop sequence FKs (DB-local), map by code/warehouse when possible
        if self._table_exists(self.src, "stock_picking_type"):
            tgt_cols = set(self._tgt_column_names("stock_picking_type"))
            rows = self._src_fetchall('SELECT * FROM stock_picking_type ORDER BY id')
            for src_row in rows:
                old_id = src_row["id"]
                data = {k: v for k, v in src_row.items() if k in tgt_cols and k != "id"}
                data["company_id"] = self._map_id("res_company", data.get("company_id")) or data.get("company_id")
                data["warehouse_id"] = self._map_id("stock_warehouse", data.get("warehouse_id")) or data.get("warehouse_id")
                for loc_f in ("default_location_src_id", "default_location_dest_id"):
                    if data.get(loc_f):
                        mapped = self._map_id("stock_location", data[loc_f])
                        data[loc_f] = mapped if mapped and self._tgt_fetchone(
                            "SELECT id FROM stock_location WHERE id = %s", (mapped,)
                        ) else None
                for seq_f in ("sequence_id", "return_picking_type_id"):
                    if seq_f == "sequence_id" and seq_f in data:
                        data[seq_f] = None
                    elif seq_f == "return_picking_type_id" and data.get(seq_f):
                        # remap later after all types exist
                        pass
                code = data.get("code") or src_row.get("code")
                match = self._tgt_fetchone(
                    """
                    SELECT id FROM stock_picking_type
                     WHERE code = %s
                       AND COALESCE(warehouse_id, 0) = COALESCE(%s, 0)
                       AND company_id = %s
                     LIMIT 1
                    """,
                    (code, data.get("warehouse_id"), data.get("company_id")),
                )
                if match:
                    self._set_map("stock_picking_type", old_id, match["id"])
                else:
                    try:
                        new_id = self._insert_or_update_by_id("stock_picking_type", data)
                        self._set_map("stock_picking_type", old_id, new_id)
                    except Exception as exc:
                        _logger.warning("Skip stock_picking_type id=%s: %s", old_id, exc)
                        try:
                            self.tgt.rollback()
                        except Exception:
                            pass
            self._commit()
            _logger.info("Migrated stock_picking_type")

        if self._table_exists(self.src, "stock_rule"):
            self._copy_table_rows(
                "stock_rule",
                fk_map={
                    "company_id": "res_company",
                    "warehouse_id": "stock_warehouse",
                    "location_id": "stock_location",
                    "location_src_id": "stock_location",
                    "location_dest_id": "stock_location",
                    "picking_type_id": "stock_picking_type",
                    "route_id": "stock_route",
                },
            )
        if self._table_exists(self.src, "stock_package_type"):
            self._copy_table_rows(
                "stock_package_type",
                fk_map={"company_id": "res_company"},
            )

        _logger.info("Migrated stock structure (no quants/moves/pickings)")

    def migrate_pos(self):
        configs = self._src_fetchall("SELECT * FROM pos_config ORDER BY id")
        for cfg in configs:
            old_id = cfg["id"]
            data = {k: v for k, v in cfg.items() if k != "id"}
            data["company_id"] = self._map_id("res_company", data.get("company_id")) or data.get("company_id")
            for fk in ("picking_type_id", "journal_id", "invoice_journal_id", "stock_location_id"):
                if not data.get(fk):
                    continue
                fk_table = {
                    "picking_type_id": "stock_picking_type",
                    "journal_id": "account_journal",
                    "invoice_journal_id": "account_journal",
                    "stock_location_id": "stock_location",
                }[fk]
                mapped = self._map_id(fk_table, data[fk])
                if mapped and self._tgt_fetchone(f'SELECT id FROM "{fk_table}" WHERE id = %s', (mapped,)):
                    data[fk] = mapped
                else:
                    data[fk] = None
            # Sequences are DB-local; never copy raw source sequence ids
            for seq_field in ("sequence_id", "sequence_line_id"):
                if seq_field in data:
                    data[seq_field] = None
            # picking_type_id is NOT NULL — fall back to warehouse outgoing type
            if not data.get("picking_type_id"):
                fallback = self._tgt_fetchone(
                    """
                    SELECT id FROM stock_picking_type
                     WHERE company_id = %s AND code = 'outgoing'
                     ORDER BY id LIMIT 1
                    """,
                    (data.get("company_id"),),
                )
                if fallback:
                    data["picking_type_id"] = fallback["id"]
            if not data.get("picking_type_id"):
                _logger.warning("Skip pos_config %s — no picking_type_id available", cfg.get("name"))
                continue
            name = self._trans_str(cfg.get("name"))
            match = self._tgt_fetchone(
                """
                SELECT id FROM pos_config
                 WHERE (name::text = %s OR name::text LIKE %s)
                   AND company_id = %s
                 LIMIT 1
                """,
                (name, f'%"{name}"%' if name else "", data.get("company_id")),
            )
            if match:
                self._set_map("pos_config", old_id, match["id"])
                if not self.dry_run:
                    keys = self._update_keys(data, exclude=("name", "sequence_id", "sequence_line_id"))
                    if keys:
                        sets = ", ".join(f'"{k}" = %s' for k in keys)
                        with self.tgt.cursor() as cur:
                            cur.execute(
                                f'UPDATE pos_config SET {sets}, write_date = NOW() WHERE id = %s',
                                [self._adapt_value(data[k]) for k in keys] + [match["id"]],
                            )
            else:
                new_id = self._insert_or_update_by_id("pos_config", data)
                self._set_map("pos_config", old_id, new_id)
        self._commit()

        methods = self._src_fetchall("SELECT * FROM pos_payment_method ORDER BY id")
        tgt_pm_cols = set(self._tgt_column_names("pos_payment_method"))
        for pm in methods:
            old_id = pm["id"]
            # Only columns that exist on target (e.g. Seerbit fields may be missing)
            data = {k: v for k, v in pm.items() if k != "id" and k in tgt_pm_cols}
            data["company_id"] = self._map_id("res_company", data.get("company_id")) or data.get("company_id")
            if data.get("journal_id"):
                mapped = self._map_id("account_journal", data["journal_id"])
                data["journal_id"] = (
                    mapped
                    if mapped and self._tgt_fetchone("SELECT id FROM account_journal WHERE id = %s", (mapped,))
                    else None
                )
            name = self._trans_str(pm.get("name"))
            match = self._tgt_fetchone(
                """
                SELECT id FROM pos_payment_method
                 WHERE (name::text = %s OR name::text LIKE %s)
                   AND company_id = %s
                 LIMIT 1
                """,
                (name, f'%"{name}"%' if name else "", data.get("company_id")),
            )
            if match:
                self._set_map("pos_payment_method", old_id, match["id"])
                if not self.dry_run:
                    keys = self._update_keys(data, exclude=("name", "company_id"))
                    if keys:
                        sets = ", ".join(f'"{k}" = %s' for k in keys)
                        with self.tgt.cursor() as cur:
                            cur.execute(
                                f'UPDATE pos_payment_method SET {sets}, write_date = NOW() WHERE id = %s',
                                [self._adapt_value(data[k]) for k in keys] + [match["id"]],
                            )
            else:
                new_id = self._insert_or_update_by_id("pos_payment_method", data)
                self._set_map("pos_payment_method", old_id, new_id)
        self._commit()
        _logger.info("Migrated POS configs and payment methods")

    def migrate_users_and_groups(self):
        """Copy active internal users from source (login match or create)."""
        users = self._src_fetchall(
            """
            SELECT u.*, p.email, p.name AS partner_name, p.phone, p.mobile
              FROM res_users u
              JOIN res_partner p ON p.id = u.partner_id
             WHERE u.active = true AND u.id > 2
             ORDER BY u.id
            """
        )
        created = 0
        mapped = 0
        for user in users:
            login = user.get("login")
            existing = self._tgt_fetchone(
                "SELECT id, partner_id FROM res_users WHERE login = %s LIMIT 1", (login,)
            )
            if existing:
                self._set_map("res_users", user["id"], existing["id"])
                self._set_map("res_partner", user["partner_id"], existing["partner_id"])
                mapped += 1
                continue

            if self.dry_run:
                _logger.info("[dry-run] would create user %s", login)
                created += 1
                continue

            company_id = self._map_id("res_company", user.get("company_id")) or user.get("company_id") or 1
            partner_cols = set(self._tgt_column_names("res_partner"))
            partner_row = {
                "name": user.get("partner_name") or login,
                "email": user.get("email"),
                "phone": user.get("phone"),
                "mobile": user.get("mobile"),
                "company_id": company_id,
                "active": True,
                "type": "contact",
                "is_company": False,
            }
            partner_row = {k: v for k, v in partner_row.items() if k in partner_cols}
            partner_id = self._insert_or_update_by_id("res_partner", partner_row)

            user_cols = set(self._tgt_column_names("res_users"))
            skip_user = set(META_COLS) | {
                "id",
                "partner_id",
                "share",
                "totp_secret",
                "signature",
            }
            user_row = {k: v for k, v in user.items() if k in user_cols and k not in skip_user}
            user_row["partner_id"] = partner_id
            user_row["company_id"] = company_id
            user_row["active"] = True
            new_user_id = self._insert_or_update_by_id("res_users", user_row)
            self._set_map("res_users", user["id"], new_user_id)
            self._set_map("res_partner", user["partner_id"], partner_id)

            # company access
            if self._table_exists(self.tgt, "res_company_users_rel"):
                with self.tgt.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO res_company_users_rel (user_id, cid)
                        VALUES (%s, %s)
                        ON CONFLICT DO NOTHING
                        """,
                        (new_user_id, company_id),
                    )

            # groups
            if self._table_exists(self.src, "res_groups_users_rel"):
                groups = self._src_fetchall(
                    "SELECT gid FROM res_groups_users_rel WHERE uid = %s", (user["id"],)
                )
                for grp in groups:
                    src_group = self._src_fetchone(
                        "SELECT name, category_id FROM res_groups WHERE id = %s", (grp["gid"],)
                    )
                    if not src_group:
                        continue
                    tgt_group = self._tgt_fetchone(
                        """
                        SELECT g.id FROM res_groups g
                         WHERE g.name::text = %s OR g.name::text LIKE %s
                         LIMIT 1
                        """,
                        (
                            self._trans_str(src_group["name"]),
                            f'%{self._trans_str(src_group["name"])}%',
                        ),
                    )
                    if tgt_group:
                        with self.tgt.cursor() as cur:
                            cur.execute(
                                """
                                INSERT INTO res_groups_users_rel (uid, gid)
                                VALUES (%s, %s)
                                ON CONFLICT DO NOTHING
                                """,
                                (new_user_id, tgt_group["id"]),
                            )
            created += 1
            _logger.info("Created user %s (id=%s)", login, new_user_id)

        self._commit()
        _logger.info("Users: %d mapped, %d created", mapped, created)

    def migrate_fix_company_labels(self):
        """Replace leftover 'My Company' labels with the real company name from res_company."""
        company = self._tgt_fetchone("SELECT id, name FROM res_company ORDER BY id LIMIT 1")
        if not company:
            return
        company_name = self._trans_str(company["name"])
        if not company_name:
            return
        old_label = "My Company"
        if company_name == old_label:
            _logger.info("Company name is still %r — skipping label fix", old_label)
            return

        updates = 0
        text_tables = (
            ("stock_warehouse", ("name",)),
            ("stock_location", ("name", "complete_name")),
            ("stock_picking_type", ("name",)),
            ("stock_route", ("name",)),
            ("res_partner", ("name",)),
        )
        for table, columns in text_tables:
            if not self._table_exists(self.tgt, table):
                continue
            tgt_cols = set(self._tgt_column_names(table))
            for col in columns:
                if col not in tgt_cols:
                    continue
                col_type = self._tgt_fetchone(
                    """
                    SELECT data_type FROM information_schema.columns
                     WHERE table_schema = 'public' AND table_name = %s AND column_name = %s
                    """,
                    (table, col),
                )
                is_json = col_type and col_type.get("data_type") in ("json", "jsonb")
                if self.dry_run:
                    count_row = self._tgt_fetchone(
                        f'SELECT COUNT(*) AS c FROM "{table}" WHERE "{col}"::text ILIKE %s',
                        (f"%{old_label}%",),
                    )
                    updates += count_row["c"] if count_row else 0
                    continue
                with self.tgt.cursor() as cur:
                    if is_json:
                        cur.execute(
                            f"""
                            UPDATE "{table}"
                               SET "{col}" = REPLACE("{col}"::text, %s, %s)::jsonb,
                                   write_date = NOW()
                             WHERE "{col}"::text ILIKE %s
                            """,
                            (old_label, company_name, f"%{old_label}%"),
                        )
                    else:
                        cur.execute(
                            f"""
                            UPDATE "{table}"
                               SET "{col}" = REPLACE("{col}"::text, %s, %s),
                                   write_date = NOW()
                             WHERE "{col}"::text ILIKE %s
                            """,
                            (old_label, company_name, f"%{old_label}%"),
                        )
                    updates += cur.rowcount

        # Sync warehouse/location names from source when codes match
        src_warehouses = self._src_fetchall("SELECT id, name, code, company_id FROM stock_warehouse ORDER BY id")
        for wh in src_warehouses:
            tgt_id = self._map_id("stock_warehouse", wh["id"])
            if not tgt_id or self.dry_run:
                continue
            with self.tgt.cursor() as cur:
                cur.execute(
                    "UPDATE stock_warehouse SET name = %s, write_date = NOW() WHERE id = %s",
                    (self._adapt_value(wh["name"]), tgt_id),
                )
                updates += cur.rowcount

        self._commit()
        _logger.info("Fixed %d 'My Company' label(s); company=%r", updates, company_name)

    def migrate_rdl_sales_teams(self):
        """Create RDL channel sales teams and assign store/van/operations users."""
        company = self._tgt_fetchone("SELECT id FROM res_company ORDER BY id LIMIT 1")
        if not company:
            return
        company_id = company["id"]
        channel_teams = (
            ("Store Sales", "storepos@rdltrading.com"),
            ("Van Sales", "vanpos@rdltrading.com"),
            ("Operations", "operations@rdltrading.com"),
        )
        if not self._table_exists(self.tgt, "crm_team"):
            _logger.info("crm_team table missing — skip sales teams")
            return

        for team_name, login in channel_teams:
            user = self._tgt_fetchone(
                "SELECT id FROM res_users WHERE login = %s AND active LIMIT 1", (login,)
            )
            if not user:
                _logger.warning("User %s not found on target — create users first", login)
                continue

            existing = self._tgt_fetchone(
                """
                SELECT id FROM crm_team
                 WHERE (name::text = %s OR name::text LIKE %s)
                   AND company_id = %s
                 LIMIT 1
                """,
                (team_name, f'%"en_US": "{team_name}"%', company_id),
            )
            if existing:
                team_id = existing["id"]
            elif self.dry_run:
                _logger.info("[dry-run] would create crm.team %r for %s", team_name, login)
                continue
            else:
                team_row = {
                    "name": self._localized_name(team_name),
                    "company_id": company_id,
                    "active": True,
                    "sequence": 10,
                }
                tgt_cols = set(self._tgt_column_names("crm_team"))
                team_row = {k: v for k, v in team_row.items() if k in tgt_cols}
                team_id = self._insert_or_update_by_id("crm_team", team_row)

            if self.dry_run:
                continue

            # Team leader
            if "user_id" in self._tgt_column_names("crm_team"):
                with self.tgt.cursor() as cur:
                    cur.execute(
                        "UPDATE crm_team SET user_id = %s, write_date = NOW() WHERE id = %s",
                        (user["id"], team_id),
                    )

            # Member (Odoo 18 crm.team.member)
            if self._table_exists(self.tgt, "crm_team_member"):
                member = self._tgt_fetchone(
                    "SELECT id FROM crm_team_member WHERE crm_team_id = %s AND user_id = %s LIMIT 1",
                    (team_id, user["id"]),
                )
                if not member:
                    member_cols = set(self._tgt_column_names("crm_team_member"))
                    member_row = {
                        k: v
                        for k, v in {
                            "crm_team_id": team_id,
                            "user_id": user["id"],
                            "active": True,
                        }.items()
                        if k in member_cols
                    }
                    self._insert_or_update_by_id("crm_team_member", member_row)
            _logger.info("Sales team %r -> user %s (team id=%s)", team_name, login, team_id)

        self._commit()


    def migrate_ir_default(self):
        # Escape %% for psycopg2 — bare % in LIKE breaks when execute() parses placeholders
        rows = self._src_fetchall(
            """
            SELECT * FROM ir_default
             WHERE field_id IN (
                SELECT id FROM ir_model_fields
                 WHERE model NOT LIKE 'product.%%'
                   AND model NOT LIKE 'stock.move%%'
                   AND model NOT LIKE 'mrp.%%'
             )
            """
        )
        migrated = 0
        skipped = 0
        for row in rows:
            row = dict(row)
            row.pop("id", None)
            row["company_id"] = self._map_id("res_company", row.get("company_id")) or row.get("company_id")

            # Remap field_id by model + name (ids differ across DBs)
            src_field = self._src_fetchone(
                "SELECT model, name FROM ir_model_fields WHERE id = %s",
                (row.get("field_id"),),
            )
            if not src_field:
                skipped += 1
                continue
            tgt_field = self._tgt_fetchone(
                "SELECT id FROM ir_model_fields WHERE model = %s AND name = %s LIMIT 1",
                (src_field["model"], src_field["name"]),
            )
            if not tgt_field:
                skipped += 1
                continue
            row["field_id"] = tgt_field["id"]

            if self.dry_run:
                migrated += 1
                continue
            adapted = self._adapt_row(row)
            cols = list(adapted.keys())
            with self.tgt.cursor() as cur:
                cur.execute(
                    """
                    DELETE FROM ir_default
                     WHERE field_id = %(field_id)s
                       AND COALESCE(company_id, 0) = COALESCE(%(company_id)s, 0)
                       AND COALESCE(user_id, 0) = COALESCE(%(user_id)s, 0)
                    """,
                    adapted,
                )
                col_list = ", ".join(f'"{c}"' for c in cols)
                placeholders = ", ".join(f"%({c})s" for c in cols)
                cur.execute(f'INSERT INTO ir_default ({col_list}) VALUES ({placeholders})', adapted)
            migrated += 1
        self._commit()
        _logger.info("Migrated %d ir.default entries (skipped %d)", migrated, skipped)

    def run(self) -> dict[str, Any]:
        steps = (
            ("ir_config_parameter", self.migrate_ir_config_parameter),
            ("uom", self.migrate_uom),
            ("product_categories", self.migrate_product_categories),
            ("companies", self.migrate_companies),
            ("partners", self.migrate_partners),
            ("res_bank", self.migrate_res_bank_and_partner_banks),
            ("accounting", self.migrate_accounting),
            ("company_tax_fks", self.migrate_company_tax_fks),
            ("stock_structure", self.migrate_stock_structure),
            ("pos", self.migrate_pos),
            ("ir_default", self.migrate_ir_default),
            ("users", self.migrate_users_and_groups),
            ("fix_company_labels", self.migrate_fix_company_labels),
            ("rdl_sales_teams", self.migrate_rdl_sales_teams),
        )
        summary = {}
        for name, func in steps:
            _logger.info("=== Migrating %s ===", name)
            try:
                func()
                summary[name] = "ok"
            except Exception as exc:
                _logger.exception("Failed migrating %s: %s", name, exc)
                summary[name] = f"error: {exc}"
                if not self.dry_run:
                    try:
                        self.tgt.rollback()
                    except Exception:
                        pass
        return summary

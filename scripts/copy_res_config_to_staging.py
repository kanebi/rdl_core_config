#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Copy res.config / system settings from a source Odoo database into rdl_staging.

Focus areas:
  - Seerbit (pos_seerbit) ir.config_parameter keys
  - res.company + linked res.partner address/contact details
  - Seerbit POS payment method terminal IDs (matched by company + method name)
  - Company bank accounts (res.partner.bank)

Usage (standalone, recommended):
    python3 copy_res_config_to_staging.py
    python3 copy_res_config_to_staging.py --source braw-live --target rdl_staging
    python3 copy_res_config_to_staging.py --dry-run

Via Odoo shell on the target database (uses the same logic):
    cd /home/kane/odoo-18
    ./odoo-source/odoo-bin shell -c odoo.conf -d rdl_staging \\
        < extra-addons/rdl_core_config/scripts/copy_res_config_to_staging.py
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from typing import Any

try:
    import psycopg2
    import psycopg2.extras
except ImportError:
    psycopg2 = None  # type: ignore

_logger = logging.getLogger(__name__)

# Keys managed by pos_seerbit res.config.settings (+ related runtime params)
SEERBIT_CONFIG_PREFIXES = ("pos_seerbit.",)
SEERBIT_CONFIG_KEYS = (
    "pos_seerbit.seerbit_firestore_cred",
    "pos_seerbit.seerbit_firestore_project_id",
    "pos_seerbit.seerbit_firebase_api_key",
    "pos_seerbit.seerbit_public_key",
    "pos_seerbit.seerbit_secret_key",
    "pos_seerbit.seerbit_pocket_id",
    "pos_seerbit.seerbit_pocket_email",
    "pos_seerbit.seerbit_pocket_password",
    "pos_seerbit.seerbit_auto_post",
    "pos_seerbit.seerbit_auto_reconcile",
    "pos_seerbit.pocket_bearer_token",
    "pos_seerbit.default_send_with_seerbit",
)

COMPANY_FIELDS = (
    "name",
    "email",
    "phone",
    "mobile",
    "vat",
    "company_registry",
    "website",
)

PARTNER_FIELDS = (
    "name",
    "email",
    "phone",
    "mobile",
    "vat",
    "street",
    "street2",
    "city",
    "zip",
    "website",
)


def _db_connect(dbname: str, host: str, port: int, user: str, password: str):
    if psycopg2 is None:
        raise RuntimeError("psycopg2 is required. Install with: pip install psycopg2-binary")
    return psycopg2.connect(
        dbname=dbname,
        host=host or "localhost",
        port=port,
        user=user,
        password=password,
    )


def _fetchall_dict(cur, query: str, params=None) -> list[dict[str, Any]]:
    cur.execute(query, params or ())
    cols = [desc[0] for desc in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def _fetchone_dict(cur, query: str, params=None) -> dict[str, Any] | None:
    rows = _fetchall_dict(cur, query, params)
    return rows[0] if rows else None


def copy_ir_config_parameters(src, tgt, dry_run: bool) -> int:
    """Copy Seerbit-related system parameters."""
    with src.cursor() as sc, tgt.cursor() as tc:
        like_clauses = " OR ".join(["key LIKE %s"] * len(SEERBIT_CONFIG_PREFIXES))
        params = [f"{prefix}%" for prefix in SEERBIT_CONFIG_PREFIXES]
        rows = _fetchall_dict(
            sc,
            f"""
                SELECT key, value
                  FROM ir_config_parameter
                 WHERE {like_clauses}
                    OR key = ANY(%s)
                 ORDER BY key
            """,
            params + [list(SEERBIT_CONFIG_KEYS)],
        )

        copied = 0
        for row in rows:
            key, value = row["key"], row["value"]
            if dry_run:
                display = value if len(value or "") <= 80 else f"{value[:77]}..."
                _logger.info("[dry-run] ir.config_parameter %s = %r", key, display)
                copied += 1
                continue

            tc.execute(
                """
                INSERT INTO ir_config_parameter (key, value, create_uid, write_uid, create_date, write_date)
                VALUES (%s, %s, 1, 1, NOW(), NOW())
                ON CONFLICT (key) DO UPDATE
                   SET value = EXCLUDED.value,
                       write_uid = 1,
                       write_date = NOW()
                """,
                (key, value),
            )
            _logger.info("Copied ir.config_parameter: %s", key)
            copied += 1

        if not dry_run:
            tgt.commit()
        return copied


def _resolve_country_id(cur, country_id: int | None) -> int | None:
    if not country_id:
        return None
    row = _fetchone_dict(cur, "SELECT code FROM res_country WHERE id = %s", (country_id,))
    if not row:
        return None
    target = _fetchone_dict(cur, "SELECT id FROM res_country WHERE code = %s", (row["code"],))
    return target["id"] if target else None


def _resolve_state_id(cur, state_id: int | None, target_country_id: int | None) -> int | None:
    if not state_id:
        return None
    row = _fetchone_dict(
        cur,
        "SELECT code, country_id FROM res_country_state WHERE id = %s",
        (state_id,),
    )
    if not row:
        return None
    if target_country_id:
        target = _fetchone_dict(
            cur,
            """
            SELECT id FROM res_country_state
             WHERE code = %s AND country_id = %s
            """,
            (row["code"], target_country_id),
        )
        if target:
            return target["id"]
    target = _fetchone_dict(cur, "SELECT id FROM res_country_state WHERE code = %s", (row["code"],))
    return target["id"] if target else None


def _load_companies(cur) -> list[dict[str, Any]]:
    return _fetchall_dict(
        cur,
        """
        SELECT c.id,
               c.name,
               c.partner_id,
               c.email,
               c.phone,
               c.mobile,
               c.vat,
               c.company_registry,
               c.website,
               c.logo,
               p.name AS partner_name,
               p.email AS partner_email,
               p.phone AS partner_phone,
               p.mobile AS partner_mobile,
               p.vat AS partner_vat,
               p.street,
               p.street2,
               p.city,
               p.zip,
               p.website AS partner_website,
               p.country_id,
               p.state_id
          FROM res_company c
          JOIN res_partner p ON p.id = c.partner_id
         ORDER BY c.id
        """,
    )


def _match_target_company(
    source_company: dict[str, Any],
    target_companies: list[dict[str, Any]],
    match_mode: str,
) -> dict[str, Any] | None:
    if match_mode == "id":
        for company in target_companies:
            if company["id"] == source_company["id"]:
                return company
        return None

    if match_mode == "single":
        return target_companies[0] if len(target_companies) == 1 else None

    # default: match by name (case-insensitive)
    source_name = (source_company["name"] or "").strip().lower()
    for company in target_companies:
        if (company["name"] or "").strip().lower() == source_name:
            return company
    return None


def copy_company_details(src, tgt, dry_run: bool, match_mode: str) -> int:
    with src.cursor() as sc, tgt.cursor() as tc:
        source_companies = _load_companies(sc)
        target_companies = _load_companies(tc)
        copied = 0

        for src_company in source_companies:
            tgt_company = _match_target_company(src_company, target_companies, match_mode)
            if not tgt_company:
                _logger.warning(
                    "No target company match for source company %r (id=%s)",
                    src_company["name"],
                    src_company["id"],
                )
                continue

            target_country_id = _resolve_country_id(tc, src_company["country_id"])
            target_state_id = _resolve_state_id(tc, src_company["state_id"], target_country_id)

            company_values = {field: src_company.get(field) for field in COMPANY_FIELDS}
            partner_values = {
                "name": src_company.get("partner_name") or src_company.get("name"),
                "email": src_company.get("partner_email") or src_company.get("email"),
                "phone": src_company.get("partner_phone") or src_company.get("phone"),
                "mobile": src_company.get("partner_mobile") or src_company.get("mobile"),
                "vat": src_company.get("partner_vat") or src_company.get("vat"),
                "street": src_company.get("street"),
                "street2": src_company.get("street2"),
                "city": src_company.get("city"),
                "zip": src_company.get("zip"),
                "website": src_company.get("partner_website") or src_company.get("website"),
                "country_id": target_country_id,
                "state_id": target_state_id,
            }

            if dry_run:
                _logger.info(
                    "[dry-run] company %r -> %r: %s",
                    src_company["name"],
                    tgt_company["name"],
                    json.dumps({**company_values, **{f"partner_{k}": v for k, v in partner_values.items()}}, default=str),
                )
                copied += 1
                continue

            set_company = ", ".join(f"{field} = %({field})s" for field in COMPANY_FIELDS)
            tc.execute(
                f"""
                UPDATE res_company
                   SET {set_company},
                       write_uid = 1,
                       write_date = NOW()
                 WHERE id = %(company_id)s
                """,
                {**company_values, "company_id": tgt_company["id"]},
            )

            if src_company.get("logo") is not None:
                tc.execute(
                    """
                    UPDATE res_company
                       SET logo = %s,
                           write_uid = 1,
                           write_date = NOW()
                     WHERE id = %s
                    """,
                    (psycopg2.Binary(src_company["logo"]), tgt_company["id"]),
                )

            set_partner = ", ".join(f"{field} = %({field})s" for field in partner_values)
            tc.execute(
                f"""
                UPDATE res_partner
                   SET {set_partner},
                       write_uid = 1,
                       write_date = NOW()
                 WHERE id = %(partner_id)s
                """,
                {**partner_values, "partner_id": tgt_company["partner_id"]},
            )

            _logger.info(
                "Copied company details: %r (source id=%s -> target id=%s)",
                src_company["name"],
                src_company["id"],
                tgt_company["id"],
            )
            copied += 1

        if not dry_run:
            tgt.commit()
        return copied


def copy_company_bank_accounts(src, tgt, dry_run: bool, match_mode: str) -> int:
    with src.cursor() as sc, tgt.cursor() as tc:
        source_companies = _load_companies(sc)
        target_companies = _load_companies(tc)
        copied = 0

        for src_company in source_companies:
            tgt_company = _match_target_company(src_company, target_companies, match_mode)
            if not tgt_company:
                continue

            banks = _fetchall_dict(
                sc,
                """
                SELECT pb.acc_number,
                       pb.acc_holder_name,
                       pb.active,
                       b.name AS bank_name,
                       b.bic AS bank_bic,
                       c.code AS country_code
                  FROM res_partner_bank pb
             LEFT JOIN res_bank b ON b.id = pb.bank_id
             LEFT JOIN res_country c ON c.id = b.country
                 WHERE pb.partner_id = %s
                """,
                (src_company["partner_id"],),
            )

            for bank_row in banks:
                bank_id = None
                if bank_row.get("bank_name"):
                    if bank_row.get("country_code"):
                        tgt_bank = _fetchone_dict(
                            tc,
                            """
                            SELECT b.id
                              FROM res_bank b
                              JOIN res_country c ON c.id = b.country
                             WHERE b.name = %s AND c.code = %s
                             LIMIT 1
                            """,
                            (bank_row["bank_name"], bank_row["country_code"]),
                        )
                    else:
                        tgt_bank = _fetchone_dict(
                            tc,
                            "SELECT id FROM res_bank WHERE name = %s LIMIT 1",
                            (bank_row["bank_name"],),
                        )
                    bank_id = tgt_bank["id"] if tgt_bank else None

                    if not bank_id and not dry_run:
                        country_id = None
                        if bank_row.get("country_code"):
                            country = _fetchone_dict(
                                tc,
                                "SELECT id FROM res_country WHERE code = %s",
                                (bank_row["country_code"],),
                            )
                            country_id = country["id"] if country else None
                        tc.execute(
                            """
                            INSERT INTO res_bank (name, bic, country, create_uid, write_uid, create_date, write_date)
                            VALUES (%s, %s, %s, 1, 1, NOW(), NOW())
                            RETURNING id
                            """,
                            (bank_row["bank_name"], bank_row.get("bank_bic"), country_id),
                        )
                        bank_id = tc.fetchone()[0]

                if dry_run:
                    _logger.info(
                        "[dry-run] bank account %s for company %r",
                        bank_row["acc_number"],
                        src_company["name"],
                    )
                    copied += 1
                    continue

                tc.execute(
                    """
                    SELECT id FROM res_partner_bank
                     WHERE partner_id = %s AND acc_number = %s
                     LIMIT 1
                    """,
                    (tgt_company["partner_id"], bank_row["acc_number"]),
                )
                existing = tc.fetchone()
                if existing:
                    tc.execute(
                        """
                        UPDATE res_partner_bank
                           SET acc_holder_name = %s,
                               bank_id = %s,
                               active = %s,
                               write_uid = 1,
                               write_date = NOW()
                         WHERE id = %s
                        """,
                        (
                            bank_row.get("acc_holder_name"),
                            bank_id,
                            bank_row.get("active", True),
                            existing[0],
                        ),
                    )
                else:
                    tc.execute(
                        """
                        INSERT INTO res_partner_bank (
                            acc_number, acc_holder_name, bank_id, partner_id, company_id,
                            active, create_uid, write_uid, create_date, write_date
                        ) VALUES (%s, %s, %s, %s, %s, %s, 1, 1, NOW(), NOW())
                        """,
                        (
                            bank_row["acc_number"],
                            bank_row.get("acc_holder_name"),
                            bank_id,
                            tgt_company["partner_id"],
                            tgt_company["id"],
                            bank_row.get("active", True),
                        ),
                    )
                _logger.info(
                    "Copied bank account %s for company %r",
                    bank_row["acc_number"],
                    src_company["name"],
                )
                copied += 1

        if not dry_run:
            tgt.commit()
        return copied


def copy_seerbit_payment_methods(src, tgt, dry_run: bool, match_mode: str) -> int:
    with src.cursor() as sc, tgt.cursor() as tc:
        source_companies = _load_companies(sc)
        target_companies = _load_companies(tc)
        copied = 0

        for src_company in source_companies:
            tgt_company = _match_target_company(src_company, target_companies, match_mode)
            if not tgt_company:
                continue

            methods = _fetchall_dict(
                sc,
                """
                SELECT name, use_payment_terminal, seerbit_terminal_id
                  FROM pos_payment_method
                 WHERE company_id = %s
                   AND (use_payment_terminal = 'seerbit'
                        OR seerbit_terminal_id IS NOT NULL
                        OR name ILIKE '%seerbit%')
                """,
                (src_company["id"],),
            )

            for method in methods:
                if dry_run:
                    _logger.info(
                        "[dry-run] payment method %r terminal=%s for company %r",
                        method["name"],
                        method.get("seerbit_terminal_id"),
                        src_company["name"],
                    )
                    copied += 1
                    continue

                tc.execute(
                    """
                    UPDATE pos_payment_method
                       SET use_payment_terminal = %s,
                           seerbit_terminal_id = %s,
                           write_uid = 1,
                           write_date = NOW()
                     WHERE company_id = %s
                       AND name = %s
                    """,
                    (
                        method.get("use_payment_terminal") or "seerbit",
                        method.get("seerbit_terminal_id"),
                        tgt_company["id"],
                        method["name"],
                    ),
                )
                if tc.rowcount:
                    _logger.info(
                        "Updated Seerbit payment method %r for company %r",
                        method["name"],
                        tgt_company["name"],
                    )
                    copied += 1
                else:
                    _logger.warning(
                        "Payment method %r not found on target for company %r",
                        method["name"],
                        tgt_company["name"],
                    )

        if not dry_run:
            tgt.commit()
        return copied


def ensure_seerbit_module_installed(src, tgt, dry_run: bool) -> bool:
    with src.cursor() as sc, tgt.cursor() as tc:
        src_mod = _fetchone_dict(
            sc,
            "SELECT state, latest_version FROM ir_module_module WHERE name = 'pos_seerbit'",
        )
        if not src_mod or src_mod["state"] != "installed":
            _logger.info("pos_seerbit is not installed on source; skipping module state sync")
            return False

        if dry_run:
            _logger.info("[dry-run] would mark pos_seerbit as installed on target")
            return True

        src_version = src_mod.get("latest_version")
        tc.execute(
            """
            UPDATE ir_module_module
               SET state = 'installed',
                   latest_version = COALESCE(%s, latest_version),
                   write_uid = 1,
                   write_date = NOW()
             WHERE name = 'pos_seerbit'
            """,
            (src_version,),
        )
        tgt.commit()
        _logger.info("Ensured pos_seerbit module is marked installed on target")
        return True


def run_migration(
    source_db: str,
    target_db: str,
    *,
    db_host: str = "localhost",
    db_port: int = 5432,
    db_user: str = "kane",
    db_password: str = "kane24",
    dry_run: bool = False,
    match_mode: str = "name",
) -> dict[str, int]:
    src = _db_connect(source_db, db_host, db_port, db_user, db_password)
    tgt = _db_connect(target_db, db_host, db_port, db_user, db_password)
    src.autocommit = False
    tgt.autocommit = False

    try:
        summary = {
            "config_params": copy_ir_config_parameters(src, tgt, dry_run),
            "companies": copy_company_details(src, tgt, dry_run, match_mode),
            "bank_accounts": copy_company_bank_accounts(src, tgt, dry_run, match_mode),
            "payment_methods": copy_seerbit_payment_methods(src, tgt, dry_run, match_mode),
        }
        ensure_seerbit_module_installed(src, tgt, dry_run)
        return summary
    finally:
        src.close()
        tgt.close()


def _default_from_odoo_conf() -> dict[str, Any]:
    conf_path = os.environ.get("ODOO_RC", "/home/kane/odoo-18/odoo.conf")
    values = {
        "db_host": "localhost",
        "db_port": 5432,
        "db_user": "kane",
        "db_password": "kane24",
    }
    if not os.path.isfile(conf_path):
        return values
    with open(conf_path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith(";") or line.startswith("#") or "=" not in line:
                continue
            key, raw = line.split("=", 1)
            key = key.strip()
            raw = raw.strip()
            if key == "db_host" and raw.lower() not in ("false", ""):
                values["db_host"] = raw
            elif key == "db_port" and raw:
                values["db_port"] = int(raw)
            elif key == "db_user" and raw:
                values["db_user"] = raw
            elif key == "db_password" and raw:
                values["db_password"] = raw
    return values


def main(argv: list[str] | None = None) -> int:
    defaults = _default_from_odoo_conf()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", default=os.environ.get("SOURCE_DB", "braw-live"))
    parser.add_argument("--target", default=os.environ.get("TARGET_DB", "rdl_staging"))
    parser.add_argument("--db-host", default=defaults["db_host"])
    parser.add_argument("--db-port", type=int, default=defaults["db_port"])
    parser.add_argument("--db-user", default=defaults["db_user"])
    parser.add_argument("--db-password", default=defaults["db_password"])
    parser.add_argument(
        "--company-match",
        choices=("name", "id", "single"),
        default="name",
        help="How to pair companies between databases (default: name)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print actions without writing")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    _logger.info("Copying settings from %s -> %s", args.source, args.target)

    summary = run_migration(
        args.source,
        args.target,
        db_host=args.db_host,
        db_port=args.db_port,
        db_user=args.db_user,
        db_password=args.db_password,
        dry_run=args.dry_run,
        match_mode=args.company_match,
    )

    _logger.info("Done. Summary: %s", summary)
    return 0


def _run_from_odoo_shell() -> None:
    """Execute when the file is piped into `odoo-bin shell -d <target>`."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    source_db = os.environ.get("SOURCE_DB", "braw-live")
    target_db = env.cr.dbname  # noqa: F821
    if source_db == target_db:
        raise SystemExit("Source and target database must differ")
    _logger.info("Odoo shell mode: copying %s -> %s", source_db, target_db)
    summary = run_migration(source_db, target_db)
    _logger.info("Done. Summary: %s", summary)


if "env" in globals() and globals().get("env") is not None:
    _run_from_odoo_shell()
elif __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Post-migration fix for rdl_live (or any target DB):

  1. Replace leftover "My Company" labels with the real res.company name
  2. Sync warehouse names from staging
  3. Create/copy RDL users from staging
  4. Create Store / Van / Operations sales teams and assign users

Usage:
    source /home/kane/odoo-18/odoo-18env/bin/activate
    cd extra-addons/rdl_core_config/scripts
    python3 fix_rdl_live_labels_and_teams.py
    python3 fix_rdl_live_labels_and_teams.py --dry-run
"""
from __future__ import annotations

import argparse
import logging
import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

try:
    import psycopg2
except ImportError:
    psycopg2 = None  # type: ignore

from migrate_config_data import ConfigMigrator

SOURCE_DB = os.environ.get("SOURCE_DB", "rdl_staging_dev")
TARGET_DB = os.environ.get("TARGET_DB", "rdl_live")
ODOO_CONF = os.environ.get("ODOO_CONF", "/home/kane/odoo-18/odoo.conf")


def _conf():
    values = {"db_host": "localhost", "db_port": 5432, "db_user": "kane", "db_password": "kane24"}
    if os.path.isfile(ODOO_CONF):
        with open(ODOO_CONF, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line or line.startswith(";") or "=" not in line:
                    continue
                key, raw = line.split("=", 1)
                key, raw = key.strip(), raw.strip()
                if key == "db_host" and raw.lower() not in ("false", ""):
                    values["db_host"] = raw
                elif key == "db_port" and raw:
                    values["db_port"] = int(raw)
                elif key == "db_user" and raw:
                    values["db_user"] = raw
                elif key == "db_password" and raw:
                    values["db_password"] = raw
    return values


def _connect(dbname, conf):
    if psycopg2 is None:
        raise RuntimeError("psycopg2 is required")
    host = conf["db_host"]
    if host in (False, "False", "", None):
        host = "localhost"
    return psycopg2.connect(
        dbname=dbname,
        host=host,
        port=conf["db_port"],
        user=conf["db_user"],
        password=conf["db_password"],
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", default=SOURCE_DB)
    parser.add_argument("--target", default=TARGET_DB)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    conf = _conf()
    src = _connect(args.source, conf)
    tgt = _connect(args.target, conf)
    src.autocommit = True
    tgt.autocommit = False
    try:
        migrator = ConfigMigrator(src, tgt, dry_run=args.dry_run)
        # Build warehouse map from staging codes for label sync
        warehouses = migrator._src_fetchall(
            "SELECT id, code, company_id FROM stock_warehouse ORDER BY id"
        )
        for wh in warehouses:
            company_id = migrator._map_id("res_company", wh.get("company_id")) or wh.get("company_id")
            match = migrator._tgt_fetchone(
                "SELECT id FROM stock_warehouse WHERE code = %s AND company_id = %s LIMIT 1",
                (wh.get("code"), company_id),
            )
            if match:
                migrator._set_map("stock_warehouse", wh["id"], match["id"])

        steps = (
            ("users", migrator.migrate_users_and_groups),
            ("fix_company_labels", migrator.migrate_fix_company_labels),
            ("rdl_sales_teams", migrator.migrate_rdl_sales_teams),
        )
        summary = {}
        for name, func in steps:
            logging.info("=== %s ===", name)
            try:
                func()
                summary[name] = "ok"
            except Exception as exc:
                logging.exception("Failed %s: %s", name, exc)
                summary[name] = f"error: {exc}"
                if not args.dry_run:
                    tgt.rollback()
        logging.info("Done: %s", summary)
    finally:
        src.close()
        tgt.close()


if __name__ == "__main__":
    main()

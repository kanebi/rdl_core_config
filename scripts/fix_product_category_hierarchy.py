#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Fix product.category parent_path / complete_name after SQL migration."""
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
    parser.add_argument("--target", default=TARGET_DB)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    conf = _conf()
    tgt = _connect(args.target, conf)
    tgt.autocommit = False
    try:
        migrator = ConfigMigrator(None, tgt, dry_run=args.dry_run)
        migrator._recompute_parent_path("product_category")
        migrator._recompute_product_category_complete_names()
        migrator._recompute_parent_path("stock_location", parent_column="location_id")
        migrator._commit()
        broken = migrator._tgt_fetchone(
            """
            SELECT COUNT(*) AS c FROM product_category
             WHERE parent_path IS NULL OR parent_path = '' OR parent_path = 'False'
            """
        )
        logging.info(
            "Category hierarchy fixed on %s (%s broken parent_path remaining)",
            args.target,
            broken["c"] if broken else "?",
        )
    finally:
        tgt.close()


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Create a fresh rdl_live database and migrate configuration from rdl_staging_dev.

This does NOT clone the source database. It:
  1. Creates an empty PostgreSQL database
  2. Initializes Odoo with the same installed modules as the source
  3. Selectively migrates config/master data (res config, UoM, categories,
     company, accounting, warehouses, POS/Seerbit, partners, etc.)

Explicitly EXCLUDED: products, MRP/BOMs, stock quants/moves/pickings/valuation,
                     sales/purchase orders, POS orders, accounting entries.

Usage:
    python3 create_rdl_live.py --dry-run
    python3 create_rdl_live.py --recreate
    python3 create_rdl_live.py --migrate-only   # target already initialized
"""
from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

try:
    import psycopg2
except ImportError:
    psycopg2 = None  # type: ignore

from migrate_config_data import ConfigMigrator

_logger = logging.getLogger(__name__)

SOURCE_DB = os.environ.get("SOURCE_DB", "rdl_staging_dev")
TARGET_DB = os.environ.get("TARGET_DB", "rdl_live")
ODOO_ROOT = os.environ.get("ODOO_ROOT", "/home/kane/odoo-18")
VENV_PYTHON = os.environ.get(
    "VENV_PYTHON",
    os.path.join(ODOO_ROOT, "odoo-18env", "bin", "python3"),
)
ODOO_BIN = os.path.join(ODOO_ROOT, "odoo-source", "odoo-bin")
ODOO_CONF = os.path.join(ODOO_ROOT, "odoo.conf")

# Force odoo-source addons only (avoid system /usr/lib/python3/dist-packages/odoo)
ADDONS_PATH = ",".join(
    p for p in (
        os.path.join(ODOO_ROOT, "odoo-source", "odoo", "addons"),
        os.path.join(ODOO_ROOT, "odoo-source", "addons"),
        os.path.join(ODOO_ROOT, "labule-addons"),
        os.path.join(ODOO_ROOT, "extra-addons"),
    )
    if os.path.isdir(p)
)


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


def _db_exists(conf, dbname):
    conn = _connect("postgres", conf)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (dbname,))
            return bool(cur.fetchone())
    finally:
        conn.close()


def _terminate_connections(conf, dbname):
    conn = _connect("postgres", conf)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT pg_terminate_backend(pid)
                  FROM pg_stat_activity
                 WHERE datname = %s AND pid <> pg_backend_pid()
                """,
                (dbname,),
            )
    finally:
        conn.close()


def _drop_database(conf, dbname, dry_run=False):
    if not _db_exists(conf, dbname):
        return
    _logger.info("Dropping database %s", dbname)
    if dry_run:
        return
    import time
    for attempt in range(5):
        _terminate_connections(conf, dbname)
        conn = _connect("postgres", conf)
        conn.autocommit = True
        try:
            with conn.cursor() as cur:
                try:
                    cur.execute(f'DROP DATABASE "{dbname}" WITH (FORCE)')
                except Exception:
                    cur.execute(f'DROP DATABASE IF EXISTS "{dbname}"')
        finally:
            conn.close()
        if not _db_exists(conf, dbname):
            _logger.info("Dropped database %s", dbname)
            return
        _logger.warning("Drop attempt %s failed, retrying...", attempt + 1)
        time.sleep(2)
    raise SystemExit(
        f"Could not drop database {dbname!r}. Stop Odoo and close connections, then retry."
    )


def _create_empty_database(conf, dbname, dry_run=False):
    if _db_exists(conf, dbname):
        raise SystemExit(
            f"Database {dbname!r} already exists. Use --recreate to drop it first, or --migrate-only."
        )
    _logger.info("Creating empty database %s", dbname)
    if dry_run:
        return
    conn = _connect("postgres", conf)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(f'CREATE DATABASE "{dbname}" OWNER %s', (conf["db_user"],))
    finally:
        conn.close()


def _table_exists(conf, dbname, table_name):
    conn = _connect(dbname, conf)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass(%s)", (table_name,))
            return cur.fetchone()[0] is not None
    finally:
        conn.close()


def _odoo_schema_ready(conf, dbname):
    return _table_exists(conf, dbname, "public.ir_module_module")


def _get_installed_modules(conf, dbname):
    conn = _connect(dbname, conf)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT name FROM ir_module_module
                 WHERE state IN ('installed', 'to upgrade', 'to install')
                   AND name NOT IN ('studio_customization')
                 ORDER BY name
                """
            )
            return [row[0] for row in cur.fetchall()]
    finally:
        conn.close()


def _get_target_module_states(conf, dbname):
    if not _odoo_schema_ready(conf, dbname):
        return {}
    conn = _connect(dbname, conf)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT name, state FROM ir_module_module
                 WHERE name NOT IN ('studio_customization')
                 ORDER BY name
                """
            )
            return {row[0]: row[1] for row in cur.fetchall()}
    finally:
        conn.close()


def _reset_stuck_module_states(conf, dbname, dry_run=False):
    """Reset modules left in transient states after a failed install."""
    if not _odoo_schema_ready(conf, dbname):
        return 0
    conn = _connect(dbname, conf)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(*) FROM ir_module_module
                 WHERE state IN ('to install', 'to upgrade', 'to remove')
                """
            )
            count = cur.fetchone()[0]
            if not count:
                return 0
            _logger.info("Resetting %d module(s) stuck in transient state on %s", count, dbname)
            if dry_run:
                return count
            cur.execute(
                """
                UPDATE ir_module_module
                   SET state = 'uninstalled'
                 WHERE state IN ('to install', 'to upgrade', 'to remove')
                   AND name <> 'base'
                """
            )
            cur.execute(
                """
                UPDATE ir_module_module
                   SET state = 'installed'
                 WHERE name = 'base'
                """
            )
            return count
    finally:
        conn.close()


def _missing_modules(conf, source, target):
    source_modules = set(_get_installed_modules(conf, source))
    target_states = _get_target_module_states(conf, target)
    installed = {name for name, state in target_states.items() if state == "installed"}
    return sorted(source_modules - installed)


def _install_modules_in_batches(conf, dbname, modules, dry_run=False, batch_size=12):
    """Install modules in small batches to avoid PostgreSQL OOM/crashes on WSL."""
    if not modules:
        return
    for idx in range(0, len(modules), batch_size):
        batch = modules[idx : idx + batch_size]
        batch_no = idx // batch_size + 1
        batch_total = (len(modules) + batch_size - 1) // batch_size
        mod_list = ",".join(batch)
        _logger.info(
            "Installing module batch %d/%d (%d modules): %s",
            batch_no,
            batch_total,
            len(batch),
            mod_list[:120] + ("..." if len(mod_list) > 120 else ""),
        )
        _run_odoo_cmd(
            conf,
            dbname,
            ["-i", mod_list],
            f"install_batch_{batch_no:02d}",
            dry_run,
        )


def _run_odoo_cmd(conf, dbname, extra_args, log_suffix, dry_run=False):
    if dry_run:
        _logger.info("[dry-run] odoo-bin -d %s %s", dbname, " ".join(extra_args))
        return

    if not os.path.isfile(ODOO_BIN):
        raise SystemExit(f"Odoo binary not found: {ODOO_BIN}")

    env = os.environ.copy()
    env["PYTHONPATH"] = os.path.join(ODOO_ROOT, "odoo-source")
    env["PYTHONNOUSERSITE"] = "1"
    env["VIRTUAL_ENV"] = os.path.dirname(os.path.dirname(VENV_PYTHON))
    env["PATH"] = os.path.dirname(VENV_PYTHON) + os.pathsep + env.get("PATH", "")
    log_path = os.path.join(ODOO_ROOT, f"rdl_live_{log_suffix}.log")
    python = VENV_PYTHON if os.path.isfile(VENV_PYTHON) else (sys.executable or "python3")
    if not os.path.isfile(python):
        raise SystemExit(
            f"Python not found at {VENV_PYTHON}. Set ODOO_VENV_PYTHON or activate your venv."
        )
    _logger.info("Using Python: %s", python)
    cmd = [
        python,
        ODOO_BIN,
        "-c", ODOO_CONF,
        "--addons-path", ADDONS_PATH,
        "-d", dbname,
        "--stop-after-init",
        "--without-demo=all",
        "--no-http",
        "--log-level=info",
        *extra_args,
    ]
    _logger.info("Running: odoo-bin -d %s %s (log: %s)", dbname, " ".join(extra_args), log_path)
    with open(log_path, "w", encoding="utf-8") as logf:
        result = subprocess.run(cmd, cwd=ODOO_ROOT, env=env, stdout=logf, stderr=subprocess.STDOUT, text=True)
    if result.returncode != 0:
        try:
            with open(log_path, encoding="utf-8") as logf:
                tail = logf.read()[-8000:]
        except OSError:
            tail = "(log unavailable)"
        _logger.error("Odoo failed (exit %s). Log tail:\n%s", result.returncode, tail)
        raise SystemExit(f"Odoo command failed ({log_suffix}): exit {result.returncode}. See {log_path}")


def _init_odoo_database(conf, dbname, modules, dry_run=False, batch_size=12):
    if dry_run:
        _logger.info("[dry-run] would install %d modules on %s", len(modules), dbname)
        return

    if not _odoo_schema_ready(conf, dbname):
        _logger.info("Fresh database %s — installing base module first", dbname)
        _run_odoo_cmd(conf, dbname, ["-i", "base"], "install_base", dry_run)
    else:
        target_states = _get_target_module_states(conf, dbname)
        if target_states.get("base") != "installed":
            _run_odoo_cmd(conf, dbname, ["-i", "base"], "install_base", dry_run)

    _reset_stuck_module_states(conf, dbname, dry_run)
    others = [m for m in modules if m != "base"]
    if others:
        _install_modules_in_batches(conf, dbname, others, dry_run, batch_size=batch_size)


def _resume_module_install(conf, source, target, dry_run=False, batch_size=12):
    if not _odoo_schema_ready(conf, target):
        _logger.info("Target %s has no Odoo schema — installing base first", target)
        _run_odoo_cmd(conf, target, ["-i", "base"], "install_base", dry_run)
    _reset_stuck_module_states(conf, target, dry_run)
    missing = _missing_modules(conf, source, target)
    if not missing:
        _logger.info("All %d source modules already installed on %s", len(_get_installed_modules(conf, source)), target)
        return
    _logger.info("Resuming install: %d module(s) remaining on %s", len(missing), target)
    _install_modules_in_batches(conf, target, missing, dry_run, batch_size=batch_size)


def _run_migration(conf, source, target, dry_run=False):
    _logger.info("Migrating config/master data: %s -> %s", source, target)
    src = _connect(source, conf)
    tgt = _connect(target, conf)
    src.autocommit = True
    tgt.autocommit = False
    try:
        migrator = ConfigMigrator(src, tgt, dry_run=dry_run)
        return migrator.run()
    finally:
        src.close()
        tgt.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", default=SOURCE_DB)
    parser.add_argument("--target", default=TARGET_DB)
    parser.add_argument("--recreate", action="store_true", help="Drop target DB before creating fresh")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--migrate-only",
        action="store_true",
        help="Skip DB create/init; only run selective config migration",
    )
    parser.add_argument(
        "--skip-init",
        action="store_true",
        help="Create empty DB but skip Odoo module install (DB must already be initialized)",
    )
    parser.add_argument(
        "--resume-install",
        action="store_true",
        help="Install remaining modules on existing target DB (after a failed batch install)",
    )
    parser.add_argument(
        "--install-batch-size",
        type=int,
        default=12,
        help="Modules per Odoo install pass (default 12; lower if PostgreSQL crashes)",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    conf = _conf()

    if not _db_exists(conf, args.source):
        raise SystemExit(f"Source database {args.source!r} does not exist.")

    if args.resume_install:
        if not _db_exists(conf, args.target):
            raise SystemExit(f"Target database {args.target!r} does not exist.")
        _resume_module_install(
            conf,
            args.source,
            args.target,
            args.dry_run,
            batch_size=args.install_batch_size,
        )
    elif not args.migrate_only:
        if args.recreate:
            _drop_database(conf, args.target, args.dry_run)
        _create_empty_database(conf, args.target, args.dry_run)

        if not args.skip_init:
            modules = _get_installed_modules(conf, args.source)
            _logger.info("Source has %d installed modules", len(modules))
            if "base" not in modules:
                modules.insert(0, "base")
            _init_odoo_database(
                conf,
                args.target,
                modules,
                args.dry_run,
                batch_size=args.install_batch_size,
            )

    if not _db_exists(conf, args.target) and not args.dry_run:
        raise SystemExit(f"Target database {args.target!r} does not exist.")

    summary = _run_migration(conf, args.source, args.target, args.dry_run)

    _logger.info("Migration summary: %s", summary)
    _logger.info(
        "Done. Fresh %r has staging config (no products/MRP/stock). Import products from Excel when ready.",
        args.target,
    )


if __name__ == "__main__":
    main()

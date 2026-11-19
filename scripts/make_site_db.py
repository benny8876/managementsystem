#!/usr/bin/env python3
"""Build the starting database for a site server (the hospital's own LAN server)
from a copy of the cloud database: same logins (and passwords), settings, branch,
doctors and item list, but no patients, bills, stock, wards or logs — like
reset_data.py, except the item list is kept (so the site has its items even
before its first sync with the cloud) and the cloud-only Central Warehouse is
dropped. Standard library only.

Usage:
    python3 scripts/make_site_db.py <cloud-copy.db> <out.db>
"""
import shutil
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from reset_data import KEEP_TABLES, MARKER  # noqa: E402

CLOUD_ONLY_WAREHOUSES = ("Central Warehouse",)


def main(src: str, out: str) -> dict:
    shutil.copyfile(src, out)
    db = sqlite3.connect(out)
    try:
        if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise SystemExit("source database failed its integrity check")
        if db.execute("SELECT COUNT(*) FROM branches").fetchone()[0] != 1:
            raise SystemExit("expected a single-branch cloud database")
        tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        deleted = {}
        for table in tables:
            if table not in KEEP_TABLES:
                n = db.execute(f'DELETE FROM "{table}"').rowcount
                if n:
                    deleted[table] = n
        marks = ",".join("?" * len(CLOUD_ONLY_WAREHOUSES))
        deleted["warehouses"] = db.execute(f"DELETE FROM warehouses WHERE name IN ({marks})", CLOUD_ONLY_WAREHOUSES).rowcount
        if not db.execute("SELECT 1 FROM warehouses").fetchone():
            raise SystemExit("no warehouse left for the site")
        db.execute("UPDATE warehouses SET is_default = 1 WHERE id = (SELECT MIN(id) FROM warehouses)")
        db.execute("UPDATE petty_cash SET balance = 0")
        db.execute("UPDATE users SET last_login = NULL")
        db.execute("DELETE FROM app_meta WHERE key IN (?, 'catalog_version')", (MARKER,))
        db.execute("INSERT INTO app_meta (key, value) VALUES (?, 'done')", (MARKER,))
        db.commit()
        db.execute("VACUUM")
        counts = {t: db.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for t in ("users", "catalog_items", "warehouses", "branches")}
        return {"deleted": deleted, "kept": counts}
    finally:
        db.close()


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    print(main(sys.argv[1], sys.argv[2]))

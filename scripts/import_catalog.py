#!/usr/bin/env python3
"""Load an item list + doctor list (from scripts/tara_export.py) into a Hospital
database — used on the cloud server and by the Windows clinic installer.

Runs once per database (marker "tara_catalog_v1" in app_meta), so items staff
delete or edit afterwards are not put back by a later reinstall. Only adds:
an item whose SKU already exists, or a doctor whose name already exists, is
left as it is. Standard library only. Stop the app first.

Usage:
    python3 import_catalog.py <hospital.db> <catalog.json> [--force]
"""
import hashlib
import json
import secrets
import sqlite3
import sys

MARKER = "tara_catalog_v1"


def _hash(password: str) -> str:
    # Same format as app.core.security.hash_password.
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 120_000)
    return f"pbkdf2${salt}${dk.hex()}"


def _insert(db: sqlite3.Connection, table: str, row: dict) -> None:
    """Insert, filling NOT NULL columns the row doesn't mention with an empty/zero value."""
    cols = db.execute(f'PRAGMA table_info("{table}")').fetchall()
    for _cid, name, ctype, notnull, default, pk in cols:
        if pk or name in row or not notnull or default is not None:
            continue
        row[name] = 0 if any(t in (ctype or "").upper() for t in ("INT", "REAL", "FLOAT", "BOOL", "NUM")) else ""
    names = ",".join(f'"{k}"' for k in row)
    db.execute(f'INSERT INTO "{table}" ({names}) VALUES ({",".join("?" * len(row))})', list(row.values()))


def run(db_path: str, catalog_path: str, force: bool = False) -> dict:
    data = json.load(open(catalog_path, encoding="utf-8"))
    db = sqlite3.connect(db_path)
    try:
        if not force and db.execute("SELECT 1 FROM app_meta WHERE key = ?", (MARKER,)).fetchone():
            return {"skipped": "already imported"}
        skus = {r[0] for r in db.execute("SELECT sku FROM catalog_items")}
        added = 0
        for it in data["items"]:
            if it["sku"] in skus:
                continue
            _insert(db, "catalog_items", {
                "sku": it["sku"], "barcode": it["barcode"], "name": it["name"], "name_mm": it["name"],
                "category": it["category"], "department": it["department"], "sub_category": it["sub_category"],
                "unit": it["unit"], "expiry_alert_months": int(it["expiry_alert_months"]), "price": it["price"],
                "cost": it["cost"], "doctor_fee_pct": 0, "is_stock": 1 if it["is_stock"] else 0, "is_controlled": 0,
                "is_package": 0, "package_items": "", "min_stock": it["min_stock"], "is_active": 1,
            })
            skus.add(it["sku"])
            added += 1

        branch = db.execute("SELECT id FROM branches ORDER BY id LIMIT 1").fetchone()
        names = {r[0] for r in db.execute("SELECT full_name FROM users WHERE role = 'doctor'")}
        usernames = {r[0] for r in db.execute("SELECT username FROM users")}
        doctors = 0
        n = 1
        for d in data["doctors"]:
            if d["full_name"] in names:
                continue
            while f"doc{n:06d}" in usernames:
                n += 1
            username = f"doc{n:06d}"
            usernames.add(username)
            _insert(db, "users", {
                "username": username, "hashed_password": _hash(secrets.token_urlsafe(16)), "full_name": d["full_name"],
                "role": "doctor", "department": "", "branch_id": branch[0] if branch else None, "phone": "", "email": "",
                "employee_code": "", "is_active": 1, "specialty": d["specialty"], "consultation_fee": d["consultation_fee"],
                "commission_pct": 0, "is_external": 0, "license_no": "",
            })
            doctors += 1
        db.execute("DELETE FROM app_meta WHERE key = ?", (MARKER,))
        db.execute("INSERT INTO app_meta (key, value) VALUES (?, 'done')", (MARKER,))
        db.commit()
        return {"items_added": added, "doctors_added": doctors}
    finally:
        db.close()


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "--force"]
    if len(args) != 2:
        sys.exit(__doc__)
    print(run(args[0], args[1], force="--force" in sys.argv))

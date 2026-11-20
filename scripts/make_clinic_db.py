"""Build a fresh, empty single-branch database: the cloud (Hospital)
or the Windows clinic server (Clinic).

Like make_clean_db.py — keeps only branches, warehouses, settings, the chart of
accounts, the LAB-P01..P16 panels and the CONSULT fee item; no patients, bills,
stock, wards or logs — but keeps just ONE branch, renumbered to id 1 so the
other branch doesn't exist at all on that server, and gives every login a fresh
random password (the owner password can be shared between both databases).

Usage (from the repo root):
    backend/.venv-local/bin/python scripts/make_clinic_db.py <hospital|clinic> <seeded.db> <out.db> <logins.txt> [owner-password]

<seeded.db>: run the app once with SEED_ON_START=true against an empty database.
"""
import secrets
import shutil
import sqlite3
import string
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from app.core.security import hash_password  # noqa: E402

KEEP_TABLES = {"app_meta", "branches", "warehouses", "users", "ledger_accounts", "settings", "petty_cash", "catalog_items", "sqlite_stat1"}
BRANCH_TABLES = ("warehouses", "users", "petty_cash")
# Seeded branch id, and username -> display name for the logins to keep.
PROFILES = {
    "hospital": (1, {
        "admin": "Owner", "admin1": "Hospital Admin", "receptionist": "Reception", "cashier": "Cashier",
        "pharmacy": "Pharmacy", "nurse": "Nurse", "lab": "Lab", "xray": "X-Ray", "usg": "USG", "warehouse": "Store",
    }),
    "clinic": (2, {
        "admin": "Owner", "admin2": "Clinic Admin", "receptionist": "Reception", "cashier": "Cashier",
        "pharmacy": "Pharmacy", "nurse": "Nurse", "lab": "Lab",
    }),
}


def _password() -> str:
    alphabet = "".join(c for c in string.ascii_letters + string.digits if c not in "0OolI1")
    return "".join(secrets.choice(alphabet) for _ in range(10))


def main(profile: str, src: str, out: str, logins_path: str, owner_pw: str = "") -> None:
    keep_branch, users = PROFILES[profile]
    shutil.copyfile(src, out)
    db = sqlite3.connect(out)
    tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
    for table in tables:
        if table not in KEEP_TABLES:
            db.execute(f"DELETE FROM {table}")
    db.execute("DELETE FROM catalog_items WHERE NOT (sku LIKE 'LAB-P__' OR sku = 'CONSULT')")
    db.execute("UPDATE petty_cash SET balance = 0")

    # One branch only, always id 1 (the other branch's rows are removed, not hidden).
    for table in ("warehouses", "petty_cash"):  # users are picked by name below
        db.execute(f"DELETE FROM {table} WHERE branch_id IS NOT NULL AND branch_id != ?", (keep_branch,))
    db.execute("DELETE FROM branches WHERE id != ?", (keep_branch,))
    if keep_branch != 1:
        db.execute("UPDATE branches SET id = 1 WHERE id = ?", (keep_branch,))
        for table in BRANCH_TABLES:
            db.execute(f"UPDATE {table} SET branch_id = 1 WHERE branch_id = ?", (keep_branch,))
    db.execute("UPDATE branches SET is_main = 1")
    if not db.execute("SELECT 1 FROM petty_cash WHERE branch_id = 1").fetchone():
        db.execute("INSERT INTO petty_cash (branch_id, balance) VALUES (1, 0)")

    marks = ",".join("?" * len(users))
    db.execute(f"DELETE FROM users WHERE username NOT IN ({marks})", list(users))
    branch_name = db.execute("SELECT name FROM branches").fetchone()[0]
    lines = [f"{branch_name} — logins", "Change these passwords after the first login.", "", "username\trole\tpassword"]
    for user_id, username, role in db.execute("SELECT id, username, role FROM users ORDER BY id").fetchall():
        pw = owner_pw if (username == "admin" and owner_pw) else _password()
        db.execute(
            "UPDATE users SET hashed_password = ?, branch_id = 1, full_name = ?, last_login = NULL, allowed_counters = '', allowed_features = '', extra_permissions = '' WHERE id = ?",
            (hash_password(pw), users[username], user_id),
        )
        lines.append(f"{username}\t{role}\t{pw}")
    db.commit()
    db.execute("VACUUM")
    db.close()
    Path(logins_path).write_text("\n".join(lines) + "\n", encoding="utf-8")

    check = sqlite3.connect(out)
    print(profile, {
        label: check.execute(sql).fetchone()[0]
        for label, sql in {
            "branches": "SELECT group_concat(id || ':' || name) FROM branches",
            "warehouses": "SELECT group_concat(id || ':' || name || '@' || branch_id) FROM warehouses",
            "users": "SELECT COUNT(*) FROM users",
            "patients": "SELECT COUNT(*) FROM patients",
            "invoices": "SELECT COUNT(*) FROM invoices",
            "catalog": "SELECT COUNT(*) FROM catalog_items",
        }.items()
    })


if __name__ == "__main__":
    if len(sys.argv) not in (5, 6):
        sys.exit(__doc__)
    main(*sys.argv[1:])

"""Build an empty production database for a new deployment.

Starts from a freshly seeded demo DB (run the app once with SEED_ON_START=true
against an empty DATABASE_URL), then keeps only the structure a real hospital
needs on day one — branches, warehouses, staff logins (fresh random passwords,
no demo doctors), the 16 LAB-P01..P16 lab panels, the CONSULT fee item, the
ledger chart of accounts and system settings — and empties everything else
(patients, bills, payments, stock, wards/beds, demo medicines, logs).

Usage (from the repo root):
    python3 scripts/make_clean_db.py <seeded.db> <out.db> <logins.txt>

Run the deployment with SEED_ON_START=false afterwards, or the startup seed
would put the demo data straight back.
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


def _password() -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(14))


def main(src: str, out: str, logins_path: str) -> None:
    shutil.copyfile(src, out)
    db = sqlite3.connect(out)
    tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
    for table in tables:
        if table not in KEEP_TABLES:
            db.execute(f"DELETE FROM {table}")
    db.execute("DELETE FROM catalog_items WHERE NOT (sku LIKE 'LAB-P__' OR sku = 'CONSULT')")
    db.execute("DELETE FROM users WHERE role = 'doctor'")
    db.execute("UPDATE petty_cash SET balance = 0")
    db.execute("UPDATE users SET last_login = NULL")

    lines = ["username\trole\tbranch\tpassword"]
    for user_id, username, role, branch_id in db.execute("SELECT id, username, role, branch_id FROM users ORDER BY branch_id, id").fetchall():
        pw = _password()
        db.execute("UPDATE users SET hashed_password = ? WHERE id = ?", (hash_password(pw), user_id))
        lines.append(f"{username}\t{role}\t{branch_id}\t{pw}")
    db.commit()
    db.execute("VACUUM")
    db.close()
    Path(logins_path).write_text("\n".join(lines) + "\n")

    check = sqlite3.connect(out)
    labs = check.execute("SELECT COUNT(*) FROM catalog_items WHERE sku LIKE 'LAB-P__'").fetchone()[0]
    consult = check.execute("SELECT COUNT(*) FROM catalog_items WHERE sku = 'CONSULT'").fetchone()[0]
    users = check.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    patients = check.execute("SELECT COUNT(*) FROM patients").fetchone()[0]
    print(f"lab panels={labs} consult_item={consult} users={users} patients={patients}")


if __name__ == "__main__":
    if len(sys.argv) != 4:
        sys.exit(__doc__)
    main(*sys.argv[1:])

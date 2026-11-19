"""Start over with no operational data — used to clear the test data before
go-live, on the cloud database and (through the Windows installer) the clinic's.

Keeps: user accounts (with their current passwords), branches, warehouses,
settings, the chart of accounts, schema/migration markers (app_meta) and the
default lab panels + consultation fee item. Deletes everything else: patients,
bills, payments, orders, queue tokens, stock, wards, logs, numbering sequences
(so bills start again at 000001), and resets petty cash to 0.

Only the standard library, so it runs anywhere:
    python3 reset_data.py <path-to-shwemuse.db>
Stop the app first and take a backup — this cannot be undone.
"""
import sqlite3
import sys

KEEP_TABLES = {"app_meta", "branches", "warehouses", "users", "ledger_accounts", "settings", "petty_cash", "catalog_items", "result_formats", "sqlite_stat1"}
MARKER = "data_reset_golive"


def reset(path: str) -> dict:
    db = sqlite3.connect(path)
    try:
        tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        deleted = {}
        for table in tables:
            if table not in KEEP_TABLES:
                n = db.execute(f'DELETE FROM "{table}"').rowcount
                if n:
                    deleted[table] = n
        n = db.execute("DELETE FROM catalog_items WHERE NOT (sku LIKE 'LAB-P__' OR sku = 'CONSULT')").rowcount
        if n:
            deleted["catalog_items"] = n
        db.execute("UPDATE petty_cash SET balance = 0")
        db.execute("UPDATE users SET last_login = NULL")
        db.execute("DELETE FROM app_meta WHERE key = ?", (MARKER,))
        db.execute("INSERT INTO app_meta (key, value) VALUES (?, 'done')", (MARKER,))
        db.commit()
        db.execute("VACUUM")
        return deleted
    finally:
        db.close()


def already_reset(path: str) -> bool:
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return bool(db.execute("SELECT 1 FROM app_meta WHERE key = ?", (MARKER,)).fetchone())
    finally:
        db.close()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    print(reset(sys.argv[1]))

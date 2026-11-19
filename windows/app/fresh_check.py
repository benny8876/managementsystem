"""Installer helper, run while the server is stopped:
    python fresh_check.pyc <path-to-shwemuse.db> [hospital|clinic]

Prints:
  "old"   — the database is from before the hybrid go-live (no FRESH_START
            marker — test data), or, clinic only, the cloud copy shipped by the
            first installer version; install.ps1 sets it aside (kept in backups)
            and the site starts from the clean database shipped with this ZIP.
  "reset" — the clinic's test data was cleared for go-live (once only; a backup
            is saved in C:\\ShweMuse\\backups first). See reset_data.py.
  "ok"    — nothing to do.
"""
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import reset_data  # noqa: E402

# Written into every ZIP's initial.db (scripts/build_windows.py): a site database
# without it predates the go-live and is replaced once.
FRESH_START = "site_fresh_start_1"


def main(path: str, site: str = "clinic") -> str:
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        old = con.execute("SELECT COUNT(*) FROM branches WHERE name = 'Shwe Muse Hospital'").fetchone()[0]
        fresh = con.execute("SELECT 1 FROM app_meta WHERE key = ?", (FRESH_START,)).fetchone()
        con.close()
    except Exception:  # noqa: BLE001 — unreadable / no branches table: leave it alone
        return "ok"
    if not fresh or (old and site == "clinic"):
        return "old"
    if reset_data.already_reset(path):
        return "ok"
    backups = Path(path).resolve().parent.parent / "backups"
    backups.mkdir(exist_ok=True)
    src = sqlite3.connect(path)
    dst = sqlite3.connect(backups / f"before-reset_{datetime.now():%Y%m%d_%H%M%S}.db")
    with dst:
        src.backup(dst)
    dst.close()
    src.close()
    reset_data.reset(path)
    return "reset"


if __name__ == "__main__":
    print(main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "clinic"))

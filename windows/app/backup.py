"""Daily backup of the clinic database (run by the "Hospital Backup" task).

Uses SQLite's own backup API, which is safe while the server is running.
Snapshots go to C:\\Hospital\\backups (kept 60 days). If config.env has
BACKUP_COPY_TO=D:\\HospitalBackup (or a USB drive path, several separated by ;),
each snapshot is copied there too whenever that drive is present — so the data
survives even if this computer's disk dies.
"""
import shutil
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

HOME = Path(__file__).resolve().parent.parent
DB = HOME / "data" / "hospital.db"
BACKUPS = HOME / "backups"
KEEP_DAYS = 60

BACKUPS.mkdir(exist_ok=True)
(HOME / "logs").mkdir(exist_ok=True)
sys.stdout = sys.stderr = open(HOME / "logs" / "backup.log", "a", encoding="utf-8", buffering=1)


def config_value(key: str) -> str:
    path = HOME / "config.env"
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith(key + "="):
                return line.split("=", 1)[1].strip()
    return ""


def main() -> None:
    if not DB.exists():
        print(f"{datetime.now():%Y-%m-%d %H:%M} no database yet — skipped")
        return
    target = BACKUPS / f"hospital_{datetime.now():%Y%m%d_%H%M%S}.db"
    src = sqlite3.connect(DB)
    dst = sqlite3.connect(target)
    with dst:
        src.backup(dst)
    dst.close()
    src.close()
    print(f"{datetime.now():%Y-%m-%d %H:%M} backup -> {target} ({target.stat().st_size:,} bytes)")

    cutoff = time.time() - KEEP_DAYS * 86400
    for old in BACKUPS.glob("hospital_*.db"):
        if old.stat().st_mtime < cutoff:
            old.unlink()

    for extra in filter(None, (p.strip() for p in config_value("BACKUP_COPY_TO").split(";"))):
        dest = Path(extra)
        if not Path(dest.anchor).exists():
            print(f"  copy skipped — {dest.anchor} not connected")
            continue
        try:
            dest.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target, dest / target.name)
            for old in dest.glob("hospital_*.db"):
                if old.stat().st_mtime < cutoff:
                    old.unlink()
            print(f"  copied to {dest}")
        except OSError as e:
            print(f"  copy to {dest} failed: {e}")


def keep_server_task_enabled() -> None:
    """Safety net: an app update switches the server task off for a few seconds
    while it swaps folders. If the power went off right then, the task would stay
    off — the twice-a-day backup switches it back on."""
    import subprocess
    import sys

    if sys.platform == "win32":
        try:
            subprocess.run(["schtasks", "/Change", "/TN", "Hospital Server", "/ENABLE"],
                           capture_output=True, timeout=60, creationflags=0x08000000)
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    keep_server_task_enabled()
    main()

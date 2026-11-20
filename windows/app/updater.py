"""Applies a downloaded app update on a site server (started by the admin's
"⬆ Update" button — see backend/app/api/v1/site_update.py).

Runs as its own one-time scheduled task ("ShweMuse Update", SYSTEM), from a
copy outside the app folder (C:\\ShweMuse\\run_update.pyc), because it replaces
the app folder:

    1. stop the server task (and keep the 5-minute watchdog from restarting it)
    2. back up the database (sqlite backup, includes the WAL)
    3. unpack the update next to the app, keep initial.db, swap folders
       (app -> app.prev, app.new -> app)
    4. start the server and wait for /health
    5. if it does not come up: put app.prev back and start that again

The data folder is never touched. Result in data\\update_result.json and
logs\\update.log.
"""
import json
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.request
import zipfile
from datetime import datetime
from pathlib import Path

HOME = Path(__file__).resolve().parent
APP = HOME / "app"
NEW = HOME / "app.new"
PREV = HOME / "app.prev"
DB = HOME / "data" / "shwemuse.db"
LOG = HOME / "logs" / "update.log"
RESULT = HOME / "data" / "update_result.json"
TASK = "ShweMuse Server"
NO_WINDOW = 0x08000000


def log(msg: str) -> None:
    LOG.parent.mkdir(exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S}  {msg}\n")


def run(*cmd: str) -> str:
    try:
        p = subprocess.run(list(cmd), capture_output=True, text=True, timeout=60, creationflags=NO_WINDOW)
        return (p.stdout + p.stderr).strip()
    except Exception as e:  # noqa: BLE001
        return f"error {e!r}"


def port() -> int:
    try:
        for line in (HOME / "config.env").read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("PORT="):
                return int(line.split("=", 1)[1].strip())
    except (OSError, ValueError):
        pass
    return 80


def healthy(p: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{p}/health", timeout=3) as r:
            return r.status == 200
    except Exception:  # noqa: BLE001
        return False


def stop_server(p: int) -> None:
    run("schtasks", "/Change", "/TN", TASK, "/DISABLE")
    run("schtasks", "/End", "/TN", TASK)
    for _ in range(20):
        if not healthy(p):
            break
        time.sleep(1)
    # Anything still serving from the app folder (started by hand, older task)
    run("powershell", "-NoProfile", "-Command",
        "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*\\app\\server.pyc*' } | "
        "ForEach-Object { Invoke-CimMethod -InputObject $_ -MethodName Terminate | Out-Null }")
    time.sleep(3)


def start_server(p: int, wait: int = 120) -> bool:
    run("schtasks", "/Change", "/TN", TASK, "/ENABLE")
    run("schtasks", "/Run", "/TN", TASK)
    for _ in range(wait):
        if healthy(p):
            return True
        time.sleep(1)
    return False


def swap(src: Path, dst: Path) -> None:
    """Rename with retries (a file can stay locked for a moment after a process exits)."""
    for i in range(30):
        try:
            src.rename(dst)
            return
        except OSError:
            if i == 29:
                raise
            time.sleep(1)


def backup_db(version: str) -> str:
    if not DB.exists():
        return "no database"
    out = HOME / "backups" / f"before-update_{version}_{datetime.now():%Y%m%d_%H%M%S}.db"
    out.parent.mkdir(exist_ok=True)
    src = sqlite3.connect(DB)
    dst = sqlite3.connect(out)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    return out.name


def finish(version: str, ok: bool, message: str) -> None:
    log(("OK " if ok else "FAILED ") + message)
    RESULT.write_text(json.dumps({"version": version, "ok": ok, "message": message,
                                  "at": datetime.now().isoformat(timespec="seconds")}), encoding="utf-8")


def main(package: str, version: str) -> None:
    p = port()
    log(f"update to {version} from {package}")
    # Unpack and check first, while the server is still serving: the downtime is
    # only the backup + folder swap + start.
    try:
        shutil.rmtree(NEW, ignore_errors=True)
        with zipfile.ZipFile(package) as z:
            bad = z.testzip()
            if bad:
                raise RuntimeError(f"damaged file in package: {bad}")
            z.extractall(NEW)
        if not (NEW / "server.pyc").exists() or not (NEW / "backend").is_dir():
            raise RuntimeError("update package is incomplete")
        for keep in ("initial.db",):
            if (APP / keep).exists() and not (NEW / keep).exists():
                shutil.copy2(APP / keep, NEW / keep)
    except Exception as e:  # noqa: BLE001 — nothing stopped yet
        shutil.rmtree(NEW, ignore_errors=True)
        finish(version, False, f"not installed ({e!r}); old version still running")
        return
    stop_server(p)
    try:
        saved = backup_db(version)
        log(f"database backup: {saved}")
        shutil.rmtree(PREV, ignore_errors=True)
        swap(APP, PREV)
        swap(NEW, APP)
    except Exception as e:  # noqa: BLE001 — keep / restore the old app
        if not APP.exists() and PREV.exists():
            swap(PREV, APP)
        ok = start_server(p)
        finish(version, False, f"not installed ({e!r}); old version {'running' if ok else 'NOT running'}")
        return
    if start_server(p):
        finish(version, True, f"{version} installed")
        return
    # New version did not start: roll back.
    log("new version did not start — rolling back")
    stop_server(p)
    try:
        failed = HOME / "app.failed"
        shutil.rmtree(failed, ignore_errors=True)
        swap(APP, failed)
        swap(PREV, APP)
        ok = start_server(p)
        finish(version, False, f"{version} did not start; rolled back ({'old version running' if ok else 'old version NOT running'})")
    except Exception as e:  # noqa: BLE001
        start_server(p)
        finish(version, False, f"{version} did not start; rollback error {e!r}")


if __name__ == "__main__":
    try:
        main(sys.argv[1], sys.argv[2])
    except Exception as e:  # noqa: BLE001
        log(f"updater crashed: {e!r}")
        run("schtasks", "/Change", "/TN", TASK, "/ENABLE")
        run("schtasks", "/Run", "/TN", TASK)

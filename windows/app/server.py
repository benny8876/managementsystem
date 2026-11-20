"""Hospital offline server for Windows (the hospital's or the clinic's LAN install).

Runs the same FastAPI backend + built frontend as the cloud server, on one
port, for every computer on the site's LAN (http://<this-pc-ip>/). The cloud
stays the hub: see site_sync.py.

Started at boot by the "Hospital Server" scheduled task (see setup/install.ps1)
with pythonw.exe, so there is no console window: everything is logged to
C:\\Hospital\\logs\\server.log instead.

Layout (C:\\Hospital):
    python\\        embedded Python 3.12 + packages
    app\\           this file, backend\\, frontend\\dist\\, initial.db
    data\\          hospital.db  (never overwritten by a reinstall)
    backups\\       daily snapshots (backup.py)
    logs\\          server.log
    config.env     PORT, SECRET_KEY (written once by the installer), SITE, CLOUD_URL, SYNC_KEY
"""
import os
import shutil
import sys
from pathlib import Path

APP = Path(__file__).resolve().parent
HOME = APP.parent
DATA = HOME / "data"
LOGS = HOME / "logs"
DB = DATA / "hospital.db"
DIST = APP / "frontend" / "dist"

DATA.mkdir(exist_ok=True)
LOGS.mkdir(exist_ok=True)

# pythonw.exe has no console — send output to a log file (rotated at ~5 MB).
log_file = LOGS / "server.log"
if log_file.exists() and log_file.stat().st_size > 5_000_000:
    shutil.move(log_file, LOGS / "server.old.log")
sys.stdout = sys.stderr = open(log_file, "a", encoding="utf-8", buffering=1)


def read_config() -> dict[str, str]:
    cfg = {}
    path = HOME / "config.env"
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, value = line.split("=", 1)
                cfg[key.strip()] = value.strip()
    return cfg


cfg = read_config()
PORT = int(cfg.get("PORT", "80"))

# First start: begin from the database shipped with the installer.
if not DB.exists() and (APP / "initial.db").exists():
    shutil.copy(APP / "initial.db", DB)

os.environ["DATABASE_URL"] = f"sqlite:///{DB.as_posix()}"
os.environ["SEED_ON_START"] = "false"
os.environ["SECRET_KEY"] = cfg.get("SECRET_KEY", "shwe-muse-offline-change-me")
os.environ.setdefault("CORS_ORIGINS", f"http://localhost:{PORT},http://127.0.0.1:{PORT}")
# Site server: items come from the cloud; patients' QR links point to the cloud.
os.environ["SITE"] = cfg.get("SITE", "clinic") or "clinic"
os.environ["QR_BASE_URL"] = cfg.get("CLOUD_URL", "")

sys.path.insert(0, str(APP / "backend"))
os.chdir(APP / "backend")

from fastapi import HTTPException  # noqa: E402
from fastapi.responses import FileResponse  # noqa: E402

from app.main import app  # noqa: E402

NO_CACHE = {"Cache-Control": "no-cache"}


@app.get("/{path:path}", include_in_schema=False)
def frontend(path: str):
    """Everything that isn't /api or /health: the built web app (SPA fallback)."""
    if path.startswith("api/"):
        raise HTTPException(404, "Not Found")
    file = (DIST / path).resolve()
    if path and file.is_file() and DIST in file.parents:
        headers = NO_CACHE if file.name in ("sw.js", "manifest.webmanifest") else None
        return FileResponse(file, headers=headers)
    return FileResponse(DIST / "index.html", headers=NO_CACHE)


if __name__ == "__main__":
    import time
    import traceback

    import uvicorn

    # Site <-> cloud sync (data, item master, QR queue), for as long as the server runs.
    sys.path.insert(0, str(APP))
    import site_sync

    site_sync.start()

    # Keep serving: if uvicorn ever stops with an error, log it and start again.
    while True:
        print(f"Hospital offline server starting on port {PORT}", flush=True)
        try:
            uvicorn.run(app, host="0.0.0.0", port=PORT, log_config=None, access_log=False)
        except (Exception, SystemExit):  # noqa: BLE001 — uvicorn exits via SystemExit if the port is busy
            traceback.print_exc()
        time.sleep(5)

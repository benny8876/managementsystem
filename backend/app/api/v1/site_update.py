"""⬆ App update on a site server (hospital / clinic LAN install).

The site's sync thread downloads a newer app version from the cloud in the
background (windows/app/site_sync.py check_update). Here an admin sees that it
is ready and starts the install; the install itself is done by
windows/app/updater.py as a separate one-time scheduled task, because it stops
this server and replaces the app folder (database backup first, rollback if the
new version does not start).
"""
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException

from app.core.config import settings
from app.core.deps import get_current_user, require
from app.models.users import User
from app.services.utils import audit
from app.db.session import get_db
from sqlalchemy.orm import Session

router = APIRouter(prefix="/site-update", tags=["site-update"])

UPDATE_ROLES = ("super_admin", "hospital_admin", "branch_admin")
UPDATE_TASK = "Hospital Update"


def _home() -> Path | None:
    """C:\\Hospital on a site server (the database lives in <home>\\data); None elsewhere."""
    if not settings.site or not settings.database_url.startswith("sqlite:///"):
        return None
    db = Path(settings.database_url[len("sqlite:///"):])
    home = db.resolve().parent.parent
    return home if (home / "app").is_dir() and (home / "config.env").exists() else None


def _read(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


@router.get("/status")
def update_status(user: User = Depends(get_current_user)):
    home = _home()
    if home is None:
        return {"site": False}
    try:
        current = (home / "app" / "VERSION").read_text(encoding="utf-8").strip()
    except OSError:
        current = ""
    ready = _read(home / "updates" / "ready.json")
    ok_ready = bool(ready.get("version")) and ready.get("version") != current and Path(ready.get("file", "")).exists()
    return {
        "site": True,
        "current": current,
        "ready": {"version": ready["version"], "size": ready.get("size"), "downloaded_at": ready.get("downloaded_at")} if ok_ready else None,
        "last": _read(home / "data" / "update_result.json") or None,
        "can_apply": user.role in UPDATE_ROLES,
    }


@router.post("/apply")
def apply_update(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    if user.role not in UPDATE_ROLES:
        raise HTTPException(403, "Admin account ပဲ update လုပ်လို့ရပါတယ်")
    home = _home()
    if home is None or sys.platform != "win32":
        raise HTTPException(400, "Site server မဟုတ်ပါ")
    ready = _read(home / "updates" / "ready.json")
    pkg = Path(ready.get("file", ""))
    if not ready.get("version") or not pkg.exists():
        raise HTTPException(400, "Update အသစ် မရှိသေးပါ")
    h = hashlib.sha256()
    with open(pkg, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    if h.hexdigest() != str(ready.get("sha256", "")).lower():
        raise HTTPException(400, "Update file ပျက်နေပါတယ် — ပြန်ဒေါင်းပါမယ်၊ ခဏနေမှ ထပ်နှိပ်ပါ")
    runner = home / "run_update.pyc"
    shutil.copy2(home / "app" / "updater.pyc", runner)
    pyw = Path(sys.executable)
    pyw = pyw.with_name("pythonw.exe") if pyw.with_name("pythonw.exe").exists() else pyw
    cmd = f'"{pyw}" "{runner}" "{pkg}" "{ready["version"]}"'
    flags = 0x08000000  # no console window
    subprocess.run(["schtasks", "/Create", "/F", "/TN", UPDATE_TASK, "/SC", "ONCE", "/ST", "00:00",
                    "/RU", "SYSTEM", "/RL", "HIGHEST", "/TR", cmd], capture_output=True, creationflags=flags, timeout=60)
    r = subprocess.run(["schtasks", "/Run", "/TN", UPDATE_TASK], capture_output=True, text=True, creationflags=flags, timeout=60)
    if r.returncode != 0:
        raise HTTPException(500, f"Update စလို့ မရပါ: {(r.stdout + r.stderr).strip()[:200]}")
    audit(db, user.id, "site_update", "app", ready["version"], f"update to {ready['version']} started")
    db.commit()
    return {"ok": True, "version": ready["version"]}

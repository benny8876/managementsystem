import gzip
import hashlib
import hmac
import json
import os
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool
from sqlalchemy.orm import Session

from app.core import clinic_view, clock
from app.core.config import settings
from app.core.deps import require
from app.core.live import broker
from app.db.session import get_db
from app.models.accounting import SyncSnapshot
from app.models.users import User
from app.schemas.sync import SyncPushIn

router = APIRouter(prefix="/sync", tags=["sync"])


def _verify_sync_key(x_sync_key: str = Header(default="")) -> None:
    # A branch's cron job (scripts/push_sync.py) has no logged-in user to
    # authenticate as, so this endpoint uses a shared secret header instead of
    # the normal JWT login flow every other endpoint in this app uses.
    if not hmac.compare_digest(x_sync_key.encode(), settings.sync_shared_key.encode()):
        raise HTTPException(401, "Invalid sync key")


MIRROR_MAX_BYTES = 300 * 1024 * 1024


def _client_ip(request: Request) -> str:
    return (request.headers.get("x-forwarded-for") or "").split(",")[0].strip() or (request.client.host if request.client else "")


def _site(request: Request, x_site: str = Header(default="clinic")) -> str:
    """Which site server is calling (X-Site header; older clinic installers send none)."""
    site = (x_site or "clinic").strip().lower()
    if site not in clinic_view.SITES:
        raise HTTPException(400, "Unknown site")
    blocked = {s.strip().lower() for s in settings.sync_blocked_sites.split(",") if s.strip()}
    if site in blocked:
        # Remember who is still calling, so the stray install can be found.
        folder = Path(settings.mirror_dir)
        folder.mkdir(parents=True, exist_ok=True)
        with open(folder / "blocked-calls.log", "a", encoding="utf-8") as f:
            f.write(f"{clock.now():%Y-%m-%d %H:%M:%S}  {site}  {_client_ip(request)}  {request.url.path}\n")
        raise HTTPException(403, f"{site} server is switched off on the cloud")
    return site


def _store_mirror(site: str, raw: bytes, clinic_time: str) -> dict:
    """Unpack an uploaded site database, check it, and swap it in atomically."""
    folder = Path(settings.mirror_dir)
    folder.mkdir(parents=True, exist_ok=True)
    tmp = folder / f"{site}.db.incoming"
    try:
        data = gzip.decompress(raw)
    except OSError as e:
        raise HTTPException(400, "Upload is not gzip data") from e
    if not data.startswith(b"SQLite format 3\x00"):
        raise HTTPException(400, "Upload is not a SQLite database")
    tmp.write_bytes(data)
    con = sqlite3.connect(tmp)
    try:
        if con.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise HTTPException(400, "Uploaded database failed its integrity check")
        if not con.execute("SELECT 1 FROM sqlite_master WHERE name = 'users'").fetchone():
            raise HTTPException(400, "Uploaded database has no users table")
        _upgrade_schema(con)
        con.execute("PRAGMA journal_mode=DELETE")
    finally:
        con.close()
    os.replace(tmp, clinic_view.db_path(site))
    now = clock.now().isoformat(timespec="seconds")
    meta = {"synced_at": now, "checked_at": now, "clinic_time": clinic_time, "bytes": len(data)}
    _write_meta(site, meta)
    return meta


def _upgrade_schema(con: sqlite3.Connection) -> None:
    """A site still on an older version uploads a database without the newest
    tables / columns; add them (empty) so the Owner Panel can read the copy."""
    from sqlalchemy.dialects import sqlite as sqlite_dialect
    from sqlalchemy.schema import CreateTable

    from app.db.session import Base

    dialect = sqlite_dialect.dialect()
    for table in Base.metadata.sorted_tables:
        cols = {r[1] for r in con.execute(f'PRAGMA table_info("{table.name}")')}
        if not cols:
            con.execute(str(CreateTable(table).compile(dialect=dialect)))
            continue
        for col in table.columns:
            if col.name not in cols:
                con.execute(f'ALTER TABLE "{table.name}" ADD COLUMN "{col.name}" {col.type.compile(dialect=dialect)}')
    con.commit()


def _meta_file(site: str) -> Path:
    return Path(settings.mirror_dir) / f"{site}.json"


def _read_meta(site: str = "clinic") -> dict:
    meta_file = _meta_file(site)
    try:
        return json.loads(meta_file.read_text()) if meta_file.exists() else {}
    except ValueError:
        return {}


def _write_meta(site: str, meta: dict) -> None:
    folder = Path(settings.mirror_dir)
    folder.mkdir(parents=True, exist_ok=True)
    tmp = folder / f"{site}.json.tmp"
    tmp.write_text(json.dumps(meta))
    os.replace(tmp, _meta_file(site))


@router.post("/mirror-upload")
async def mirror_upload(
    request: Request,
    x_clinic_time: str = Header(default=""),
    site: str = Depends(_site),
    _: None = Depends(_verify_sync_key),
):
    """A site server (site_sync.py) posts a gzip copy of its whole database here
    every few seconds while it has internet and its data changed."""
    raw = await request.body()
    if not raw or len(raw) > MIRROR_MAX_BYTES:
        raise HTTPException(413, "Upload is empty or too large")
    meta = await run_in_threadpool(_store_mirror, site, raw, x_clinic_time)
    return {"ok": True, **meta}


@router.post("/clinic-session")
async def clinic_session(user: User = Depends(require("__admin__"))):
    """Hospital owner -> a read-only session on the clinic copy (Owner Panel's
    "Clinic" button). Owners only; the mirror is asked over localhost."""
    if user.role not in ("super_admin", "hospital_admin") or settings.mirror_mode:
        raise HTTPException(403, "Owner only")
    if not settings.mirror_internal_key:
        raise HTTPException(503, "Clinic view is not set up on this server")
    import httpx

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(f"{settings.mirror_url}/api/v1/auth/mirror-sso", headers={"X-Mirror-Key": settings.mirror_internal_key})
    except httpx.HTTPError as e:
        raise HTTPException(503, "Clinic view is not reachable") from e
    if r.status_code != 200:
        raise HTTPException(r.status_code if r.status_code < 500 else 502, r.json().get("detail", "Clinic view error"))
    return r.json()


@router.post("/mirror-heartbeat")
def mirror_heartbeat(x_clinic_time: str = Header(default=""), site: str = Depends(_site), _: None = Depends(_verify_sync_key)):
    """A site is online but nothing changed since its last upload."""
    meta = _read_meta(site)
    meta["checked_at"] = clock.now().isoformat(timespec="seconds")
    meta["clinic_time"] = x_clinic_time
    _write_meta(site, meta)
    return {"ok": True}


@router.get("/mirror-info")
def mirror_info():
    """How fresh each site's copy is: synced_at = data last uploaded,
    checked_at = site last online. The top-level fields are the clinic's
    (what older web app versions read)."""
    clinic = _read_meta("clinic")
    return {
        "mirror": settings.mirror_mode,
        "synced_at": clinic.get("synced_at"), "checked_at": clinic.get("checked_at"), "clinic_time": clinic.get("clinic_time"),
        "sites": {s: {**_read_meta(s), "pending_command": {k: v for k, v in (_pending_command(s) or {}).items() if k != "by"} or None} for s in clinic_view.SITES},
    }


@router.get("/site-info")
def site_info():
    """Public: what kind of server this is, for the web app — a site server (its
    items come from the cloud, QR links point to the cloud), the cloud hub, or
    a plain single server."""
    return {
        "site": settings.site,
        "hub": settings.hub_mode,
        "catalog_managed": bool(settings.site),
        "qr_base": settings.qr_base_url.rstrip("/"),
    }


# ---- Item master: the cloud's item list, pulled by every site server ----------

CATALOG_FIELDS = (
    "sku", "barcode", "name", "name_mm", "category", "department", "sub_category", "unit",
    "expiry_alert_months", "price", "doctor_fee_pct", "is_stock", "is_controlled", "is_package",
    "min_stock", "is_active", "bill_group",
)


def _catalog_payload(db: Session) -> dict:
    from app.models.catalog import CatalogItem

    items = db.query(CatalogItem).order_by(CatalogItem.id).all()
    sku_of = {i.id: i.sku for i in items}
    rows = []
    for i in items:
        row = {f: getattr(i, f) for f in CATALOG_FIELDS}
        row["deleted_at"] = i.deleted_at.isoformat() if i.deleted_at else None
        try:
            ids = json.loads(i.package_items) if i.package_items else []
        except ValueError:
            ids = []
        row["package_skus"] = [sku_of[x] for x in ids if x in sku_of]
        rows.append(row)
    body = json.dumps(rows, sort_keys=True, ensure_ascii=False)
    # "|gone-1": sites now also hide items the cloud deleted for good — the new
    # version makes every site pull the list once more even if nothing changed.
    return {"version": hashlib.sha1((body + "|gone-1").encode()).hexdigest(), "items": rows}


@router.get("/catalog")
def catalog(response: Response, if_none_match: str = Header(default=""), db: Session = Depends(get_db), _: None = Depends(_verify_sync_key)):
    """Every item with its price (the item master). Sites send the version they
    already have in If-None-Match and get 304 when nothing changed."""
    data = _catalog_payload(db)
    if if_none_match.strip('"') == data["version"]:
        return Response(status_code=304)
    response.headers["ETag"] = f'"{data["version"]}"'
    return data


# ---- Patients' QR page for tokens printed by a site server -----------------------
# The site posts today's public queue view (numbers, statuses, doctor names — no
# patient details) whenever it changes, and pings while nothing changes, so the
# cloud's /q/<code> page works from any phone. Kept in memory and on disk.

QUEUE_STALE_SECONDS = 60
_queues: dict[str, dict] = {}
_queue_lock = threading.Lock()


def _queue_file(site: str) -> Path:
    return Path(settings.mirror_dir) / f"{site}-queue.json"


def _load_queue(site: str) -> dict:
    with _queue_lock:
        if site not in _queues:
            try:
                _queues[site] = json.loads(_queue_file(site).read_text())
            except (OSError, ValueError):
                _queues[site] = {"tickets": {}, "expired": {}, "seen_at": 0, "date": ""}
        return _queues[site]


class QueueSnapshotIn(BaseModel):
    date: str
    tickets: dict[str, dict]


@router.post("/queue")
def queue_snapshot(data: QueueSnapshotIn, site: str = Depends(_site), _: None = Depends(_verify_sync_key)):
    q = _load_queue(site)
    with _queue_lock:
        if q.get("date") and q["date"] != data.date:
            # A new day: yesterday's codes still answer "expired" (capped).
            expired = {**q.get("expired", {}), **{c: q["date"] for c in q.get("tickets", {})}}
            q["expired"] = dict(list(expired.items())[-5000:])
        q["date"] = data.date
        q["tickets"] = data.tickets
        q["seen_at"] = time.time()
        folder = Path(settings.mirror_dir)
        folder.mkdir(parents=True, exist_ok=True)
        tmp = folder / f"{site}-queue.json.tmp"
        tmp.write_text(json.dumps(q, ensure_ascii=False))
        os.replace(tmp, _queue_file(site))
    broker.publish("queue", clinic_view.SITES[site]["id"])
    return {"ok": True, "tickets": len(data.tickets)}


@router.post("/queue-ping")
def queue_ping(site: str = Depends(_site), _: None = Depends(_verify_sync_key)):
    q = _load_queue(site)
    with _queue_lock:
        q["seen_at"] = time.time()
    return {"ok": True}


def site_ticket(code: str) -> dict | None:
    """The public QR view of a token printed by one of the site servers."""
    for site, cfg in clinic_view.SITES.items():
        q = _load_queue(site)
        t = q.get("tickets", {}).get(code)
        if t is None and code in q.get("expired", {}):
            t = {"number": "", "doctor_name": "", "queue_date": q["expired"][code], "status": "expired",
                 "now_serving": None, "ahead": 0, "eta_minutes": 0}
        if t is None:
            continue
        seen = float(q.get("seen_at") or 0)
        return {
            **t,
            "branch_id": cfg["id"],
            "as_of": datetime.fromtimestamp(seen).isoformat(timespec="seconds") if seen else None,
            "stale": time.time() - seen > QUEUE_STALE_SECONDS and t.get("status") != "expired",
        }
    return None


@router.post("/push")
def push_snapshot(data: SyncPushIn, db: Session = Depends(get_db), _: None = Depends(_verify_sync_key)):
    payload = json.dumps({
        "analytics": data.analytics,
        "stock_snapshot": data.stock_snapshot,
        "capital_assets": data.capital_assets,
    })
    snap = SyncSnapshot(
        branch_code=data.branch_code,
        period_label=data.period_label,
        date_from=datetime.fromisoformat(data.date_from) if data.date_from else None,
        date_to=datetime.fromisoformat(data.date_to) if data.date_to else None,
        payload=payload,
    )
    db.add(snap)
    db.commit()
    db.refresh(snap)
    return {"ok": True, "id": snap.id}


@router.get("/latest")
def latest_snapshots(db: Session = Depends(get_db), _: User = Depends(require("__admin__"))):
    """Latest snapshot per branch_code — the cloud Admin Panel's Owner Panel
    reads this (logged-in browser session, hence the normal admin-only
    permission check here, unlike /push's shared-key auth) instead of
    querying live Invoice/Payment tables, which don't exist on a cloud
    instance that only ever receives summarized rollups."""
    rows = db.query(SyncSnapshot).order_by(SyncSnapshot.received_at.desc()).all()
    latest: dict[str, SyncSnapshot] = {}
    for r in rows:
        latest.setdefault(r.branch_code, r)
    return [
        {
            "branch_code": r.branch_code,
            "period_label": r.period_label,
            "date_from": r.date_from.isoformat() if r.date_from else None,
            "date_to": r.date_to.isoformat() if r.date_to else None,
            "received_at": r.received_at.isoformat(),
            **json.loads(r.payload),
        }
        for r in latest.values()
    ]


# ---- Go-live: clear test history (Owner Panel) ---------------------------------

class ResetHistoryIn(BaseModel):
    branch_id: int
    confirm: str
    password: str
    until: str | None = None  # YYYY-MM-DD, inclusive; None = everything


class CommandDoneIn(BaseModel):
    id: str
    result: dict = {}


def _command_file(site: str) -> Path:
    return Path(settings.mirror_dir) / f"{site}-command.json"


def _pending_command(site: str) -> dict | None:
    f = _command_file(site)
    try:
        return json.loads(f.read_text()) if f.exists() else None
    except ValueError:
        return None


@router.post("/reset-history")
def reset_history_endpoint(data: ResetHistoryIn, db: Session = Depends(get_db), user: User = Depends(require("__admin__"))):
    """Owner -> clear one branch's test history before going live. The cloud's
    own branch is cleared now; a site server is asked to clear itself the next
    time it is online (it backs itself up first)."""
    from app.core.security import verify_password
    from app.services import history_reset

    if user.role not in ("super_admin", "hospital_admin") or settings.mirror_mode:
        raise HTTPException(403, "Owner only")
    if data.confirm.strip().upper() != "DELETE":
        raise HTTPException(400, "DELETE လို့ ရိုက်ထည့်ပါ")
    if not verify_password(data.password, user.hashed_password):
        raise HTTPException(400, "Password မှားနေပါတယ်")
    try:
        until = datetime.fromisoformat(data.until).date() if data.until else None
    except ValueError as e:
        raise HTTPException(400, "Date မှားနေပါတယ်") from e
    site = clinic_view.site_for(data.branch_id)
    if site:
        cmd = {
            "until": until.isoformat() if until else None,
            "id": hashlib.sha1(f"{site}{time.time()}".encode()).hexdigest()[:12],
            "action": "reset_history",
            "requested_at": clock.now().isoformat(timespec="seconds"),
            "by": user.username,
        }
        folder = Path(settings.mirror_dir)
        folder.mkdir(parents=True, exist_ok=True)
        _command_file(site).write_text(json.dumps(cmd))
        return {"queued": True, "site": site, **cmd}
    path = history_reset.sqlite_path(settings.database_url)
    saved = history_reset.backup(path, Path(settings.mirror_dir).parent / "history-reset-backups", f"branch{data.branch_id}")
    res = history_reset.reset_history(db, until)
    for topic in ("invoice", "expense", "stock"):
        broker.publish(topic, data.branch_id)
    return {"queued": False, "backup": saved.name, **res}


@router.get("/commands")
def site_commands(site: str = Depends(_site), _: None = Depends(_verify_sync_key)):
    """A site server asks whether the cloud wants it to do something."""
    cmd = _pending_command(site)
    return {"commands": [cmd] if cmd else []}


@router.post("/commands/done")
def site_command_done(data: CommandDoneIn, site: str = Depends(_site), _: None = Depends(_verify_sync_key)):
    cmd = _pending_command(site)
    if cmd and cmd.get("id") == data.id:
        _command_file(site).unlink(missing_ok=True)
        meta = _read_meta(site)
        meta["last_command"] = {**cmd, "done_at": clock.now().isoformat(timespec="seconds"), "result": data.result}
        _write_meta(site, meta)
    return {"ok": True}

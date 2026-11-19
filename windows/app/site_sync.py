"""Site server <-> cloud, from background threads inside the site server (server.py),
so it runs whenever the server runs. The site works fully offline; whenever this
computer has internet:

  data     every 15 s   a copy of the database goes to the cloud (Owner Panel)
                        — or a tiny "still connected" heartbeat if nothing changed
  items    every 15 s   the cloud's item master (names, prices, new / deleted
                        items) is brought into this site's item list
  commands every 15 s   anything the cloud asks this site to do (the Owner
                        Panel's "clear test history" before going live)
  queue    every 5 s    today's token queue (numbers, statuses, doctor names — no
                        patient details) goes to the cloud, so the QR link on a
                        token slip works from any phone

Settings in C:\\ShweMuse\\config.env: SITE (hospital / clinic), CLOUD_URL, SYNC_KEY.
"""
import gzip
import hashlib
import json
import os
import sqlite3
import tempfile
import threading
import time
from datetime import date, datetime
from pathlib import Path

HOME = Path(__file__).resolve().parent.parent
DB = HOME / "data" / "shwemuse.db"
STATE = HOME / "data" / "sync_state.json"
LOG = HOME / "logs" / "sync.log"

DATA_SECONDS = 15
ITEMS_SECONDS = 15
QUEUE_SECONDS = 5
QUEUE_PING_SECONDS = 15
RESEND_SECONDS = 6 * 3600
SYNCED_LOG_SECONDS = 600  # an upload every 15 s would flood the log
LOCAL_BRANCH = 1

_log_lock = threading.Lock()


def log(msg: str) -> None:
    with _log_lock:
        LOG.parent.mkdir(exist_ok=True)
        if LOG.exists() and LOG.stat().st_size > 2_000_000:
            LOG.replace(LOG.with_name("sync.old.log"))
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S}  {msg}\n")


def _read_json(path: Path) -> dict:
    """A state file; empty or broken (e.g. power cut mid-write) counts as no state."""
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_json(path: Path, data: dict) -> None:
    """Write via a temp file + rename so a crash never leaves a half-written file."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    os.replace(tmp, path)


def config() -> dict[str, str]:
    cfg = {}
    path = HOME / "config.env"
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, value = line.split("=", 1)
                cfg[key.strip()] = value.strip()
    return cfg


def _cloud() -> tuple[str, dict] | None:
    cfg = config()
    url = cfg.get("CLOUD_URL", "").rstrip("/")
    key = cfg.get("SYNC_KEY", "")
    if not url or not key:
        return None
    return url, {"X-Sync-Key": key, "X-Site": cfg.get("SITE", "clinic") or "clinic"}


# ---------------------------------------------------------------- data (mirror)

def signature() -> str:
    """Changes whenever the database (or its WAL) is written to."""
    parts = []
    for f in (DB, DB.with_name(DB.name + "-wal")):
        if f.exists():
            st = f.stat()
            parts.append(f"{st.st_mtime_ns}:{st.st_size}")
    return "|".join(parts)


def run_once() -> str:
    """One data upload attempt. Returns what happened."""
    import httpx

    cloud = _cloud()
    if not cloud:
        return "not configured"
    url, headers = cloud
    if not DB.exists():
        return "no database yet"
    state = _read_json(STATE)
    sig = signature()
    headers = {**headers, "X-Clinic-Time": datetime.now().isoformat(timespec="seconds")}
    try:
        if state.get("signature") == sig and time.time() - state.get("sent_at", 0) < RESEND_SECONDS:
            r = httpx.post(f"{url}/api/v1/sync/mirror-heartbeat", headers=headers, timeout=30)
            return "heartbeat ok" if r.status_code == 200 else f"heartbeat refused {r.status_code}"

        with tempfile.TemporaryDirectory() as tmp:
            copy = Path(tmp) / "site.db"
            # VACUUM INTO gives up after the busy timeout instead of retrying
            # forever like Connection.backup() can on a locked database.
            src = sqlite3.connect(DB, timeout=30)
            try:
                src.execute("VACUUM INTO ?", (str(copy),))
            finally:
                src.close()
            body = gzip.compress(copy.read_bytes(), compresslevel=6)
        r = httpx.post(
            f"{url}/api/v1/sync/mirror-upload",
            content=body,
            headers={**headers, "Content-Type": "application/gzip"},
            timeout=180,
        )
    except httpx.HTTPError as e:
        return f"offline ({e.__class__.__name__})"
    if r.status_code != 200:
        return f"cloud refused upload: {r.status_code} {r.text[:200]}"
    _write_json(STATE, {"signature": sig, "sent_at": time.time()})
    return f"synced ({len(body):,} bytes)"


# ---------------------------------------------------------------- items (item master)

def pull_items() -> str:
    """Bring the cloud's item master into this site's item list."""
    import httpx

    from app.db.session import SessionLocal
    from app.services.catalog_sync import apply_catalog, current_version

    cloud = _cloud()
    if not cloud:
        return "not configured"
    url, headers = cloud
    db = SessionLocal()
    try:
        have = current_version(db)
        try:
            r = httpx.get(f"{url}/api/v1/sync/catalog", headers={**headers, "If-None-Match": f'"{have}"'}, timeout=60)
        except httpx.HTTPError as e:
            return f"offline ({e.__class__.__name__})"
        if r.status_code == 304:
            return "items up to date"
        if r.status_code != 200:
            return f"cloud refused items: {r.status_code} {r.text[:200]}"
        data = r.json()
        res = apply_catalog(db, data["items"], data["version"])
        return f"items synced (+{res['added']} new, {res['updated']} changed, {res.get('removed', 0)} removed, {res['total']} in cloud)"
    finally:
        db.close()


# ---------------------------------------------------------------- commands from the cloud

DONE = HOME / "data" / "commands_done.json"


def run_commands() -> str:
    """Do what the Owner Panel asked this site to do (once per command id)."""
    import httpx

    from app.core.config import settings
    from app.db.session import SessionLocal
    from app.services import history_reset

    cloud = _cloud()
    if not cloud:
        return "not configured"
    url, headers = cloud
    try:
        r = httpx.get(f"{url}/api/v1/sync/commands", headers=headers, timeout=30)
    except httpx.HTTPError as e:
        return f"offline ({e.__class__.__name__})"
    if r.status_code == 404:
        return "cloud has no commands yet"
    if r.status_code != 200:
        return f"cloud refused commands: {r.status_code}"
    done = _read_json(DONE)
    out = "no commands"
    for cmd in r.json().get("commands", []):
        cid = cmd.get("id", "")
        if cid in done:
            result = done[cid]
        elif cmd.get("action") == "reset_history":
            saved = history_reset.backup(history_reset.sqlite_path(settings.database_url), HOME / "backups", "cloud-request")
            db = SessionLocal()
            try:
                until = date.fromisoformat(cmd["until"]) if cmd.get("until") else None
                result = {**history_reset.reset_history(db, until), "backup": saved.name}
            finally:
                db.close()
            done[cid] = result
            _write_json(DONE, done)
            out = f"test history cleared (backup {saved.name})"
        else:
            result = {"skipped": f"unknown action {cmd.get('action')}"}
        try:
            httpx.post(f"{url}/api/v1/sync/commands/done", json={"id": cid, "result": result}, headers=headers, timeout=30)
        except httpx.HTTPError:
            pass
    return out


# ---------------------------------------------------------------- queue (QR page)

_queue_state = {"hash": "", "sent_at": 0.0}


def push_queue() -> str:
    """Send today's token queue to the cloud when it changed; ping otherwise."""
    import httpx

    from app.core import clock
    from app.db.session import SessionLocal
    from app.services.queue_service import public_snapshot

    cloud = _cloud()
    if not cloud:
        return "not configured"
    url, headers = cloud
    db = SessionLocal()
    try:
        tickets = public_snapshot(db, LOCAL_BRANCH)
        today = clock.today().isoformat()
    finally:
        db.close()
    body = {"date": today, "tickets": tickets}
    digest = hashlib.sha1(json.dumps(body, sort_keys=True).encode()).hexdigest()
    try:
        if digest == _queue_state["hash"]:
            if time.time() - _queue_state["sent_at"] < QUEUE_PING_SECONDS:
                return "queue up to date"
            r = httpx.post(f"{url}/api/v1/sync/queue-ping", headers=headers, timeout=15)
            if r.status_code == 200:
                _queue_state["sent_at"] = time.time()
                return "queue up to date"
            return f"cloud refused queue ping: {r.status_code}"
        r = httpx.post(f"{url}/api/v1/sync/queue", json=body, headers=headers, timeout=30)
    except httpx.HTTPError as e:
        return f"offline ({e.__class__.__name__})"
    if r.status_code != 200:
        return f"cloud refused queue: {r.status_code} {r.text[:200]}"
    _queue_state.update(hash=digest, sent_at=time.time())
    return f"queue sent ({len(tickets)} tokens)"


# ---------------------------------------------------------------- loops

def _loop(name: str, fn, seconds: int, noisy_prefixes: tuple[str, ...]) -> None:
    """Run fn forever. Logs only when the outcome changes, plus a "noisy" success
    (an upload, a queue send) at most every 10 minutes."""
    last = None
    logged_noisy = 0.0
    while True:
        try:
            result = fn()
        except Exception as e:  # noqa: BLE001 — never let a sync thread die
            result = f"error: {e!r}"
        noisy = result.startswith(noisy_prefixes)
        changed = (last or "").startswith(noisy_prefixes) != noisy if noisy else result != last
        if changed or (noisy and time.time() - logged_noisy >= SYNCED_LOG_SECONDS):
            log(f"[{name}] {result}")
            if noisy:
                logged_noisy = time.time()
        last = result
        time.sleep(seconds)


def loop() -> None:
    """Data upload loop (kept as the module's main loop for older callers)."""
    log(f"[data] started (db {DB}, exists={DB.exists()})")
    _loop("data", run_once, DATA_SECONDS, ("synced",))


def start() -> None:
    """Start every sync thread (called once by server.py)."""
    for name, target in (
        ("cloud-data", loop),
        ("cloud-items", lambda: _loop("items", pull_items, ITEMS_SECONDS, ())),
        ("cloud-commands", lambda: _loop("commands", run_commands, ITEMS_SECONDS, ())),
        ("cloud-queue", lambda: _loop("queue", push_queue, QUEUE_SECONDS, ("queue sent",))),
    ):
        threading.Thread(target=target, name=name, daemon=True).start()


if __name__ == "__main__":
    log(run_once())

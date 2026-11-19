"""The offline site servers (hospital and clinic) as branches of the cloud Owner Panel.

Each site runs on its own Windows server and uploads a copy of its database
every few seconds (POST /sync/mirror-upload, header X-Site -> <mirror_dir>/<site>.db).
This module lets the cloud's own endpoints read those copies, so the Owner Panel
shows each site next to the cloud's own branch and "All Branches" adds them up —
without merging the databases (their ids overlap).

In the cloud a site appears as branch SITES[site]["id"] (the site's own
database calls it branch 1). Ids coming out of a site copy that could clash
with other ids in a combined list (doctors, stock items, assets) are shifted by
that site's offset.
"""
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from app.core.config import settings

SITES = {
    "clinic": {"id": 900, "offset": 1_000_000},
    "hospital": {"id": 901, "offset": 2_000_000},
}
CLINIC_ID = SITES["clinic"]["id"]
CLINIC_LOCAL_ID = LOCAL_ID = 1
ID_OFFSET = SITES["clinic"]["offset"]

_engines: dict[str, sessionmaker] = {}


def db_path(site: str = "clinic") -> Path:
    return Path(settings.mirror_dir) / f"{site}.db"


def available(site: str = "clinic") -> bool:
    return not settings.mirror_mode and site in SITES and db_path(site).exists()


def available_sites() -> list[str]:
    return [s for s in SITES if available(s)]


def site_for(branch_id: int | None) -> str | None:
    """The site whose uploaded copy a cloud branch id stands for (None: a cloud branch)."""
    return next((s for s, v in SITES.items() if v["id"] == branch_id), None)


def offset(site: str) -> int:
    return SITES[site]["offset"]


@contextmanager
def clinic_session(site: str = "clinic"):
    """Read-only session on the latest upload of a site (a fresh connection each
    time, since the file is replaced whenever the site syncs)."""
    path = db_path(site)
    key = str(path)
    if key not in _engines:
        engine = create_engine(
            f"sqlite:///file:{path.as_posix()}?mode=ro&uri=true",
            connect_args={"check_same_thread": False, "timeout": 30},
            poolclass=NullPool,
            future=True,
        )
        _engines[key] = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    db = _engines[key]()
    try:
        yield db
    finally:
        db.close()


site_session = clinic_session


def virtual_branch(site: str = "clinic") -> dict | None:
    """A site as a /branches row (its name etc. from the site's own data)."""
    if not available(site):
        return None
    from app.models.org import Branch

    try:
        with clinic_session(site) as db:
            b = db.get(Branch, LOCAL_ID)
            if not b:
                return None
            return {
                "id": SITES[site]["id"], "code": b.code, "name": b.name, "name_mm": b.name_mm,
                "address": b.address, "phone": b.phone, "is_main": False, "is_active": True,
                "offline_clinic": True, "site": site,
            }
    except Exception:  # noqa: BLE001 — a half-written or missing upload must not break /branches
        return None


def virtual_branches() -> list[dict]:
    return [b for b in (virtual_branch(s) for s in SITES) if b]


def shift_doctor_ids(analytics: dict, site: str = "clinic") -> dict:
    for row in [*analytics.get("by_doctor", []), *(analytics.get("by_doctor_round") or []), *(analytics.get("by_doctor_surgery") or [])]:
        if row.get("doctor_id") is not None:
            row["doctor_id"] = row["doctor_id"] + offset(site)
    return analytics

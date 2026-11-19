"""OPD waiting queue: one queue per doctor per day. Token numbers (001, 002, ...)
run across the whole branch for the day, so a number is never shared by two
doctors' patients and the waiting-room TV can show it on its own.

Reception issues a token on OPD registration; the nurse outside the doctor's
room moves it along (Call Next / Recall / Skip) since doctors don't use a
computer; the patient follows their own token from the QR link on the printed
slip (public, no login), and a waiting-room TV shows who is being called.
"""
import secrets

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core import clock
from app.models.clinical import QueueToken
from app.models.users import User

DEFAULT_MINUTES_PER_PATIENT = 10
GENERAL_LABEL = "General OPD"


def _same_doctor(doctor_id: int | None):
    return QueueToken.doctor_id.is_(None) if doctor_id is None else QueueToken.doctor_id == doctor_id


def _today_q(db: Session, branch_id: int, doctor_id: int | None):
    return db.query(QueueToken).filter(
        QueueToken.branch_id == branch_id,
        QueueToken.queue_date == clock.today(),
        _same_doctor(doctor_id),
    )


def issue_token(db: Session, *, branch_id: int, patient_id: int, doctor_id: int | None) -> QueueToken:
    # Registering the same patient for the same doctor twice in a day (e.g. a
    # re-print or a mistaken double click) keeps their place instead of
    # sending them to the back of the line.
    existing = (
        _today_q(db, branch_id, doctor_id)
        .filter(QueueToken.patient_id == patient_id, QueueToken.status.in_(["waiting", "serving"]))
        .first()
    )
    if existing:
        return existing
    # Numbered across all doctors (not per doctor) so no two patients share a
    # number today; each doctor's call order is still their own (by seq).
    last = (
        db.query(func.max(QueueToken.seq))
        .filter(QueueToken.branch_id == branch_id, QueueToken.queue_date == clock.today())
        .scalar()
        or 0
    )
    seq = last + 1
    token = QueueToken(
        branch_id=branch_id,
        department="OPD",
        patient_id=patient_id,
        doctor_id=doctor_id,
        queue_date=clock.today(),
        seq=seq,
        number=f"{seq:03d}",
        public_code=secrets.token_urlsafe(12),
    )
    db.add(token)
    db.flush()
    return token


def _minutes_per_patient(tokens: list[QueueToken]) -> float:
    """Average gap between consecutive calls today (last 5), so the estimate
    tracks how fast this doctor is actually going; a fixed default until
    there are at least two calls."""
    called = sorted(t.called_at for t in tokens if t.called_at)
    gaps = [(b - a).total_seconds() / 60 for a, b in zip(called, called[1:])]
    gaps = [g for g in gaps[-5:] if 0.5 <= g <= 90]  # ignore recalls and lunch breaks
    return sum(gaps) / len(gaps) if gaps else DEFAULT_MINUTES_PER_PATIENT


def _doctor_name(db: Session, doctor_id: int | None) -> str:
    if doctor_id is None:
        return GENERAL_LABEL
    d = db.get(User, doctor_id)
    return d.full_name if d else f"Doctor #{doctor_id}"


def _queue_summary(tokens: list[QueueToken]) -> dict:
    serving = max((t for t in tokens if t.status == "serving"), key=lambda t: t.called_at or t.created_at, default=None)
    waiting = sorted((t for t in tokens if t.status == "waiting"), key=lambda t: t.seq)
    return {"serving": serving, "waiting": waiting, "per_patient": _minutes_per_patient(tokens)}


def public_ticket(db: Session, code: str) -> dict | None:
    if not code or len(code) < 8:
        return None
    t = db.query(QueueToken).filter(QueueToken.public_code == code).first()
    if not t:
        return None
    doctor_name = _doctor_name(db, t.doctor_id)
    base = {
        "branch_id": t.branch_id,  # lets the page open the live-update socket
        "number": t.number,
        "doctor_name": doctor_name,
        "queue_date": t.queue_date.isoformat() if t.queue_date else None,
        "status": t.status,
        "now_serving": None,
        "ahead": 0,
        "eta_minutes": 0,
    }
    if t.queue_date != clock.today():
        base["status"] = "expired"
        return base
    tokens = _today_q(db, t.branch_id, t.doctor_id).all()
    s = _queue_summary(tokens)
    base["now_serving"] = s["serving"].number if s["serving"] else None
    if t.status == "waiting":
        ahead = sum(1 for w in s["waiting"] if w.seq < t.seq)
        base["ahead"] = ahead
        base["eta_minutes"] = round(ahead * s["per_patient"] + (s["per_patient"] / 2 if s["serving"] else 0))
    return base


def public_snapshot(db: Session, branch_id: int) -> dict[str, dict]:
    """Today's public_ticket() view of every token, keyed by QR code — what a site
    server sends to the cloud so the patients' QR links work from any phone."""
    tokens = db.query(QueueToken).filter(QueueToken.branch_id == branch_id, QueueToken.queue_date == clock.today()).all()
    by_doctor: dict[int | None, list[QueueToken]] = {}
    for t in tokens:
        by_doctor.setdefault(t.doctor_id, []).append(t)
    out: dict[str, dict] = {}
    for doctor_id, group in by_doctor.items():
        name = _doctor_name(db, doctor_id)
        s = _queue_summary(group)
        serving = s["serving"].number if s["serving"] else None
        for t in group:
            if not t.public_code:
                continue
            ahead = sum(1 for w in s["waiting"] if w.seq < t.seq) if t.status == "waiting" else 0
            out[t.public_code] = {
                "number": t.number,
                "doctor_name": name,
                "queue_date": t.queue_date.isoformat(),
                "status": t.status,
                "now_serving": serving,
                "ahead": ahead,
                "eta_minutes": round(ahead * s["per_patient"] + (s["per_patient"] / 2 if s["serving"] else 0)) if t.status == "waiting" else 0,
            }
    return out


def display_board(db: Session, branch_id: int) -> dict:
    """Waiting-room TV: the numbers currently with a doctor, latest call first.
    No patient names — this is shown on a public screen."""
    tokens = (
        db.query(QueueToken)
        .filter(
            QueueToken.branch_id == branch_id,
            QueueToken.queue_date == clock.today(),
            QueueToken.status.in_(["waiting", "serving"]),
        )
        .all()
    )
    serving = sorted((t for t in tokens if t.status == "serving"), key=lambda t: t.called_at or t.created_at, reverse=True)
    return {
        "serving": [
            {
                "number": t.number,
                "doctor_name": _doctor_name(db, t.doctor_id),
                "called_at": t.called_at.isoformat() if t.called_at else None,
            }
            for t in serving
        ],
        "waiting_count": sum(1 for t in tokens if t.status == "waiting"),
    }


def _token_row(t: QueueToken) -> dict:
    return {
        "id": t.id,
        "number": t.number,
        "seq": t.seq,
        "status": t.status,
        "patient_id": t.patient_id,
        "patient_name": t.patient.name if t.patient else "",
        "patient_uhid": t.patient.uhid if t.patient else "",
        "created_at": t.created_at.isoformat() if t.created_at else None,
        "called_at": t.called_at.isoformat() if t.called_at else None,
        "public_code": t.public_code,
    }


def staff_board(db: Session, branch_id: int) -> list[dict]:
    """Nurse's Queue tab: every doctor's queue today, with patient names."""
    # Tokens left "waiting" from earlier days (nobody pressed Done) are closed
    # so they don't pile up — the QR page already shows them as expired.
    db.query(QueueToken).filter(
        QueueToken.branch_id == branch_id, QueueToken.queue_date < clock.today(), QueueToken.status.in_(["waiting", "serving"])
    ).update({QueueToken.status: "expired"}, synchronize_session=False)
    tokens = (
        db.query(QueueToken)
        .filter(QueueToken.branch_id == branch_id, QueueToken.queue_date == clock.today())
        .order_by(QueueToken.seq)
        .all()
    )
    by_doctor: dict[int | None, list[QueueToken]] = {}
    for t in tokens:
        by_doctor.setdefault(t.doctor_id, []).append(t)
    out = []
    for doctor_id, ts in by_doctor.items():
        s = _queue_summary(ts)
        out.append({
            "doctor_id": doctor_id,
            "doctor_name": _doctor_name(db, doctor_id),
            "serving": _token_row(s["serving"]) if s["serving"] else None,
            "waiting": [_token_row(t) for t in s["waiting"]],
            "skipped": [_token_row(t) for t in ts if t.status == "skipped"],
            "done_count": sum(1 for t in ts if t.status == "done"),
            "minutes_per_patient": round(s["per_patient"], 1),
        })
    out.sort(key=lambda r: (r["doctor_id"] is None, r["doctor_name"]))
    return out


def call_next(db: Session, *, branch_id: int, doctor_id: int | None) -> QueueToken | None:
    """Finish whoever is with the doctor now and call the next waiting number."""
    now = clock.now()
    tokens = _today_q(db, branch_id, doctor_id).filter(QueueToken.status.in_(["waiting", "serving"])).all()
    for t in tokens:
        if t.status == "serving":
            t.status = "done"
            t.done_at = now
    waiting = sorted((t for t in tokens if t.status == "waiting"), key=lambda t: t.seq)
    nxt = waiting[0] if waiting else None
    if nxt:
        nxt.status = "serving"
        nxt.called_at = now
    db.flush()
    return nxt


def call_token(db: Session, *, token_id: int, branch_id: int) -> QueueToken:
    """Call a specific waiting number (the nurse's single, number-ordered list):
    whoever is with that number's doctor now is finished first."""
    t = _get_today(db, token_id, branch_id)
    if t.status != "waiting":
        raise ValueError("Only a waiting number can be called")
    now = clock.now()
    for cur in _today_q(db, branch_id, t.doctor_id).filter(QueueToken.status == "serving").all():
        cur.status = "done"
        cur.done_at = now
    t.status = "serving"
    t.called_at = now
    db.flush()
    return t


def _get_today(db: Session, token_id: int, branch_id: int) -> QueueToken:
    t = db.get(QueueToken, token_id)
    if not t or t.branch_id != branch_id or t.queue_date != clock.today():
        raise ValueError("Queue token not found")
    return t


def recall(db: Session, *, token_id: int, branch_id: int) -> QueueToken:
    t = _get_today(db, token_id, branch_id)
    if t.status != "serving":
        raise ValueError("Only the number being served can be recalled")
    t.called_at = clock.now()  # TV chimes again on a new called_at
    db.flush()
    return t


def skip(db: Session, *, token_id: int, branch_id: int) -> QueueToken:
    t = _get_today(db, token_id, branch_id)
    if t.status not in ("waiting", "serving"):
        raise ValueError("This number is already finished")
    t.status = "skipped"
    db.flush()
    return t


def requeue(db: Session, *, token_id: int, branch_id: int) -> QueueToken:
    """A skipped patient came back — put them back in line at their original
    number, i.e. ahead of everyone who registered after them."""
    t = _get_today(db, token_id, branch_id)
    if t.status != "skipped":
        raise ValueError("Only a skipped number can be put back in line")
    t.status = "waiting"
    db.flush()
    return t


def finish(db: Session, *, token_id: int, branch_id: int) -> QueueToken:
    t = _get_today(db, token_id, branch_id)
    if t.status != "serving":
        raise ValueError("Only the number being served can be finished")
    t.status = "done"
    t.done_at = clock.now()
    db.flush()
    return t



def queue_ahead(db: Session, t: QueueToken) -> int:
    if t.status != "waiting":
        return 0
    return (
        _today_q(db, t.branch_id, t.doctor_id)
        .filter(QueueToken.status == "waiting", QueueToken.seq < t.seq)
        .count()
    )

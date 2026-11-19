import re
from datetime import datetime

from pydantic import BaseModel
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core import clinic_view, clock
from app.core.deps import get_current_user, require
from app.db.session import get_db
from app.models.billing import Invoice
from app.models.clinical import Appointment, QueueToken, Visit
from app.models.org import Branch
from app.models.patients import Patient
from app.models.users import User
from app.schemas.actions import AppointmentIn, AppointmentUpdateIn
from app.schemas.common import AppointmentOut, PatientIn, PatientOut, QueueIn, VisitIn
from app.services.utils import audit, next_number, next_uhid

APPOINTMENT_STATUSES = {"booked", "arrived", "done", "cancelled"}


router = APIRouter(tags=["patients"])


@router.get("/branches")
def branches(include_clinic: bool = False, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    rows = db.query(Branch).filter(Branch.is_active == True).all()  # noqa: E712
    # Owner Panel asks for the offline sites too (owners only; read from their uploaded copies).
    if include_clinic and user.role in ("super_admin", "hospital_admin"):
        sites = clinic_view.virtual_branches()
        if sites:
            if settings.hub_mode:
                # Cloud hub: its own branch is just the Central Warehouse — owners see only the sites.
                return sites
            return [{c.name: getattr(b, c.name) for c in Branch.__table__.columns} for b in rows] + sites
    return rows


@router.get("/patients/suggest")
def patient_suggest(field: str, q: str = "", limit: int = 8, db: Session = Depends(get_db), _: User = Depends(require("patients", "patients.read", "front_desk"))):
    """Register form autocomplete: values typed before for this field, most used first."""
    from sqlalchemy import func

    col = {"address": Patient.address, "father_name": Patient.father_name, "referring_doctor": Patient.referring_doctor}.get(field)
    if col is None:
        raise HTTPException(400, "Unknown field")
    q = q.strip()
    if len(q) < 2:
        return []
    rows = (
        db.query(col, func.count(Patient.id))
        .filter(col.isnot(None), col != "", col.ilike(f"%{q}%"))
        .group_by(col)
        .order_by(func.count(Patient.id).desc(), col)
        .limit(max(1, min(limit, 20)))
        .all()
    )
    return [{"value": v, "count": n} for v, n in rows]


@router.get("/patients/similar")
def patient_similar(name: str = "", phone: str = "", db: Session = Depends(get_db), _: User = Depends(require("patients", "patients.read", "front_desk"))):
    """Register form: patients that may be the same person (same name, or same phone)."""
    from sqlalchemy import func

    name = " ".join(name.split()).lower()
    digits = "".join(ch for ch in phone if ch.isdigit())
    conds = []
    if len(name) >= 3:
        conds.append(func.lower(func.trim(Patient.name)) == name)
    if len(digits) >= 6:
        conds.append(func.replace(func.replace(Patient.phone, " ", ""), "-", "").like(f"%{digits[-8:]}"))
    if not conds:
        return []
    rows = db.query(Patient).filter(or_(*conds)).order_by(Patient.id.desc()).limit(5).all()
    return [
        {"id": p.id, "uhid": p.uhid, "name": p.name, "phone": p.phone or "", "father_name": p.father_name or "",
         "age_years": p.age_years, "gender": p.gender, "address": p.address or ""}
        for p in rows
    ]


@router.get("/patients", response_model=list[PatientOut])
def list_patients(q: str = "", db: Session = Depends(get_db), _: User = Depends(require("patients", "patients.read"))):
    query = db.query(Patient)
    for term in re.split(r"[,\s]+", q.strip()):
        if not term:
            continue
        like = f"%{term}%"
        query = query.filter(
            (Patient.name.ilike(like))
            | (Patient.name_mm.ilike(like))
            | (Patient.uhid.ilike(like))
            | (Patient.father_name.ilike(like))
            | (Patient.phone.ilike(like))
        )
    return query.order_by(Patient.id.desc()).limit(50).all()


@router.post("/patients", response_model=PatientOut)
def create_patient(data: PatientIn, db: Session = Depends(get_db), _: User = Depends(require("patients"))):
    p = Patient(uhid=next_uhid(db), **data.model_dump())
    if data.dob:
        from datetime import date

        p.dob = date.fromisoformat(data.dob)
        p.birthday = p.dob
    db.add(p)
    db.commit()
    db.refresh(p)
    return p


class PatientEditIn(BaseModel):
    name: str | None = None
    name_mm: str | None = None
    father_name: str | None = None
    phone: str | None = None
    gender: str | None = None
    dob: str | None = None
    age_years: int | None = None
    age_months: int | None = None
    age_days: int | None = None
    nrc: str | None = None
    address: str | None = None
    referring_doctor: str | None = None
    blood_group: str | None = None
    allergies: str | None = None
    insurance_name: str | None = None
    insurance_no: str | None = None
    note: str = ""


PATIENT_EDIT_TEXT = ("name", "name_mm", "father_name", "phone", "gender", "nrc", "address",
                     "referring_doctor", "blood_group", "allergies", "insurance_name", "insurance_no")


PATIENT_EDIT_ROLES = ("super_admin", "hospital_admin", "branch_admin", "cashier", "receptionist", "accountant")


@router.patch("/patients/{patient_id}", response_model=PatientOut)
def edit_patient(patient_id: int, data: PatientEditIn, db: Session = Depends(get_db),
                 user: User = Depends(get_current_user)):
    """✏️ Patient Records: correct a patient's details (everything but the UHID).
    Bills, results and history follow the patient, so they show the new details."""
    from datetime import date as _date

    if user.role not in PATIENT_EDIT_ROLES:
        raise HTTPException(403, "Reception / Cashier / Admin ပဲ ပြင်ခွင့်ရှိပါတယ်")

    p = db.get(Patient, patient_id)
    if not p:
        raise HTTPException(404, "Patient not found")
    note = (data.note or "").strip()
    if len(note) < 2:
        raise HTTPException(400, "Note (ဘာကြောင့် ပြင်တာလဲ) ထည့်ပါ")
    sent = data.model_dump(exclude_unset=True)
    changes: list[str] = []
    for f in PATIENT_EDIT_TEXT:
        if f not in sent or sent[f] is None:
            continue
        v = str(sent[f]).strip()
        if f == "name" and len(v) < 1:
            raise HTTPException(400, "နာမည် ထည့်ပါ")
        old = getattr(p, f) or ""
        if v != old:
            changes.append(f"{f}: {old or '—'} → {v or '—'}")
            setattr(p, f, v)
    if "dob" in sent:
        try:
            nd = _date.fromisoformat(sent["dob"][:10]) if sent["dob"] else None
        except ValueError as e:
            raise HTTPException(400, "Date of birth must be YYYY-MM-DD") from e
        if nd != p.dob:
            changes.append(f"dob: {p.dob or '—'} → {nd or '—'}")
            p.dob = nd
            p.birthday = nd
    for f in ("age_years", "age_months", "age_days"):
        if f in sent:
            v = sent[f]
            if v is not None and (v < 0 or v > 150):
                raise HTTPException(400, "အသက် မှားနေပါတယ်")
            if v != getattr(p, f):
                changes.append(f"{f}: {getattr(p, f) if getattr(p, f) is not None else '—'} → {v if v is not None else '—'}")
                setattr(p, f, v)
    if changes:
        audit(db, user.id, "patient_edit", "patient", str(p.id), f"{p.uhid} · " + " · ".join(changes) + f" — {note}"[:900])
        db.commit()
        db.refresh(p)
    return p


@router.get("/patients/{patient_id}", response_model=PatientOut)
def get_patient(patient_id: int, db: Session = Depends(get_db), _: User = Depends(require("patients.read"))):
    p = db.get(Patient, patient_id)
    if not p:
        raise HTTPException(404)
    return p


@router.get("/doctors")
def doctors(specialty: str | None = None, db: Session = Depends(get_db), _: User = Depends(get_current_user)):
    q = db.query(User).filter(User.role == "doctor", User.is_active == True)  # noqa: E712
    if specialty and specialty.strip():
        q = q.filter(User.specialty == specialty.strip())
    return q.order_by(User.full_name).all()


@router.post("/appointments", response_model=AppointmentOut)
def create_appointment(data: AppointmentIn, db: Session = Depends(get_db), _: User = Depends(require("appointments"))):
    if not db.get(Patient, data.patient_id):
        raise HTTPException(404, "Patient not found")
    doctor = db.get(User, data.doctor_id)
    if not doctor or doctor.role != "doctor":
        raise HTTPException(404, "Doctor not found")
    ap = Appointment(
        patient_id=data.patient_id,
        doctor_id=data.doctor_id,
        branch_id=data.branch_id,
        scheduled_at=data.scheduled_at,
        duration_minutes=data.duration_minutes,
        source=data.source,
        notes=data.notes,
    )
    db.add(ap)
    db.commit()
    db.refresh(ap)
    return ap


@router.get("/appointments", response_model=list[AppointmentOut])
def list_appointments(
    branch_id: int | None = None,
    doctor_id: int | None = None,
    status: str = "",
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    q = db.query(Appointment)
    if branch_id:
        q = q.filter(Appointment.branch_id == branch_id)
    if doctor_id:
        q = q.filter(Appointment.doctor_id == doctor_id)
    if status.strip():
        q = q.filter(Appointment.status == status.strip())
    if date_from:
        q = q.filter(Appointment.scheduled_at >= date_from)
    if date_to:
        q = q.filter(Appointment.scheduled_at <= date_to)
    rows = q.order_by(Appointment.scheduled_at.asc()).limit(200).all()
    patient_ids = {a.patient_id for a in rows}
    patients = {p.id: p for p in db.query(Patient).filter(Patient.id.in_(patient_ids))} if patient_ids else {}
    out = []
    for a in rows:
        p = patients.get(a.patient_id)
        out.append(AppointmentOut.model_validate(a).model_copy(
            update={"patient_name": p.name if p else "", "patient_uhid": p.uhid if p else ""},
        ))
    return out


@router.patch("/appointments/{appointment_id}", response_model=AppointmentOut)
def update_appointment(
    appointment_id: int,
    data: AppointmentUpdateIn,
    db: Session = Depends(get_db),
    _: User = Depends(require("appointments")),
):
    ap = db.get(Appointment, appointment_id)
    if not ap:
        raise HTTPException(404, "Appointment not found")
    if data.status is not None:
        if data.status not in APPOINTMENT_STATUSES:
            raise HTTPException(400, f"Status must be one of {sorted(APPOINTMENT_STATUSES)}")
        ap.status = data.status
    if data.notes is not None:
        ap.notes = data.notes
    reschedule = data.scheduled_at is not None or data.doctor_id is not None or data.duration_minutes is not None
    if reschedule:
        next_doctor_id = data.doctor_id if data.doctor_id is not None else ap.doctor_id
        next_scheduled_at = data.scheduled_at if data.scheduled_at is not None else ap.scheduled_at
        next_duration = data.duration_minutes if data.duration_minutes is not None else ap.duration_minutes
        ap.doctor_id = next_doctor_id
        ap.scheduled_at = next_scheduled_at
        ap.duration_minutes = next_duration
    db.commit()
    db.refresh(ap)
    return ap


@router.post("/queue")
def create_queue(data: QueueIn, db: Session = Depends(get_db), _: User = Depends(require("queue"))):
    token = QueueToken(
        branch_id=data.branch_id,
        department=data.department,
        patient_id=data.patient_id,
        number=next_number(db, f"queue-{data.department}", f"{data.department[:1]}-"),
    )
    db.add(token)
    db.commit()
    db.refresh(token)
    return token


@router.get("/queue")
def list_queue(branch_id: int, department: str = "OPD", db: Session = Depends(get_db)):
    return (
        db.query(QueueToken)
        .filter(QueueToken.branch_id == branch_id, QueueToken.department == department, QueueToken.status.in_(["waiting", "serving"]))
        .order_by(QueueToken.id.asc())
        .all()
    )


@router.patch("/queue/{token_id}/call")
def call_queue(token_id: int, db: Session = Depends(get_db), _: User = Depends(require("queue"))):
    t = db.get(QueueToken, token_id)
    if not t:
        raise HTTPException(404)
    t.status = "serving"
    t.called_at = clock.now()
    db.commit()
    db.refresh(t)
    return t


@router.patch("/queue/{token_id}/done")
def finish_queue(token_id: int, db: Session = Depends(get_db), _: User = Depends(require("queue"))):
    t = db.get(QueueToken, token_id)
    if not t:
        raise HTTPException(404)
    t.status = "done"
    db.commit()
    db.refresh(t)
    return t


@router.patch("/queue/{token_id}/skip")
def skip_queue(token_id: int, db: Session = Depends(get_db), _: User = Depends(require("queue"))):
    t = db.get(QueueToken, token_id)
    if not t:
        raise HTTPException(404)
    t.status = "skipped"
    db.commit()
    db.refresh(t)
    return t


@router.get("/queue-dashboard")
def queue_dashboard(branch_id: int, department: str = "OPD", db: Session = Depends(get_db), _: User = Depends(get_current_user)):
    """Live waiting-room view: every waiting/serving token, who's assigned to
    which doctor (via the patient's open OPD invoice, since the token itself
    doesn't carry a doctor), and how long they've been waiting — plus a
    per-doctor summary so Reception can see where the backlog is."""
    tokens = (
        db.query(QueueToken)
        .filter(
            QueueToken.branch_id == branch_id,
            QueueToken.department == department,
            QueueToken.status.in_(["waiting", "serving"]),
        )
        .order_by(QueueToken.id.asc())
        .all()
    )
    now = clock.now()
    rows = []
    by_doctor: dict[str, dict] = {}
    for t in tokens:
        patient = db.get(Patient, t.patient_id) if t.patient_id else None
        inv = (
            db.query(Invoice)
            .filter(
                Invoice.patient_id == t.patient_id,
                Invoice.branch_id == branch_id,
                Invoice.kind == "opd",
                Invoice.status.in_(["draft", "open", "partial"]),
            )
            .order_by(Invoice.id.desc())
            .first()
            if t.patient_id
            else None
        )
        doctor = db.get(User, inv.doctor_id) if inv and inv.doctor_id else None
        doctor_name = doctor.full_name if doctor else "Unassigned"
        waited_seconds = int((now - t.created_at).total_seconds()) if t.created_at else 0
        rows.append({
            "token_id": t.id,
            "number": t.number,
            "patient_id": t.patient_id,
            "patient_name": patient.name if patient else "",
            "uhid": patient.uhid if patient else "",
            "doctor_id": doctor.id if doctor else None,
            "doctor_name": doctor_name,
            "status": t.status,
            "created_at": t.created_at.isoformat() if t.created_at else None,
            "called_at": t.called_at.isoformat() if t.called_at else None,
            "waited_seconds": waited_seconds,
        })
        bucket = by_doctor.setdefault(doctor_name, {"doctor_name": doctor_name, "waiting": 0, "serving": 0, "wait_seconds_total": 0, "wait_count": 0})
        if t.status == "waiting":
            bucket["waiting"] += 1
            bucket["wait_seconds_total"] += waited_seconds
            bucket["wait_count"] += 1
        elif t.status == "serving":
            bucket["serving"] += 1

    summary = [
        {
            "doctor_name": b["doctor_name"],
            "waiting": b["waiting"],
            "serving": b["serving"],
            "avg_wait_seconds": int(b["wait_seconds_total"] / b["wait_count"]) if b["wait_count"] else 0,
        }
        for b in sorted(by_doctor.values(), key=lambda b: -b["waiting"])
    ]
    return {"tokens": rows, "by_doctor": summary}


@router.post("/visits")
def create_visit(data: VisitIn, db: Session = Depends(get_db), _: User = Depends(require("opd", "emr"))):
    v = Visit(
        patient_id=data.patient_id,
        branch_id=data.branch_id,
        doctor_id=data.doctor_id,
        diagnosis=data.diagnosis,
        prescription=data.prescription,
    )
    db.add(v)
    db.commit()
    db.refresh(v)
    return v


@router.get("/visits")
def list_visits(
    patient_id: int | None = None,
    doctor_id: int | None = None,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    q = db.query(Visit)
    if patient_id:
        q = q.filter(Visit.patient_id == patient_id)
    if doctor_id:
        q = q.filter(Visit.doctor_id == doctor_id)
    return q.order_by(Visit.id.desc()).limit(100).all()


PATIENT_ADMINS = ("super_admin", "hospital_admin", "branch_admin")


def _patient_refs(db: Session):
    """Every (table, column) that points at patients.id."""
    from app.db.session import Base

    out = []
    for table in Base.metadata.sorted_tables:
        for col in table.columns:
            if any(fk.target_fullname == "patients.id" for fk in col.foreign_keys):
                out.append((table, col))
    return out


class PatientMergeIn(BaseModel):
    into_id: int
    reason: str = ""


@router.post("/patients/{patient_id}/merge")
def merge_patient(patient_id: int, data: PatientMergeIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Same person registered twice: move everything (bills, visits, tokens,
    lab/X-ray, IPD...) onto the patient we keep, then remove the duplicate."""
    from sqlalchemy import update

    from app.models.ipd import Admission
    from app.services.utils import audit

    if user.role not in PATIENT_ADMINS:
        raise HTTPException(403, "Admin ပဲ လုပ်ခွင့်ရှိပါတယ်")
    if patient_id == data.into_id:
        raise HTTPException(400, "လူနာ တစ်ယောက်တည်း ဖြစ်နေပါတယ်")
    src, dst = db.get(Patient, patient_id), db.get(Patient, data.into_id)
    if not src or not dst:
        raise HTTPException(404, "Patient not found")
    active = ("admitted", "transferred", "pending_bed")
    if db.query(Admission.id).filter(Admission.patient_id == src.id, Admission.status.in_(active)).first() and \
            db.query(Admission.id).filter(Admission.patient_id == dst.id, Admission.status.in_(active)).first():
        raise HTTPException(400, "လူနာ ၂ ယောက်လုံး ဆေးရုံတက်နေဆဲပါ — တစ်ယောက် ဆေးရုံဆင်းပြီးမှ ပေါင်းပါ")
    moved = 0
    for table, col in _patient_refs(db):
        if table.name == "patients":
            res = db.execute(update(table).where(col == src.id).values({col.name: dst.id}))
        else:
            res = db.execute(update(table).where(col == src.id).values({col.name: dst.id}))
        moved += res.rowcount or 0
    for f in ("phone", "father_name", "address", "nrc", "allergies", "blood_group"):
        if hasattr(dst, f) and not getattr(dst, f) and getattr(src, f, None):
            setattr(dst, f, getattr(src, f))
    audit(db, user.id, "patient_merge", "patient", str(dst.id), f"{src.uhid} {src.name} → {dst.uhid} {dst.name} ({moved} records) — {data.reason}"[:200])
    db.delete(src)
    db.commit()
    return {"ok": True, "moved": moved, "kept": {"id": dst.id, "uhid": dst.uhid, "name": dst.name}}


@router.delete("/patients/{patient_id}")
def delete_patient(patient_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Delete a patient registered by mistake. What registering made — queue
    token, an empty visit, unpaid bills, orders with no result yet — goes with
    them. Anything real (money taken, doctor's notes, results, a dispensed
    prescription, an IPD stay) blocks it: use 🔀 merge for a double
    registration. Admins: any such patient; Reception / Cashier: registered today."""
    from sqlalchemy import func, select

    from app.models.ancillary import LabOrder, OTSchedule, RadiologyOrder, ServiceOrder
    from app.models.billing import Payment
    from app.models.clinical import Prescription
    from app.models.ipd import Admission
    from app.services.billing_service import delete_invoice
    from app.services.utils import audit

    if user.role not in PATIENT_ADMINS and user.role not in ("receptionist", "cashier"):
        raise HTTPException(403, "Admin / Reception / Cashier ပဲ လုပ်ခွင့်ရှိပါတယ်")
    p = db.get(Patient, patient_id)
    if not p:
        raise HTTPException(404)
    if user.role not in PATIENT_ADMINS and (not p.created_at or p.created_at.date() != clock.today()):
        raise HTTPException(403, "ဒီနေ့ register လုပ်ထားတဲ့ လူနာကိုပဲ ဖျက်လို့ရပါတယ် — အရင်ရက်ကလူနာကို Admin ဖျက်ပါ")
    why = []
    invs = db.query(Invoice).filter(Invoice.patient_id == p.id).all()
    if invs and db.query(Payment).filter(Payment.invoice_id.in_([i.id for i in invs])).count():
        why.append("ငွေယူထားတဲ့ bill ရှိ")
    if db.query(Admission).filter(Admission.patient_id == p.id).count():
        why.append("IPD မှတ်တမ်း ရှိ")
    if db.query(Visit).filter(Visit.patient_id == p.id).filter(
            (func.trim(func.coalesce(Visit.diagnosis, "")) != "") | (func.trim(func.coalesce(Visit.prescription, "")) != "")
            | (func.trim(func.coalesce(Visit.notes, "")) != "")).count():
        why.append("ဆရာဝန် စမ်းသပ်ချက် ရှိ")
    if db.query(Prescription).filter(Prescription.patient_id == p.id, Prescription.status.in_(["dispensed", "partial"])).count():
        why.append("ဆေးထုတ်ပေးပြီး")
    for M, label in ((LabOrder, "Lab result"), (RadiologyOrder, "X-ray/USG result")):
        if db.query(M).filter(M.patient_id == p.id, M.status == "completed").count():
            why.append(f"{label} ရှိ")
    if why:
        raise HTTPException(400, "ဒီလူနာကို ဖျက်လို့ မရပါ — " + "၊ ".join(why) + " (လူနာ ၂ ခါ register မိတာဆိုရင် 🔀 ပေါင်း ကို သုံးပါ)")
    try:
        gone = [delete_invoice(db, i, user_id=user.id, reason=f"patient {p.uhid} deleted") for i in invs]
    except ValueError as e:
        db.rollback()
        raise HTTPException(400, str(e)) from e
    for pr in db.query(Prescription).filter(Prescription.patient_id == p.id).all():
        db.delete(pr)
    for M in (LabOrder, RadiologyOrder, OTSchedule, ServiceOrder):
        db.query(M).filter(M.patient_id == p.id).delete(synchronize_session=False)
    db.query(QueueToken).filter(QueueToken.patient_id == p.id).delete(synchronize_session=False)
    db.query(Visit).filter(Visit.patient_id == p.id).delete(synchronize_session=False)
    db.query(Appointment).filter(Appointment.patient_id == p.id).delete(synchronize_session=False)
    db.flush()
    for table, col in _patient_refs(db):
        if table.name == "patients":
            if col.name != "id":
                db.execute(table.update().where(col == p.id).values({col.name: None}))
            continue
        n = db.execute(select(func.count()).select_from(table).where(col == p.id)).scalar() or 0
        if n:
            db.rollback()
            raise HTTPException(400, f"ဒီလူနာမှာ မှတ်တမ်း ({table.name}) ရှိနေလို့ ဖျက်လို့ မရပါ — လူနာ ၂ ခါ register မိတာဆိုရင် 🔀 ပေါင်း ကို သုံးပါ")
    audit(db, user.id, "patient_delete", "patient", str(p.id), f"{p.uhid} {p.name}" + (f" · bills deleted: {', '.join(gone)}" if gone else ""))
    db.delete(p)
    db.commit()
    return {"ok": True, "bills_deleted": gone}
def delete_patient(patient_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Delete a patient registered by mistake — only one with no records at all."""
    from sqlalchemy import func, select

    from app.services.utils import audit

    if user.role not in PATIENT_ADMINS:
        raise HTTPException(403, "Admin ပဲ လုပ်ခွင့်ရှိပါတယ်")
    p = db.get(Patient, patient_id)
    if not p:
        raise HTTPException(404)
    for table, col in _patient_refs(db):
        if table.name == "patients":
            continue
        n = db.execute(select(func.count()).select_from(table).where(col == p.id)).scalar() or 0
        if n:
            raise HTTPException(400, f"ဒီလူနာမှာ မှတ်တမ်း ({table.name}) ရှိနေလို့ ဖျက်လို့ မရပါ — လူနာ ၂ ခါ register မိတာဆိုရင် 🔀 ပေါင်း ကို သုံးပါ")
    audit(db, user.id, "patient_delete", "patient", str(p.id), f"{p.uhid} {p.name}")
    db.delete(p)
    db.commit()
    return {"ok": True}

import re
from datetime import date, datetime, time, timedelta

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import and_, func, or_
from sqlalchemy.orm import Session, joinedload

from app.core import clinic_view, clock
from app.core.deps import ensure_catalog_editable, get_current_user, require
from app.db.session import get_db
from app.models.billing import BILL_DATE, Invoice, InvoiceLine
from app.models.catalog import CatalogItem, LabTestReagent
from app.models.ipd import Admission, Bed, Ward
from app.models.org import AuditLog, Branch
from app.models.patients import Patient
from app.models.clinical import QueueToken
from app.models.users import User
from app.schemas.common import InvoiceOut, PatientOut, UserOut
from app.services.billing_service import update_line, void_line
from app.services.queue_service import queue_ahead
from app.services.utils import audit
from app.schemas.counter import (
    AssignBedIn,
    ConvertTypeIn,
    DoctorExamIn,
    DoctorIn,
    DoctorUpdateIn,
    LabOrderCounterIn,
    LabResultIn,
    LabWalkIn,
    PharmacyCounterIn,
    RadiologyResultIn,
    ResultFormatIn,
    ReceptionRegisterIn,
    ServiceItemIn,
    ServiceItemUpdateIn,
    StockCountIn,
    UsgOrderIn,
    XrayCounterIn,
)
from app.services.counter_service import (
    assign_bed,
    apply_stock_count,
    cashier_analytics,
    cashier_analytics_combined,
    cashier_analytics_range,
    doctor_patients,
    lab_by_doctor,
    _period_start,
    convert_patient_type,
    create_doctor,
    create_service_item,
    doctor_examine,
    get_open_invoice,
    lab_order,
    lab_walk_in,
    lab_result,
    list_doctors,
    remove_doctor,
    list_service_items,
    patient_medical_history,
    search_patient_records,
    pending_prescriptions,
    pharmacy_charge,
    pharmacy_walk_in_sale,
    radiology_result,
    reception_register,
    update_doctor,
    update_service_item,
    usg_order,
    xray_charge,
)

router = APIRouter(prefix="/counter", tags=["counter"])


@router.get("/register-check")
def register_check(branch_id: int, patient_id: int, db: Session = Depends(get_db),
                   _: User = Depends(require("front_desk", "patients", "pos", "billing", "pharmacy"))):
    """Before registering an existing patient: an unpaid OPD bill (for IPD
    register: move it over?) and an ongoing admission (no new OPD bill)."""
    adm = (
        db.query(Admission)
        .filter(Admission.patient_id == patient_id, Admission.branch_id == branch_id,
                Admission.status.in_(["admitted", "transferred", "pending_bed"]))
        .order_by(Admission.id.desc()).first()
    )
    adm_out = None
    if adm:
        inv = db.query(Invoice).filter(Invoice.admission_id == adm.id).order_by(Invoice.id.desc()).first()
        adm_out = {**_admission_info(db, adm.id), "invoice_id": inv.id if inv else None, "invoice_number": inv.number if inv else ""}
    opd = (
        db.query(Invoice)
        .filter(Invoice.patient_id == patient_id, Invoice.branch_id == branch_id, Invoice.admission_id.is_(None),
                Invoice.kind == "opd", Invoice.status.in_(["draft", "open"]))
        .order_by(Invoice.id.desc()).all()
    )
    return {
        "admission": adm_out,
        "open_opd": [
            {"id": i.id, "number": i.number, "total": float(i.total or 0), "items": len(i.lines),
             "created_at": i.created_at.isoformat() if i.created_at else None}
            for i in opd if float(i.paid or 0) <= 0.01
        ],
    }


@router.post("/reception")
def counter_reception(data: ReceptionRegisterIn, db: Session = Depends(get_db), user: User = Depends(require("front_desk", "patients", "pos", "billing", "pharmacy"))):
    try:
        result = reception_register(
            db,
            branch_id=data.branch_id,
            doctor_id=data.doctor_id,
            patient_id=data.patient_id,
            name=data.name,
            phone=data.phone,
            gender=data.gender,
            user_id=user.id,
            patient_type=data.patient_type,
            bed_id=data.bed_id,
            ward_id=data.ward_id,
            deposit=data.deposit,
            billing_mode=data.billing_mode,
            age_years=data.age_years,
            age_months=data.age_months,
            age_days=data.age_days,
            address=data.address,
            father_name=data.father_name,
            referring_doctor=data.referring_doctor,
            home_stay=data.home_stay,
        )
        if data.move_from_invoice_id and result["patient_type"] == "ipd":
            move_unpaid_bill(db, data.move_from_invoice_id, result["invoice"], user.id)
        db.commit()
        db.refresh(result["patient"])
        db.refresh(result["invoice"])
        if result.get("admission"):
            db.refresh(result["admission"])
        return {
            "patient": PatientOut.model_validate(result["patient"]),
            "patient_type": result["patient_type"],
            "invoice_id": result["invoice"].id,
            "invoice_number": result["invoice"].number,
            "token_number": result["token"].number if result.get("token") else None,
            "token_code": result["token"].public_code if result.get("token") else None,
            "token_ahead": queue_ahead(db, result["token"]) if result.get("token") else None,
            "doctor_name": result["doctor"].full_name if result["doctor"] else "",
            "doctor_specialty": (result["doctor"].specialty or "") if result["doctor"] else "",
            "consultation_fee": result["doctor"].consultation_fee if result["doctor"] and result["patient_type"] == "opd" else 0,
            "total": result["invoice"].total,
            "balance": result["invoice"].balance,
            "admission_id": result["admission"].id if result.get("admission") else None,
            "bed_code": result["bed"].code if result.get("bed") else None,
            "ward_name": result["ward"].name if result.get("ward") else None,
            "deposit": result["admission"].deposit if result.get("admission") else 0,
        }
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@router.get("/active-patients")
def active_patients(
    branch_id: int,
    q: str = "",
    doctor_id: int | None = None,
    limit: int = 50,
    with_total: bool = False,
    focus: bool = False,
    db: Session = Depends(get_db),
    _: User = Depends(require("pharmacy", "radiology", "front_desk", "pos", "billing.read", "patients.read")),
):
    query = (
        db.query(Invoice, Patient)
        .join(Patient, Invoice.patient_id == Patient.id)
        .options(joinedload(Invoice.doctor))
        .filter(
            Invoice.branch_id == branch_id,
            Invoice.status.in_(["draft", "open", "partial"]),
        )
    )
    if doctor_id:
        query = query.filter(Invoice.doctor_id == doctor_id)
    q = q.strip()
    if q:
        like = f"%{q}%"
        conditions = [
            Patient.name.ilike(like),
            Patient.name_mm.ilike(like),
            Patient.uhid.ilike(like),
            Patient.phone.ilike(like),
            Invoice.number.ilike(like),
            User.full_name.ilike(like),
        ]
        if q.isdigit():
            conditions.append(Patient.id == int(q))
        query = query.outerjoin(User, Invoice.doctor_id == User.id).filter(or_(*conditions))
    elif focus:
        # Counter chip list (Pharmacy / Lab / X-ray / USG): only today's bills
        # and patients staying in IPD — searching still finds every open bill.
        start = datetime.combine(clock.today(), datetime.min.time())
        query = query.filter(or_(Invoice.created_at >= start, Invoice.admission_id.isnot(None)))
    total = query.order_by(None).count() if with_total else None
    rows = query.order_by(Invoice.id.desc()).limit(limit * 3 if focus else limit).all()
    if focus:
        # One chip per patient (their newest open bill).
        seen: set[int] = set()
        rows = [r for r in rows if not (r[1].id in seen or seen.add(r[1].id))][:limit]
        if with_total:
            total = len(rows)
    items = [
        {
            "patient_id": p.id,
            "name": p.name,
            "uhid": p.uhid,
            "phone": p.phone,
            "invoice_id": inv.id,
            "invoice_number": inv.number,
            "invoice_kind": inv.kind,
            "invoice_status": inv.status,
            "admission_id": inv.admission_id,
            "doctor_id": inv.doctor_id,
            "doctor_name": inv.doctor.full_name if inv.doctor else "",
            "total": inv.total,
            "balance": inv.balance,
        }
        for inv, p in rows
    ]
    # Existing callers (Nurse, Reception) expect a bare array — only opt-in
    # callers passing with_total=true (the "Load more" waiting-list UI) get
    # the wrapped shape with a total count to size the button/counter against.
    if with_total:
        return {"items": items, "total": total}
    return items


@router.get("/wards")
def counter_wards(branch_id: int, db: Session = Depends(get_db), _: User = Depends(require("front_desk", "pos", "patients.read", "pharmacy", "billing"))):
    return db.query(Ward).filter(Ward.branch_id == branch_id, Ward.is_active != False).order_by(Ward.name).all()  # noqa: E712


@router.get("/beds")
def counter_beds(ward_id: int, available_only: bool = True, db: Session = Depends(get_db), _: User = Depends(require("front_desk", "pos", "patients.read", "pharmacy", "billing"))):
    q = db.query(Bed).filter(Bed.ward_id == ward_id, Bed.status != "removed")
    if available_only:
        q = q.filter(Bed.status == "available")
    beds = q.order_by(Bed.code).all()
    return [
        {
            "id": b.id,
            "code": b.code,
            "status": b.status,
            "daily_rate": b.daily_rate,
            "hourly_rate": b.hourly_rate,
            "package_rate": b.package_rate,
        }
        for b in beds
    ]


@router.get("/patient/{patient_id}/invoice", response_model=InvoiceOut | None)
def patient_open_invoice(
    patient_id: int,
    branch_id: int,
    kind: str | None = None,
    invoice_id: int | None = None,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    if invoice_id:
        inv = (
            db.query(Invoice)
            .filter(
                Invoice.id == invoice_id,
                Invoice.patient_id == patient_id,
                Invoice.branch_id == branch_id,
                Invoice.status.in_(["draft", "open", "partial"]),
            )
            .first()
        )
    else:
        inv = get_open_invoice(db, patient_id, branch_id, kind)
    if not inv:
        return None
    inv = (
        db.query(Invoice)
        .options(joinedload(Invoice.lines), joinedload(Invoice.payments), joinedload(Invoice.doctor))
        .filter(Invoice.id == inv.id)
        .first()
    )
    return inv


OPEN_BILLS_LIMIT = 100


def _admission_info(db: Session, admission_id: int | None) -> dict:
    if not admission_id:
        return {}
    adm = db.get(Admission, admission_id)
    if not adm:
        return {}
    bed = db.get(Bed, adm.bed_id) if adm.bed_id else None
    ward = db.get(Ward, bed.ward_id if bed else adm.requested_ward_id) if (bed or adm.requested_ward_id) else None
    approver = db.get(User, adm.discharge_approved_by) if adm.discharge_approved_by else None
    return {
        "admission_status": adm.status,
        "home_stay": bool(adm.home_stay),
        "bed_code": bed.code if bed else None,
        "ward_id": ward.id if ward else None,
        "ward_name": ward.name if ward else None,
        "deposit": adm.deposit or 0,
        "billing_mode": adm.billing_mode,
        "admitted_at": adm.admitted_at.isoformat() if adm.admitted_at else None,
        "discharge_approved_at": adm.discharge_approved_at.isoformat() if adm.discharge_approved_at else None,
        "discharge_approved_by": approver.full_name if approver else None,
        "discharge_type": adm.discharge_type or "",
        "discharge_summary": adm.discharge_summary or "",
    }


@router.post("/admissions/{admission_id}/home-stay")
def counter_home_stay(admission_id: int, payload: dict, db: Session = Depends(get_db), user: User = Depends(require("pos", "billing"))):
    """Mark a waiting IPD patient (no bed yet) as 🏠 Home stay — or undo it."""
    adm = db.get(Admission, admission_id)
    if not adm:
        raise HTTPException(404, "Admission not found")
    on = bool(payload.get("on", True))
    note = "🏠 Home stay" if on else "Home stay off"
    if adm.status in ("admitted", "transferred") and on:
        # In a bed now: charge the room up to today (the day they leave the bed
        # is still charged), then free the bed. No more room charges after this.
        from app.services.billing_service import ensure_daily_room_charges

        ensure_daily_room_charges(db, adm)
        bed = db.get(Bed, adm.bed_id) if adm.bed_id else None
        if bed:
            bed.status = "available"
            note = f"🏠 Home stay · left bed {bed.code}"
        adm.bed_id = None
        adm.status = "pending_bed"
    elif adm.status != "pending_bed":
        raise HTTPException(400, "ဆေးရုံဆင်းပြီးသား လူနာပါ")
    adm.home_stay = on
    if on:
        adm.requested_ward_id = None
    audit(db, user.id, "ipd_home_stay", "admission", str(adm.id), note)
    db.commit()
    return {"ok": True, "admission_id": adm.id, "home_stay": on}


@router.post("/admissions/{admission_id}/assign-bed")
def counter_assign_bed(
    admission_id: int,
    data: AssignBedIn,
    db: Session = Depends(get_db),
    user: User = Depends(require("pos", "billing")),
):
    """Cashier gives a waiting (pending_bed) IPD patient their bed — see
    counter_service.assign_bed()."""
    adm = db.get(Admission, admission_id)
    if not adm:
        raise HTTPException(404, "Admission not found")
    try:
        bed = assign_bed(
            db, adm, bed_id=data.bed_id, user_id=user.id, billing_mode=data.billing_mode,
            emergency=data.emergency, reason=data.reason,
        )
        db.commit()
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return {"ok": True, "admission_id": adm.id, "bed_code": bed.code}


@router.get("/open-invoices")
def open_invoices(
    branch_id: int,
    kind: str = "",
    q: str = "",
    db: Session = Depends(get_db),
    _: User = Depends(require("pos", "billing", "billing.read")),
):
    # Room days owed up to today go on each stay's bill before the Due is shown,
    # so the amount the cashier collects is the amount Discharge will check.
    from app.services.billing_service import ensure_daily_room_charges

    if any([ensure_daily_room_charges(db, a) for a in db.query(Admission).filter(
            Admission.branch_id == branch_id, Admission.status.in_(["admitted", "transferred"]))]):
        db.commit()
    ongoing_ipd = and_(
        Invoice.kind == "ipd",
        Invoice.admission_id.isnot(None),
        Admission.status.in_(["admitted", "transferred", "pending_bed"]),
    )
    base = (
        db.query(Invoice, Patient)
        .outerjoin(Admission, Invoice.admission_id == Admission.id)
        .outerjoin(Patient, Invoice.patient_id == Patient.id)
        .filter(
            Invoice.branch_id == branch_id,
            Invoice.status.in_(["draft", "open", "partial"]),
            # Today's still-empty bills too: a patient Reception just registered
            # (no doctor fee yet) shows up at Cashier straight away.
            # "partial" with nothing owed = a settled bill put back by ↩ Open Bill.
            or_(Invoice.balance > 0.01, ongoing_ipd, Invoice.status == "partial",
                Invoice.created_at >= datetime.combine(clock.today(), time.min)),
        )
    )
    if kind in {"opd", "ipd"}:
        base = base.filter(Invoice.kind == kind)
    terms = [t for t in re.split(r"[,\s]+", q.strip()) if t]
    for term in terms:
        like = f"%{term}%"
        base = base.filter(
            Patient.name.ilike(like) | Patient.uhid.ilike(like) | Patient.phone.ilike(like)
            | Patient.father_name.ilike(like) | Invoice.number.ilike(like)
        )
    if terms:
        rows = base.order_by(Invoice.id.desc()).limit(OPEN_BILLS_LIMIT).all()
    else:
        # Admitted patients' bills stay open for the whole stay, so they'd drift
        # past a "newest N" cut-off as OPD bills pile up — always include them
        # (bounded by bed count), and cap only the rest.
        ipd_rows = base.filter(ongoing_ipd).order_by(Invoice.id.desc()).all()
        other_rows = base.filter(~ongoing_ipd).order_by(Invoice.id.desc()).limit(OPEN_BILLS_LIMIT).all()
        rows = sorted(ipd_rows + other_rows, key=lambda r: r[0].id, reverse=True)
    return [
        {
            "id": inv.id,
            "number": inv.number,
            "kind": inv.kind,
            "status": "open" if inv.status == "draft" else inv.status,
            "patient_id": inv.patient_id,
            "patient_name": patient.name if patient else "—",
            "uhid": patient.uhid if patient else "",
            "phone": patient.phone if patient else "",
            "admission_id": inv.admission_id,
            **_admission_info(db, inv.admission_id),
            "total": inv.total,
            "paid": inv.paid,
            "balance": inv.balance,
            "discount": inv.discount,
            "created_at": inv.created_at.isoformat() if inv.created_at else None,
        }
        for inv, patient in rows
    ]


@router.get("/xray-items")
def xray_items(db: Session = Depends(get_db), _: User = Depends(get_current_user)):
    return (
        db.query(CatalogItem)
        .filter(CatalogItem.category == "radiology", CatalogItem.department != "usg", CatalogItem.is_active == True)  # noqa: E712
        .order_by(CatalogItem.price.asc())
        .all()
    )


@router.get("/pharmacy/history")
def pharmacy_history(
    branch_id: int,
    q: str = "",
    days: int = 30,
    date_from: date | None = None,
    date_to: date | None = None,
    patient_id: int | None = None,
    exclude_invoice_id: int | None = None,
    limit: int = 500,
    db: Session = Depends(get_db),
    _: User = Depends(require("pharmacy", "pos")),
):
    """Dispense history for Pharmacy's own reference. Pharmacy screens do show
    unit_price/amount here (unlike most of the app being Cashier-facing for
    money) because a walk-in customer sometimes buys and pays for medicine
    directly at Pharmacy without a separate Cashier stop, so the pharmacist
    needs to quote a total.

    Search and dates are applied in the database *before* the row limit, so an
    old patient is still found on a busy day. A search with no dates looks at
    every day; no search and no dates = the last `days` days. Dated by when
    the medicine was given (the line), not when the bill was opened — an IPD
    bill gets medicine for many days."""
    given = func.coalesce(InvoiceLine.created_at, Invoice.created_at)
    query = (
        db.query(InvoiceLine, Invoice, Patient)
        .join(Invoice, InvoiceLine.invoice_id == Invoice.id)
        .join(Patient, Invoice.patient_id == Patient.id)
        .filter(InvoiceLine.source.in_(("pharmacy", "ot")), Invoice.branch_id == branch_id)
    )
    if patient_id:
        query = query.filter(Invoice.patient_id == patient_id)
    if exclude_invoice_id:
        query = query.filter(Invoice.id != exclude_invoice_id)
    q = q.strip()
    if q:
        like = f"%{q}%"
        conds = [
            Patient.name.ilike(like),
            Patient.name_mm.ilike(like),
            Patient.uhid.ilike(like),
            Patient.phone.ilike(like),
            Invoice.number.ilike(like),
            InvoiceLine.description.ilike(like),
        ]
        if q.isdigit():
            conds.append(Patient.id == int(q))
            conds.append(Patient.uhid.ilike(f"%{int(q):03d}"))
        query = query.filter(or_(*conds))
    if date_from:
        query = query.filter(given >= datetime.combine(date_from, time.min))
    if date_to:
        query = query.filter(given < datetime.combine(date_to + timedelta(days=1), time.min))
    if not q and not date_from and not date_to and not patient_id:
        query = query.filter(given >= clock.now() - timedelta(days=days))
    limit = max(1, min(int(limit or 500), 2000))
    rows = query.order_by(given.desc(), InvoiceLine.id.desc()).limit(limit + 1).all()
    more = len(rows) > limit
    rows = rows[:limit]
    ids = [line.id for line, _i, _p in rows]
    returned: dict[int, float] = {}
    if ids:
        for orig, qty in (
            db.query(InvoiceLine.returned_from_line_id, func.sum(InvoiceLine.qty))
            .filter(InvoiceLine.returned_from_line_id.in_(ids))
            .group_by(InvoiceLine.returned_from_line_id)
        ):
            returned[orig] = -float(qty or 0)
    out = []
    for line, inv, patient in rows:
        out.append({
            "invoice_id": inv.id,
            "line_id": line.id,
            "item_id": line.item_id,
            "is_return": bool(line.returned_from_line_id),
            # what can still be given back (the Return button)
            "returnable_qty": 0 if line.returned_from_line_id or inv.status == "void" else max(line.qty - returned.get(line.id, 0.0), 0),
            "invoice_number": inv.number,
            "invoice_kind": inv.kind,
            "invoice_status": inv.status,
            "patient_id": patient.id,
            "patient_name": patient.name,
            "patient_name_mm": patient.name_mm or "",
            "uhid": patient.uhid,
            "phone": patient.phone or "",
            "item_name": line.description or (line.item.name if line.item else ""),
            "unit": line.item.unit if line.item else "",
            "qty": line.qty,
            "unit_price": line.unit_price,
            "amount": line.amount,
            "date": str(line.created_at or inv.created_at),
            "bill_date": str(inv.created_at),
            "more": more,
        })
    return out


@router.post("/pharmacy")
def counter_pharmacy(data: PharmacyCounterIn, db: Session = Depends(get_db), user: User = Depends(require("pharmacy", "dispense"))):
    try:
        result = pharmacy_charge(
            db,
            branch_id=data.branch_id,
            patient_id=data.patient_id,
            item_id=data.item_id,
            warehouse_id=data.warehouse_id,
            qty=data.qty,
            user_id=user.id,
            prescription_item_id=data.prescription_item_id,
            invoice_id=data.invoice_id,
        )
        db.commit()
        return {
            "ok": True,
            "invoice_id": result["invoice"].id,
            "invoice_number": result["invoice"].number,
            "total": result["invoice"].total,
            "balance": result["invoice"].balance,
        }
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


def _pharmacy_editable_line(db: Session, line_id: int) -> tuple[Invoice, InvoiceLine]:
    """A medicine line the pharmacist may still change: on an OPD / walk-in bill
    that isn't fully paid yet (e.g. only the doctor fee was paid first). Once paid
    in full, Return refunds and restocks instead."""
    line = db.get(InvoiceLine, line_id)
    if not line or line.source not in ("pharmacy", "ot") or line.qty <= 0 or line.returned_from_line_id:
        raise HTTPException(404, "Medicine line not found")
    inv = db.get(Invoice, line.invoice_id)
    if not inv or inv.status not in ("draft", "open", "partial"):
        raise HTTPException(400, "ငွေရှင်းပြီးသား bill ပါ — ပြန်ပေးရင် Order History → Return ကို သုံးပါ")
    return inv, line


def _keep_above_paid(db: Session, inv: Invoice) -> None:
    """A partly-paid bill must not drop below what was already paid — that money
    would have to be handed back, which is what Return (with refund) is for."""
    db.flush()
    db.refresh(inv)
    # IPD: payments are a deposit — whatever is left over is settled at discharge.
    if inv.kind != "ipd" and (inv.paid or 0) > (inv.total or 0) + 0.01:
        db.rollback()
        raise ValueError(f"ပေးပြီးသားငွေ ({inv.paid:,.0f}) ထက် bill နည်းသွားပါမယ် — Order History → Return ကို သုံးပါ")


@router.patch("/pharmacy/lines/{line_id}")
def pharmacy_line_qty(line_id: int, qty: float, db: Session = Depends(get_db), user: User = Depends(require("pharmacy", "dispense"))):
    """Qty-only correction for an already-dispensed medicine line (stock follows) —
    deliberately doesn't accept unit_price, so this can't become a side door
    for editing price."""
    inv, line = _pharmacy_editable_line(db, line_id)
    before = line.qty
    try:
        update_line(db, inv, line, qty=qty)
        _keep_above_paid(db, inv)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    audit(db, user.id, "pharmacy_line_qty", "invoice_line", str(line.id), f"{line.description}: {before:g} -> {qty:g}"[:100])
    db.commit()
    return {"ok": True, "qty": line.qty}


@router.delete("/pharmacy/lines/{line_id}")
def pharmacy_line_remove(line_id: int, db: Session = Depends(get_db), user: User = Depends(require("pharmacy", "dispense"))):
    """Take an already-dispensed medicine off the patient's unpaid bill; it goes back into stock."""
    inv, line = _pharmacy_editable_line(db, line_id)
    detail = f"{line.description} x{line.qty:g}"[:100]
    try:
        void_line(db, inv, line)
        _keep_above_paid(db, inv)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    audit(db, user.id, "pharmacy_line_remove", "invoice", str(inv.id), detail)
    db.commit()
    return {"ok": True}


@router.post("/pharmacy/walk-in")
def counter_pharmacy_walk_in(branch_id: int, name: str = "", phone: str = "", db: Session = Depends(get_db), user: User = Depends(require("pharmacy", "dispense"))):
    result = pharmacy_walk_in_sale(db, branch_id=branch_id, name=name, phone=phone, user_id=user.id)
    db.commit()
    inv = result["invoice"]
    patient = result["patient"]
    return {
        "patient_id": patient.id,
        "name": patient.name,
        "uhid": patient.uhid,
        "invoice_id": inv.id,
        "invoice_number": inv.number,
        "invoice_kind": inv.kind,
        "total": inv.total,
        "balance": inv.balance,
    }


@router.get("/pending-prescriptions")
def counter_pending_prescriptions(
    branch_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(require("pharmacy", "dispense")),
):
    return pending_prescriptions(db, branch_id)


@router.get("/doctor/queue")
def counter_doctor_queue(
    branch_id: int,
    doctor_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(require("opd", "emr")),
):
    from app.models.clinical import Visit

    rows = (
        db.query(Invoice, Patient)
        .join(Patient, Invoice.patient_id == Patient.id)
        .filter(
            Invoice.branch_id == branch_id,
            Invoice.doctor_id == doctor_id,
            Invoice.status.in_(["draft", "open", "partial"]),
        )
        .order_by(Invoice.id.desc())
        .limit(50)
        .all()
    )
    out = []
    for inv, p in rows:
        # Reception creates an empty Visit stub at registration time — only a Visit
        # with actual exam content (diagnosis/chief complaint/notes) counts as "examined".
        seen = (
            db.query(Visit)
            .filter(
                Visit.patient_id == p.id,
                Visit.doctor_id == doctor_id,
                (Visit.diagnosis != "") | (Visit.chief_complaint != "") | (Visit.notes != ""),
            )
            .order_by(Visit.id.desc())
            .first()
        )
        out.append(
            {
                "patient_id": p.id,
                "name": p.name,
                "uhid": p.uhid,
                "age_years": p.age_years,
                "gender": p.gender,
                "invoice_id": inv.id,
                "invoice_number": inv.number,
                "examined": bool(seen and seen.created_at and inv.created_at and seen.created_at >= inv.created_at),
            }
        )
    return out


@router.post("/doctor/examine")
def counter_doctor_examine(
    data: DoctorExamIn,
    db: Session = Depends(get_db),
    user: User = Depends(require("opd", "emr")),
):
    try:
        result = doctor_examine(
            db,
            branch_id=data.branch_id,
            doctor_id=data.doctor_id,
            patient_id=data.patient_id,
            chief_complaint=data.chief_complaint,
            diagnosis=data.diagnosis,
            notes=data.notes,
            follow_up_at=data.follow_up_at,
            rx_items=data.rx_items,
            user_id=user.id,
        )
        db.commit()
        return {
            "ok": True,
            "visit_id": result["visit"].id,
            "prescription_id": result["prescription"].id if result.get("prescription") else None,
        }
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@router.get("/lab-items")
def counter_lab_items(db: Session = Depends(get_db), _: User = Depends(get_current_user)):
    items = (
        db.query(CatalogItem)
        .filter(CatalogItem.category == "lab", CatalogItem.is_active == True)  # noqa: E712
        .order_by(CatalogItem.price.asc())
        .all()
    )
    links = (
        db.query(LabTestReagent, CatalogItem)
        .join(CatalogItem, LabTestReagent.reagent_item_id == CatalogItem.id)
        .all()
    )
    reagents: dict[int, list] = {}
    for link, reagent in links:
        reagents.setdefault(link.test_item_id, []).append({
            "item_id": reagent.id,
            "sku": reagent.sku,
            "name": reagent.name,
            "qty": link.qty,
            "unit": reagent.unit or "ea",
        })
    return [
        {
            "id": item.id,
            "sku": item.sku,
            "name": item.name,
            "name_mm": item.name_mm,
            "price": item.price,
            "category": item.category,
            "department": item.department,
            "reagents": reagents.get(item.id, []),
        }
        for item in items
    ]


@router.post("/lab")
def counter_lab(data: LabOrderCounterIn, db: Session = Depends(get_db), user: User = Depends(require("lab"))):
    try:
        result = lab_order(
            db,
            branch_id=data.branch_id,
            patient_id=data.patient_id,
            item_id=data.item_id,
            user_id=user.id,
            invoice_id=data.invoice_id,
        )
        db.commit()
        return {
            "ok": True,
            "invoice_id": result["invoice"].id,
            "invoice_number": result["invoice"].number,
            "order_id": result["order"].id,
            "item_name": result["item_name"],
            "total": result["invoice"].total,
            "balance": result["invoice"].balance,
        }
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@router.post("/lab/walk-in")
def counter_lab_walk_in(data: LabWalkIn, db: Session = Depends(get_db), user: User = Depends(require("lab"))):
    try:
        result = lab_walk_in(
            db,
            branch_id=data.branch_id,
            name=data.name,
            phone=data.phone,
            gender=data.gender,
            age_years=data.age_years,
            age_months=data.age_months,
            age_days=data.age_days,
            referring_doctor=data.referring_doctor,
            user_id=user.id,
        )
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    db.commit()
    inv = result["invoice"]
    patient = result["patient"]
    return {
        "patient_id": patient.id,
        "name": patient.name,
        "uhid": patient.uhid,
        "age_years": patient.age_years,
        "age_months": patient.age_months,
        "age_days": patient.age_days,
        "gender": patient.gender,
        "invoice_id": inv.id,
        "invoice_number": inv.number,
        "invoice_kind": inv.kind,
        "total": inv.total,
        "balance": inv.balance,
    }


def _role_keys(role: str) -> list[str]:
    """A typed OT role plus the short key it was stored under before (Surgeon ↔ surgeon)."""
    from app.services.billing_service import SURGERY_ROLES, surgery_role

    name = surgery_role(role)
    keys = {name.lower()} | {k for k, v in SURGERY_ROLES.items() if v.lower() == name.lower()}
    return list(keys)


@router.get("/ot-roles")
def ot_roles(db: Session = Depends(get_db), _: User = Depends(require("billing", "pos"))):
    """OT role names used before (most used first) — suggestions for "+ OT Doctor"."""
    from app.services.billing_service import SURGERY_ROLES, surgery_role

    counts: dict[str, int] = {}
    for ft, n in (
        db.query(InvoiceLine.fee_type, func.count(InvoiceLine.id))
        .filter(InvoiceLine.source == "surgery", InvoiceLine.fee_type != "")
        .group_by(InvoiceLine.fee_type)
        .all()
    ):
        name = surgery_role(ft)
        counts[name] = counts.get(name, 0) + int(n or 0)
    for v in SURGERY_ROLES.values():
        counts.setdefault(v, 0)
    return sorted(counts, key=lambda k: (-counts[k], k))


@router.get("/doctor-last-fee")
def doctor_last_fee(doctor_id: int, fee_type: str = "night", db: Session = Depends(get_db), _: User = Depends(require("billing", "pos"))):
    """The last price charged for this doctor's fee of this type (Cashier's Night
    button suggests it — night fees aren't fixed)."""
    line = (
        db.query(InvoiceLine)
        .filter(
            InvoiceLine.doctor_id == doctor_id,
            *(
                (InvoiceLine.source == "round",) if fee_type == "round"
                else (InvoiceLine.source == "surgery", func.lower(InvoiceLine.fee_type).in_(_role_keys(fee_type.split(":", 1)[1]))) if fee_type.startswith("surgery:")
                else (InvoiceLine.source == "opd", InvoiceLine.fee_type == fee_type)
            ),
        )
        .order_by(InvoiceLine.id.desc())
        .first()
    )
    return {"unit_price": line.unit_price if line else None}


RESULT_FORMAT_PERMS = {"lab": "lab", "usg": "radiology", "xray": "radiology"}


def _check_format_kind(kind: str, user: User) -> None:
    from app.core.access import can_user

    if kind not in RESULT_FORMAT_PERMS:
        raise HTTPException(400, "kind must be lab, usg or xray")
    if user.role not in ("super_admin", "hospital_admin") and not can_user(user, RESULT_FORMAT_PERMS[kind]):
        raise HTTPException(403, "Not allowed")


@router.get("/result-formats")
def list_result_formats(kind: str, db: Session = Depends(get_db), user: User = Depends(require("lab", "radiology"))):
    """Remembered result layouts for the Lab / USG / X-Ray result box."""
    from app.models.ancillary import ResultFormat

    _check_format_kind(kind, user)
    rows = db.query(ResultFormat).filter(ResultFormat.kind == kind).order_by(ResultFormat.name).all()
    return [{"id": r.id, "kind": r.kind, "name": r.name, "body": r.body} for r in rows]


@router.put("/result-formats")
def save_result_format(data: ResultFormatIn, db: Session = Depends(get_db), user: User = Depends(require("lab", "radiology"))):
    from app.models.ancillary import ResultFormat

    _check_format_kind(data.kind, user)
    name = data.name.strip()
    if not name:
        raise HTTPException(400, "Format name required")
    if not data.body.strip():
        raise HTTPException(400, "Format is empty")
    row = db.query(ResultFormat).filter(ResultFormat.kind == data.kind, ResultFormat.name == name).first()
    if not row:
        row = ResultFormat(kind=data.kind, name=name)
        db.add(row)
    row.body = data.body
    row.updated_by = user.id
    audit(db, user.id, "result_format_save", "result_format", f"{data.kind}:{name}", "")
    db.commit()
    return {"id": row.id, "kind": row.kind, "name": row.name, "body": row.body}


@router.delete("/result-formats/{format_id}")
def delete_result_format(format_id: int, db: Session = Depends(get_db), user: User = Depends(require("lab", "radiology"))):
    from app.models.ancillary import ResultFormat

    row = db.get(ResultFormat, format_id)
    if not row:
        raise HTTPException(404, "Format not found")
    _check_format_kind(row.kind, user)
    audit(db, user.id, "result_format_delete", "result_format", f"{row.kind}:{row.name}", "")
    db.delete(row)
    db.commit()
    return {"ok": True}


@router.get("/lab-order/{order_id}")
def counter_lab_order_detail(order_id: int, db: Session = Depends(get_db), _: User = Depends(require("lab", "patients.read", "nursing", "ipd", "billing.read"))):
    from app.models.ancillary import LabOrder

    order = db.get(LabOrder, order_id)
    if not order:
        raise HTTPException(404, "Lab order not found")
    patient = db.get(Patient, order.patient_id)
    # The attending doctor for this specific order (carried over from the OPD
    # invoice at order time) is a far more reliable "referring doctor" than
    # Patient.referring_doctor, which is a free-text registration field that's
    # rarely filled in practice.
    doctor = db.get(User, order.doctor_id) if order.doctor_id else None
    return {
        "order_id": order.id,
        "patient_name": patient.name if patient else "",
        "uhid": patient.uhid if patient else "",
        "age_years": patient.age_years if patient else None,
        "age_months": patient.age_months if patient else None,
        "age_days": patient.age_days if patient else None,
        "gender": patient.gender if patient else "",
        "referring_doctor": doctor.full_name if doctor else (patient.referring_doctor if patient else ""),
        "tests": order.tests,
        "result": order.result,
        "status": order.status,
        "sample_id": order.sample_id,
        "created_at": order.created_at.isoformat() if order.created_at else None,
    }


@router.get("/lab/history")
def lab_history(
    branch_id: int,
    q: str = "",
    test: str = "",
    status: str = "all",
    date_from: str = "",
    date_to: str = "",
    db: Session = Depends(get_db),
    _: User = Depends(require("lab")),
):
    """Past lab orders for the lab counter's History tab. Filters combine:
    date range, status, test name, and a free-text match on patient name, ID,
    sample id, or test name."""
    from app.models.ancillary import LabOrder

    try:
        start = date.fromisoformat(date_from) if date_from else None
        end = date.fromisoformat(date_to) if date_to else None
    except ValueError as e:
        raise HTTPException(400, "date_from / date_to must be YYYY-MM-DD") from e

    query = (
        db.query(LabOrder, Patient)
        .join(Patient, LabOrder.patient_id == Patient.id)
        .filter(LabOrder.branch_id == branch_id)
    )
    if start:
        query = query.filter(LabOrder.created_at >= datetime.combine(start, time.min))
    if end:
        query = query.filter(LabOrder.created_at < datetime.combine(end + timedelta(days=1), time.min))
    if status and status != "all":
        query = query.filter(LabOrder.status == status)
    if test.strip():
        query = query.filter(LabOrder.tests.ilike(f"%{test.strip()}%"))
    ql = q.strip()
    if ql:
        like = f"%{ql}%"
        query = query.filter(or_(
            Patient.name.ilike(like),
            Patient.uhid.ilike(like),
            LabOrder.tests.ilike(like),
            LabOrder.sample_id.ilike(like),
        ))
    rows = query.order_by(LabOrder.id.desc()).limit(200).all()
    return [
        {
            "order_id": o.id,
            "patient_id": p.id,
            "patient_name": p.name,
            "uhid": p.uhid,
            "tests": o.tests,
            "result": o.result,
            "status": o.status,
            "sample_id": o.sample_id,
            "created_at": o.created_at.isoformat() if o.created_at else None,
        }
        for o, p in rows
    ]


@router.get("/lab-orders")
def counter_lab_orders(branch_id: int, status: str = "all", db: Session = Depends(get_db), _: User = Depends(require("lab"))):
    from app.models.ancillary import LabOrder

    q = db.query(LabOrder, Patient).join(Patient, LabOrder.patient_id == Patient.id).filter(LabOrder.branch_id == branch_id)
    if status != "all":
        q = q.filter(LabOrder.status == status)
    else:  # tests taken off the bill (cancelled) don't belong on the counter
        q = q.filter(LabOrder.status != "cancelled")
    rows = q.order_by(LabOrder.id.desc()).limit(100).all()
    return [
        {
            "order_id": o.id,
            "patient_id": p.id,
            "patient_name": p.name,
            "uhid": p.uhid,
            "tests": o.tests,
            "result": o.result,
            "status": o.status,
            "sample_id": o.sample_id,
            "created_at": o.created_at.isoformat() if o.created_at else None,
        }
        for o, p in rows
    ]


@router.patch("/lab/{order_id}/result")
def counter_lab_result(order_id: int, data: LabResultIn, db: Session = Depends(get_db), user: User = Depends(require("lab"))):
    try:
        order = lab_result(db, order_id=order_id, result=data.result, user_id=user.id)
        db.commit()
        return order
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@router.get("/usg-items")
def counter_usg_items(db: Session = Depends(get_db), _: User = Depends(get_current_user)):
    return (
        db.query(CatalogItem)
        .filter(CatalogItem.category == "radiology", CatalogItem.department == "usg", CatalogItem.is_active == True)  # noqa: E712
        .order_by(CatalogItem.price.asc())
        .all()
    )


@router.post("/usg")
def counter_usg(data: UsgOrderIn, db: Session = Depends(get_db), user: User = Depends(require("radiology"))):
    try:
        result = usg_order(db, branch_id=data.branch_id, patient_id=data.patient_id, item_id=data.item_id, user_id=user.id)
        db.commit()
        return {
            "ok": True,
            "invoice_id": result["invoice"].id,
            "invoice_number": result["invoice"].number,
            "order_id": result["order"].id,
            "item_name": result["item_name"],
            "total": result["invoice"].total,
            "balance": result["invoice"].balance,
        }
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@router.get("/usg/history")
def usg_history(
    branch_id: int,
    q: str = "",
    exam: str = "",
    status: str = "all",
    date_from: str = "",
    date_to: str = "",
    db: Session = Depends(get_db),
    _: User = Depends(require("radiology")),
):
    """Past USG orders. The order row only stores modality='usg'; the scan name
    is the radiology invoice line created with that order, matched in order on
    the same invoice. Filters match the X-ray history tab."""
    import json
    from collections import defaultdict

    from app.models.ancillary import RadiologyOrder

    try:
        start = date.fromisoformat(date_from) if date_from else None
        end = date.fromisoformat(date_to) if date_to else None
    except ValueError as e:
        raise HTTPException(400, "date_from / date_to must be YYYY-MM-DD") from e

    query = (
        db.query(RadiologyOrder, Patient)
        .join(Patient, RadiologyOrder.patient_id == Patient.id)
        .filter(RadiologyOrder.branch_id == branch_id, RadiologyOrder.modality == "usg")
    )
    if start:
        query = query.filter(RadiologyOrder.created_at >= datetime.combine(start, time.min))
    if end:
        query = query.filter(RadiologyOrder.created_at < datetime.combine(end + timedelta(days=1), time.min))
    if status and status != "all":
        query = query.filter(RadiologyOrder.status == status)
    rows = query.order_by(RadiologyOrder.id.desc()).limit(500).all()

    invoice_ids = {o.invoice_id for o, _p in rows if o.invoice_id}
    lines_by_invoice: dict[int, list[str]] = defaultdict(list)
    if invoice_ids:
        line_rows = (
            db.query(InvoiceLine, CatalogItem)
            .outerjoin(CatalogItem, InvoiceLine.item_id == CatalogItem.id)
            .filter(InvoiceLine.invoice_id.in_(invoice_ids), InvoiceLine.source == "radiology")
            .order_by(InvoiceLine.id.asc())
            .all()
        )
        for line, item in line_rows:
            if item is None or item.department != "usg":
                continue
            lines_by_invoice[line.invoice_id].append(line.description or item.name)

    orders_by_invoice: dict[int | None, list] = defaultdict(list)
    for pair in rows:
        orders_by_invoice[pair[0].invoice_id].append(pair)
    exam_by_order: dict[int, str] = {}
    for inv_id, pairs in orders_by_invoice.items():
        names = lines_by_invoice.get(inv_id or 0, [])
        for index, (order, _patient) in enumerate(sorted(pairs, key=lambda pair: pair[0].id)):
            exam_by_order[order.id] = names[index] if index < len(names) else ""

    def exam_label(order: RadiologyOrder) -> str:
        named = exam_by_order.get(order.id) or ""
        if named:
            return named
        try:
            parsed = json.loads(order.findings or "")
            if isinstance(parsed, dict) and parsed.get("examType"):
                return str(parsed["examType"])
        except json.JSONDecodeError:
            pass
        return "USG"

    exam_q = exam.strip().lower()
    text_q = q.strip().lower()
    out = []
    for order, patient in rows:
        label = exam_label(order)
        if exam_q and exam_q not in label.lower():
            continue
        if text_q and text_q not in patient.name.lower() and text_q not in (patient.uhid or "").lower() and text_q not in label.lower():
            continue
        out.append({
            "order_id": order.id,
            "patient_id": patient.id,
            "patient_name": patient.name,
            "uhid": patient.uhid,
            "modality": order.modality,
            "exam_name": label,
            "findings": order.findings,
            "status": order.status,
            "created_at": order.created_at.isoformat() if order.created_at else None,
        })
        if len(out) >= 200:
            break
    return out


@router.get("/xray/history")
def xray_history(
    branch_id: int,
    q: str = "",
    exam: str = "",
    status: str = "all",
    date_from: str = "",
    date_to: str = "",
    db: Session = Depends(get_db),
    _: User = Depends(require("radiology")),
):
    """Past X-ray orders for the X-ray counter's History tab. USG orders stay
    out (they use modality='usg'). Filters combine date, status, exam name,
    and a free-text match on patient name, ID, or exam."""
    from app.models.ancillary import RadiologyOrder

    try:
        start = date.fromisoformat(date_from) if date_from else None
        end = date.fromisoformat(date_to) if date_to else None
    except ValueError as e:
        raise HTTPException(400, "date_from / date_to must be YYYY-MM-DD") from e

    query = (
        db.query(RadiologyOrder, Patient, CatalogItem)
        .join(Patient, RadiologyOrder.patient_id == Patient.id)
        .outerjoin(CatalogItem, func.lower(CatalogItem.sku) == func.lower(RadiologyOrder.modality))
        .filter(RadiologyOrder.branch_id == branch_id, RadiologyOrder.modality != "usg")
    )
    if start:
        query = query.filter(RadiologyOrder.created_at >= datetime.combine(start, time.min))
    if end:
        query = query.filter(RadiologyOrder.created_at < datetime.combine(end + timedelta(days=1), time.min))
    if status and status != "all":
        query = query.filter(RadiologyOrder.status == status)
    if exam.strip():
        like = f"%{exam.strip()}%"
        query = query.filter(or_(CatalogItem.name.ilike(like), RadiologyOrder.modality.ilike(like)))
    ql = q.strip()
    if ql:
        like = f"%{ql}%"
        query = query.filter(or_(
            Patient.name.ilike(like),
            Patient.uhid.ilike(like),
            RadiologyOrder.modality.ilike(like),
            CatalogItem.name.ilike(like),
        ))
    rows = query.order_by(RadiologyOrder.id.desc()).limit(200).all()
    return [
        {
            "order_id": o.id,
            "patient_id": p.id,
            "patient_name": p.name,
            "uhid": p.uhid,
            "modality": o.modality,
            "exam_name": item.name if item else o.modality,
            "findings": o.findings,
            "status": o.status,
            "created_at": o.created_at.isoformat() if o.created_at else None,
        }
        for o, p, item in rows
    ]


def _exam_label(db: Session, modality: str) -> str:
    """An X-ray order stores its exam's SKU as `modality` (USG orders store "usg");
    show the exam name instead of the code (Tara item codes are just numbers)."""
    if not modality or modality == "usg":
        return modality
    item = db.query(CatalogItem).filter(func.lower(CatalogItem.sku) == modality.lower()).first()
    return item.name if item else modality


@router.get("/radiology-orders")
def counter_radiology_orders(
    branch_id: int,
    modality: str = "all",
    status: str = "all",
    db: Session = Depends(get_db),
    _: User = Depends(require("radiology")),
):
    from app.models.ancillary import RadiologyOrder

    q = (
        db.query(RadiologyOrder, Patient)
        .join(Patient, RadiologyOrder.patient_id == Patient.id)
        .filter(RadiologyOrder.branch_id == branch_id)
    )
    if modality == "usg":
        q = q.filter(RadiologyOrder.modality == "usg")
    elif modality == "xray":
        q = q.filter(RadiologyOrder.modality != "usg")
    if status != "all":
        q = q.filter(RadiologyOrder.status == status)
    else:  # scans taken off the bill (cancelled) don't belong on the counter
        q = q.filter(RadiologyOrder.status != "cancelled")
    rows = q.order_by(RadiologyOrder.id.desc()).limit(100).all()
    return [
        {
            "order_id": o.id,
            "patient_id": p.id,
            "patient_name": p.name,
            "uhid": p.uhid,
            "modality": _exam_label(db, o.modality),
            "findings": o.findings,
            "status": o.status,
            "created_at": o.created_at.isoformat() if o.created_at else None,
        }
        for o, p in rows
    ]


@router.get("/radiology/{order_id}")
def counter_radiology_order_detail(order_id: int, db: Session = Depends(get_db), _: User = Depends(require("radiology", "patients.read", "nursing", "ipd", "billing.read"))):
    from app.models.ancillary import RadiologyOrder

    order = db.get(RadiologyOrder, order_id)
    if not order:
        raise HTTPException(404, "Radiology order not found")
    patient = db.get(Patient, order.patient_id)
    # See counter_lab_order_detail: the order's own doctor_id (the OPD
    # invoice's attending doctor at order time) is far more reliable than
    # Patient.referring_doctor, which is rarely filled in practice.
    doctor = db.get(User, order.doctor_id) if order.doctor_id else None
    return {
        "order_id": order.id,
        "patient_name": patient.name if patient else "",
        "uhid": patient.uhid if patient else "",
        "age_years": patient.age_years if patient else None,
        "age_months": patient.age_months if patient else None,
        "age_days": patient.age_days if patient else None,
        "gender": patient.gender if patient else "",
        "referring_doctor": doctor.full_name if doctor else (patient.referring_doctor if patient else ""),
        "modality": _exam_label(db, order.modality),
        "findings": order.findings,
        "status": order.status,
        "created_at": order.created_at.isoformat() if order.created_at else None,
    }


@router.patch("/radiology/{order_id}/result")
def counter_radiology_result(order_id: int, data: RadiologyResultIn, db: Session = Depends(get_db), user: User = Depends(require("radiology"))):
    try:
        order = radiology_result(db, order_id=order_id, findings=data.findings, user_id=user.id)
        db.commit()
        return order
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


def move_unpaid_bill(db: Session, src_id: int, dest: Invoice, user_id: int) -> int:
    """Cashier OPD -> IPD: the patient's unpaid OPD bill becomes part of the new
    IPD bill (its items and orders move over; the empty OPD bill is voided), so
    the patient doesn't end up with two bills for one visit."""
    from app.models.ancillary import LabOrder, RadiologyOrder, ServiceOrder
    from app.services.billing_service import recalc_invoice

    src = db.get(Invoice, src_id)
    if not src or src.id == dest.id or src.patient_id != dest.patient_id or src.kind == "ipd":
        return 0
    if src.status not in ("draft", "open") or (src.paid or 0) > 0.01:
        raise ValueError("ငွေပေးပြီးသား OPD bill ကို IPD ထဲ ရွှေ့လို့ မရပါ")
    lines = list(src.lines)
    have_doc = {l.doctor_id for l in dest.lines if l.source == "opd" and l.doctor_id and (l.qty or 0) > 0}
    moved = 0
    for line in lines:
        if line.source == "opd" and line.doctor_id in have_doc:
            continue  # the IPD bill already charges this doctor's consultation
        line.invoice_id = dest.id
        moved += 1
    for model in (LabOrder, RadiologyOrder, ServiceOrder):
        db.query(model).filter(model.invoice_id == src.id).update({model.invoice_id: dest.id})
    db.flush()
    db.expire(src)
    db.expire(dest)
    recalc_invoice(dest, db)
    if not db.query(InvoiceLine).filter(InvoiceLine.invoice_id == src.id).count():
        src.status = "void"
        src.notes = ((src.notes or "") + f" → {dest.number} (IPD)").strip()[:500]
    recalc_invoice(src, db)
    audit(db, user_id, "convert_opd_to_ipd", "invoice", str(dest.id), f"{src.number} -> {dest.number}, {moved} items")
    return moved


@router.post("/convert-type")
def counter_convert_type(data: ConvertTypeIn, db: Session = Depends(get_db), user: User = Depends(require("front_desk", "patients", "billing", "pos"))):
    try:
        result = convert_patient_type(
            db,
            branch_id=data.branch_id,
            patient_id=data.patient_id,
            target_type=data.target_type,
            doctor_id=data.doctor_id,
            bed_id=data.bed_id,
            ward_id=data.ward_id,
            deposit=data.deposit,
            billing_mode=data.billing_mode,
            user_id=user.id,
        )
        moved = 0
        if data.move_from_invoice_id and result["patient_type"] == "ipd":
            moved = move_unpaid_bill(db, data.move_from_invoice_id, result["invoice"], user.id)
        db.commit()
        return {
            "ok": True,
            "moved_lines": moved,
            "patient_type": result["patient_type"],
            "invoice_id": result["invoice"].id,
            "invoice_number": result["invoice"].number,
        }
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@router.post("/xray")
def counter_xray(data: XrayCounterIn, db: Session = Depends(get_db), user: User = Depends(require("radiology"))):
    try:
        result = xray_charge(
            db,
            branch_id=data.branch_id,
            patient_id=data.patient_id,
            item_id=data.item_id,
            user_id=user.id,
        )
        db.commit()
        return {
            "ok": True,
            "invoice_id": result["invoice"].id,
            "invoice_number": result["invoice"].number,
            "order_id": result["order"].id,
            "item_name": result["item_name"],
            "total": result["invoice"].total,
            "balance": result["invoice"].balance,
        }
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@router.post("/stock-count")
def stock_count(data: StockCountIn, db: Session = Depends(get_db), user: User = Depends(require("pos", "inventory"))):
    try:
        result = apply_stock_count(db, data.item_id, data.warehouse_id, data.counted_qty, user.id, data.notes)
        db.commit()
        return result
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


def _bill_history_rows(
    db: Session, branch_id: int, q: str, status: str, days: int, date_from: str = "", date_to: str = "",
    doctor_id: int | None = None, discounted: bool = False, kind: str = "",
    t_from: str = "", t_to: str = "",
) -> list[dict]:
    query = (
        db.query(Invoice, Patient)
        .outerjoin(Patient, Invoice.patient_id == Patient.id)
        .options(joinedload(Invoice.payments))
        .filter(Invoice.branch_id == branch_id)
    )
    from app.models.billing import Payment

    # An explicit date range (e.g. a single day) takes priority over the rolling `days` window.
    # A bill shows on a day when it was settled/opened that day, or when money for it
    # came in that day (an IPD deposit, a part payment) — so the day's list adds up
    # to the money actually received that day (Sale Report / Z-report).
    win_from: datetime | None = None
    win_to: datetime | None = None
    if date_from or date_to:
        try:
            if date_from:
                win_from = datetime.fromisoformat(date_from)
            if date_to:
                win_to = datetime.fromisoformat(date_to) + timedelta(days=1)
        except ValueError as e:
            raise HTTPException(400, "date_from / date_to must be YYYY-MM-DD") from e
    else:
        win_from = clock.now() - timedelta(days=max(days, 1))
    # One shift (a 🔒 cash close, or the open shift since the last close): exact moments.
    if t_from:
        try:
            win_from = datetime.fromisoformat(t_from)
            win_to = datetime.fromisoformat(t_to) if t_to else None
        except ValueError as e:
            raise HTTPException(400, "t_from / t_to must be ISO date-times") from e
    bill_conds = []
    pay_conds = []
    if win_from is not None:
        bill_conds.append(BILL_DATE >= win_from)
        pay_conds.append(Payment.created_at >= win_from)
    if win_to is not None:
        bill_conds.append(BILL_DATE < win_to)
        pay_conds.append(Payment.created_at < win_to)
    paid_in_window = db.query(Payment.invoice_id).filter(*pay_conds)
    query = query.filter(or_(and_(*bill_conds), Invoice.id.in_(paid_in_window)))
    # Status filter: one or more (comma-separated, e.g. "paid,deposit"); any match shows.
    sel = {x.strip() for x in (status or "all").split(",") if x.strip()}
    if sel and "all" not in sel:
        conds = []
        plain = [x for x in sel if x not in ("due", "deposit")]
        if plain:
            conds.append(Invoice.status.in_(plain))
        if "due" in sel:
            conds.append(and_(Invoice.balance > 0.01, Invoice.status.in_(["open", "partial", "credit"])))
        if "deposit" in sel:  # IPD deposits / part payments: money in that day
            conds.append(Invoice.id.in_(paid_in_window))
        query = query.filter(or_(*conds))
    if doctor_id:
        query = query.filter(Invoice.doctor_id == doctor_id)
    if kind and kind != "all":  # OPD / IPD / POS
        query = query.filter(Invoice.kind == kind)
    if discounted:
        query = query.filter(Invoice.discount > 0)
    for term in re.split(r"[,\s]+", q.strip()):
        if not term:
            continue
        like = f"%{term}%"
        query = query.filter(
            (Patient.name.ilike(like))
            | (Patient.father_name.ilike(like))
            | (Patient.uhid.ilike(like))
            | (Patient.phone.ilike(like))
            | (Invoice.number.ilike(like))
        )
    rows = query.order_by(Invoice.id.desc()).limit(1000).all()
    doctor_ids = {inv.doctor_id for inv, _ in rows if inv.doctor_id}
    doctor_names = (
        {u.id: u.full_name for u in db.query(User).filter(User.id.in_(doctor_ids))} if doctor_ids else {}
    )
    # OPD queue token of that visit (same patient, same day) so Reception can reprint it.
    patient_ids = {inv.patient_id for inv, _ in rows if inv.patient_id}
    tokens: dict[tuple[int, object], QueueToken] = {}
    if patient_ids:
        for t in (
            db.query(QueueToken)
            .filter(QueueToken.branch_id == branch_id, QueueToken.patient_id.in_(patient_ids))
            .order_by(QueueToken.id)
        ):
            tokens[(t.patient_id, t.queue_date)] = t
    # Who took the money (no cashier shifts in use: each cashier's drawer is
    # reconciled by what they received that day).
    taker_ids = {p.received_by for inv, _ in rows for p in inv.payments if p.received_by}
    takers = {u.id: (u.full_name or u.username) for u in db.query(User).filter(User.id.in_(taker_ids))} if taker_ids else {}
    out = []
    for inv, patient in rows:
        methods = ", ".join(sorted({p.method.upper() for p in inv.payments if p.method != "refund"}))
        # Money in for this bill inside the window (refunds handed back come off).
        day_in = 0.0
        day_by: dict[str, float] = {}
        day_users: dict[str, float] = {}
        for p in inv.payments:
            at = p.created_at
            if at is None or (win_from is not None and at < win_from) or (win_to is not None and at >= win_to):
                continue
            amt = float(p.amount or 0)
            m = (p.refund_method or "cash") if p.method == "refund" else p.method
            sign = -1 if p.method == "refund" else 1
            day_in += sign * amt
            day_by[m.upper()] = day_by.get(m.upper(), 0.0) + sign * amt
            who = takers.get(p.received_by, "—")
            day_users[who] = day_users.get(who, 0.0) + sign * amt
        bd = inv.paid_at or inv.created_at
        bill_in_window = bd is not None and (win_from is None or bd >= win_from) and (win_to is None or bd < win_to)
        token = tokens.get((inv.patient_id, inv.created_at.date() if inv.created_at else None)) if inv.kind != "ipd" else None
        out.append(
            {
                "id": inv.id,
                "number": inv.number,
                "patient_id": inv.patient_id,
                "patient_name": patient.name if patient else "—",
                "uhid": patient.uhid if patient else "—",
                "phone": patient.phone if patient else "",
                "status": inv.status,
                "doctor_id": inv.doctor_id,
                "doctor_name": doctor_names.get(inv.doctor_id, ""),
                "subtotal": inv.subtotal,
                "discount": inv.discount,
                "refunded": inv.refunded,
                "total": inv.total,
                "paid": inv.paid,
                "balance": inv.balance,
                "created_at": inv.created_at.isoformat() if inv.created_at else None,
                "paid_at": inv.paid_at.isoformat() if inv.paid_at else None,
                "payment_methods": methods or "—",
                "day_in": round(day_in, 2),
                # All money taken for this bill and handed back (deposit 1,000,000 −
                # refund 400,000 = 600,000) so a refund day can be explained on the row.
                "paid_in": round(sum(float(p.amount or 0) for p in inv.payments if p.method != "refund"), 2),
                "paid_out": round(sum(float(p.amount or 0) for p in inv.payments if p.method == "refund"), 2),
                "day_methods": ", ".join(f"{k} {v:,.0f}" for k, v in day_by.items() if abs(v) > 0.005),
                "day_by": {k: round(v, 2) for k, v in day_by.items() if abs(v) > 0.005},
                "day_users": {k: round(v, 2) for k, v in day_users.items() if abs(v) > 0.005},
                # Shown only because money came in that day (bill not settled/opened in the window)
                # or a running IPD stay: a deposit / part payment, not a finished bill.
                "deposit": (not bill_in_window) or (inv.kind == "ipd" and inv.status != "paid"),
                "invoice_kind": inv.kind,
                "token_number": token.number if token else None,
                "token_code": token.public_code if token else None,
                "token_ahead": queue_ahead(db, token) if token else None,
            }
        )
    if sel and "all" not in sel and "deposit" in sel:
        plain = {x for x in sel if x not in ("due", "deposit")}

        def keep(r: dict) -> bool:
            if r["deposit"] and r["day_in"] > 0.005:
                return True
            if r["status"] in plain:
                return True
            return "due" in sel and r["balance"] > 0.01 and r["status"] in ("open", "partial", "credit")

        out = [r for r in out if keep(r)]
    return out


@router.get("/patient-records/search")
def counter_patient_records_search(
    branch_id: int,
    date_from: str = "",
    date_to: str = "",
    kind: str = "",
    source: str = "",
    q: str = "",
    db: Session = Depends(get_db),
    _: User = Depends(require("front_desk", "patients.read", "patients")),
):
    from datetime import date

    try:
        start = date.fromisoformat(date_from) if date_from else None
        end = date.fromisoformat(date_to) if date_to else None
    except ValueError as e:
        raise HTTPException(400, "date_from / date_to must be YYYY-MM-DD") from e
    try:
        return search_patient_records(
            db,
            branch_id=branch_id,
            date_from=start,
            date_to=end,
            kind=kind,
            source=source,
            q=q,
        )
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@router.get("/patient-medical-history")
def counter_patient_medical_history(
    patient_id: int,
    branch_id: int | None = None,
    db: Session = Depends(get_db),
    _: User = Depends(require("front_desk", "patients.read", "patients")),
):
    try:
        result = patient_medical_history(db, patient_id=patient_id, branch_id=branch_id)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return {
        "patient": PatientOut.model_validate(result["patient"]),
        "visits": result["visits"],
        "clinical_notes": result["clinical_notes"],
    }


def daily_close_data(db: Session, branch_id: int, date_from: str, date_to: str = "") -> dict:
    """One sheet to close the day: the money that came in (by way, by person,
    by kind of bill), what went back out, what is still owed, and the cash that
    should be in the drawer. Same counting as History's day total and the Sale
    Report's Collected."""
    try:
        d_from = date.fromisoformat(date_from)
        d_to = date.fromisoformat(date_to) if date_to else d_from
    except ValueError as e:
        raise HTTPException(400, "date_from / date_to must be YYYY-MM-DD") from e
    if d_to < d_from:
        d_from, d_to = d_to, d_from
    since = datetime.combine(d_from, time.min)
    until = datetime.combine(d_to + timedelta(days=1), time.min)
    return money_window(db, branch_id, since, until, by_time=False)


def money_window(db: Session, branch_id: int, since: datetime, until: datetime, by_time: bool = True) -> dict:
    """Money in / out between two moments — the Daily Close (whole days) and
    the shift close (last close → now) share this. `by_time` takes expenses by
    when they were keyed in (a shift) instead of their expense date (a day)."""
    from app.models.accounting import Expense
    from app.models.billing import Payment

    d_from, d_to = since.date(), (until - timedelta(microseconds=1)).date()
    pays = (
        db.query(Payment, Invoice)
        .join(Invoice, Payment.invoice_id == Invoice.id)
        .filter(Invoice.branch_id + 0 == branch_id, Payment.created_at >= since, Payment.created_at < until)
        .all()
    )
    user_ids = {p.received_by for p, _ in pays if p.received_by}
    users = {u.id: (u.full_name or u.username) for u in db.query(User).filter(User.id.in_(user_ids))} if user_ids else {}

    methods: dict[str, dict] = {}
    people: dict[str, dict] = {}
    kinds = {k: 0.0 for k in ("opd", "ipd_deposit", "ipd_final", "pharmacy", "older", "credit", "other")}
    refunds: list[dict] = []
    received = refunded = 0.0
    for p, inv in pays:
        amt = float(p.amount or 0)
        is_ref = p.method == "refund"
        m = ((p.refund_method or "cash") if is_ref else (p.method or "cash")).upper()
        sign = -1.0 if is_ref else 1.0
        row = methods.setdefault(m, {"method": m, "in": 0.0, "out": 0.0, "net": 0.0})
        row["out" if is_ref else "in"] += amt
        row["net"] += sign * amt
        who = users.get(p.received_by, "—")
        pr = people.setdefault(who, {"name": who, "net": 0.0, "by": {}})
        pr["net"] += sign * amt
        pr["by"][m] = pr["by"].get(m, 0.0) + sign * amt
        if is_ref:
            refunded += amt
            refunds.append({"number": inv.number, "amount": amt, "method": m, "by": who})
        else:
            received += amt
        bd = inv.paid_at or inv.created_at
        if inv.credit_at is not None and p.created_at and p.created_at > inv.credit_at:
            key = "credit"  # 📒 a credit (အကြွေး) paid back
        elif inv.kind == "ipd":
            key = "ipd_final" if inv.status == "paid" and inv.paid_at and since <= inv.paid_at < until else "ipd_deposit"
        elif bd is not None and bd < since:
            key = "older"
        elif inv.kind in ("opd", "pharmacy"):
            key = inv.kind
        else:
            key = "other"
        kinds[key] += sign * amt

    bills = (
        db.query(Invoice, Patient)
        .outerjoin(Patient, Invoice.patient_id == Patient.id)
        .filter(Invoice.branch_id == branch_id, BILL_DATE >= since, BILL_DATE < until)
        .all()
    )
    finished = [i for i, _ in bills if i.status == "paid"]
    voids = [i for i, _ in bills if i.status == "void"]
    due = [
        {"number": i.number, "patient": pt.name if pt else "—", "uhid": pt.uhid if pt else "", "kind": i.kind,
         "total": float(i.total or 0), "balance": float(i.balance or 0)}
        for i, pt in bills
        if i.status in ("open", "partial") and float(i.balance or 0) > 0.01 and i.kind != "ipd"
    ]
    due.sort(key=lambda d: -d["balance"])
    # 📒 Closed on credit in this window: not money in — shown so the day's
    # sales can be explained (cash in + on credit).
    credit_rows = (
        db.query(Invoice, Patient)
        .outerjoin(Patient, Invoice.patient_id == Patient.id)
        .filter(Invoice.branch_id == branch_id, Invoice.credit_at >= since, Invoice.credit_at < until, Invoice.status != "void")
        .all()
    )
    credits = [
        {"id": i.id, "number": i.number, "patient": pt.name if pt else "—", "name": i.credit_name or "",
         "amount": float(i.credit_amount or 0), "balance": float(i.balance or 0)}
        for i, pt in credit_rows
    ]

    exp_when = (and_(Expense.created_at >= since, Expense.created_at < until) if by_time
                else and_(Expense.expense_date >= d_from, Expense.expense_date <= d_to))
    exp_rows = (
        db.query(Expense.paid_from, func.sum(Expense.amount), func.count(Expense.id))
        .filter(Expense.branch_id == branch_id, exp_when)
        .group_by(Expense.paid_from)
        .all()
    )
    expenses = [{"paid_from": str(pf or "petty"), "amount": float(a or 0), "count": int(c or 0)} for pf, a, c in exp_rows]
    cash_exp = sum(e["amount"] for e in expenses if e["paid_from"].lower() == "cash")
    from app.models.accounting import PettyTopup

    top_when = (and_(PettyTopup.created_at >= since, PettyTopup.created_at < until) if by_time
                else and_(PettyTopup.created_at >= datetime.combine(d_from, time.min),
                          PettyTopup.created_at < datetime.combine(d_to + timedelta(days=1), time.min)))
    petty_from_drawer = float(
        db.query(func.coalesce(func.sum(PettyTopup.amount), 0))
        .filter(PettyTopup.branch_id == branch_id, PettyTopup.source == "drawer", top_when)
        .scalar() or 0
    )
    cash_net = sum(r["net"] for k, r in methods.items() if k in ("CASH", "DEPOSIT"))

    r2 = lambda v: round(v, 2)  # noqa: E731
    return {
        "since": since.isoformat(timespec="seconds"),
        "until": until.isoformat(timespec="seconds"),
        "date_from": d_from.isoformat(),
        "date_to": d_to.isoformat(),
        "net": r2(received - refunded),
        "received": r2(received),
        "refunded": r2(refunded),
        "methods": sorted(
            ({k: (r2(v) if isinstance(v, float) else v) for k, v in r.items()} for r in methods.values() if r["in"] or r["out"]),
            key=lambda r: -r["net"],
        ),
        "people": sorted(
            ({"name": r["name"], "net": r2(r["net"]), "by": {k: r2(v) for k, v in r["by"].items() if abs(v) > 0.005}} for r in people.values()),
            key=lambda r: -r["net"],
        ),
        "kinds": {k: r2(v) for k, v in kinds.items()},
        "refunds": refunds,
        "bills": {
            "finished": len(finished),
            "finished_total": r2(sum(float(i.total or 0) for i in finished)),
            "discount": r2(sum(float(i.discount or 0) for i in finished)),
            "void": len(voids),
        },
        "due": due,
        "due_total": r2(sum(d["balance"] for d in due)),
        "credits": credits,
        "credit_total": r2(sum(c["amount"] for c in credits)),
        "credit_back": r2(kinds["credit"]),
        "expenses": expenses,
        "cash_expense": r2(cash_exp),
        "petty_from_drawer": r2(petty_from_drawer),
        "cash_in_drawer": r2(cash_net - cash_exp - petty_from_drawer),
    }


# Day-by-day ward charges Tara's "InPatient D/C" list had: (name to look for,
# charged per day of the stay, ticked by default).
DISCHARGE_TEMPLATE = (
    ("hospital service", True, True),
    ("medical care", True, True),
    ("nursing care", True, True),
    ("customer service", False, True),
    ("ward nurse", False, False),
    ("special nurse", False, False),
)


@router.get("/ipd-discharge-check")
def ipd_discharge_check(
    invoice_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(require("pos", "billing.read", "billing")),
):
    """Before an IPD patient goes home: the day-by-day ward charges to add (with
    the number of days filled in) and a short checklist of what is often missed."""
    from app.models.ipd import WardMedOrder, WardMedOrderItem

    inv = db.get(Invoice, invoice_id)
    if not inv:
        raise HTTPException(404, "Bill not found")
    adm = db.get(Admission, inv.admission_id) if inv.admission_id else None
    today = clock.today()
    start = adm.admitted_at.date() if adm and adm.admitted_at else (inv.created_at.date() if inv.created_at else today)
    days = max(1, (today - start).days + 1)
    lines = [ln for ln in inv.lines if float(ln.qty or 0) != 0]

    def qty_of(pred) -> float:
        return round(sum(float(ln.qty or 0) for ln in inv.lines if pred(ln)), 2)

    cands = (
        db.query(CatalogItem)
        .filter(CatalogItem.is_active.is_(True), CatalogItem.deleted_at.is_(None), CatalogItem.is_stock.is_(False))
        .all()
    )
    items = []
    used: set[int] = set()
    for word, per_day, on in DISCHARGE_TEMPLATE:
        hit = sorted(
            (c for c in cands if word in (c.name or "").lower() and c.id not in used),
            key=lambda c: (c.department != "cashier", len(c.name or "")),
        )
        if not hit:
            continue
        c = hit[0]
        used.add(c.id)
        have = qty_of(lambda ln, cid=c.id: ln.item_id == cid)
        want = days if per_day else 1
        items.append({
            "item_id": c.id, "name": c.name, "price": float(c.price or 0), "per_day": per_day,
            "already": have, "qty": max(want - have, 0) if per_day else (0 if have else 1),
            "checked": on and (want - have > 0 if per_day else not have),
        })

    rounds = qty_of(lambda ln: ln.source == "round")
    docs = qty_of(lambda ln: ln.source in ("opd", "surgery"))
    room_days = qty_of(lambda ln: ln.source == "ipd_room")
    meds = [ln for ln in lines if ln.source in ("pharmacy", "ot") and ln.is_stock]
    pending = (
        db.query(func.count(WardMedOrderItem.id))
        .join(WardMedOrder, WardMedOrderItem.order_id == WardMedOrder.id)
        .filter(WardMedOrderItem.status == "pending",
                or_(WardMedOrder.invoice_id == inv.id, and_(WardMedOrder.admission_id == inv.admission_id, WardMedOrder.admission_id.isnot(None))))
        .scalar()
    ) or 0
    checks = [
        {"ok": rounds > 0, "text": f"Round fee {rounds:g} ကြိမ်" if rounds else "Round fee မထည့်ရသေးပါ"},
        {"ok": True, "text": f"ဆရာဝန်ခ {docs:g} ကြောင်း"} if docs else {"ok": None, "text": "ဆရာဝန်ခ မပါပါ"},
        {"ok": room_days > 0, "text": f"Room ခ {room_days:g} ရက်" if room_days else "Room ခ မဝင်ရသေးပါ"},
        {"ok": bool(meds) or None, "text": f"ဆေး / ပစ္စည်း {len(meds)} မျိုး" if meds else "ဆေးဖိုး မပါပါ"},
    ]
    if pending:
        checks.append({"ok": False, "text": f"Ward order {pending} ခု မပေးရသေးပါ"})
    checks.append({"ok": True, "text": "ဆရာဝန် ဆင်းခွင့်ပြုပြီး"} if adm and adm.discharge_approved_at
                  else {"ok": None, "text": "ဆရာဝန် ဆင်းခွင့် မပြုရသေးပါ"})
    return {
        "invoice_id": inv.id, "kind": inv.kind, "admitted": start.isoformat(), "days": days,
        "items": items, "checks": checks,
    }


BILL_CATS = ("doc", "ot", "otm", "room", "svc", "med", "lab", "img")  # otm = OT supplies (stock)
BILL_ORDER_KEY = "bill_cat_order"


def _bill_order(db: Session) -> list[str]:
    import json

    from app.models.org import Setting

    row = db.get(Setting, BILL_ORDER_KEY)
    try:
        saved = [c for c in json.loads(row.value) if c in BILL_CATS] if row and row.value else []
    except ValueError:
        saved = []
    if saved and "otm" not in saved:  # saved before OT supplies had their own group
        saved.insert(saved.index("ot") + 1 if "ot" in saved else len(saved), "otm")
    return saved + [c for c in BILL_CATS if c not in saved]


@router.get("/bill-order")
def bill_order(db: Session = Depends(get_db), _: User = Depends(get_current_user)):
    """Order of the groups (Doctor, OT, Room, ...) on the open bill and the voucher."""
    return {"order": _bill_order(db)}


@router.put("/bill-order")
def set_bill_order(
    payload: dict,
    db: Session = Depends(get_db),
    user: User = Depends(require("pos", "billing")),
):
    import json

    from app.models.org import Setting

    order = [c for c in (payload.get("order") or []) if c in BILL_CATS]
    order = list(dict.fromkeys(order)) + [c for c in BILL_CATS if c not in order]
    row = db.get(Setting, BILL_ORDER_KEY)
    if row:
        row.value = json.dumps(order)
    else:
        db.add(Setting(key=BILL_ORDER_KEY, value=json.dumps(order)))
    audit(db, user.id, "bill_order", "setting", BILL_ORDER_KEY, ",".join(order))
    db.commit()
    return {"order": order}


def _last_close(db: Session, branch_id: int):
    from app.models.accounting import CashClose

    return db.query(CashClose).filter(CashClose.branch_id == branch_id).order_by(CashClose.period_to.desc(), CashClose.id.desc()).first()


def _close_preview(db: Session, branch_id: int) -> dict:
    last = _last_close(db, branch_id)
    now = clock.now()
    since = last.period_to if last else datetime.combine(clock.today(), time.min)
    data = money_window(db, branch_id, since, now)
    float_in = float(last.float_left or 0) if last else 0.0
    data.update({
        "float_in": round(float_in, 2),
        "expected_cash": round(float_in + float(data["cash_in_drawer"]), 2),
        "last_close": {"closed_by": last.closed_by, "at": last.period_to.isoformat()} if last else None,
        "pending": _close_out(last, True) if last is not None and (last.status or "closed") == "counting" else None,
        "period_from": since.isoformat(),
        "period_to": now.isoformat(),
    })
    return data


def _close_out(c, with_summary: bool = False) -> dict:
    import json

    out = {
        "id": c.id, "period_from": c.period_from.isoformat(), "period_to": c.period_to.isoformat(),
        "closed_by": c.closed_by, "handed_to": c.handed_to, "float_in": c.float_in, "expected_cash": c.expected_cash,
        "counted_cash": c.counted_cash, "diff": round(c.counted_cash - c.expected_cash, 2), "float_left": c.float_left,
        "note": c.note, "created_at": c.created_at.isoformat(timespec="seconds") if c.created_at else None,
        "status": c.status or "closed",
    }
    if with_summary:
        try:
            out["summary"] = json.loads(c.summary or "{}")
        except ValueError:
            out["summary"] = {}
    return out


@router.get("/cash-close/preview")
def cash_close_preview(branch_id: int, db: Session = Depends(get_db), _: User = Depends(require("pos", "billing.read", "billing"))):
    """What the next hand-over covers: from the last close (or midnight) to now."""
    return _close_preview(db, branch_id)


@router.post("/cash-close")
def cash_close(payload: dict, db: Session = Depends(get_db), user: User = Depends(require("pos", "billing"))):
    """🔒 Close the drawer for this shift. Figures are worked out again here, at
    the moment of closing, so nothing taken while the form was open is missed."""
    import json

    from app.models.accounting import CashClose

    try:
        branch_id = int(payload.get("branch_id"))
        counted = float(payload.get("counted_cash"))
        float_left = float(payload.get("float_left") or 0)
    except (TypeError, ValueError) as e:
        raise HTTPException(400, "ရေတွက်ရတဲ့ ငွေသား ထည့်ပါ") from e
    by = str(payload.get("closed_by") or "").strip()[:80]
    if len(by) < 2:
        raise HTTPException(400, "ပိတ်သူ နာမည် ထည့်ပါ")
    if counted < 0 or float_left < 0:
        raise HTTPException(400, "ငွေပမာဏ မှန်အောင် ထည့်ပါ")
    d = _close_preview(db, branch_id)
    if d.get("pending"):
        raise HTTPException(400, "ရေတွက်နေဆဲ စာရင်း ရှိနေပါတယ် — အဲဒါကို အရင် အတည်ပြုပါ")
    c = CashClose(
        branch_id=branch_id,
        period_from=datetime.fromisoformat(d["period_from"]),
        period_to=datetime.fromisoformat(d["period_to"]),
        closed_by=by,
        handed_to=str(payload.get("handed_to") or "").strip()[:80],
        user_id=user.id,
        float_in=d["float_in"],
        expected_cash=d["expected_cash"],
        counted_cash=round(counted, 2),
        float_left=round(float_left, 2),
        note=str(payload.get("note") or "").strip()[:200],
        summary=json.dumps(d, ensure_ascii=False),
    )
    db.add(c)
    db.flush()
    audit(db, user.id, "cash_close", "cash_close", str(c.id),
          f"{by} · expected {c.expected_cash:,.0f} · counted {c.counted_cash:,.0f} · diff {c.counted_cash - c.expected_cash:,.0f}")
    db.commit()
    return _close_out(c, True)


def _close_money(payload: dict, key: str, required: bool = True) -> float:
    v = payload.get(key)
    if v in (None, ""):
        if required:
            raise HTTPException(400, "ရေတွက်ရတဲ့ ငွေသား ထည့်ပါ")
        return 0.0
    try:
        f = float(v)
    except (TypeError, ValueError) as e:
        raise HTTPException(400, "ငွေပမာဏ မှန်အောင် ထည့်ပါ") from e
    if f < 0:
        raise HTTPException(400, "ငွေပမာဏ မှန်အောင် ထည့်ပါ")
    return round(f, 2)


def _get_close(db: Session, close_id: int):
    from app.models.accounting import CashClose

    c = db.get(CashClose, close_id)
    if not c:
        raise HTTPException(404, "စာရင်း မတွေ့ပါ")
    return c


def _next_close(db: Session, c):
    from app.models.accounting import CashClose

    return (
        db.query(CashClose)
        .filter(CashClose.branch_id == c.branch_id, CashClose.id != c.id, CashClose.period_to > c.period_to)
        .order_by(CashClose.period_to, CashClose.id)
        .first()
    )


def _set_float_in(c, float_in: float) -> None:
    """The next hand-over starts with what the one before it left behind."""
    import json

    try:
        sm = json.loads(c.summary or "{}")
    except ValueError:
        sm = {}
    drawer = float(sm.get("cash_in_drawer", float(c.expected_cash or 0) - float(c.float_in or 0)) or 0)
    c.float_in = round(float_in, 2)
    c.expected_cash = round(float_in + drawer, 2)
    sm["float_in"] = c.float_in
    sm["expected_cash"] = c.expected_cash
    c.summary = json.dumps(sm, ensure_ascii=False)


def _recalc_close(db: Session, c) -> None:
    """Work a hand-over's figures out again for its window (after its time moved)."""
    import json

    try:
        old = json.loads(c.summary or "{}")
    except ValueError:
        old = {}
    d = money_window(db, c.branch_id, c.period_from, c.period_to)
    fi = float(c.float_in or 0)
    d.update({
        "float_in": round(fi, 2),
        "expected_cash": round(fi + float(d["cash_in_drawer"]), 2),
        "last_close": old.get("last_close"),
        "period_from": c.period_from.isoformat(),
        "period_to": c.period_to.isoformat(),
    })
    c.expected_cash = d["expected_cash"]
    c.summary = json.dumps(d, ensure_ascii=False)


@router.post("/cash-close/start")
def cash_close_start(payload: dict, db: Session = Depends(get_db), user: User = Depends(require("pos", "billing"))):
    """Step 1 of a hand-over: cut the books off NOW. Money taken after this goes
    to the next shift, so the cashier can take half an hour to count."""
    import json

    from app.models.accounting import CashClose

    try:
        branch_id = int(payload.get("branch_id"))
    except (TypeError, ValueError) as e:
        raise HTTPException(400, "branch_id required") from e
    by = str(payload.get("closed_by") or "").strip()[:80]
    if len(by) < 2:
        raise HTTPException(400, "ပိတ်သူ နာမည် ထည့်ပါ")
    d = _close_preview(db, branch_id)
    if d.get("pending"):
        raise HTTPException(400, "ရေတွက်နေဆဲ စာရင်း ရှိနေပါပြီ — အဲဒါကို အရင် အတည်ပြု (သို့) ပယ်ပါ")
    d.pop("pending", None)
    c = CashClose(
        branch_id=branch_id,
        period_from=datetime.fromisoformat(d["period_from"]),
        period_to=datetime.fromisoformat(d["period_to"]),
        closed_by=by,
        user_id=user.id,
        float_in=d["float_in"],
        expected_cash=d["expected_cash"],
        counted_cash=0,
        float_left=0,
        status="counting",
        summary=json.dumps(d, ensure_ascii=False),
    )
    db.add(c)
    db.flush()
    audit(db, user.id, "cash_close_start", "cash_close", str(c.id), f"{by} · cut off {c.period_to:%d/%m %H:%M} · expected {c.expected_cash:,.0f}")
    db.commit()
    return _close_out(c, True)


@router.post("/cash-close/{close_id}/finish")
def cash_close_finish(close_id: int, payload: dict, db: Session = Depends(get_db), user: User = Depends(require("pos", "billing"))):
    """Step 2: the cash has been counted — confirm the hand-over."""
    c = _get_close(db, close_id)
    if (c.status or "closed") != "counting":
        raise HTTPException(400, "ဒီစာရင်းက ပိတ်ပြီးသားပါ")
    counted = _close_money(payload, "counted_cash")
    float_left = _close_money(payload, "float_left", False)
    by = str(payload.get("closed_by") or c.closed_by or "").strip()[:80]
    if len(by) < 2:
        raise HTTPException(400, "ပိတ်သူ နာမည် ထည့်ပါ")
    c.closed_by = by
    c.handed_to = str(payload.get("handed_to") or "").strip()[:80]
    c.counted_cash = counted
    c.float_left = float_left
    c.note = str(payload.get("note") or "").strip()[:200]
    c.status = "closed"
    nx = _next_close(db, c)  # a later cut-off (e.g. the next midnight) starts with this float
    if nx is not None:
        _set_float_in(nx, float(float_left))
    audit(db, user.id, "cash_close", "cash_close", str(c.id),
          f"{by} · expected {c.expected_cash:,.0f} · counted {c.counted_cash:,.0f} · diff {c.counted_cash - c.expected_cash:,.0f}")
    db.commit()
    return _close_out(c, True)


@router.post("/cash-close/{close_id}/cancel")
def cash_close_cancel(close_id: int, db: Session = Depends(get_db), user: User = Depends(require("pos", "billing"))):
    """Pressed by mistake: drop a cut-off that is still being counted."""
    c = _get_close(db, close_id)
    if (c.status or "closed") != "counting":
        raise HTTPException(400, "ပိတ်ပြီးသား စာရင်းကို ပယ်လို့ မရပါ — ↩ ပြန်ဖွင့် ကို သုံးပါ")
    if _next_close(db, c) is not None:
        raise HTTPException(400, "နောက်ထပ် စာရင်း ရှိနေလို့ ပယ်လို့ မရပါ — ငွေရေတွက်ပြီး အတည်ပြုပါ")
    audit(db, user.id, "cash_close_cancel", "cash_close", str(c.id), f"{c.closed_by} · cut off {c.period_to:%d/%m %H:%M} cancelled")
    db.delete(c)
    db.commit()
    return {"ok": True}


@router.post("/cash-close/{close_id}/edit")
def cash_close_edit(close_id: int, payload: dict, db: Session = Depends(get_db), user: User = Depends(require("pos", "billing"))):
    """Fix a closed hand-over typed in wrong: counted cash, float left, names,
    note. The expected (system) figure is never typed in — it is worked out."""
    c = _get_close(db, close_id)
    if (c.status or "closed") != "closed":
        raise HTTPException(400, "ရေတွက်နေဆဲ စာရင်းကို အတည်ပြုပါ")
    reason = str(payload.get("reason") or "").strip()[:150]
    if len(reason) < 2:
        raise HTTPException(400, "ဘာလို့ ပြင်တာလဲ ရေးပါ")
    old = {"by": c.closed_by, "to": c.handed_to, "counted": c.counted_cash, "float": c.float_left, "note": c.note,
           "time": c.period_to.strftime("%d/%m %H:%M"), "expected": c.expected_cash}
    nx = _next_close(db, c)
    time_moved = False
    if payload.get("period_to"):
        try:
            new_to = datetime.fromisoformat(str(payload.get("period_to")))
        except ValueError as e:
            raise HTTPException(400, "အချိန် မှန်အောင် ထည့်ပါ") from e
        if new_to.replace(second=0, microsecond=0) != c.period_to.replace(second=0, microsecond=0):
            if new_to <= c.period_from:
                raise HTTPException(400, f"ပိတ်ချိန်က shift စချိန် ({c.period_from:%d/%m %H:%M}) နောက်မှာ ဖြစ်ရမယ်")
            if nx is not None and new_to >= nx.period_to:
                raise HTTPException(400, f"ပိတ်ချိန်က နောက် shift ပိတ်ချိန် ({nx.period_to:%d/%m %H:%M}) မတိုင်ခင် ဖြစ်ရမယ်")
            if nx is None and new_to > clock.now():
                raise HTTPException(400, "နောက်ရက်/အချိန်ကို မရွေးနိုင်ပါ")
            c.period_to = new_to
            _recalc_close(db, c)
            if nx is not None:
                nx.period_from = new_to
                _recalc_close(db, nx)
            time_moved = True
    counted = _close_money(payload, "counted_cash") if "counted_cash" in payload else c.counted_cash
    float_left = _close_money(payload, "float_left", False) if "float_left" in payload else c.float_left
    if "closed_by" in payload:
        by = str(payload.get("closed_by") or "").strip()[:80]
        if len(by) < 2:
            raise HTTPException(400, "ပိတ်သူ နာမည် ထည့်ပါ")
        c.closed_by = by
    if "handed_to" in payload:
        c.handed_to = str(payload.get("handed_to") or "").strip()[:80]
    if "note" in payload:
        c.note = str(payload.get("note") or "").strip()[:200]
    c.counted_cash = counted
    float_changed = abs(float(float_left) - float(c.float_left or 0)) > 0.005
    c.float_left = float_left
    if float_changed and nx is not None:
        _set_float_in(nx, float(float_left))
    new = {"by": c.closed_by, "to": c.handed_to, "counted": c.counted_cash, "float": c.float_left, "note": c.note,
           "time": c.period_to.strftime("%d/%m %H:%M"), "expected": c.expected_cash}
    lab = {"by": "ပိတ်သူ", "to": "လွှဲယူသူ", "counted": "ရေတွက်", "float": "ချန်ငွေ", "note": "note", "time": "ပိတ်ချိန်", "expected": "စနစ်ငွေ"}
    fmt = lambda v: f"{v:,.0f}" if isinstance(v, (int, float)) else (v or "—")
    ch = [f"{lab[k]} {fmt(old[k])} → {fmt(new[k])}" for k in old if old[k] != new[k]]
    if not ch:
        raise HTTPException(400, "ဘာမှ မပြောင်းထားပါ")
    audit(db, user.id, "cash_close_edit", "cash_close", str(c.id),
          (f"{c.period_to:%d/%m %H:%M} · " + " · ".join(ch) + f" · {reason}" + (f" · next shift float {float_left:,.0f}" if float_changed and nx is not None else "")
           + (f" · next shift from {c.period_to:%H:%M} (expected {nx.expected_cash:,.0f})" if time_moved and nx is not None else ""))[:500])
    db.commit()
    return _close_out(c, True)


@router.post("/cash-close/{close_id}/reopen")
def cash_close_reopen(close_id: int, db: Session = Depends(get_db), user: User = Depends(require("pos", "billing"))):
    """Undo the LAST hand-over (closed by mistake): its money goes back into the
    open shift, which then runs from the close before it."""
    c = _get_close(db, close_id)
    if (c.status or "closed") != "closed":
        raise HTTPException(400, "ရေတွက်နေဆဲ စာရင်းကို ❌ ပယ်မယ် နဲ့ ပယ်ပါ")
    last = _last_close(db, c.branch_id)
    if last is None or last.id != c.id:
        raise HTTPException(400, "နောက်ဆုံး ပိတ်ထားတဲ့ စာရင်းကိုပဲ ပြန်ဖွင့်လို့ ရပါတယ်")
    audit(db, user.id, "cash_close_reopen", "cash_close", str(c.id),
          f"{c.closed_by} · {c.period_from:%d/%m %H:%M}–{c.period_to:%d/%m %H:%M} · expected {c.expected_cash:,.0f} · counted {c.counted_cash:,.0f} reopened")
    db.delete(c)
    db.commit()
    return {"ok": True}


@router.get("/cash-closes")
def cash_closes(branch_id: int, day: str = "", db: Session = Depends(get_db), _: User = Depends(require("pos", "billing.read", "billing"))):
    """Hand-overs that ended on a day (default today), newest first."""
    from app.models.accounting import CashClose

    try:
        d = date.fromisoformat(day) if day else clock.today()
    except ValueError as e:
        raise HTTPException(400, "day must be YYYY-MM-DD") from e
    rows = (
        db.query(CashClose)
        .filter(CashClose.branch_id == branch_id, CashClose.period_to >= datetime.combine(d, time.min),
                CashClose.period_to < datetime.combine(d + timedelta(days=1), time.min))
        .order_by(CashClose.period_to.desc())
        .all()
    )
    return [_close_out(c, True) for c in rows]


@router.get("/daily-close")
def daily_close(
    branch_id: int,
    date_from: str,
    date_to: str = "",
    db: Session = Depends(get_db),
    _: User = Depends(require("pos", "billing.read", "billing")),
):
    return daily_close_data(db, branch_id, date_from, date_to)


@router.get("/bill-history")
def bill_history(
    branch_id: int,
    q: str = "",
    status: str = "all",
    days: int = 30,
    date_from: str = "",
    date_to: str = "",
    doctor_id: int | None = None,
    discounted: bool = False,
    kind: str = "",
    t_from: str = "",
    t_to: str = "",
    db: Session = Depends(get_db),
    _: User = Depends(require("pos", "billing.read", "billing", "front_desk", "patients.read")),
):
    return _bill_history_rows(db, branch_id, q, status, days, date_from, date_to, doctor_id, discounted, kind, t_from, t_to)


ITEM_HISTORY_ROLES = ("super_admin", "hospital_admin", "branch_admin", "cashier", "accountant")


def _item_history_user(user: User = Depends(get_current_user)) -> User:
    """Item History: Cashier, Owner and Accountant only."""
    if user.role not in ITEM_HISTORY_ROLES:
        raise HTTPException(403, "Not allowed")
    return user


@router.get("/item-history/items")
def item_history_items(
    branch_id: int,
    q: str = "",
    db: Session = Depends(get_db),
    _: User = Depends(_item_history_user),
):
    """Items that were ever put on a bill in this branch, matching the search text."""
    q = q.strip()
    query = (
        db.query(CatalogItem, func.count(InvoiceLine.id))
        .join(InvoiceLine, InvoiceLine.item_id == CatalogItem.id)
        .join(Invoice, InvoiceLine.invoice_id == Invoice.id)
        .filter(Invoice.branch_id == branch_id)
    )
    if q:
        like = f"%{q}%"
        query = query.filter(or_(CatalogItem.name.ilike(like), CatalogItem.name_mm.ilike(like), CatalogItem.sku.ilike(like)))
    rows = query.group_by(CatalogItem.id).order_by(func.count(InvoiceLine.id).desc()).limit(30).all()
    return [
        {"id": it.id, "name": it.name, "name_mm": it.name_mm or "", "sku": it.sku, "category": it.category,
         "unit": it.unit, "price": it.price, "lines": n}
        for it, n in rows
    ]


@router.get("/item-history")
def item_history(
    branch_id: int,
    item_id: int,
    date_from: str = "",
    date_to: str = "",
    doctor_id: int | None = None,
    kind: str = "",
    db: Session = Depends(get_db),
    _: User = Depends(_item_history_user),
):
    """Every bill line of one item: when, which patient / bill, qty, price, amount,
    the bill's status and doctor. Totals split by bill status (void left out of
    the grand total)."""
    item = db.get(CatalogItem, item_id)
    if not item:
        raise HTTPException(404, "Item not found")
    when = func.coalesce(InvoiceLine.created_at, Invoice.created_at)
    query = (
        db.query(InvoiceLine, Invoice, Patient)
        .join(Invoice, InvoiceLine.invoice_id == Invoice.id)
        .outerjoin(Patient, Invoice.patient_id == Patient.id)
        .filter(Invoice.branch_id == branch_id, InvoiceLine.item_id == item_id)
    )
    try:
        if date_from:
            query = query.filter(when >= datetime.combine(date.fromisoformat(date_from), time.min))
        if date_to:
            query = query.filter(when < datetime.combine(date.fromisoformat(date_to) + timedelta(days=1), time.min))
    except ValueError as e:
        raise HTTPException(400, "date must be YYYY-MM-DD") from e
    if doctor_id:
        query = query.filter(or_(
            InvoiceLine.doctor_id == doctor_id,
            and_(InvoiceLine.doctor_id.is_(None), Invoice.doctor_id == doctor_id),
        ))
    if kind and kind != "all":
        query = query.filter(Invoice.kind == kind)
    rows = query.order_by(when.desc(), InvoiceLine.id.desc()).limit(2000).all()
    doc_ids = {(ln.doctor_id or inv.doctor_id) for ln, inv, _ in rows if (ln.doctor_id or inv.doctor_id)}
    docs = {u.id: u.full_name or u.username for u in db.query(User).filter(User.id.in_(doc_ids))} if doc_ids else {}
    out = []
    by_status: dict[str, dict] = {}
    by_doctor: dict[str, dict] = {}
    tot_qty = tot_amt = 0.0
    for ln, inv, pt in rows:
        at = ln.created_at or inv.created_at
        did = ln.doctor_id or inv.doctor_id
        st = inv.status or ""
        qty, amt = float(ln.qty or 0), float(ln.amount or 0)
        is_return = bool(ln.returned_from_line_id) or qty < 0
        out.append({
            "line_id": ln.id,
            "invoice_id": inv.id,
            "number": inv.number,
            "kind": inv.kind,
            "status": st,
            "at": at.isoformat() if at else None,
            "patient_id": inv.patient_id,
            "patient_name": pt.name if pt else "—",
            "uhid": pt.uhid if pt else "—",
            "doctor_id": did,
            "doctor_name": docs.get(did, "") if did else "",
            "description": ln.description,
            "qty": qty,
            "unit_price": float(ln.unit_price or 0),
            "discount": float(ln.discount or 0),
            "amount": amt,
            "is_return": is_return,
        })
        b = by_status.setdefault(st, {"status": st, "lines": 0, "qty": 0.0, "amount": 0.0})
        b["lines"] += 1
        b["qty"] += qty
        b["amount"] += amt
        if st == "void":
            continue
        tot_qty += qty
        tot_amt += amt
        dname = docs.get(did, "") if did else ""
        d = by_doctor.setdefault(dname or "—", {"doctor_id": did, "doctor": dname or "—", "lines": 0, "qty": 0.0, "amount": 0.0})
        d["lines"] += 1
        d["qty"] += qty
        d["amount"] += amt
    order = {"paid": 0, "credit": 1, "partial": 2, "open": 3, "draft": 4, "void": 9}
    return {
        "item": {"id": item.id, "name": item.name, "name_mm": item.name_mm or "", "unit": item.unit,
                 "category": item.category, "price": item.price},
        "rows": out,
        "total_lines": sum(1 for r in out if r["status"] != "void"),
        "total_qty": round(tot_qty, 3),
        "total_amount": round(tot_amt, 2),
        "by_status": sorted(
            ({**v, "qty": round(v["qty"], 3), "amount": round(v["amount"], 2)} for v in by_status.values()),
            key=lambda v: order.get(v["status"], 5),
        ),
        "by_doctor": sorted(
            ({**v, "qty": round(v["qty"], 3), "amount": round(v["amount"], 2)} for v in by_doctor.values()),
            key=lambda v: -v["amount"],
        ),
        "truncated": len(rows) >= 2000,
    }


@router.get("/bill-history/export")
def bill_history_export(
    branch_id: int,
    q: str = "",
    status: str = "all",
    days: int = 30,
    date_from: str = "",
    date_to: str = "",
    doctor_id: int | None = None,
    discounted: bool = False,
    kind: str = "",
    db: Session = Depends(get_db),
    _: User = Depends(require("pos", "billing.read", "billing")),
):
    import openpyxl
    from io import BytesIO
    from fastapi.responses import StreamingResponse

    rows = _bill_history_rows(db, branch_id, q, status, days, date_from, date_to, doctor_id, discounted, kind)
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Bill History"
    ws.append(["Patient ID", "Patient", "Phone", "Type", "Doctor", "Status", "Discount", "Total", "Paid", "Balance", "Paid Via", "Date", "Bill No."])
    for r in rows:
        ws.append([
            r["uhid"], r["patient_name"], r["phone"], r["invoice_kind"],
            r["doctor_name"] or "—", r["status"], r["discount"], r["total"], r["paid"], r["balance"], r["payment_methods"],
            r["created_at"][:19].replace("T", " ") if r["created_at"] else "", r["number"],
        ])
    ws.append([])
    ws.append([
        "", "", "", "", "", "Totals", sum(r["discount"] for r in rows),
        sum(r["total"] for r in rows), sum(r["paid"] for r in rows), sum(r["balance"] for r in rows),
    ])
    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=bill-history.xlsx"},
    )


@router.get("/bill-history/export/pdf")
def bill_history_export_pdf(
    branch_id: int,
    q: str = "",
    status: str = "all",
    days: int = 30,
    date_from: str = "",
    date_to: str = "",
    doctor_id: int | None = None,
    discounted: bool = False,
    kind: str = "",
    db: Session = Depends(get_db),
    _: User = Depends(require("pos", "billing.read", "billing")),
):
    from fastapi.responses import StreamingResponse

    from reportlab.lib import colors

    from app.services.pdf_service import build_table_pdf

    rows = _bill_history_rows(db, branch_id, q, status, days, date_from, date_to, doctor_id, discounted, kind)
    table_rows = [
        [
            r["uhid"],
            r["patient_name"],
            r["invoice_kind"],
            r["doctor_name"] or "—",
            r["status"],
            f"{r['discount']:,.0f}",
            f"{r['total']:,.0f}",
            f"{r['paid']:,.0f}",
            f"{r['balance']:,.0f}",
            r["created_at"][:16].replace("T", " ") if r["created_at"] else "",
            r["number"],
        ]
        for r in rows
    ]
    tot = lambda k: sum(float(r[k] or 0) for r in rows)  # noqa: E731
    buf = build_table_pdf(
        title="Bill History",
        meta=[("Status", status), ("Dates", f"{date_from or '…'} – {date_to or '…'}" if (date_from or date_to) else f"Last {days} days")],
        kpis=[("Bills", f"{len(rows):,}"), ("Total", f"{tot('total'):,.0f}"), ("Paid", f"{tot('paid'):,.0f}"), ("Balance (due)", f"{tot('balance'):,.0f}")],
        columns=["Patient ID", "Patient", "Type", "Doctor", "Status", "Discount", "Total", "Paid", "Balance", "Date", "Bill No."],
        rows=table_rows,
        total=["Total", f"{len(rows)} bills", "", "", "", f"{tot('discount'):,.0f}", f"{tot('total'):,.0f}", f"{tot('paid'):,.0f}", f"{tot('balance'):,.0f}", "", ""],
        highlight={i: colors.HexColor("#b91c1c") for i, r in enumerate(rows) if float(r["balance"] or 0) > 0.01},
        landscape_mode=True,
    )
    return StreamingResponse(
        buf,
        media_type="application/pdf",
        headers={"Content-Disposition": 'attachment; filename="bill-history.pdf"'},
    )


@router.get("/doctors", response_model=list[UserOut])
def counter_doctors(
    specialty: str | None = None,
    db: Session = Depends(get_db),
    _: User = Depends(require("pos", "billing", "front_desk", "patients.read", "management")),
):
    return list_doctors(db, specialty=specialty)


@router.post("/doctors", response_model=UserOut)
def add_doctor(
    data: DoctorIn,
    branch_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require("pos", "billing", "management")),
):
    try:
        doctor = create_doctor(
            db,
            full_name=data.full_name,
            consultation_fee=data.consultation_fee,
            branch_id=branch_id,
            specialty=data.specialty,
            user_id=user.id,
        )
        db.commit()
        db.refresh(doctor)
        return doctor
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@router.patch("/doctors/{doctor_id}", response_model=UserOut)
def patch_doctor(
    doctor_id: int,
    data: DoctorUpdateIn,
    db: Session = Depends(get_db),
    user: User = Depends(require("pos", "billing", "management")),
):
    try:
        doctor = update_doctor(
            db,
            doctor_id,
            full_name=data.full_name,
            consultation_fee=data.consultation_fee,
            specialty=data.specialty,
            user_id=user.id,
        )
        db.commit()
        db.refresh(doctor)
        return doctor
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@router.delete("/doctors/{doctor_id}")
def delete_doctor(
    doctor_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require("pos", "billing", "management")),
):
    try:
        doctor = remove_doctor(db, doctor_id, user_id=user.id)
        db.commit()
        return {"ok": True, "id": doctor.id}
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@router.get("/services")
def counter_list_services(
    department: str,
    db: Session = Depends(get_db),
    _: User = Depends(require("pos", "billing", "management")),
):
    try:
        return list_service_items(db, department)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@router.post("/services")
def counter_create_service(
    data: ServiceItemIn,
    db: Session = Depends(get_db),
    user: User = Depends(require("pos", "billing", "management")),
):
    ensure_catalog_editable()
    try:
        item = create_service_item(db, department=data.department, name=data.name, price=data.price, user_id=user.id)
        db.commit()
        db.refresh(item)
        return item
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@router.patch("/services/{item_id}")
def counter_update_service(
    item_id: int,
    data: ServiceItemUpdateIn,
    db: Session = Depends(get_db),
    user: User = Depends(require("pos", "billing", "management")),
):
    ensure_catalog_editable()
    try:
        item = update_service_item(
            db,
            item_id,
            name=data.name,
            price=data.price,
            is_active=data.is_active,
            user_id=user.id,
        )
        db.commit()
        db.refresh(item)
        return item
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


def _resolve_analytics(db: Session, branch_id: int | None, period: str, date_from: str | None, date_to: str | None) -> dict:
    """Owner Panel: an offline site's branch id reads that site's uploaded copy,
    and "All Branches" (None) adds every site to the cloud's own numbers."""
    site = clinic_view.site_for(branch_id)
    if site:
        if not clinic_view.available(site):
            raise HTTPException(404, f"{site.title()} data has not been synced yet")
        with clinic_view.clinic_session(site) as cdb:
            return clinic_view.shift_doctor_ids(_resolve_local(cdb, clinic_view.LOCAL_ID, period, date_from, date_to), site)
    if branch_id is None and clinic_view.available_sites():
        extra = []
        for s in clinic_view.available_sites():
            try:
                with clinic_view.clinic_session(s) as cdb:
                    extra.append(clinic_view.shift_doctor_ids(_resolve_local(cdb, clinic_view.LOCAL_ID, period, date_from, date_to), s))
            except HTTPException:
                raise
            except Exception:  # noqa: BLE001 — a bad site upload must not break the other branches' numbers
                pass
        return _resolve_local(db, None, period, date_from, date_to, extra=extra or None)
    return _resolve_local(db, branch_id, period, date_from, date_to)


def _resolve_local(
    db: Session, branch_id: int | None, period: str, date_from: str | None, date_to: str | None, extra: list[dict] | None = None,
) -> dict:
    if date_from and date_to:
        try:
            start = datetime.fromisoformat(date_from)
            end = datetime.fromisoformat(date_to).replace(hour=23, minute=59, second=59)
        except ValueError as e:
            raise HTTPException(400, "date_from/date_to must be YYYY-MM-DD") from e
        if end < start:
            raise HTTPException(400, "date_to must be on or after date_from")
        if branch_id is None:
            return cashier_analytics_combined(db, date_from=date_from, date_to=date_to, extra=extra)
        return cashier_analytics_range(db, branch_id, start, end)
    if period not in {"day", "month", "3m", "year"}:
        raise HTTPException(400, "period must be day, month, 3m, or year")
    if branch_id is None:
        return cashier_analytics_combined(db, period, extra=extra)
    return cashier_analytics(db, branch_id, period)


@router.get("/analytics/doctor-patients")
def counter_analytics_doctor_patients(
    branch_id: int,
    doctor_id: int,
    period: str = "month",
    date_from: str | None = None,
    date_to: str | None = None,
    kind: str = "",
    db: Session = Depends(get_db),
    _: User = Depends(require("pos", "billing", "accounts", "analytics", "reports")),
):
    if date_from and date_to:
        try:
            since = datetime.fromisoformat(date_from)
            until = datetime.fromisoformat(date_to).replace(hour=23, minute=59, second=59)
        except ValueError as e:
            raise HTTPException(400, "date_from/date_to must be YYYY-MM-DD") from e
    else:
        if period not in {"day", "month", "3m", "year"}:
            raise HTTPException(400, "period must be day, month, 3m, or year")
        since, until = _period_start(period), clock.now()
    site = clinic_view.site_for(branch_id)
    if site:
        if not clinic_view.available(site):
            return []
        with clinic_view.clinic_session(site) as cdb:
            off = clinic_view.offset(site)
            local_doctor = doctor_id - off if doctor_id >= off else doctor_id
            return doctor_patients(cdb, clinic_view.LOCAL_ID, local_doctor, since=since, until=until, kind=kind)
    return doctor_patients(db, branch_id, doctor_id, since=since, until=until, kind=kind)


@router.get("/analytics/lab-by-doctor")
def counter_analytics_lab_by_doctor(
    branch_id: int,
    period: str = "month",
    kind: str = "lab",
    date_from: str | None = None,
    date_to: str | None = None,
    db: Session = Depends(get_db),
    _: User = Depends(require("pos", "billing", "accounts", "analytics", "reports")),
):
    if date_from and date_to:
        try:
            since = datetime.fromisoformat(date_from)
            until = datetime.fromisoformat(date_to).replace(hour=23, minute=59, second=59)
        except ValueError as e:
            raise HTTPException(400, "date_from/date_to must be YYYY-MM-DD") from e
    else:
        if period not in {"day", "month", "3m", "year"}:
            raise HTTPException(400, "period must be day, month, 3m, or year")
        since, until = _period_start(period), clock.now()
    try:
        return lab_by_doctor(db, branch_id, since=since, until=until, kind=kind)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@router.get("/analytics")
def counter_analytics(
    branch_id: int | None = None,
    period: str = "month",
    date_from: str | None = None,
    date_to: str | None = None,
    db: Session = Depends(get_db),
    _: User = Depends(require("pos", "billing", "accounts", "analytics", "reports")),
):
    # branch_id omitted/None = "All Branches" combined view (Owner Panel only —
    # every other caller of this endpoint, e.g. Cashier's own Analyze tab,
    # always passes its own branch_id).
    return _resolve_analytics(db, branch_id, period, date_from, date_to)


@router.get("/analytics/export")
def counter_analytics_export(
    branch_id: int | None = None,
    period: str = "month",
    date_from: str | None = None,
    date_to: str | None = None,
    db: Session = Depends(get_db),
    _: User = Depends(require("pos", "billing", "accounts", "analytics", "reports")),
):
    import openpyxl
    from io import BytesIO
    from fastapi.responses import StreamingResponse

    a = _resolve_analytics(db, branch_id, period, date_from, date_to)
    wb = openpyxl.Workbook()

    ws = wb.active
    ws.title = "Summary"
    ws.append(["Period", a.get("period_label", "")])
    ws.append(["From", str(a.get("from_date", ""))[:19].replace("T", " ")])
    ws.append(["To", str(a.get("to_date", ""))[:19].replace("T", " ")])
    ws.append([])
    ws.append(["Cash In", a.get("total_collected", 0)])
    ws.append(["Today's Billing", a.get("total_billed", 0)])
    ws.append(["Expenses", a.get("total_expenses", 0)])
    ws.append(["Net", a.get("net", 0)])
    ws.append(["Outstanding", a.get("outstanding", 0)])
    ws.append([])
    ws.append(["Total Bills", a.get("total_bills", 0)])
    ws.append(["Paid Bills", a.get("paid_bills", 0)])
    ws.append(["Avg per Paid Bill", a.get("avg_bill", 0)])

    ws2 = wb.create_sheet("Payment Methods")
    ws2.append(["Method", "Amount"])
    for p in a.get("payment_methods", []) or []:
        ws2.append([p.get("method", ""), p.get("amount", 0)])

    ws3 = wb.create_sheet("By Department")
    ws3.append(["Department", "Amount"])
    for s in a.get("by_source", []) or []:
        ws3.append([s.get("source", ""), s.get("amount", 0)])

    ws4 = wb.create_sheet("Doctor Income")
    ws4.append(["Doctor", "Bills", "Billed", "Fee Income"])
    for d in a.get("by_doctor", []) or []:
        ws4.append([d.get("doctor", ""), d.get("bills", 0), d.get("billed", 0), d.get("fee_income", 0)])

    ws4r = wb.create_sheet("Round Fee")
    ws4r.append(["Doctor", "Rounds", "Bills", "Round Fee"])
    for d in a.get("by_doctor_round", []) or []:
        ws4r.append([d.get("doctor", ""), d.get("rounds", 0), d.get("bills", 0), d.get("fee_income", 0)])

    ws4s = wb.create_sheet("OT Doctor Fee")
    ws4s.append(["Doctor", "Operations", "Bills", "OT Doctor Fee"])
    for d in a.get("by_doctor_surgery", []) or []:
        ws4s.append([d.get("doctor", ""), d.get("ops", 0), d.get("bills", 0), d.get("fee_income", 0)])

    ws5 = wb.create_sheet("Expenses")
    ws5.append(["Date", "Category", "Paid From", "Notes", "Amount"])
    for e in a.get("expense_details", []) or []:
        ws5.append([
            str(e.get("created_at", ""))[:19].replace("T", " "),
            e.get("category", ""),
            e.get("paid_from", ""),
            e.get("notes", ""),
            e.get("amount", 0),
        ])
    ws5.append([])
    ws5.append(["", "", "", "Total", a.get("total_expenses", 0)])

    ws6 = wb.create_sheet("Trend")
    ws6.append(["Label", "Amount"])
    for t in a.get("trend", []) or []:
        ws6.append([t.get("label", ""), t.get("amount", 0)])

    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=analytics-report.xlsx"},
    )


@router.get("/analytics/export/pdf")
def counter_analytics_export_pdf(
    branch_id: int | None = None,
    period: str = "month",
    date_from: str | None = None,
    date_to: str | None = None,
    db: Session = Depends(get_db),
    _: User = Depends(require("pos", "billing", "accounts", "analytics", "reports")),
):
    from io import BytesIO

    from fastapi.responses import StreamingResponse

    from app.services.pdf_service import (
        header_elements, kpi_grid, new_pdf_document, section, side_by_side, styled_table,
    )

    a = _resolve_analytics(db, branch_id, period, date_from, date_to)
    branch = ""
    if branch_id:
        site = clinic_view.site_for(branch_id)
        b_obj = None if site else db.get(Branch, branch_id)
        branch = site.title() if site else (b_obj.name if b_obj else "")

    def money(v) -> str:
        return f"{float(v or 0):,.0f}"

    def pct(v, total) -> str:
        return f"{(float(v or 0) / total * 100):.0f}%" if total else "—"

    buf = BytesIO()
    doc = new_pdf_document(buf)
    W = doc.width
    meta = [("Period", str(a.get("period_label", ""))), ("From", str(a.get("from_date", ""))[:10]), ("To", str(a.get("to_date", ""))[:10])]
    if branch:
        meta.insert(0, ("Branch", branch))
    el = header_elements("Sales Report", meta, width=W)

    el.append(kpi_grid([
        ("Cash In", money(a.get("total_collected"))),
        ("Billed", money(a.get("total_billed"))),
        ("Expenses", money(a.get("total_expenses"))),
        ("Net", money(a.get("net"))),
        ("Outstanding", money(a.get("outstanding"))),
        ("Bills (paid / total)", f"{a.get('paid_bills', 0)} / {a.get('total_bills', 0)}"),
        ("Avg per paid bill", money(a.get("avg_bill"))),
        ("Refunded", money(a.get("refunded"))),
    ], width=W))

    half = (W - 8) / 2
    pm = a.get("payment_methods") or []
    pm_total = sum(float(x.get("amount") or 0) for x in pm)
    t_pay = styled_table(
        ["Payment method", "Amount", "%"],
        [[x.get("method", ""), money(x.get("amount")), pct(x.get("amount"), pm_total)] for x in pm],
        total=["Total", money(pm_total), "100%" if pm_total else "—"], width=half,
    )
    src = a.get("by_source") or []
    src_total = sum(float(x.get("amount") or 0) for x in src)
    t_src = styled_table(
        ["Department", "Amount", "%"],
        [[x.get("source", ""), money(x.get("amount")), pct(x.get("amount"), src_total)] for x in src],
        total=["Total", money(src_total), "100%" if src_total else "—"], width=half,
    )
    el.append(side_by_side(section("Payment Methods", t_pay), section("By Department", t_src), width=W))

    kinds = a.get("by_invoice_kind") or []
    t_kind = styled_table(
        ["Visit type", "Bills", "Billed", "Collected"],
        [[x.get("label") or str(x.get("kind", "")).upper(), x.get("bills", 0), money(x.get("billed")), money(x.get("collected"))] for x in kinds],
        total=["Total", sum(int(x.get("bills") or 0) for x in kinds), money(sum(float(x.get("billed") or 0) for x in kinds)), money(sum(float(x.get("collected") or 0) for x in kinds))],
        width=half,
    )
    pp = a.get("pharmacy_profit") or {}
    t_pp = styled_table(
        ["Pharmacy", "Amount"],
        [["Revenue", money(pp.get("revenue"))], ["Cost", money(pp.get("cost"))], ["Margin", f"{pp.get('margin_pct', 0)}%"]],
        total=["Profit", money(pp.get("profit"))], width=half,
    )
    el.append(side_by_side(section("By Visit Type", t_kind), section("Pharmacy Profit", t_pp), width=W))

    docs = a.get("by_doctor") or []
    if docs:
        def fee(d, k):
            n = int(d.get(f"fee_{k}_n") or 0)
            return f"{n} · {money(d.get(f'fee_{k}_amount'))}" if n else "—"
        t_doc = styled_table(
            ["Doctor", "Bills", "Billed", "Normal", "Night", "Half", "FOC", "Fee income"],
            [[d.get("doctor", ""), d.get("bills", 0), money(d.get("billed")), fee(d, "normal"), fee(d, "night"), fee(d, "half"), str(int(d.get("fee_foc_n") or 0) or "—"), money(d.get("fee_income"))] for d in docs],
            total=["Total", sum(int(d.get("bills") or 0) for d in docs), money(sum(float(d.get("billed") or 0) for d in docs)), "", "", "", "", money(sum(float(d.get("fee_income") or 0) for d in docs))],
            width=W,
        )
        el.append(section("Doctor Income", t_doc))

    rds = a.get("by_doctor_round") or []
    if rds:
        t_rd = styled_table(
            ["Doctor", "Rounds", "Bills", "Round fee"],
            [[d.get("doctor", ""), f"{float(d.get('rounds') or 0):g}", d.get("bills", 0), money(d.get("fee_income"))] for d in rds],
            total=["Total", f"{sum(float(d.get('rounds') or 0) for d in rds):g}", sum(int(d.get("bills") or 0) for d in rds), money(sum(float(d.get("fee_income") or 0) for d in rds))],
            width=W,
        )
        el.append(section("Doctor Round Fee (IPD)", t_rd))

    sgs = a.get("by_doctor_surgery") or []
    if sgs:
        t_sg = styled_table(
            ["Doctor", "Operations", "Bills", "OT doctor fee"],
            [[d.get("doctor", ""), d.get("ops", 0), d.get("bills", 0), money(d.get("fee_income"))] for d in sgs],
            total=["Total", sum(int(d.get("ops") or 0) for d in sgs), sum(int(d.get("bills") or 0) for d in sgs), money(sum(float(d.get("fee_income") or 0) for d in sgs))],
            width=W,
        )
        el.append(section("OT Doctor Fee", t_sg))

    cats = a.get("by_expense_category") or []
    if cats:
        t_exp = styled_table(
            ["Expense category", "Count", "Amount"],
            [[c.get("category", ""), c.get("count", 0), money(c.get("amount"))] for c in cats],
            total=["Total", sum(int(c.get("count") or 0) for c in cats), money(sum(float(c.get("amount") or 0) for c in cats))],
            width=W,
        )
        el.append(section("Expenses", t_exp))

    doc.build(el)
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/pdf",
        headers={"Content-Disposition": 'attachment; filename="sales-report.pdf"'},
    )


# Activity Log: plain labels (Myanmar, English) + a group per audit action (unknown actions fall
# back to their code name, grouped under "other").
ACTIVITY_ACTIONS: dict[str, tuple[str, str, str]] = {
    "payment": ("money", "ငွေရှင်း", "Payment"), "multi_payment": ("money", "ငွေရှင်း (ခွဲပေး)", "Payment (split)"), "refund": ("money", "ငွေပြန်အမ်း (Refund)", "Refund"),
    "void_line": ("money", "Bill item ဖျက်", "Bill item removed"), "history_edit_line": ("money", "Bill item ပြင်/ဖျက်", "Bill item edited"), "history_add_item": ("money", "Bill item ထပ်ထည့်", "Bill item added"), "edit_line": ("money", "Bill item ဈေး/အရေအတွက် ပြင်", "Bill item edited"), "add_line": ("money", "Bill item ထည့်", "Bill item added"),
    "return_line": ("money", "Item ပြန်အမ်း (Return)", "Item returned"), "exchange_line": ("money", "Item လဲ (Exchange)", "Item exchanged"), "discount": ("money", "Discount ပေး", "Discount given"),
    "cashier_order": ("money", "Cashier မှ order ထည့်", "Order added at Cashier"), "cashier_add_pharmacy_line": ("money", "Cashier မှ ဆေးထည့်", "Medicine added at Cashier"), "ipd_deposit": ("money", "IPD Deposit ကောက်", "IPD deposit"),
    "reception_register": ("patient", "လူနာ Register", "Patient registered"), "patient_edit": ("patient", "✏️ လူနာ အချက်အလက် ပြင်", "Patient details edited"), "convert_ipd_to_opd": ("patient", "IPD → OPD ပြောင်း", "IPD → OPD"), "convert_opd_to_ipd": ("patient", "OPD → IPD ပြောင်း", "OPD → IPD"), "ipd_admit": ("patient", "IPD Admit", "IPD admitted"),
    "ipd_assign_bed": ("patient", "Room/Bed ပေး", "Room/Bed assigned"), "ipd_home_stay": ("patient", "🏠 Home stay", "Home stay"), "ipd_transfer": ("patient", "Bed ပြောင်း", "Bed transfer"), "ipd_discharge": ("patient", "ဆေးရုံဆင်း", "Discharged"), "add_doctor_fee": ("money", "ဆရာဝန် ထပ်ထည့်", "Doctor added to bill"), "add_round_fee": ("money", "Round fee ထည့်", "Round fee added"), "change_paid_date": ("money", "ငွေရှင်းရက် ပြောင်း", "Paid date changed"), "change_payment_date": ("money", "Deposit ရက် ပြောင်း", "Payment date changed"), "edit_payment": ("money", "Deposit ငွေပမာဏ ပြင်", "Payment amount edited"), "reopen_bill": ("money", "Open Bill ပြန်ဖွင့်", "Bill reopened"), "cash_close": ("money", "🔒 စာရင်းပိတ်", "Cash closed (shift)"), "cash_close_start": ("money", "✂️ စာရင်းဖြတ် (ရေတွက်ဆဲ)", "Cash cut-off (counting)"), "cash_close_cancel": ("money", "❌ စာရင်းဖြတ် ပယ်", "Cash cut-off cancelled"), "cash_close_edit": ("money", "✏️ ပိတ်စာရင်း ပြင်", "Cash close edited"), "cash_close_reopen": ("money", "↩ ပိတ်စာရင်း ပြန်ဖွင့်", "Cash close reopened"), "cash_close_auto": ("money", "🕛 ည ၁၂ နာရီ auto စာရင်းဖြတ် (ရေတွက်ရန်)", "Midnight auto cut-off (to count)"), "credit_close": ("money", "📒 အကြွေးနဲ့ ပိတ်", "Closed on credit"), "close_bill": ("money", "✅ Bill ပိတ် (ကျန်ငွေ 0)", "Bill closed (nothing due)"), "invoice_delete": ("money", "🗑 Voucher ဖျက်", "Voucher deleted"), "invoice_unvoid": ("money", "↩ Void ပြန်ဖျက်", "Void undone"), "credit_cancel": ("money", "↩ အကြွေး ပယ်", "Credit cancelled"), "petty_topup": ("money", "Petty cash ဖြည့်", "Petty cash top-up"), "line_order": ("money", "Item အစဉ် ပြောင်း", "Item order changed"), "add_surgery_fee": ("money", "OT ဆရာဝန်ခ ထည့်", "OT doctor fee added"), "item_delete": ("stock", "Item ဖျက်", "Item deleted"), "item_deactivate": ("stock", "Item ပိတ်", "Item deactivated"), "item_restore": ("stock", "Item ပြန်ဖွင့်", "Item restored"), "item_unlimited": ("stock", "Unlimited / Stock ပြောင်း", "Unlimited / stock changed"), "result_format_save": ("order", "Result format မှတ်", "Result format saved"), "result_format_delete": ("order", "Result format ဖျက်", "Result format deleted"), "ipd_discharge_approve": ("patient", "ဆင်းခွင့်ပြု (ဆရာဝန်)", "Discharge approved"), "ipd_discharge_unapprove": ("patient", "ဆင်းခွင့် ပြန်ရုပ်", "Discharge approval cancelled"),
    "doctor_examine": ("patient", "ဆရာဝန် စမ်းသပ်", "Doctor examination"), "vitals_record": ("patient", "Vitals မှတ်", "Vitals recorded"),
    "lab_order": ("order", "Lab order", "Lab order"), "lab_walk_in": ("order", "Lab walk-in", "Lab walk-in"), "lab_result": ("order", "Lab result ထည့်", "Lab result entered"),
    "xray_order": ("order", "X-Ray order", "X-Ray order"), "usg_order": ("order", "USG order", "USG order"), "radiology_result": ("order", "X-Ray/USG result ထည့်", "X-Ray/USG result entered"),
    "ward_med_order_create": ("order", "Ward ဆေး order", "Ward medicine order"), "ward_med_order_cancel": ("order", "Ward ဆေး order ပယ်ဖျက်", "Ward medicine order cancelled"),
    "pharmacy_dispense": ("order", "ဆေးထုတ်ပေး", "Medicine dispensed"), "pharmacy_walk_in_sale": ("order", "ဆေး walk-in ရောင်း", "Pharmacy walk-in sale"), "pharmacy_line_qty": ("order", "ဆေးအရေအတွက် ပြင်", "Medicine qty changed"), "pharmacy_line_remove": ("order", "ဆေး order ထဲက ဖယ်", "Medicine removed from order"),
    "dispense": ("stock", "Stock ထဲမှ ထုတ်", "Stock out (dispense)"), "stock_receive": ("stock", "Stock In", "Stock in"), "stock_count": ("stock", "Stock စစ်/ညှိ", "Stock count / adjust"),
    "medicine_create": ("stock", "Stock item အသစ်", "New stock item"),
    "staff_create": ("admin", "Staff account ဖွင့်", "Staff account created"), "staff_update": ("admin", "Staff account ပြင်", "Staff account updated"), "staff_reset_password": ("admin", "Staff password reset", "Staff password reset"),
    "change_password": ("admin", "Password ပြောင်း", "Password changed"), "doctor_create": ("admin", "ဆရာဝန် ထည့်", "Doctor added"), "doctor_update": ("admin", "ဆရာဝန် ပြင်", "Doctor updated"), "doctor_delete": ("admin", "ဆရာဝန် ဖျက်", "Doctor removed"), "expense_edit": ("money", "Expense ပြင်", "Expense edited"), "expense_delete": ("money", "Expense ဖျက်", "Expense deleted"), "invoice_note": ("money", "Voucher note", "Voucher note set"), "history_add_item": ("money", "History မှ item ထပ်ထည့်", "Item added from History"), "invoice_void": ("money", "Bill ပယ်", "Bill voided"), "payment_void": ("money", "ငွေပေးချေမှု ပယ်", "Payment voided"), "stock_receive_undo": ("stock", "Stock In ပယ်", "Stock in undone"), "stock_out_undo": ("stock", "Stock Out ပယ်", "Stock out undone"), "supplier_delete": ("stock", "Supplier ဖျက်", "Supplier deleted"), "patient_merge": ("patient", "လူနာ ပေါင်း", "Patients merged"), "patient_delete": ("patient", "လူနာ ဖျက်", "Patient deleted"),
    "service_item_create": ("admin", "Test/Service ဈေး ထည့်", "Test/Service price added"), "service_item_update": ("admin", "Test/Service ဈေး ပြင်", "Test/Service price updated"),
    "ward_create": ("admin", "Ward ထည့်", "Ward added"), "ward_update": ("admin", "Ward ပြင်", "Ward updated"), "bed_create": ("admin", "Bed/Room ထည့်", "Bed/Room added"), "bed_update": ("admin", "Bed/Room ပြင်", "Bed/Room updated"), "bed_delete": ("admin", "Bed/Room ဖျက်", "Bed/Room deleted"), "ward_delete": ("admin", "Ward ဖျက်", "Ward deleted"),
    "branch_create": ("admin", "Branch ထည့်", "Branch added"), "branch_update": ("admin", "Branch ပြင်", "Branch updated"),
    "capital_asset_create": ("admin", "Capital asset ထည့်", "Capital asset added"), "capital_asset_delete": ("admin", "Capital asset ဖျက်", "Capital asset removed"),
    "developer_user_create": ("admin", "User ဖွင့် (Developer)", "User created (Developer)"), "developer_user_update": ("admin", "User ပြင် (Developer)", "User updated (Developer)"),
    "developer_reset_password": ("admin", "Password reset (Developer)", "Password reset (Developer)"),
}


@router.get("/activity")
def activity_log(
    limit: int = 200,
    q: str = "",
    group: str = "",
    date_from: str = "",
    date_to: str = "",
    branch_id: int | None = None,
    v: int = 1,
    user_id: int | None = None,
    invoice_id: int | None = None,
    offset: int = 0,
    db: Session = Depends(get_db),
    _: User = Depends(require("pos", "billing.read", "front_desk", "pharmacy", "radiology", "patients.read")),
):
    """Who did what. Scoped to one branch by the staff member's branch (branch-
    bound staff always get their own — see BranchScopeMiddleware); searchable
    by text, filterable by group and date range.

    v=2 → readable rows (patient, bill, names instead of ids, twins hidden),
    paged with offset, `group=alert` for the ⚠ list, `invoice_id` for one
    bill's whole story (oldest first)."""
    if v >= 2:
        from app.services.activity_view import activity_rows

        return activity_rows(
            db, branch_id=branch_id, q=q, group=group, date_from=date_from, date_to=date_to, user_id=user_id,
            invoice_id=invoice_id, limit=limit, offset=offset, labels=ACTIVITY_ACTIONS,
        )
    query = db.query(AuditLog, User).outerjoin(User, AuditLog.user_id == User.id)
    if branch_id is not None:
        query = query.filter(User.branch_id == branch_id)
    if group:
        actions = [a for a, (g, *_labels) in ACTIVITY_ACTIONS.items() if g == group]
        query = query.filter(AuditLog.action.in_(actions)) if group != "other" else query.filter(~AuditLog.action.in_(list(ACTIVITY_ACTIONS)))
    if date_from:
        query = query.filter(AuditLog.created_at >= datetime.fromisoformat(date_from))
    if date_to:
        query = query.filter(AuditLog.created_at < datetime.fromisoformat(date_to) + timedelta(days=1))
    q = q.strip()
    if q:
        like = f"%{q}%"
        label_hits = [a for a, (_g, *labels) in ACTIVITY_ACTIONS.items() if any(q.lower() in lb.lower() for lb in labels)]
        query = query.filter(or_(
            AuditLog.detail.ilike(like), AuditLog.action.ilike(like), AuditLog.entity.ilike(like),
            AuditLog.entity_id.ilike(like), User.full_name.ilike(like), User.username.ilike(like),
            AuditLog.action.in_(label_hits),
        ))
    rows = query.order_by(AuditLog.id.desc()).limit(min(max(limit, 1), 1000)).all()
    return [
        {
            "id": log.id,
            "action": log.action,
            "label": ACTIVITY_ACTIONS.get(log.action, ("other", log.action.replace("_", " ")))[1],
            "label_en": ACTIVITY_ACTIONS.get(log.action, ("other", "", log.action.replace("_", " ").capitalize()))[2],
            "group": ACTIVITY_ACTIONS.get(log.action, ("other",))[0],
            "entity": log.entity,
            "entity_id": log.entity_id,
            "detail": log.detail,
            "user_name": user.full_name if user else "—",
            "created_at": log.created_at.isoformat() if log.created_at else None,
        }
        for log, user in rows
    ]


@router.get("/activity-users")
def activity_staff(
    branch_id: int | None = None,
    db: Session = Depends(get_db),
    _: User = Depends(require("pos", "billing.read", "front_desk", "pharmacy", "radiology", "patients.read")),
):
    from app.services.activity_view import activity_users

    return activity_users(db, branch_id)


@router.get("/credit")
def credit_book(
    branch_id: int,
    month: str = "",
    state: str = "owing",
    q: str = "",
    date_from: str = "",
    date_to: str = "",
    db: Session = Depends(get_db),
    _: User = Depends(_item_history_user),
):
    """📒 Credit book. `month` (YYYY-MM) picks bills put on credit that month and
    sums what came back that month; empty = every month. `state`: owing / paid / all."""
    from app.models.billing import Payment

    m_from = m_to = None
    if month:
        try:
            y, mo = (int(x) for x in month.split("-")[:2])
            m_from = datetime(y, mo, 1)
            m_to = datetime(y + (mo == 12), mo % 12 + 1, 1)
        except ValueError as e:
            raise HTTPException(400, "month must be YYYY-MM") from e
    if date_from:  # a day (or a range of days) instead of a month
        try:
            m_from = datetime.fromisoformat(date_from[:10])
            m_to = datetime.fromisoformat((date_to or date_from)[:10]) + timedelta(days=1)
        except ValueError as e:
            raise HTTPException(400, "date_from / date_to must be YYYY-MM-DD") from e
    query = (
        db.query(Invoice, Patient)
        .outerjoin(Patient, Invoice.patient_id == Patient.id)
        .options(joinedload(Invoice.payments))
        .filter(Invoice.branch_id == branch_id, Invoice.credit_at.isnot(None), Invoice.status != "void")
    )
    if m_from is not None:
        query = query.filter(Invoice.credit_at >= m_from, Invoice.credit_at < m_to)
    for term in re.split(r"[,\s]+", q.strip()):
        if term:
            like = f"%{term}%"
            query = query.filter(
                Patient.name.ilike(like) | Patient.uhid.ilike(like) | Patient.phone.ilike(like)
                | Invoice.number.ilike(like) | Invoice.credit_name.ilike(like) | Invoice.credit_phone.ilike(like)
            )
    today = clock.today()
    rows = []
    for inv, pt in query.order_by(Invoice.credit_at.desc()).all():
        back = [p for p in inv.payments if p.created_at and p.created_at > inv.credit_at]
        got = sum(float(p.amount or 0) * (-1 if p.method == "refund" else 1) for p in back)
        owing = float(inv.balance or 0) > 0.01
        st = "owing" if owing and got <= 0.01 else ("part" if owing else "paid")
        if state == "owing" and not owing or state == "paid" and owing:
            continue
        rows.append({
            "id": inv.id, "number": inv.number, "patient_id": inv.patient_id,
            "patient": pt.name if pt else "—", "uhid": pt.uhid if pt else "", "phone": pt.phone if pt else "",
            "credit_at": inv.credit_at.isoformat(timespec="seconds"), "name": inv.credit_name or "",
            "credit_phone": inv.credit_phone or "", "due": inv.credit_due.isoformat() if inv.credit_due else None,
            "note": inv.credit_note or "", "total": float(inv.total or 0),
            "paid_before": round(float(inv.total or 0) - float(inv.credit_amount or 0), 2),
            "credit": float(inv.credit_amount or 0), "back": round(got, 2), "balance": float(inv.balance or 0),
            "last_back": max((p.created_at for p in back), default=None) and max(p.created_at for p in back).isoformat(timespec="seconds"),
            "state": st, "overdue": bool(owing and inv.credit_due and inv.credit_due < today),
        })
    # Month summary: put on credit / paid back (any month's credit) / still owed.
    base = db.query(Invoice).filter(Invoice.branch_id == branch_id, Invoice.credit_at.isnot(None), Invoice.status != "void")
    sold_q = base.filter(Invoice.credit_at >= m_from, Invoice.credit_at < m_to) if m_from is not None else base
    sold = float(sum(float(i.credit_amount or 0) for i in sold_q))
    pq = (
        db.query(Payment, Invoice)
        .join(Invoice, Payment.invoice_id == Invoice.id)
        .filter(Invoice.branch_id == branch_id, Invoice.credit_at.isnot(None), Invoice.status != "void",
                Payment.created_at > Invoice.credit_at)
    )
    if m_from is not None:
        pq = pq.filter(Payment.created_at >= m_from, Payment.created_at < m_to)
    back_all = sum(float(p.amount or 0) * (-1 if p.method == "refund" else 1) for p, _ in pq)
    owing_now = float(sum(float(i.balance or 0) for i in base.filter(Invoice.status == "credit")))
    return {
        "month": month, "rows": rows,
        "summary": {
            "sold": round(sold, 2), "sold_count": sold_q.count(), "back": round(back_all, 2),
            "owing_month": round(float(sum(float(i.balance or 0) for i in sold_q.filter(Invoice.status == "credit"))), 2),
            "owing_all": round(owing_now, 2), "owing_all_count": base.filter(Invoice.status == "credit").count(),
        },
    }

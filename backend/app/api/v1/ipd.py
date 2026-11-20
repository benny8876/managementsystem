from datetime import date, datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core import clock
from app.core.deps import require
from app.db.session import get_db
from app.models.ancillary import LabOrder, RadiologyOrder
from app.models.billing import Invoice
from app.models.catalog import CatalogItem
from app.models.ipd import Admission, Bed, BedTransferLog, NursingNote, VitalSign, Ward, WardMedOrder, WardMedOrderItem
from app.models.patients import Patient
from app.models.users import User
from app.schemas.actions import AdmitIn, DischargeIn, IpdDepositIn, NoteIn, TransferIn, VitalsIn
from app.schemas.common import InvoiceOut, OrderItemIn
from app.services.billing_service import collect_ipd_deposit, create_invoice, ensure_daily_room_charges, move_today_room_charge, recalc_invoice
from app.services.utils import audit

router = APIRouter(prefix="/ipd", tags=["ipd"])


@router.get("/dashboard")
def ipd_dashboard(branch_id: int, db: Session = Depends(get_db), _: User = Depends(require("ipd", "ipd.read", "nursing", "patients.read"))):
    wards = db.query(Ward).filter(Ward.branch_id == branch_id, Ward.is_active != False).order_by(Ward.name).all()  # noqa: E712
    out = []
    charged_any = False
    for ward in wards:
        beds = db.query(Bed).filter(Bed.ward_id == ward.id, Bed.status != "removed").order_by(Bed.code).all()
        bed_rows = []
        for bed in beds:
            admission_payload = None
            if bed.status == "occupied":
                adm = (
                    db.query(Admission)
                    .filter(Admission.bed_id == bed.id, Admission.status.in_(["admitted", "transferred"]))
                    .order_by(Admission.id.desc())
                    .first()
                )
                if adm:
                    if ensure_daily_room_charges(db, adm):
                        charged_any = True
                    patient = db.get(Patient, adm.patient_id)
                    inv = db.query(Invoice).filter(Invoice.admission_id == adm.id).order_by(Invoice.id.desc()).first()
                    lines_out = []
                    lab_orders_out = []
                    radiology_orders_out = []
                    if inv:
                        for line in inv.lines:
                            category = None
                            if line.item_id:
                                item = db.get(CatalogItem, line.item_id)
                                category = item.category if item else None
                            lines_out.append(
                                {
                                    "id": line.id,
                                    "description": line.description,
                                    "source": line.source,
                                    "category": category,
                                    "qty": line.qty,
                                    "unit_price": line.unit_price,
                                    "amount": line.amount,
                                }
                            )
                        lab_orders_out = [
                            {"order_id": o.id, "tests": o.tests, "status": o.status}
                            for o in db.query(LabOrder).filter(LabOrder.invoice_id == inv.id).order_by(LabOrder.id).all()
                        ]
                        radiology_orders_out = [
                            {"order_id": o.id, "modality": o.modality, "status": o.status}
                            for o in db.query(RadiologyOrder).filter(RadiologyOrder.invoice_id == inv.id).order_by(RadiologyOrder.id).all()
                        ]
                    admission_payload = {
                        "admission_id": adm.id,
                        "patient_id": adm.patient_id,
                        "patient_name": patient.name if patient else "",
                        "uhid": patient.uhid if patient else "",
                        "admitted_at": adm.admitted_at.isoformat() if adm.admitted_at else None,
                        "diagnosis": adm.diagnosis or "",
                        "billing_mode": adm.billing_mode,
                        "discharge_approved_at": adm.discharge_approved_at.isoformat() if adm.discharge_approved_at else None,
                        "discharge_type": adm.discharge_type or "",
                        "discharge_summary": adm.discharge_summary or "",
                        "invoice_id": inv.id if inv else None,
                        "invoice_number": inv.number if inv else None,
                        "invoice_status": inv.status if inv else None,
                        "total": inv.total if inv else 0,
                        "subtotal": inv.subtotal if inv else 0,
                        "balance": inv.balance if inv else 0,
                        "deposit_paid": inv.paid if inv else 0,
                        "lines": lines_out,
                        "lab_orders": lab_orders_out,
                        "radiology_orders": radiology_orders_out,
                    }
            bed_rows.append(
                {
                    "id": bed.id,
                    "code": bed.code,
                    "status": bed.status,
                    "daily_rate": bed.daily_rate,
                    "hourly_rate": bed.hourly_rate,
                    "package_rate": bed.package_rate,
                    "admission": admission_payload,
                }
            )
        out.append(
            {
                "ward": {
                    "id": ward.id,
                    "name": ward.name,
                    "category": ward.category,
                    "floor": ward.floor,
                },
                "beds": bed_rows,
            }
        )
    if charged_any:
        db.commit()
    return out


@router.get("/wards")
def wards(branch_id: int, db: Session = Depends(get_db), _: User = Depends(require("ipd", "ipd.read", "nursing"))):
    return db.query(Ward).filter(Ward.branch_id == branch_id, Ward.is_active != False).all()  # noqa: E712


@router.get("/beds")
def beds(ward_id: int | None = None, db: Session = Depends(get_db), _: User = Depends(require("ipd", "ipd.read", "nursing"))):
    q = db.query(Bed).filter(Bed.status != "removed")
    if ward_id:
        q = q.filter(Bed.ward_id == ward_id)
    return q.all()


@router.post("/admit")
def admit(data: AdmitIn, db: Session = Depends(get_db), user: User = Depends(require("ipd"))):
    bed = db.get(Bed, data.bed_id)
    if not bed or bed.status != "available":
        raise HTTPException(400, "Bed not available")
    bed.status = "occupied"
    adm = Admission(
        patient_id=data.patient_id,
        branch_id=data.branch_id,
        bed_id=data.bed_id,
        doctor_id=data.doctor_id,
        deposit=data.deposit,
        billing_mode=data.billing_mode,
        last_room_charge_date=clock.today(),
    )
    db.add(adm)
    db.flush()
    db.add(BedTransferLog(admission_id=adm.id, from_bed_id=None, to_bed_id=bed.id, from_ward_id=None, to_ward_id=bed.ward_id, transferred_by=user.id))
    inv = create_invoice(db, data.branch_id, data.patient_id, kind="ipd", doctor_id=data.doctor_id)
    inv.admission_id = adm.id
    db.flush()
    if data.deposit > 0:
        from app.services.billing_service import pay_invoice

        pay_invoice(db, inv, "deposit", data.deposit, data.doctor_id or 1, allow_overpay=True)
    db.commit()
    db.refresh(adm)
    return {"admission": adm, "invoice_id": inv.id}


@router.post("/admissions/{admission_id}/transfer")
def transfer(admission_id: int, data: TransferIn, db: Session = Depends(get_db), user: User = Depends(require("pos", "billing"))):
    # Room changes are done at the Cashier counter (Reception only registers).
    adm = db.get(Admission, admission_id)
    new_bed = db.get(Bed, data.bed_id)
    if not adm or adm.status not in ("admitted", "transferred"):
        raise HTTPException(400, "Patient is not admitted to a room")
    if not new_bed or new_bed.status != "available":
        raise HTTPException(400, "ဒီ Room က မလွတ်ပါ")
    # Charge the old room up to today before the rate changes.
    ensure_daily_room_charges(db, adm)
    old_bed_id = adm.bed_id
    old_ward_id = None
    old = db.get(Bed, adm.bed_id) if adm.bed_id else None
    if old:
        old.status = "available"
        old_ward_id = old.ward_id
    new_bed.status = "occupied"
    adm.bed_id = data.bed_id
    adm.status = "transferred"
    db.add(BedTransferLog(
        admission_id=admission_id,
        from_bed_id=old_bed_id,
        to_bed_id=new_bed.id,
        from_ward_id=old_ward_id,
        to_ward_id=new_bed.ward_id,
        reason=data.reason,
        transferred_by=user.id,
    ))
    moved = move_today_room_charge(db, adm, old, new_bed)
    audit(db, user.id, "ipd_transfer", "admission", str(admission_id),
          f"bed {old.code if old else old_bed_id} -> {new_bed.code}" + (" (today charged at new room)" if moved else ""))
    db.commit()
    return {"admission_id": adm.id, "bed_id": adm.bed_id, "status": adm.status}


@router.get("/admissions/{admission_id}/transfers")
def list_transfers(admission_id: int, db: Session = Depends(get_db), _: User = Depends(require("ipd", "ipd.read", "nursing"))):
    rows = (
        db.query(BedTransferLog)
        .filter(BedTransferLog.admission_id == admission_id)
        .order_by(BedTransferLog.transferred_at)
        .all()
    )
    bed_ids = {r.from_bed_id for r in rows if r.from_bed_id} | {r.to_bed_id for r in rows}
    beds_by_id = {b.id: b for b in db.query(Bed).filter(Bed.id.in_(bed_ids))} if bed_ids else {}
    user_ids = {r.transferred_by for r in rows if r.transferred_by}
    names_by_id = {u.id: u.full_name for u in db.query(User).filter(User.id.in_(user_ids))} if user_ids else {}
    return [
        {
            "id": r.id,
            "from_bed": beds_by_id[r.from_bed_id].code if r.from_bed_id and r.from_bed_id in beds_by_id else None,
            "to_bed": beds_by_id[r.to_bed_id].code if r.to_bed_id in beds_by_id else None,
            "reason": r.reason,
            "transferred_by": names_by_id.get(r.transferred_by, ""),
            "transferred_at": r.transferred_at.isoformat() if r.transferred_at else None,
        }
        for r in rows
    ]


@router.post("/admissions/{admission_id}/order")
def ipd_order(
    admission_id: int,
    data: OrderItemIn,
    db: Session = Depends(get_db),
    user: User = Depends(require("ipd", "nursing")),
):
    """Order from the IPD bed panel. Medicine becomes a Ward Medicine Order for
    Pharmacy (charged and taken from stock only when Pharmacy dispenses it, as
    from the Nurse station); lab / X-ray / USG / services go straight onto the
    admission's bill and show up on their counters."""
    from app.services.counter_service import order_on_invoice, resolve_open_invoice

    adm = db.get(Admission, admission_id)
    if not adm or adm.status not in ("admitted", "transferred"):
        raise HTTPException(404, "Active admission not found")
    item = db.get(CatalogItem, data.item_id)
    if not item or not item.is_active:
        raise HTTPException(400, "Item not found")
    if data.qty <= 0:
        raise HTTPException(400, "Quantity must be positive")
    try:
        inv = resolve_open_invoice(db, patient_id=adm.patient_id, branch_id=adm.branch_id, admission_id=adm.id)
        if item.is_stock:
            order = WardMedOrder(
                patient_id=adm.patient_id, branch_id=adm.branch_id, admission_id=adm.id,
                invoice_id=inv.id, ordered_by=user.id, note=(data.note or "").strip(),
            )
            db.add(order)
            db.flush()
            db.add(WardMedOrderItem(order_id=order.id, catalog_item_id=item.id, qty=data.qty))
            audit(db, user.id, "ward_med_order_create", "ward_med_order", str(order.id), f"patient={adm.patient_id} items=1 (IPD bed panel)")
            kind = "ward_order"
        else:
            order_on_invoice(db, inv, item, qty=data.qty, user_id=user.id)
            kind = "charged"
        db.commit()
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return {"ok": True, "kind": kind, "item_name": item.name}


@router.post("/admissions/{admission_id}/deposit", response_model=InvoiceOut)
def add_deposit(
    admission_id: int,
    data: IpdDepositIn,
    db: Session = Depends(get_db),
    user: User = Depends(require("ipd", "front_desk", "pos", "billing")),
):
    from sqlalchemy.orm import joinedload

    from app.models.billing import CashierShift, Invoice

    # Tag the cashier's open shift, so a cash deposit is in that till's count.
    shift = db.query(CashierShift).filter(CashierShift.user_id == user.id, CashierShift.status == "open").first()
    try:
        inv, _adm = collect_ipd_deposit(
            db,
            admission_id=admission_id,
            amount=data.amount,
            method=data.method,
            user_id=user.id,
            shift_id=shift.id if shift else None,
        )
        db.commit()
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    inv = (
        db.query(Invoice)
        .options(joinedload(Invoice.lines), joinedload(Invoice.payments))
        .filter(Invoice.id == inv.id)
        .first()
    )
    return inv


@router.post("/admissions/{admission_id}/approve-discharge")
def approve_discharge(
    admission_id: int,
    data: DischargeIn,
    db: Session = Depends(get_db),
    user: User = Depends(require("nursing", "emr")),
):
    """Step 1: doctor / IPD nurse clears the patient to go home. Room charges
    stop at this day; the Cashier then settles the bill and discharges."""
    adm = db.get(Admission, admission_id)
    if not adm:
        raise HTTPException(404)
    if adm.status not in ("admitted", "transferred"):
        raise HTTPException(400, "Patient is not admitted to a room")
    ensure_daily_room_charges(db, adm)
    adm.discharge_approved_at = clock.now()
    adm.discharge_approved_by = user.id
    adm.discharge_summary = data.summary or ""
    adm.discharge_type = data.discharge_type or "routine"
    inv = db.query(Invoice).filter(Invoice.admission_id == admission_id).order_by(Invoice.id.desc()).first()
    if inv:
        recalc_invoice(inv, db)
    audit(db, user.id, "ipd_discharge_approve", "admission", str(admission_id), (data.summary or adm.discharge_type)[:120])
    db.commit()
    return {"ok": True, "admission_id": adm.id, "discharge_approved_at": adm.discharge_approved_at.isoformat()}


@router.post("/admissions/{admission_id}/cancel-discharge-approval")
def cancel_discharge_approval(
    admission_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require("nursing", "emr")),
):
    adm = db.get(Admission, admission_id)
    if not adm or adm.status not in ("admitted", "transferred") or not adm.discharge_approved_at:
        raise HTTPException(400, "No discharge approval to cancel")
    adm.discharge_approved_at = None
    adm.discharge_approved_by = None
    ensure_daily_room_charges(db, adm)  # catch up the days the approval held back
    audit(db, user.id, "ipd_discharge_unapprove", "admission", str(admission_id), "")
    db.commit()
    return {"ok": True}


@router.post("/admissions/{admission_id}/discharge")
def discharge(
    admission_id: int,
    data: DischargeIn,
    db: Session = Depends(get_db),
    user: User = Depends(require("pos", "billing")),
):
    """Step 2 (Cashier): settle the bill, discharge and release the bed.
    Needs a cleared balance — unless the cashier writes a reason (credit),
    which is kept in the Activity Log."""
    adm = db.get(Admission, admission_id)
    if not adm:
        raise HTTPException(404)
    if adm.status == "discharged":
        raise HTTPException(400, "Already discharged")
    reason = (data.reason or "").strip()
    # Doctor / IPD approval is optional: Cashier may discharge directly (the
    # discharge type is then taken from the Cashier). Only leaving with money
    # still owed needs a written reason.
    # No room days are added here: the cashier has already seen (and may have
    # corrected) the room days on the bill and taken the money for exactly that
    # bill — discharging must not grow it by another day. Room days are caught
    # up whenever the bill is opened (GET invoice / open bills list).
    inv = db.query(Invoice).filter(Invoice.admission_id == admission_id).order_by(Invoice.id.desc()).first()
    if inv:
        recalc_invoice(inv, db)
        if inv.balance > 0.01 and not reason:
            raise HTTPException(400, f"ကျန်ငွေ {inv.balance:,.0f} MMK ရှင်းပြီးမှ ဆင်းလို့ရပါမယ် (သို့) အကြွေးနဲ့ ဆင်းရင် အကြောင်းပြချက် ရေးပါ")
    adm.status = "discharged"
    adm.discharged_at = clock.now()
    if data.summary:
        adm.discharge_summary = data.summary
    if not adm.discharge_approved_at:
        adm.discharge_type = data.discharge_type or "routine"
    if adm.bed_id:
        bed = db.get(Bed, adm.bed_id)
        if bed:
            bed.status = "available"
    change_due = 0.0
    kept = 0.0
    if inv:
        recalc_invoice(inv, db)
        if inv.paid > inv.total + 0.01 and data.keep_extra:
            # 💰 Keep the extra deposit: a "not refunded" line makes the bill equal
            # what was paid, so nothing goes back and income stays as received.
            from app.services.billing_service import add_line

            kept = round(inv.paid - inv.total, 2)
            add_line(db, inv, None, 1, kept, "deposit_keep", "Deposit ပိုငွေ (ပြန်မအမ်း)")
            db.flush()
            recalc_invoice(inv, db)
        # Deposit bigger than the final bill: the extra goes back to the patient
        # now, recorded as a refund so income and the cash drawer stay right.
        if inv.paid > inv.total + 0.01:
            from app.models.billing import CashierShift
            from app.services.billing_service import refund_payment

            shift = db.query(CashierShift).filter(CashierShift.user_id == user.id, CashierShift.status == "open").first()
            change_due = round(inv.paid - inv.total, 2)
            refund_payment(db, inv, amount=change_due, user_id=user.id, method="cash",
                           reason="IPD deposit ပိုငွေ ပြန်အမ်း (ဆေးရုံဆင်း)", shift_id=shift.id if shift else None)
    detail = f"balance={inv.balance:,.0f}" if inv else ""
    if change_due:
        detail = f"{detail} deposit_change={change_due:,.0f}"
    if kept:
        detail = f"{detail} deposit_kept={kept:,.0f}"
    if reason:
        detail = f"{detail} override: {reason}".strip()
    audit(db, user.id, "ipd_discharge", "admission", str(admission_id), detail[:200])
    db.commit()
    db.refresh(adm)
    inv_payload = None
    if inv:
        inv_payload = {
            "id": inv.id,
            "number": inv.number,
            "status": inv.status,
            "total": inv.total,
            "balance": inv.balance,
            "paid": inv.paid,
            "change_due": change_due,
            "kept": kept,
        }
    return {"admission": adm, "invoice": inv_payload}


@router.get("/admissions")
def admissions(branch_id: int | None = None, status: str = "admitted", db: Session = Depends(get_db), _: User = Depends(require("ipd", "ipd.read", "nursing"))):
    q = db.query(Admission)
    if status == "admitted":
        q = q.filter(Admission.status.in_(["admitted", "transferred"]))
    else:
        q = q.filter(Admission.status == status)
    if branch_id:
        q = q.filter(Admission.branch_id == branch_id)
    rows = q.all()
    if status == "admitted" and any(ensure_daily_room_charges(db, adm) for adm in rows):
        db.commit()
    return rows


@router.post("/admissions/{admission_id}/vitals")
def add_vitals(admission_id: int, data: VitalsIn, db: Session = Depends(get_db), user: User = Depends(require("vitals", "nursing"))):
    adm = db.get(Admission, admission_id)
    if not adm or adm.status not in ("admitted", "transferred"):
        raise HTTPException(400, "Admission is not active")
    v = VitalSign(admission_id=admission_id, bp=data.bp, pulse=data.pulse, temp=data.temp, spo2=data.spo2, weight=data.weight)
    db.add(v)
    audit(db, user.id, "vitals_record", "admission", str(admission_id), f"bp={data.bp} pulse={data.pulse}")
    db.commit()
    db.refresh(v)
    return v


@router.post("/admissions/{admission_id}/notes")
def add_note(admission_id: int, data: NoteIn, db: Session = Depends(get_db), user: User = Depends(require("nursing"))):
    n = NursingNote(admission_id=admission_id, nurse_id=user.id, note=data.note)
    db.add(n)
    db.commit()
    return n


@router.get("/admissions/{admission_id}/vitals")
def list_vitals(admission_id: int, db: Session = Depends(get_db), _: User = Depends(require("ipd", "nursing"))):
    return db.query(VitalSign).filter(VitalSign.admission_id == admission_id).order_by(VitalSign.id.desc()).all()


@router.get("/admissions/{admission_id}/notes")
def list_notes(admission_id: int, db: Session = Depends(get_db), _: User = Depends(require("ipd", "nursing"))):
    return db.query(NursingNote).filter(NursingNote.admission_id == admission_id).order_by(NursingNote.id.desc()).all()


@router.get("/admissions/{admission_id}/bill")
def running_bill(admission_id: int, db: Session = Depends(get_db), _: User = Depends(require("ipd", "billing.read"))):
    inv = db.query(Invoice).filter(Invoice.admission_id == admission_id).first()
    if not inv:
        raise HTTPException(404)
    return inv

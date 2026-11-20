from datetime import date, datetime, time, timedelta

from sqlalchemy import and_, case, exists, func, or_
from sqlalchemy.orm import Session, joinedload

from app.core import clock
from app.models.accounting import Expense
from app.models.ancillary import LabOrder, RadiologyOrder
from app.models.billing import BILL_DATE, Invoice, InvoiceLine, Payment
from app.models.catalog import CatalogItem, LabTestReagent
from app.models.clinical import Visit
from app.models.ipd import Admission, Bed, BedTransferLog, Ward, WardMedOrder
from app.models.org import Branch, Warehouse
from app.models.patients import Patient
from app.models.users import User
from app.services.billing_service import add_line, add_room_charge, create_invoice, pay_invoice, recalc_invoice, refund_payment, return_line
from app.services.inventory_service import dispense, stock_on_hand
from app.services.queue_service import issue_token
from app.services.utils import audit, next_number, next_uhid


def get_open_invoice(db: Session, patient_id: int, branch_id: int, kind: str | None = None) -> Invoice | None:
    q = (
        db.query(Invoice)
        .filter(
            Invoice.patient_id == patient_id,
            Invoice.branch_id == branch_id,
            Invoice.status.in_(["draft", "open", "partial"]),
        )
    )
    if kind:
        q = q.filter(Invoice.kind == kind)
    return q.order_by(Invoice.id.desc()).first()


def resolve_open_invoice(
    db: Session,
    *,
    patient_id: int,
    branch_id: int,
    invoice_id: int | None = None,
    admission_id: int | None = None,
) -> Invoice:
    """Pick the bill pharmacy/nurse actions should hit — OPD vs IPD must not mix."""
    open_status = ["draft", "open", "partial"]
    if invoice_id:
        inv = db.get(Invoice, invoice_id)
        if not inv or inv.patient_id != patient_id or inv.branch_id != branch_id:
            raise ValueError("Bill not found for this patient")
        if inv.status not in open_status:
            raise ValueError("Bill is no longer open")
        return inv
    if admission_id:
        inv = (
            db.query(Invoice)
            .filter(
                Invoice.admission_id == admission_id,
                Invoice.branch_id == branch_id,
                Invoice.status.in_(open_status),
            )
            .order_by(Invoice.id.desc())
            .first()
        )
        if inv:
            return inv
        raise ValueError("No open IPD bill for this admission")
    inv = get_open_invoice(db, patient_id, branch_id, kind="opd")
    if inv:
        return inv
    inv = (
        db.query(Invoice)
        .filter(
            Invoice.patient_id == patient_id,
            Invoice.branch_id == branch_id,
            Invoice.admission_id.is_(None),
            Invoice.kind.in_(["opd", "pos"]),
            Invoice.status.in_(open_status),
        )
        .order_by(Invoice.id.desc())
        .first()
    )
    if inv:
        return inv
    inv = get_open_invoice(db, patient_id, branch_id)
    if inv:
        return inv
    raise ValueError("No open bill — patient must register at Reception first")


def resolve_ward_order_invoice(db: Session, order: WardMedOrder) -> Invoice:
    return resolve_open_invoice(
        db,
        patient_id=order.patient_id,
        branch_id=order.branch_id,
        invoice_id=order.invoice_id,
        admission_id=order.admission_id,
    )


def reception_register(
    db: Session,
    *,
    branch_id: int,
    doctor_id: int | None,
    patient_id: int | None,
    name: str,
    phone: str,
    gender: str,
    user_id: int,
    patient_type: str = "opd",
    bed_id: int | None = None,
    ward_id: int | None = None,
    deposit: float = 0,
    billing_mode: str = "daily",
    age_years: int | None = None,
    age_months: int | None = None,
    age_days: int | None = None,
    address: str = "",
    father_name: str = "",
    referring_doctor: str = "",
    home_stay: bool = False,
    reuse_invoice_id: int | None = None,
) -> dict:
    # Doctor is optional at registration — a patient can be registered (and
    # billed at Lab/X-ray/Pharmacy) before or without seeing a doctor.
    doctor = None
    if doctor_id:
        doctor = db.get(User, doctor_id)
        if not doctor or doctor.role != "doctor":
            raise ValueError("Doctor not found")

    if patient_id:
        patient = db.get(Patient, patient_id)
        if not patient:
            raise ValueError("Patient not found")
    else:
        if not name.strip():
            raise ValueError("Patient name required")
        if age_months is not None and not 0 <= age_months <= 11:
            raise ValueError("Months must be between 0 and 11")
        if age_days is not None and not 0 <= age_days <= 30:
            raise ValueError("Days must be between 0 and 30")
        patient = Patient(
            uhid=next_uhid(db),
            name=name.strip(),
            phone=phone,
            gender=gender,
            age_years=age_years,
            age_months=age_months,
            age_days=age_days,
            address=address,
            father_name=father_name.strip(),
            referring_doctor=referring_doctor,
        )
        db.add(patient)
        db.flush()

    ptype = (patient_type or "opd").lower()
    if ptype not in {"opd", "ipd"}:
        raise ValueError("Patient type must be OPD or IPD")

    if ptype == "ipd":
        return _register_ipd(
            db,
            branch_id=branch_id,
            patient=patient,
            doctor=doctor,
            bed_id=bed_id,
            ward_id=ward_id,
            deposit=deposit,
            billing_mode=billing_mode,
            user_id=user_id,
            home_stay=home_stay,
        )

    return _register_opd(db, branch_id=branch_id, patient=patient, doctor=doctor, user_id=user_id, reuse_invoice_id=reuse_invoice_id)


def add_doctor_fee(db: Session, inv: Invoice, doctor: User, *, skip_if_present: bool = False) -> InvoiceLine | None:
    """Add a doctor's consultation-fee line to a bill. A bill can carry several
    doctors (seen by two doctors), but the same doctor only once."""
    if doctor.role != "doctor":
        raise ValueError("Not a doctor")
    if any(l.source == "opd" and l.doctor_id == doctor.id and l.qty > 0 for l in inv.lines):
        if skip_if_present:
            return None
        raise ValueError(f"{doctor.full_name} ရဲ့ ဆရာဝန်ခ ဒီ bill ထဲမှာ ရှိပြီးသားပါ")
    consult = db.query(CatalogItem).filter(CatalogItem.sku == "CONSULT").first()
    if not consult:
        raise ValueError("Consultation fee item (CONSULT) is missing")
    fee = doctor.consultation_fee or consult.price or 0
    line = add_line(db, inv, consult, 1, fee, "opd", f"Consultation — {doctor.full_name}", doctor)
    if not inv.doctor_id:
        inv.doctor_id = doctor.id
        # Lab tests / X-rays / USG put on the bill before its doctor was known count for this doctor.
        for l in inv.lines:
            if l.source in ("lab", "radiology") and not l.doctor_id:
                l.doctor_id = doctor.id
        for order in db.query(LabOrder).filter(LabOrder.invoice_id == inv.id, LabOrder.doctor_id.is_(None)):
            order.doctor_id = doctor.id
        for order in db.query(RadiologyOrder).filter(RadiologyOrder.invoice_id == inv.id, RadiologyOrder.doctor_id.is_(None)):
            order.doctor_id = doctor.id
    return line


def _register_opd(
    db: Session, *, branch_id: int, patient: Patient, doctor: User | None, user_id: int, reuse_invoice_id: int | None = None,
) -> dict:
    admitted = (
        db.query(Admission)
        .filter(Admission.patient_id == patient.id, Admission.branch_id == branch_id,
                Admission.status.in_(["admitted", "transferred", "pending_bed"]))
        .first()
    )
    if admitted:
        # One stay, one bill: an admitted patient's medicine / tests / doctors go on the IPD bill.
        raise ValueError(f"{patient.uhid} {patient.name} ဆေးရုံတက်နေဆဲ — OPD bill အသစ် မဖွင့်ပါ။ IPD bill ထဲမှာ ထည့်ပါ")
    doctor_id = doctor.id if doctor else None
    token = issue_token(db, branch_id=branch_id, patient_id=patient.id, doctor_id=doctor_id)
    # Same visit = same bill: only a bill opened *today* is carried on. A bill
    # left open from an earlier day stays as it is (Cashier collects it) and
    # this visit gets its own bill — unless staff chose to carry on that bill.
    inv = None
    if reuse_invoice_id:
        inv = db.get(Invoice, reuse_invoice_id)
        if (not inv or inv.patient_id != patient.id or inv.branch_id != branch_id or inv.kind != "opd"
                or inv.admission_id or inv.status not in ("draft", "open", "partial")):
            raise ValueError("ဒီ bill ကို ဆက်သုံးလို့ မရတော့ပါ — bill အသစ်နဲ့ register လုပ်ပါ")
    else:
        inv = get_open_invoice(db, patient.id, branch_id, kind="opd")
        if inv and (inv.admission_id or not inv.created_at or inv.created_at.date() < clock.today()):
            inv = None
    if not inv:
        inv = create_invoice(db, branch_id, patient.id, kind="opd", doctor_id=doctor_id)
    elif doctor:
        inv.doctor_id = doctor.id

    if doctor:
        add_doctor_fee(db, inv, doctor, skip_if_present=True)

    db.add(Visit(patient_id=patient.id, branch_id=branch_id, doctor_id=doctor_id))
    audit(db, user_id, "reception_register", "patient", str(patient.id), f"opd doctor={doctor_id} inv={inv.id}")
    db.flush()
    recalc_invoice(inv, db)
    return {
        "patient": patient,
        "invoice": inv,
        "token": token,
        "doctor": doctor,
        "patient_type": "opd",
        "admission": None,
        "bed": None,
        "ward": None,
    }


def _register_ipd(
    db: Session,
    *,
    branch_id: int,
    patient: Patient,
    doctor: User | None,
    bed_id: int | None,
    deposit: float,
    billing_mode: str,
    user_id: int,
    ward_id: int | None = None,
    home_stay: bool = False,
) -> dict:
    """Reception admits the patient and may note the ward they should go to;
    Cashier assigns the actual bed later (assign_bed), once the deposit is in.
    Until then the admission is pending_bed: no bed, no room charge."""
    active = (
        db.query(Admission)
        .filter(
            Admission.patient_id == patient.id,
            Admission.branch_id == branch_id,
            Admission.status.in_(["admitted", "transferred", "pending_bed"]),
        )
        .first()
    )
    if active:
        raise ValueError("Patient is already admitted to IPD")

    if bed_id and not ward_id:
        # Older screens still send a bed — treat it as the ward wanted.
        bed = db.get(Bed, bed_id)
        ward_id = bed.ward_id if bed else None
    ward = db.get(Ward, ward_id) if ward_id else None
    if ward_id and (not ward or ward.branch_id != branch_id):
        raise ValueError("Invalid ward for this branch")

    adm = Admission(
        patient_id=patient.id,
        branch_id=branch_id,
        bed_id=None,
        requested_ward_id=ward.id if ward else None,
        doctor_id=doctor.id if doctor else None,
        deposit=deposit,
        billing_mode=billing_mode or "daily",
        status="pending_bed",
        home_stay=bool(home_stay),
    )
    db.add(adm)
    db.flush()

    inv = get_open_invoice(db, patient.id, branch_id, kind="ipd")
    if not inv:
        inv = create_invoice(db, branch_id, patient.id, kind="ipd", doctor_id=doctor.id if doctor else None)
    elif doctor:
        inv.doctor_id = doctor.id
    inv.admission_id = adm.id

    if deposit > 0:
        from app.services.billing_service import pay_invoice

        pay_invoice(db, inv, "deposit", deposit, user_id, allow_overpay=True)

    audit(
        db,
        user_id,
        "ipd_admit",
        "admission",
        str(adm.id),
        f"patient={patient.id} ward={ward.name if ward else '-'} (bed pending)",
    )
    db.flush()
    recalc_invoice(inv, db)
    return {
        "patient": patient,
        "invoice": inv,
        "token": None,
        "doctor": doctor,
        "patient_type": "ipd",
        "admission": adm,
        "bed": None,
        "ward": ward,
    }


def assign_bed(
    db: Session,
    adm: Admission,
    *,
    bed_id: int,
    user_id: int,
    billing_mode: str | None = None,
    emergency: bool = False,
    reason: str = "",
) -> Bed:
    """Cashier puts a pending_bed admission into a bed. The deposit must be in
    first, unless it's an emergency (reason required, kept in the audit log).
    The room charge clock starts now, not at registration."""
    if adm.status != "pending_bed":
        raise ValueError("This patient already has a bed — use Transfer to move them")
    inv = (
        db.query(Invoice)
        .filter(Invoice.admission_id == adm.id, Invoice.kind == "ipd")
        .order_by(Invoice.id.desc())
        .first()
    )
    if not inv:
        raise ValueError("No IPD bill for this admission")
    recalc_invoice(inv, db)
    if inv.paid <= 0.01 and float(adm.deposit or 0) <= 0.01:
        if not emergency:
            raise ValueError("Deposit အရင်ကောက်ပါ — အရေးပေါ်ဆိုရင် Emergency ကို ရွေးပါ")
        if not (reason or "").strip():
            raise ValueError("Emergency reason ထည့်ပါ")
    bed = db.get(Bed, bed_id)
    if not bed or bed.status != "available":
        raise ValueError("Bed not available")
    ward = db.get(Ward, bed.ward_id)
    if not ward or ward.branch_id != adm.branch_id:
        raise ValueError("Invalid bed for this branch")
    mode = billing_mode or adm.billing_mode or "daily"
    bed.status = "occupied"
    adm.bed_id = bed.id
    adm.billing_mode = mode
    # Back from 🏠 Home stay the same day it left a bed: today is already charged.
    charged_today = mode == "daily" and adm.last_room_charge_date == clock.today()
    adm.status = "admitted"
    adm.home_stay = False
    adm.last_room_charge_date = clock.today()
    db.add(BedTransferLog(admission_id=adm.id, from_bed_id=None, to_bed_id=bed.id, from_ward_id=None, to_ward_id=bed.ward_id, transferred_by=user_id))
    rate = {"hourly": bed.hourly_rate, "package": bed.package_rate}.get(mode, bed.daily_rate)
    if not charged_today:
        add_room_charge(db, inv, f"Room charge {bed.code} ({mode})", rate, 1)
    note = f"bed={bed.code} ward={ward.name}" + (f" EMERGENCY (no deposit): {reason.strip()}" if emergency and inv.paid <= 0.01 else "")
    audit(db, user_id, "ipd_assign_bed", "admission", str(adm.id), note)
    db.flush()
    recalc_invoice(inv, db)
    return bed


def order_on_invoice(
    db: Session, inv: Invoice, item: CatalogItem | None, *, qty: float, user_id: int, warehouse_id: int | None = None,
) -> None:
    """Cashier's one search box: put any catalog item on an open bill the same
    way its own counter would — medicine is dispensed from stock, lab tests
    become Lab orders, X-ray/USG become Radiology orders (so they show up on
    those counters), anything else is a plain charge."""
    if item is None or not item.is_active:
        raise ValueError("Item not found")
    if inv.status not in ("draft", "open", "partial", "credit"):
        raise ValueError("This bill is already paid — use History → ↩ to exchange instead")
    if not inv.patient_id:
        raise ValueError("Bill has no patient")
    if qty <= 0:
        raise ValueError("Quantity must be positive")
    common = dict(branch_id=inv.branch_id, patient_id=inv.patient_id, item_id=item.id, user_id=user_id, invoice_id=inv.id)
    if item.is_stock and item.department == "cashier":
        wh = (
            db.query(Warehouse)
            .filter(Warehouse.branch_id == inv.branch_id)
            .order_by(Warehouse.is_default.desc(), Warehouse.id)
            .first()
        )
        wh_id = warehouse_id or (wh.id if wh else None)
        if not wh_id:
            raise ValueError("No store for this branch")
        picks = dispense(db, item, wh_id, qty, f"inv-{inv.id}", user_id)
        cost = sum(b.unit_cost * take for b, take in picks) / qty
        add_line(db, inv, item, qty, item.price, "cashier", item.name, None,
                 batch_id=picks[0][0].id if picks else None, unit_cost=cost if cost > 0 else (item.cost or 0))
        audit(db, user_id, "cashier_order", "invoice", str(inv.id), f"item={item.id} qty={qty}")
        return
    if item.is_stock:
        wh_id = warehouse_id
        if not wh_id:
            wh = (
                db.query(Warehouse)
                .filter(Warehouse.branch_id == inv.branch_id)
                .order_by(Warehouse.is_default.desc(), Warehouse.id)
                .first()
            )
            if not wh:
                raise ValueError("No pharmacy store for this branch")
            wh_id = wh.id
        pharmacy_charge(db, warehouse_id=wh_id, qty=qty, **common)
        return
    runs = max(int(round(qty)), 1)
    if item.category == "lab":
        for _ in range(runs):
            lab_order(db, **common)
    elif item.category == "radiology":
        for _ in range(runs):
            (usg_order if item.department == "usg" else xray_charge)(db, **common)
    else:
        doctor = db.get(User, inv.doctor_id) if inv.doctor_id else None
        source = "ot" if item.department == "ot" else "opd" if item.category == "consultation" else (item.category or "service")
        add_line(db, inv, item, qty, item.price, source, item.name, doctor)
    audit(db, user_id, "cashier_order", "invoice", str(inv.id), f"item={item.id} qty={qty}")


def exchange_line(
    db: Session,
    inv: Invoice,
    line: InvoiceLine,
    *,
    qty: float,
    reason: str,
    new_item: CatalogItem | None,
    new_qty: float,
    user_id: int,
    method: str = "cash",
    shift_id: int | None = None,
    warehouse_id: int | None = None,
) -> dict:
    """Swap part of an already-billed line for a different item on the same
    bill: the old item comes back (restocked if it's medicine), the new one is
    charged (dispensed from stock if it's medicine), and only the price
    difference changes hands — collected if the new item costs more, refunded
    if it costs less."""
    if new_item is None or not new_item.is_active:
        raise ValueError("Pick the item to exchange for")
    if new_qty <= 0:
        raise ValueError("New quantity must be positive")
    if new_item.id == line.item_id:
        raise ValueError("Pick a different item to exchange for")
    ret, _ = return_line(db, inv, line, qty=qty, reason=reason, user_id=user_id, refund=False)
    ret.description = f"Exchanged: {line.description}"
    doctor = db.get(User, inv.doctor_id) if inv.doctor_id else None
    if new_item.is_stock:
        wh_id = warehouse_id
        if not wh_id:
            wh = (
                db.query(Warehouse)
                .filter(Warehouse.branch_id == inv.branch_id)
                .order_by(Warehouse.is_default.desc(), Warehouse.id)
                .first()
            )
            if not wh:
                raise ValueError("No pharmacy store for this branch")
            wh_id = wh.id
        picks = dispense(db, new_item, wh_id, new_qty, f"inv-{inv.id}", user_id)
        cost = sum(b.unit_cost * take for b, take in picks) / new_qty
        new_line = add_line(
            db, inv, new_item, new_qty, new_item.price, "ot" if new_item.department == "ot" else "pharmacy", f"Exchange: {new_item.name}", doctor,
            batch_id=picks[0][0].id if picks else None, unit_cost=cost if cost > 0 else (new_item.cost or 0),
        )
    else:
        source = "ot" if new_item.department == "ot" else "opd" if new_item.category == "consultation" else (new_item.category or "service")
        new_line = add_line(db, inv, new_item, new_qty, new_item.price, source, f"Exchange: {new_item.name}", doctor)
    recalc_invoice(inv, db)
    delta = round(new_line.amount + ret.amount, 2)
    collected = refunded = 0.0
    note = f"Exchange: {line.description} → {new_item.name} — {reason.strip()}"
    if delta > 0.01:
        collected = round(min(delta, inv.balance), 2)
        if collected > 0.01:
            pay_invoice(db, inv, method, collected, user_id, shift_id)
    elif delta < -0.01:
        refunded = round(min(-delta, inv.paid - inv.total), 2)
        if refunded > 0.01:
            refund_payment(db, inv, amount=refunded, user_id=user_id, method=method, reason=note, shift_id=shift_id)
        else:
            refunded = 0.0
    audit(db, user_id, "exchange_line", "invoice_line", str(line.id), f"{note} qty={qty}->{new_qty} collected={collected:.0f} refunded={refunded:.0f}")
    return {"collected": collected, "refunded": refunded}


def pharmacy_charge(
    db: Session,
    *,
    branch_id: int,
    patient_id: int,
    item_id: int,
    warehouse_id: int,
    qty: float,
    user_id: int,
    prescription_item_id: int | None = None,
    invoice_id: int | None = None,
) -> dict:
    item = db.get(CatalogItem, item_id)
    # OT charges (OT Room Fee, OT Nurse, ...) have no stock but can be put on the
    # bill from the Pharmacy counter too.
    if not item or not (item.is_stock or item.department == "ot"):
        raise ValueError("Medicine not found")

    inv = resolve_open_invoice(db, patient_id=patient_id, branch_id=branch_id, invoice_id=invoice_id)

    picks = dispense(db, item, warehouse_id, qty, f"inv-{inv.id}", user_id) if item.is_stock else []
    doctor = db.get(User, inv.doctor_id) if inv.doctor_id else None
    total_cost = sum(batch.unit_cost * take for batch, take in picks)
    avg_unit_cost = (total_cost / qty) if qty > 0 else 0
    if avg_unit_cost <= 0:
        avg_unit_cost = item.cost or 0
    primary_batch_id = picks[0][0].id if picks else None
    add_line(
        db,
        inv,
        item,
        qty,
        item.price,
        "ot" if item.department == "ot" else "pharmacy",
        item.name,
        doctor,
        batch_id=primary_batch_id,
        unit_cost=avg_unit_cost,
    )

    if prescription_item_id:
        from app.models.clinical import Prescription, PrescriptionItem

        rx_item = db.get(PrescriptionItem, prescription_item_id)
        if rx_item:
            rx_item.dispensed_qty = (rx_item.dispensed_qty or 0) + qty
            rx_item.status = "dispensed" if rx_item.dispensed_qty >= rx_item.qty else "partial"
            rx = db.get(Prescription, rx_item.prescription_id)
            if rx:
                statuses = {i.status for i in rx.items}
                rx.status = "dispensed" if statuses == {"dispensed"} else ("cancelled" if not statuses else "partial")

    audit(db, user_id, "pharmacy_dispense", "invoice", str(inv.id), f"item={item_id} qty={qty}")
    db.flush()
    recalc_invoice(inv, db)
    return {"invoice": inv, "item": item}


def pharmacy_walk_in_sale(db: Session, *, branch_id: int, name: str = "", phone: str = "", user_id: int) -> dict:
    """A walk-in customer buying medicine directly at Pharmacy without going
    through Reception first — creates a lightweight patient record and an
    open invoice (kind="pos") so the existing pharmacy_charge()/open-invoice
    billing path (POST /counter/pharmacy) can dispense to them exactly like a
    Reception-registered patient, no separate registration step needed."""
    patient = Patient(
        uhid=next_uhid(db),
        name=name.strip() or "Walk-in Customer",
        phone=phone,
        gender="",
    )
    db.add(patient)
    db.flush()

    inv = create_invoice(db, branch_id, patient.id, kind="pos")
    db.flush()
    recalc_invoice(inv, db)

    audit(db, user_id, "pharmacy_walk_in_sale", "patient", str(patient.id), f"inv={inv.id}")
    return {"patient": patient, "invoice": inv}


def pending_prescriptions(db: Session, branch_id: int) -> list:
    from app.models.clinical import Prescription, PrescriptionItem

    rows = (
        db.query(Prescription)
        .filter(Prescription.branch_id == branch_id, Prescription.status.in_(["pending", "partial"]))
        .order_by(Prescription.id.desc())
        .limit(50)
        .all()
    )
    out = []
    for rx in rows:
        patient = db.get(Patient, rx.patient_id)
        doctor = db.get(User, rx.doctor_id) if rx.doctor_id else None
        items = [
            {
                "id": i.id,
                "catalog_item_id": i.catalog_item_id,
                "name": i.catalog_item.name if i.catalog_item else i.free_text_name,
                "qty": i.qty,
                "dispensed_qty": i.dispensed_qty,
                "dosage_instructions": i.dosage_instructions,
                "status": i.status,
            }
            for i in db.query(PrescriptionItem).filter(PrescriptionItem.prescription_id == rx.id).all()
            if i.status != "dispensed"
        ]
        if not items:
            continue
        out.append(
            {
                "prescription_id": rx.id,
                "patient_id": rx.patient_id,
                "patient_name": patient.name if patient else "—",
                "uhid": patient.uhid if patient else "—",
                "doctor_name": doctor.full_name if doctor else "—",
                "created_at": rx.created_at.isoformat() if rx.created_at else None,
                "items": items,
            }
        )
    return out


def doctor_examine(
    db: Session,
    *,
    branch_id: int,
    doctor_id: int,
    patient_id: int,
    chief_complaint: str,
    diagnosis: str,
    notes: str,
    follow_up_at,
    rx_items: list,
    user_id: int,
) -> dict:
    from app.models.clinical import Prescription, PrescriptionItem

    patient = db.get(Patient, patient_id)
    if not patient:
        raise ValueError("Patient not found")

    follow_up_dt = None
    if follow_up_at:
        follow_up_dt = follow_up_at if isinstance(follow_up_at, datetime) else datetime.fromisoformat(follow_up_at)

    # Reception creates an empty Visit stub at registration — reuse the latest
    # un-examined one for this patient/doctor instead of piling up duplicates.
    visit = (
        db.query(Visit)
        .filter(
            Visit.patient_id == patient_id,
            Visit.doctor_id == doctor_id,
            Visit.diagnosis == "",
            Visit.chief_complaint == "",
            Visit.notes == "",
        )
        .order_by(Visit.id.desc())
        .first()
    )
    if not visit:
        visit = Visit(patient_id=patient_id, branch_id=branch_id, doctor_id=doctor_id)
        db.add(visit)

    visit.chief_complaint = chief_complaint
    visit.diagnosis = diagnosis
    visit.notes = notes
    visit.follow_up_at = follow_up_dt
    db.flush()

    rx = None
    if rx_items:
        rx = Prescription(visit_id=visit.id, patient_id=patient_id, doctor_id=doctor_id, branch_id=branch_id)
        db.add(rx)
        db.flush()
        for line in rx_items:
            item_id = line.get("catalog_item_id") if isinstance(line, dict) else line.catalog_item_id
            free_text = line.get("free_text_name") if isinstance(line, dict) else line.free_text_name
            qty = line.get("qty") if isinstance(line, dict) else line.qty
            dosage = line.get("dosage_instructions") if isinstance(line, dict) else line.dosage_instructions
            if not item_id and not (free_text or "").strip():
                continue
            db.add(
                PrescriptionItem(
                    prescription_id=rx.id,
                    catalog_item_id=item_id,
                    free_text_name=free_text or "",
                    qty=qty or 1,
                    dosage_instructions=dosage or "",
                )
            )

    audit(db, user_id, "doctor_examine", "visit", str(visit.id), f"patient={patient_id}")
    db.flush()
    return {"visit": visit, "prescription": rx}


def lab_walk_in(
    db: Session,
    *,
    branch_id: int,
    name: str,
    phone: str = "",
    gender: str = "",
    age_years: int | None = None,
    age_months: int | None = None,
    age_days: int | None = None,
    referring_doctor: str = "",
    user_id: int,
) -> dict:
    """A walk-in customer testing at Lab without Reception — patient + open lab invoice."""
    if not name.strip():
        raise ValueError("Patient name required")
    if age_months is not None and not 0 <= age_months <= 11:
        raise ValueError("Months must be between 0 and 11")
    if age_days is not None and not 0 <= age_days <= 30:
        raise ValueError("Days must be between 0 and 30")
    if age_years is not None and age_years < 0:
        raise ValueError("Years must be 0 or more")
    patient = Patient(
        uhid=next_uhid(db),
        name=name.strip(),
        phone=phone.strip(),
        gender=gender.strip(),
        age_years=age_years,
        age_months=age_months,
        age_days=age_days,
        referring_doctor=referring_doctor.strip(),
    )
    db.add(patient)
    db.flush()
    inv = create_invoice(db, branch_id, patient.id, kind="lab")
    db.flush()
    recalc_invoice(inv, db)
    audit(db, user_id, "lab_walk_in", "patient", str(patient.id), f"inv={inv.id}")
    return {"patient": patient, "invoice": inv}


def _consume_lab_reagents(db: Session, *, test_item_id: int, branch_id: int, user_id: int, ref: str) -> None:
    """Pull the test's linked supplies (3–4 reagents) from the branch store."""
    rows = db.query(LabTestReagent).filter(LabTestReagent.test_item_id == test_item_id).all()
    if not rows:
        return
    warehouse = (
        db.query(Warehouse)
        .filter(Warehouse.branch_id == branch_id)
        .order_by(Warehouse.is_default.desc(), Warehouse.id)
        .first()
    )
    if not warehouse:
        raise ValueError("No store warehouse for lab reagents")
    for row in rows:
        reagent = db.get(CatalogItem, row.reagent_item_id)
        if not reagent or not reagent.is_stock:
            raise ValueError("Lab reagent is not a stock item")
        need = float(row.qty or 1)
        try:
            dispense(db, reagent, warehouse.id, need, ref, user_id)
        except ValueError as e:
            raise ValueError(f"Insufficient stock: {reagent.name} (need {need:g})") from e


def lab_order(db: Session, *, branch_id: int, patient_id: int, item_id: int, user_id: int, invoice_id: int | None = None) -> dict:
    from app.models.ancillary import LabOrder

    item = db.get(CatalogItem, item_id)
    if not item or item.category != "lab":
        raise ValueError("Lab test not found")

    if invoice_id:
        inv = resolve_open_invoice(db, patient_id=patient_id, branch_id=branch_id, invoice_id=invoice_id)
    else:
        inv = get_open_invoice(db, patient_id, branch_id)
        if not inv:
            inv = create_invoice(db, branch_id, patient_id, kind="lab")

    doctor = db.get(User, inv.doctor_id) if inv.doctor_id else None
    _consume_lab_reagents(db, test_item_id=item.id, branch_id=branch_id, user_id=user_id, ref=f"lab-{patient_id}")
    add_line(db, inv, item, 1, item.price, "lab", item.name, doctor)
    order = LabOrder(
        patient_id=patient_id,
        branch_id=branch_id,
        doctor_id=inv.doctor_id,
        invoice_id=inv.id,
        tests=item.name,
        status="ordered",
        sample_id=next_number(db, "sample", "S-"),
    )
    db.add(order)
    audit(db, user_id, "lab_order", "invoice", str(inv.id), f"item={item_id}")
    db.flush()
    recalc_invoice(inv, db)
    return {"invoice": inv, "order": order, "item_name": item.name}


def lab_result(db: Session, *, order_id: int, result: str, user_id: int) -> dict:
    from app.models.ancillary import LabOrder

    order = db.get(LabOrder, order_id)
    if not order:
        raise ValueError("Lab order not found")
    order.result = result
    order.status = "completed"
    audit(db, user_id, "lab_result", "lab_order", str(order.id), "result entered")
    db.flush()

    patient = db.get(Patient, order.patient_id)
    doctor = db.get(User, order.doctor_id) if order.doctor_id else None
    return {
        "order_id": order.id,
        "patient_name": patient.name if patient else "—",
        "uhid": patient.uhid if patient else "—",
        "age_years": patient.age_years if patient else None,
        "gender": patient.gender if patient else "",
        "doctor_name": doctor.full_name if doctor else "",
        "tests": order.tests,
        "result": order.result,
        "status": order.status,
        "sample_id": order.sample_id,
        "created_at": order.created_at.isoformat() if order.created_at else None,
    }


def usg_order(db: Session, *, branch_id: int, patient_id: int, item_id: int, user_id: int, invoice_id: int | None = None) -> dict:
    from app.models.ancillary import RadiologyOrder

    item = db.get(CatalogItem, item_id)
    if not item or item.category != "radiology" or item.department != "usg":
        raise ValueError("USG service not found")

    if invoice_id:
        inv = resolve_open_invoice(db, patient_id=patient_id, branch_id=branch_id, invoice_id=invoice_id)
    else:
        inv = get_open_invoice(db, patient_id, branch_id)
        if not inv:
            inv = create_invoice(db, branch_id, patient_id, kind="radiology")

    doctor = db.get(User, inv.doctor_id) if inv.doctor_id else None
    add_line(db, inv, item, 1, item.price, "radiology", item.name, doctor)
    order = RadiologyOrder(
        patient_id=patient_id,
        branch_id=branch_id,
        doctor_id=inv.doctor_id,
        modality="usg",
        invoice_id=inv.id,
        status="ordered",
    )
    db.add(order)
    audit(db, user_id, "usg_order", "invoice", str(inv.id), f"item={item_id}")
    db.flush()
    recalc_invoice(inv, db)
    return {"invoice": inv, "order": order, "item_name": item.name}


def radiology_result(db: Session, *, order_id: int, findings: str, user_id: int) -> dict:
    from app.models.ancillary import RadiologyOrder

    order = db.get(RadiologyOrder, order_id)
    if not order:
        raise ValueError("Order not found")
    order.findings = findings
    order.status = "completed"
    audit(db, user_id, "radiology_result", "radiology_order", str(order.id), "findings entered")
    db.flush()

    patient = db.get(Patient, order.patient_id)
    doctor = db.get(User, order.doctor_id) if order.doctor_id else None
    return {
        "order_id": order.id,
        "patient_name": patient.name if patient else "—",
        "uhid": patient.uhid if patient else "—",
        "age_years": patient.age_years if patient else None,
        "gender": patient.gender if patient else "",
        "doctor_name": doctor.full_name if doctor else "",
        "modality": order.modality,
        "findings": order.findings,
        "status": order.status,
        "created_at": order.created_at.isoformat() if order.created_at else None,
    }


def convert_patient_type(
    db: Session,
    *,
    branch_id: int,
    patient_id: int,
    target_type: str,
    doctor_id: int | None,
    bed_id: int | None,
    ward_id: int | None = None,
    deposit: float,
    billing_mode: str,
    user_id: int,
) -> dict:
    patient = db.get(Patient, patient_id)
    if not patient:
        raise ValueError("Patient not found")

    target = (target_type or "").lower()
    if target not in {"opd", "ipd"}:
        raise ValueError("target_type must be OPD or IPD")

    if target == "ipd":
        doctor = db.get(User, doctor_id) if doctor_id else None
        if not doctor:
            active_adm = (
                db.query(Admission)
                .filter(Admission.patient_id == patient_id, Admission.branch_id == branch_id)
                .order_by(Admission.id.desc())
                .first()
            )
            doctor = db.get(User, active_adm.doctor_id) if active_adm and active_adm.doctor_id else None
        if not doctor:
            raise ValueError("Doctor required to admit to IPD")
        return _register_ipd(
            db,
            branch_id=branch_id,
            patient=patient,
            doctor=doctor,
            bed_id=bed_id,
            ward_id=ward_id,
            deposit=deposit,
            billing_mode=billing_mode,
            user_id=user_id,
        )

    active = (
        db.query(Admission)
        .filter(Admission.patient_id == patient_id, Admission.branch_id == branch_id, Admission.status.in_(["admitted", "pending_bed"]))
        .first()
    )
    if not active:
        raise ValueError("Patient has no active IPD admission")
    active.status = "discharged"
    active.discharged_at = clock.now()
    # The IPD bill no longer belongs to a stay in progress: settle its status
    # (paid / partial) and paid date now, like a normal discharge does.
    for ipd_inv in db.query(Invoice).filter(Invoice.admission_id == active.id).all():
        recalc_invoice(ipd_inv, db)
    if active.bed_id:
        bed = db.get(Bed, active.bed_id)
        if bed:
            bed.status = "available"

    doctor = db.get(User, active.doctor_id) if active.doctor_id else None
    if not doctor:
        raise ValueError("Doctor not found for this admission")

    audit(db, user_id, "convert_ipd_to_opd", "admission", str(active.id), f"patient={patient_id}")
    result = _register_opd(db, branch_id=branch_id, patient=patient, doctor=doctor, user_id=user_id)
    db.flush()
    return result


def xray_charge(db: Session, *, branch_id: int, patient_id: int, item_id: int, user_id: int, invoice_id: int | None = None) -> dict:
    item = db.get(CatalogItem, item_id)
    if not item or item.category != "radiology":
        raise ValueError("X-ray service not found")

    if invoice_id:
        inv = resolve_open_invoice(db, patient_id=patient_id, branch_id=branch_id, invoice_id=invoice_id)
    else:
        inv = get_open_invoice(db, patient_id, branch_id)
        if not inv:
            inv = create_invoice(db, branch_id, patient_id, kind="radiology")

    doctor = db.get(User, inv.doctor_id) if inv.doctor_id else None
    add_line(db, inv, item, 1, item.price, "radiology", item.name, doctor)
    order = RadiologyOrder(
        patient_id=patient_id,
        branch_id=branch_id,
        doctor_id=inv.doctor_id,
        modality=item.sku.lower(),
        invoice_id=inv.id,
        status="ordered",
    )
    db.add(order)
    audit(db, user_id, "xray_order", "invoice", str(inv.id), f"item={item_id}")
    db.flush()
    recalc_invoice(inv, db)
    return {"invoice": inv, "order": order, "item_name": item.name}


def apply_stock_count(db: Session, item_id: int, warehouse_id: int, counted_qty: float, user_id: int, notes: str = "") -> dict:
    from datetime import date

    from app.models.inventory import StockBatch, StockMovement

    current = stock_on_hand(db, item_id, warehouse_id)
    diff = counted_qty - current
    if diff == 0:
        return {"item_id": item_id, "system_qty": current, "counted_qty": counted_qty, "adjusted": 0}

    if diff > 0:
        batch = (
            db.query(StockBatch)
            .filter(StockBatch.item_id == item_id, StockBatch.warehouse_id == warehouse_id)
            .order_by(StockBatch.id.desc())
            .first()
        )
        if batch:
            batch.qty += diff
            batch_id = batch.id
        else:
            batch = StockBatch(
                item_id=item_id,
                warehouse_id=warehouse_id,
                batch_no="COUNT-ADJ",
                expiry_date=clock.today().replace(year=clock.today().year + 2),
                qty=diff,
                unit_cost=0,
            )
            db.add(batch)
            db.flush()
            batch_id = batch.id
        db.add(StockMovement(item_id=item_id, batch_id=batch_id, qty_delta=diff, reason="stock_count", ref=notes or "count+"))
    else:
        from app.services.inventory_service import pick_fefo

        picks = pick_fefo(db, item_id, warehouse_id, abs(diff))
        for batch, take in picks:
            db.add(StockMovement(item_id=item_id, batch_id=batch.id, qty_delta=-take, reason="stock_count", ref=notes or "count-"))

    audit(db, user_id, "stock_count", "catalog_item", str(item_id), f"counted={counted_qty} was={current}")
    return {"item_id": item_id, "system_qty": current, "counted_qty": counted_qty, "adjusted": diff}


PERIOD_LABELS = {"day": "Today", "month": "1 Month", "3m": "3 Months", "year": "1 Year"}

SOURCE_LABELS = {
    "opd": "Consultation",
    "pharmacy": "Pharmacy",
    "radiology": "X-Ray",
    "ipd_room": "IPD Room",
    "lab": "Lab",
    "round": "Doctor Round",
    "ot": "OT",
    "surgery": "OT Doctor Fee",
}

INVOICE_KIND_LABELS = {
    "opd": "OPD",
    "ipd": "IPD",
    "pharmacy": "Pharmacy",
    "other": "Other",
}


def _period_start(period: str) -> datetime:
    now = clock.now()
    if period == "day":
        return datetime.combine(now.date(), datetime.min.time())
    days = {"month": 30, "3m": 90, "year": 365}.get(period, 30)
    return now - timedelta(days=days)


def cashier_analytics_range(db: Session, branch_id: int, date_from: datetime, date_to: datetime) -> dict:
    # A single-day range gets the same hourly trend buckets as period="day".
    period = "day" if date_from.date() == date_to.date() else "custom"
    return _cashier_analytics_since(db, branch_id, since=date_from, until=date_to, period_label="Custom", period=period)


def cashier_analytics(db: Session, branch_id: int, period: str = "month") -> dict:
    since = _period_start(period)
    return _cashier_analytics_since(db, branch_id, since=since, until=clock.now(), period_label=PERIOD_LABELS.get(period, period), period=period)


FEE_KINDS = ("normal", "night", "half", "foc")


def doctor_patients(db: Session, branch_id: int, doctor_id: int, *, since: datetime, until: datetime, kind: str = "") -> list[dict]:
    """Per-bill drill-down behind the Doctor Income card — same line-level
    attribution (InvoiceLine.doctor_id / df_amount) and date window as
    _cashier_analytics_since's by_doctor, so the rows add up to that card."""
    rows = (
        db.query(
            Invoice.id,
            Invoice.number,
            Invoice.kind,
            BILL_DATE,
            Patient.id,
            Patient.name,
            Patient.uhid,
            func.coalesce(func.sum(InvoiceLine.df_amount), 0),
            func.coalesce(func.sum(InvoiceLine.amount), 0),
            func.max(InvoiceLine.fee_type),
            func.coalesce(func.sum(InvoiceLine.qty), 0),
        )
        .join(InvoiceLine, InvoiceLine.invoice_id == Invoice.id)
        .outerjoin(Patient, Patient.id == Invoice.patient_id)
        .filter(
            Invoice.branch_id == branch_id,
            BILL_DATE >= since,
            BILL_DATE <= until,
            InvoiceLine.doctor_id == doctor_id,
            Invoice.status == "paid",
            # Round fee card ("round") and the Doctor Income card (everything else) each list their own bills.
            (InvoiceLine.source == kind) if kind in ("round", "surgery") else InvoiceLine.source.notin_(("round", "surgery")),
        )
        .group_by(Invoice.id, Invoice.number, Invoice.kind, BILL_DATE, Patient.id, Patient.name, Patient.uhid)
        .order_by(BILL_DATE.desc())
        .all()
    )
    return [
        {
            "invoice_id": int(r[0]),
            "invoice_number": r[1],
            "kind": r[2],
            "created_at": r[3].isoformat() if r[3] else None,
            "patient_id": r[4],
            "patient_name": r[5] or "—",
            "uhid": r[6] or "—",
            "fee_income": float(r[7] or 0),
            "billed": float(r[8] or 0),
            "fee_type": r[9] or "",
            "rounds": float(r[10] or 0) if kind in ("round", "surgery") else None,
        }
        for r in rows
        if float(r[7] or 0) > 0 or (r[9] or "") == "foc"
    ]


def lab_by_doctor(db: Session, branch_id: int, *, since: datetime, until: datetime, kind: str = "lab") -> list[dict]:
    """Lab tests (kind "lab"), X-rays ("xray") or USG scans ("usg") per ordering
    doctor (InvoiceLine.doctor_id) on fully paid bills in the window, net
    of returns — what the hospital hands back to each doctor is worked out by
    hand from these totals."""
    if kind == "lab":
        kind_filter = InvoiceLine.source == "lab"
    elif kind == "usg":
        kind_filter = and_(InvoiceLine.source == "radiology", CatalogItem.department == "usg")
    elif kind == "xray":
        kind_filter = and_(InvoiceLine.source == "radiology", or_(CatalogItem.department.is_(None), CatalogItem.department != "usg"))
    else:
        raise ValueError("kind must be lab, xray or usg")
    rows = (
        db.query(
            InvoiceLine.doctor_id, InvoiceLine.item_id, InvoiceLine.description, InvoiceLine.qty, InvoiceLine.amount,
            InvoiceLine.returned_from_line_id, Invoice.id, Invoice.number, Invoice.status, BILL_DATE,
            Patient.name, Patient.uhid,
        )
        .join(Invoice, Invoice.id == InvoiceLine.invoice_id)
        .outerjoin(Patient, Patient.id == Invoice.patient_id)
        .outerjoin(CatalogItem, CatalogItem.id == InvoiceLine.item_id)
        .filter(
            Invoice.branch_id == branch_id,
            BILL_DATE >= since,
            BILL_DATE <= until,
            Invoice.status == "paid",
            kind_filter,
        )
        .order_by(BILL_DATE.desc(), InvoiceLine.id)
        .all()
    )
    names = dict(db.query(User.id, User.full_name).filter(User.id.in_({r[0] for r in rows if r[0]})).all()) if rows else {}
    docs: dict = {}
    for doctor_id, item_id, desc, qty, amount, returned, inv_id, number, status, created, p_name, uhid in rows:
        d = docs.setdefault(doctor_id or 0, {
            "doctor_id": doctor_id or 0, "doctor": names.get(doctor_id) or "ဆရာဝန်မပါ / ပြင်ပ",
            "tests": 0.0, "amount": 0.0, "by_test": {}, "bills": {},
        })
        name = (desc or "")[8:] if returned and (desc or "").startswith("Return: ") else (desc or "")
        d["tests"] += float(qty or 0)
        d["amount"] += float(amount or 0)
        t = d["by_test"].setdefault(item_id or name, {"test": name, "count": 0.0, "amount": 0.0})
        t["count"] += float(qty or 0)
        t["amount"] += float(amount or 0)
        b = d["bills"].setdefault(inv_id, {
            "invoice_id": inv_id, "invoice_number": number, "status": status,
            "created_at": created.isoformat() if created else None,
            "patient_name": p_name or "—", "uhid": uhid or "—", "tests": [], "count": 0.0, "amount": 0.0,
        })
        b["count"] += float(qty or 0)
        b["amount"] += float(amount or 0)
        if not returned:
            b["tests"].append(name)
    out = []
    for d in docs.values():
        if abs(d["tests"]) < 1e-9 and abs(d["amount"]) < 0.01:
            continue
        d["by_test"] = sorted((t for t in d["by_test"].values() if abs(t["count"]) > 1e-9), key=lambda t: (-t["count"], t["test"]))
        d["bills"] = [b for b in d["bills"].values() if b["count"] > 1e-9]
        out.append(d)
    out.sort(key=lambda d: (d["doctor_id"] == 0, -d["amount"]))
    return out


def _credit_summary(db: Session, branch_id: int, since: datetime, until: datetime) -> dict:
    """📒 OPD credit for the Sale Report: put on credit in the window, paid back
    in the window (any month's credit), and what is still owed."""
    base = db.query(Invoice).filter(Invoice.branch_id == branch_id, Invoice.credit_at.isnot(None), Invoice.status != "void")
    sold = base.filter(Invoice.credit_at >= since, Invoice.credit_at <= until).all()
    back = 0.0
    n_back = set()
    for p, inv in (
        db.query(Payment, Invoice).join(Invoice, Payment.invoice_id == Invoice.id)
        .filter(Invoice.branch_id == branch_id, Invoice.credit_at.isnot(None), Invoice.status != "void",
                Payment.created_at > Invoice.credit_at, Payment.created_at >= since, Payment.created_at <= until)
    ):
        back += float(p.amount or 0) * (-1 if p.method == "refund" else 1)
        n_back.add(inv.id)
    owing = base.filter(Invoice.status == "credit").all()
    return {
        "sold": round(sum(float(i.credit_amount or 0) for i in sold), 2), "sold_count": len(sold),
        "back": round(back, 2), "back_count": len(n_back),
        "owing_period": round(sum(float(i.balance or 0) for i in sold if i.status == "credit"), 2),
        "owing_all": round(sum(float(i.balance or 0) for i in owing), 2), "owing_all_count": len(owing),
    }


def _cashier_analytics_since(db: Session, branch_id: int, *, since: datetime, until: datetime, period_label: str, period: str) -> dict:
    pay_filter = (
        # "+ 0" stops SQLite from driving this join off the invoices' branch index
        # (every bill the branch ever had) instead of the payments' date index
        # (just this window) — same result, but Today stays instant on years of data.
        Invoice.branch_id + 0 == branch_id,
        Payment.created_at >= since,
        Payment.created_at <= until,
        Payment.method != "refund",
    )

    # Each block below reads its table once and derives every figure from the
    # grouped result in Python — a year's window is hundreds of thousands of
    # rows, so re-scanning it per metric (as one query per figure) made long
    # periods take many seconds.
    invoice_in_window = case((and_(BILL_DATE >= since, BILL_DATE <= until), 1), else_=0)
    bucket = func.extract("hour", Payment.created_at) if period == "day" else func.date(Payment.created_at)
    pay_rows = (
        db.query(Payment.method, Invoice.kind, bucket, invoice_in_window, func.sum(Payment.amount))
        .join(Invoice)
        .filter(*pay_filter)
        .group_by(Payment.method, Invoice.kind, bucket, invoice_in_window)
        .all()
    )
    total_collected = 0.0
    collected_today_bills = 0.0
    by_method: dict[str, float] = {}
    collected_by_kind: dict[str, float] = {}
    by_bucket: dict = {}
    for method, kind, bkt, in_window, amount in pay_rows:
        amount = float(amount or 0)
        total_collected += amount
        if in_window:
            collected_today_bills += amount
        by_method[method] = by_method.get(method, 0.0) + amount
        kind_key = str(kind or "other")
        collected_by_kind[kind_key] = collected_by_kind.get(kind_key, 0.0) + amount
        by_bucket[bkt] = by_bucket.get(bkt, 0.0) + amount
    # Money paid back (returns, a deposit bigger than the final IPD bill) comes
    # off the day it was handed back, from the way it was handed back.
    refund_rows = (
        db.query(Payment.refund_method, Invoice.kind, bucket, invoice_in_window, func.sum(Payment.amount))
        .join(Invoice)
        .filter(Invoice.branch_id + 0 == branch_id, Payment.created_at >= since, Payment.created_at <= until, Payment.method == "refund")
        .group_by(Payment.refund_method, Invoice.kind, bucket, invoice_in_window)
        .all()
    )
    total_refunded = 0.0
    for rmethod, kind, bkt, in_window, amount in refund_rows:
        amount = float(amount or 0)
        total_refunded += amount
        total_collected -= amount
        if in_window:
            collected_today_bills -= amount
        m = str(rmethod or "cash").lower()
        by_method[m] = by_method.get(m, 0.0) - amount
        kind_key = str(kind or "other")
        collected_by_kind[kind_key] = collected_by_kind.get(kind_key, 0.0) - amount
        by_bucket[bkt] = by_bucket.get(bkt, 0.0) - amount
    collected_today_bills = max(collected_today_bills, 0)
    collected_older_bills = max(total_collected - collected_today_bills, 0)
    payment_methods = [
        {"method": str(m).upper(), "amount": a} for m, a in sorted(by_method.items(), key=lambda x: x[1], reverse=True) if abs(a) > 0.005
    ]

    invoice_rows = (
        db.query(Invoice.kind, Invoice.status, func.count(Invoice.id), func.sum(Invoice.total), func.sum(Invoice.balance))
        .filter(Invoice.branch_id == branch_id, BILL_DATE >= since, BILL_DATE <= until, Invoice.status != "void")
        .group_by(Invoice.kind, Invoice.status)
        .all()
    )
    total_bills = paid_bills = 0
    total_billed = outstanding = 0.0
    kind_billed: dict = {}
    for kind, status, count, total, balance in invoice_rows:
        total_bills += int(count or 0)
        total_billed += float(total or 0)
        if status == "paid":
            paid_bills += int(count or 0)
        if status in ("open", "partial", "draft"):
            outstanding += float(balance or 0)
        billed, bills = kind_billed.get(kind, (0.0, 0))
        kind_billed[kind] = (billed + float(total or 0), bills + int(count or 0))
    by_invoice_kind = [
        {
            "kind": str(kind or "other"),
            "label": INVOICE_KIND_LABELS.get(str(kind), str(kind or "Other").title()),
            "billed": billed,
            "bills": bills,
            "collected": collected_by_kind.get(str(kind or "other"), 0.0),
        }
        for kind, (billed, bills) in kind_billed.items()
        if billed > 0
    ]
    by_invoice_kind.sort(key=lambda x: x["billed"], reverse=True)

    unit_cost_expr = case(
        (InvoiceLine.unit_cost > 0, InvoiceLine.unit_cost),
        else_=func.coalesce(CatalogItem.cost, 0),
    )
    line_rows = (
        db.query(
            InvoiceLine.source,
            InvoiceLine.doctor_id,
            func.sum(InvoiceLine.amount),
            # Doctor fees count once, on the day the bill is fully paid — an
            # unpaid bill would show on its open date and again when paid.
            func.sum(case((Invoice.status == "paid", InvoiceLine.df_amount), else_=0)),
            func.sum(InvoiceLine.qty * unit_cost_expr),
            func.sum(case((Invoice.status == "paid", InvoiceLine.amount), else_=0)),
        )
        .select_from(Invoice)
        .join(InvoiceLine, InvoiceLine.invoice_id == Invoice.id)
        .outerjoin(CatalogItem, InvoiceLine.item_id == CatalogItem.id)
        .filter(Invoice.branch_id == branch_id, BILL_DATE >= since, BILL_DATE <= until)
        .group_by(InvoiceLine.source, InvoiceLine.doctor_id)
        .all()
    )
    source_amounts: dict = {}
    pharmacy_revenue = pharmacy_cost = 0.0
    doctor_fee: dict[int, float] = {}
    doctor_billed: dict[int, float] = {}
    round_fee: dict[int, float] = {}
    surgery_fee: dict[int, float] = {}
    for source, doctor_id, amount, df_amount, cost, paid_amount in line_rows:
        source_amounts[source] = source_amounts.get(source, 0.0) + float(amount or 0)
        if source == "pharmacy":
            pharmacy_revenue += float(amount or 0)
            pharmacy_cost += float(cost or 0)
        if doctor_id and source == "round":
            round_fee[doctor_id] = round_fee.get(doctor_id, 0.0) + float(df_amount or 0)
        elif doctor_id and source == "surgery":
            surgery_fee[doctor_id] = surgery_fee.get(doctor_id, 0.0) + float(df_amount or 0)
        elif doctor_id:
            doctor_fee[doctor_id] = doctor_fee.get(doctor_id, 0.0) + float(df_amount or 0)
            doctor_billed[doctor_id] = doctor_billed.get(doctor_id, 0.0) + float(paid_amount or 0)
    by_source = [
        {"source": SOURCE_LABELS.get(str(src), str(src or "Other").title()), "amount": amt}
        for src, amt in source_amounts.items()
        if amt > 0
    ]
    by_source.sort(key=lambda x: x["amount"], reverse=True)

    pharmacy_profit = pharmacy_revenue - pharmacy_cost
    pharmacy_profit_data = {
        "revenue": pharmacy_revenue,
        "cost": pharmacy_cost,
        "profit": pharmacy_profit,
        "margin_pct": round(pharmacy_profit / pharmacy_revenue * 100, 1) if pharmacy_revenue > 0 else 0,
    }

    fee_type_rows = (
        db.query(InvoiceLine.doctor_id, InvoiceLine.fee_type, func.count(InvoiceLine.id), func.coalesce(func.sum(InvoiceLine.amount), 0))
        .select_from(Invoice)
        .join(InvoiceLine, InvoiceLine.invoice_id == Invoice.id)
        .filter(
            Invoice.branch_id == branch_id,
            BILL_DATE >= since,
            BILL_DATE <= until,
            InvoiceLine.source == "opd",
            InvoiceLine.doctor_id.isnot(None),
            Invoice.status == "paid",
        )
        .group_by(InvoiceLine.doctor_id, InvoiceLine.fee_type)
        .all()
    )
    fee_types: dict[int, dict[str, float]] = {}
    for d_id, ftype, n, amt in fee_type_rows:
        key = ftype if ftype in ("night", "half", "foc") else "normal"
        slot = fee_types.setdefault(d_id, {})
        slot[f"fee_{key}_n"] = slot.get(f"fee_{key}_n", 0) + int(n or 0)
        slot[f"fee_{key}_amount"] = slot.get(f"fee_{key}_amount", 0.0) + float(amt or 0)
    # Ward rounds (IPD round fee): its own card — rounds, bills and fee per doctor.
    round_stats = {
        d_id: (float(n or 0), int(b or 0))
        for d_id, n, b in (
            db.query(InvoiceLine.doctor_id, func.coalesce(func.sum(InvoiceLine.qty), 0), func.count(func.distinct(Invoice.id)))
            .select_from(Invoice)
            .join(InvoiceLine, InvoiceLine.invoice_id == Invoice.id)
            .filter(
                Invoice.branch_id == branch_id,
                BILL_DATE >= since,
                BILL_DATE <= until,
                InvoiceLine.source == "round",
                InvoiceLine.doctor_id.isnot(None),
                Invoice.status == "paid",
            )
            .group_by(InvoiceLine.doctor_id)
            .all()
        )
    }
    round_names = dict(db.query(User.id, User.full_name).filter(User.id.in_(list(round_stats))).all()) if round_stats else {}
    by_doctor_round = sorted(
        (
            {
                "doctor_id": int(d_id),
                "doctor": str(round_names.get(d_id, "—")),
                "rounds": n,
                "bills": b,
                "fee_income": float(round_fee.get(d_id, 0.0)),
            }
            for d_id, (n, b) in round_stats.items()
        ),
        key=lambda r: r["fee_income"],
        reverse=True,
    )
    # OT doctor fees (surgeon / anaesthetist / assistant): their own card too.
    surgery_stats = {
        d_id: (int(n or 0), int(b or 0))
        for d_id, n, b in (
            db.query(InvoiceLine.doctor_id, func.count(InvoiceLine.id), func.count(func.distinct(Invoice.id)))
            .select_from(Invoice)
            .join(InvoiceLine, InvoiceLine.invoice_id == Invoice.id)
            .filter(
                Invoice.branch_id == branch_id,
                BILL_DATE >= since,
                BILL_DATE <= until,
                InvoiceLine.source == "surgery",
                InvoiceLine.qty > 0,
                InvoiceLine.doctor_id.isnot(None),
                Invoice.status == "paid",
            )
            .group_by(InvoiceLine.doctor_id)
            .all()
        )
    }
    surgery_names = dict(db.query(User.id, User.full_name).filter(User.id.in_(list(surgery_stats))).all()) if surgery_stats else {}
    by_doctor_surgery = sorted(
        (
            {"doctor_id": int(d_id), "doctor": str(surgery_names.get(d_id, "—")), "ops": n, "bills": b, "fee_income": float(surgery_fee.get(d_id, 0.0))}
            for d_id, (n, b) in surgery_stats.items()
        ),
        key=lambda r: r["fee_income"],
        reverse=True,
    )
    earning_doctor_ids = [d for d, fee in doctor_fee.items() if fee > 0 or fee_types.get(d, {}).get("fee_foc_n")]
    doctor_bill_counts = dict(
        db.query(InvoiceLine.doctor_id, func.count(func.distinct(Invoice.id)))
        .select_from(Invoice)
        .join(InvoiceLine, InvoiceLine.invoice_id == Invoice.id)
        .filter(
            Invoice.branch_id == branch_id,
            BILL_DATE >= since,
            BILL_DATE <= until,
            InvoiceLine.doctor_id.in_(earning_doctor_ids),
            Invoice.status == "paid",
        )
        .group_by(InvoiceLine.doctor_id)
        .all()
    ) if earning_doctor_ids else {}
    doctors = (
        db.query(User.id, User.full_name).filter(User.id.in_(earning_doctor_ids), User.role == "doctor").all()
        if earning_doctor_ids else []
    )
    doctor_rows = sorted(
        ((d_id, name, doctor_fee[d_id], doctor_billed[d_id], doctor_bill_counts.get(d_id, 0)) for d_id, name in doctors),
        key=lambda r: r[2],
        reverse=True,
    )
    by_doctor = [
        {
            "doctor_id": int(r[0]),
            "doctor": str(r[1]),
            "fee_income": float(r[2] or 0),
            "billed": float(r[3] or 0),
            "bills": int(r[4] or 0),
            **{f"fee_{k}_{m}": fee_types.get(int(r[0]), {}).get(f"fee_{k}_{m}", 0) for k in FEE_KINDS for m in ("n", "amount")},
        }
        for r in doctor_rows
    ]

    total_expenses = float(
        db.query(func.coalesce(func.sum(Expense.amount), 0))
        .filter(Expense.branch_id == branch_id, Expense.expense_date >= since.date(), Expense.expense_date <= until.date())
        .scalar()
        or 0
    )

    expense_cat_rows = (
        db.query(Expense.category, func.sum(Expense.amount), func.count(Expense.id))
        .filter(Expense.branch_id == branch_id, Expense.expense_date >= since.date(), Expense.expense_date <= until.date())
        .group_by(Expense.category)
        .order_by(func.sum(Expense.amount).desc())
        .all()
    )
    by_expense_category = [
        {
            "category": str(r[0] or "Uncategorized"),
            "amount": float(r[1] or 0),
            "count": int(r[2] or 0),
        }
        for r in expense_cat_rows
        if float(r[1] or 0) > 0
    ]

    expense_rows = (
        db.query(Expense)
        .filter(Expense.branch_id == branch_id, Expense.expense_date >= since.date(), Expense.expense_date <= until.date())
        .order_by(Expense.expense_date.desc(), Expense.id.desc())
        .limit(50)
        .all()
    )
    expense_details = [
        {
            "id": e.id,
            "category": e.category or "Uncategorized",
            "amount": float(e.amount or 0),
            "notes": e.notes or "",
            "paid_from": e.paid_from or "petty",
            "created_at": e.created_at.isoformat() if e.created_at else None,
        }
        for e in expense_rows
    ]

    if period == "day":
        trend = [{"label": f"{int(k):02d}:00", "amount": v} for k, v in sorted(by_bucket.items())]
    else:
        trend = [{"label": str(k), "amount": v} for k, v in sorted(by_bucket.items(), key=lambda x: str(x[0]))]

    return {
        "period": period,
        "period_label": period_label,
        "from_date": since.date().isoformat(),
        "to_date": until.date().isoformat(),
        "total_collected": total_collected,
        "refunded": total_refunded,
        "collected_today_bills": collected_today_bills,
        "collected_older_bills": collected_older_bills,
        "total_billed": total_billed,
        "total_expenses": total_expenses,
        "net": total_collected - total_expenses,
        "total_bills": total_bills,
        "paid_bills": paid_bills,
        "outstanding": outstanding,
        "avg_bill": round(total_collected / paid_bills) if paid_bills else 0,
        "payment_methods": payment_methods,
        "by_source": by_source,
        "by_invoice_kind": by_invoice_kind,
        "credit": _credit_summary(db, branch_id, since, until),
        "pharmacy_profit": pharmacy_profit_data,
        "by_doctor": by_doctor,
        "by_doctor_round": by_doctor_round,
        "by_doctor_surgery": by_doctor_surgery,
        "by_expense_category": by_expense_category,
        "expense_details": expense_details,
        "trend": trend,
    }


def _merge_keyed(rows_by_branch: list[list[dict]], key: str, sum_fields: list[str]) -> list[dict]:
    """Merges several branches' list-of-dict breakdowns (by_source, by_doctor,
    by_expense_category, payment_methods, trend, ...) into one, summing the
    given numeric fields for entries that share the same `key` value across
    branches — e.g. two branches' "KPAY" payment-method rows become one row
    with the combined amount, instead of two separate "KPAY" rows."""
    merged: dict[object, dict] = {}
    for rows in rows_by_branch:
        for row in rows:
            k = row[key]
            if k not in merged:
                merged[k] = dict(row)
            else:
                for f in sum_fields:
                    merged[k][f] = merged[k].get(f, 0) + row.get(f, 0)
    return list(merged.values())


def cashier_analytics_combined(
    db: Session, period: str = "month", date_from: str | None = None, date_to: str | None = None, extra: list[dict] | None = None,
) -> dict:
    """Combined "All Branches" view — calls the existing, already-correct
    per-branch cashier_analytics()/cashier_analytics_range() once per branch
    and numerically merges the results, rather than rewriting that function's
    ~15 branch-scoped filters to be conditionally branch-wide (much higher
    risk of a subtle reporting bug than summing already-correct per-branch
    numbers)."""
    branches = db.query(Branch).filter(Branch.is_active == True).all()  # noqa: E712
    if not branches:
        raise ValueError("No branches found")

    if date_from and date_to:
        start = datetime.fromisoformat(date_from)
        end = datetime.fromisoformat(date_to).replace(hour=23, minute=59, second=59)
        per_branch = [cashier_analytics_range(db, b.id, start, end) for b in branches]
    else:
        per_branch = [cashier_analytics(db, b.id, period) for b in branches]
    # Branches whose data lives elsewhere (the offline clinic's uploaded copy).
    per_branch += extra or []

    first = per_branch[0]
    total_collected = sum(a["total_collected"] for a in per_branch)
    total_expenses = sum(a["total_expenses"] for a in per_branch)
    paid_bills = sum(a["paid_bills"] for a in per_branch)
    pharmacy_revenue = sum(a["pharmacy_profit"]["revenue"] for a in per_branch)
    pharmacy_cost = sum(a["pharmacy_profit"]["cost"] for a in per_branch)
    pharmacy_profit = pharmacy_revenue - pharmacy_cost

    return {
        "period": first["period"],
        "period_label": first["period_label"],
        "from_date": first["from_date"],
        "to_date": first["to_date"],
        "total_collected": total_collected,
        "refunded": sum(a.get("refunded", 0) for a in per_branch),
        "collected_today_bills": sum(a["collected_today_bills"] for a in per_branch),
        "collected_older_bills": sum(a["collected_older_bills"] for a in per_branch),
        "total_billed": sum(a["total_billed"] for a in per_branch),
        "total_expenses": total_expenses,
        "net": total_collected - total_expenses,
        "total_bills": sum(a["total_bills"] for a in per_branch),
        "paid_bills": paid_bills,
        "outstanding": sum(a["outstanding"] for a in per_branch),
        "avg_bill": round(total_collected / paid_bills) if paid_bills else 0,
        "payment_methods": _merge_keyed([a["payment_methods"] for a in per_branch], "method", ["amount"]),
        "by_source": _merge_keyed([a["by_source"] for a in per_branch], "source", ["amount"]),
        "by_invoice_kind": _merge_keyed([a["by_invoice_kind"] for a in per_branch], "kind", ["billed", "bills", "collected"]),
        "credit": {k: round(sum((a.get("credit") or {}).get(k, 0) for a in per_branch), 2)
                   for k in ("sold", "sold_count", "back", "back_count", "owing_period", "owing_all", "owing_all_count")},
        "pharmacy_profit": {
            "revenue": pharmacy_revenue,
            "cost": pharmacy_cost,
            "profit": pharmacy_profit,
            "margin_pct": round(pharmacy_profit / pharmacy_revenue * 100, 1) if pharmacy_revenue > 0 else 0,
        },
        "by_doctor": _merge_keyed(
            [a["by_doctor"] for a in per_branch], "doctor_id",
            ["fee_income", "billed", "bills"] + [f"fee_{k}_{m}" for k in FEE_KINDS for m in ("n", "amount")],
        ),
        "by_doctor_round": sorted(
            _merge_keyed([a.get("by_doctor_round") or [] for a in per_branch], "doctor_id", ["rounds", "bills", "fee_income"]),
            key=lambda r: r["fee_income"], reverse=True,
        ),
        "by_doctor_surgery": sorted(
            _merge_keyed([a.get("by_doctor_surgery") or [] for a in per_branch], "doctor_id", ["ops", "bills", "fee_income"]),
            key=lambda r: r["fee_income"], reverse=True,
        ),
        "by_expense_category": _merge_keyed([a["by_expense_category"] for a in per_branch], "category", ["amount", "count"]),
        "expense_details": sorted(
            (e for a in per_branch for e in a["expense_details"]),
            key=lambda e: e["created_at"] or "",
            reverse=True,
        )[:50],
        "trend": sorted(_merge_keyed([a["trend"] for a in per_branch], "label", ["amount"]), key=lambda t: t["label"]),
    }


def list_doctors(db: Session, specialty: str | None = None) -> list[User]:
    q = db.query(User).filter(User.role == "doctor", User.is_active == True)  # noqa: E712
    if specialty and specialty.strip():
        q = q.filter(User.specialty == specialty.strip())
    return q.order_by(User.full_name).all()


def create_doctor(
    db: Session,
    *,
    full_name: str,
    consultation_fee: float,
    branch_id: int | None,
    specialty: str,
    user_id: int | None,
) -> User:
    name = full_name.strip()
    if not name:
        raise ValueError("Doctor name required")
    if consultation_fee < 0:
        raise ValueError("Fee cannot be negative")

    from app.core.security import hash_password

    username = next_number(db, "doc_user", "doc")
    while db.query(User).filter(User.username == username).first():
        username = next_number(db, "doc_user", "doc")

    doctor = User(
        username=username,
        hashed_password=hash_password("ChangeMe@123"),
        full_name=name,
        role="doctor",
        branch_id=branch_id,
        consultation_fee=consultation_fee,
        commission_pct=70,
        specialty=specialty.strip(),
        department="opd",
    )
    db.add(doctor)
    db.flush()
    audit(db, user_id, "doctor_create", "user", str(doctor.id), f"{name} fee={consultation_fee}")
    return doctor


def update_doctor(
    db: Session,
    doctor_id: int,
    *,
    full_name: str | None,
    consultation_fee: float | None,
    specialty: str | None,
    user_id: int | None,
) -> User:
    doctor = db.get(User, doctor_id)
    if not doctor or doctor.role != "doctor":
        raise ValueError("Doctor not found")
    if full_name is not None:
        if not full_name.strip():
            raise ValueError("Doctor name required")
        doctor.full_name = full_name.strip()
    if consultation_fee is not None:
        if consultation_fee < 0:
            raise ValueError("Fee cannot be negative")
        doctor.consultation_fee = consultation_fee
    if specialty is not None:
        doctor.specialty = specialty.strip()
    audit(db, user_id, "doctor_update", "user", str(doctor.id), doctor.full_name)
    return doctor


def remove_doctor(db: Session, doctor_id: int, *, user_id: int | None) -> User:
    """Take a doctor off the list. The doctor is switched off, not erased, so the
    bills, fees and reports that already name them stay exactly as they were;
    they can no longer be picked for new bills or tokens, nor log in."""
    doctor = db.get(User, doctor_id)
    if not doctor or doctor.role != "doctor":
        raise ValueError("Doctor not found")
    if not doctor.is_active:
        raise ValueError("Doctor already removed")
    doctor.is_active = False
    audit(db, user_id, "doctor_delete", "user", str(doctor.id), doctor.full_name)
    return doctor


SERVICE_DEPARTMENTS = {
    "lab": "lab",
    "xray": "radiology",
    "usg": "radiology",
}


def list_service_items(db: Session, department: str) -> list[CatalogItem]:
    category = SERVICE_DEPARTMENTS.get(department)
    if not category:
        raise ValueError("Invalid department")
    return (
        db.query(CatalogItem)
        .filter(CatalogItem.category == category, CatalogItem.department == department)
        .order_by(CatalogItem.name)
        .all()
    )


def create_service_item(db: Session, *, department: str, name: str, price: float, user_id: int | None) -> CatalogItem:
    category = SERVICE_DEPARTMENTS.get(department)
    if not category:
        raise ValueError("Invalid department")
    name = name.strip()
    if not name:
        raise ValueError("Name required")
    if price < 0:
        raise ValueError("Price cannot be negative")

    sku = next_number(db, f"svc_{department}", f"{department.upper()}-")
    item = CatalogItem(
        sku=sku,
        barcode=sku,
        name=name,
        name_mm=name,
        category=category,
        department=department,
        price=price,
        is_stock=False,
    )
    db.add(item)
    db.flush()
    audit(db, user_id, "service_item_create", "catalog_item", str(item.id), name)
    return item


def update_service_item(
    db: Session,
    item_id: int,
    *,
    name: str | None,
    price: float | None,
    is_active: bool | None,
    user_id: int | None,
) -> CatalogItem:
    item = db.get(CatalogItem, item_id)
    if not item or item.department not in SERVICE_DEPARTMENTS:
        raise ValueError("Service not found")
    if name is not None:
        if not name.strip():
            raise ValueError("Name required")
        item.name = name.strip()
        item.name_mm = item.name
    if price is not None:
        if price < 0:
            raise ValueError("Price cannot be negative")
        item.price = price
    if is_active is not None:
        item.is_active = is_active
        if is_active:
            item.deleted_at = None
    audit(db, user_id, "service_item_update", "catalog_item", str(item.id), item.name)
    return item


def search_patient_records(
    db: Session,
    *,
    branch_id: int,
    date_from: date | None = None,
    date_to: date | None = None,
    kind: str = "",
    source: str = "",
    q: str = "",
    limit: int = 50,
) -> list[dict]:
    """Find patients whose visits match date / visit type / service category / item text.

    source is the counter category: opd, pharmacy, lab, xray, usg.
    q matches a billed line description, or a lab test name when looking at lab.
    """
    item_q = q.strip()
    kind = kind.strip().lower()
    source = source.strip().lower()
    if source and source not in ("opd", "pharmacy", "lab", "xray", "usg"):
        raise ValueError("source must be opd, pharmacy, lab, xray, or usg")
    if not any([date_from, date_to, kind, source, item_q]):
        raise ValueError("At least one filter is required")

    query = (
        db.query(Invoice)
        .options(joinedload(Invoice.lines), joinedload(Invoice.patient))
        .filter(Invoice.patient_id.isnot(None), Invoice.branch_id == branch_id)
    )
    if date_from:
        query = query.filter(Invoice.created_at >= datetime.combine(date_from, time.min))
    if date_to:
        query = query.filter(Invoice.created_at < datetime.combine(date_to + timedelta(days=1), time.min))
    if kind:
        query = query.filter(Invoice.kind == kind)

    like = f"%{item_q}%" if item_q else None
    if source == "lab":
        line = exists().where(InvoiceLine.invoice_id == Invoice.id, InvoiceLine.source == "lab")
        if like:
            line = exists().where(
                InvoiceLine.invoice_id == Invoice.id,
                InvoiceLine.source == "lab",
                InvoiceLine.description.ilike(like),
            )
            lab = exists().where(LabOrder.invoice_id == Invoice.id, LabOrder.tests.ilike(like))
            query = query.filter(or_(line, lab))
        else:
            query = query.filter(line)
    elif source == "pharmacy":
        conds = [InvoiceLine.invoice_id == Invoice.id, InvoiceLine.source == "pharmacy"]
        if like:
            conds.append(InvoiceLine.description.ilike(like))
        query = query.filter(exists().where(*conds))
    elif source == "opd":
        conds = [InvoiceLine.invoice_id == Invoice.id, InvoiceLine.source == "opd"]
        if like:
            conds.append(InvoiceLine.description.ilike(like))
        query = query.filter(exists().where(*conds))
    elif source in ("xray", "usg"):
        conds = [RadiologyOrder.invoice_id == Invoice.id, RadiologyOrder.modality == source]
        query = query.filter(exists().where(*conds))
        if like:
            query = query.filter(
                exists().where(
                    InvoiceLine.invoice_id == Invoice.id,
                    InvoiceLine.description.ilike(like),
                )
            )
    elif like:
        query = query.filter(
            or_(
                exists().where(InvoiceLine.invoice_id == Invoice.id, InvoiceLine.description.ilike(like)),
                exists().where(LabOrder.invoice_id == Invoice.id, LabOrder.tests.ilike(like)),
            )
        )

    seen_ids: set[int] = set()
    invoices = []
    for inv in query.order_by(Invoice.created_at.desc()).limit(400).all():
        if inv.id in seen_ids:
            continue
        seen_ids.add(inv.id)
        invoices.append(inv)
    grouped: dict[int, dict] = {}
    for inv in invoices:
        patient = inv.patient
        if not patient:
            continue
        row = grouped.get(patient.id)
        if row is None:
            if len(grouped) >= limit:
                continue
            row = {
                "patient_id": patient.id,
                "name": patient.name,
                "uhid": patient.uhid,
                "phone": patient.phone or "",
                "father_name": patient.father_name or "",
                "match_count": 0,
                "last_visit": inv.created_at.isoformat() if inv.created_at else None,
                "matches": [],
            }
            grouped[patient.id] = row
        row["match_count"] += 1
        snippets = [line.description for line in inv.lines if line.description]
        if source:
            wanted = "radiology" if source in ("xray", "usg") else source
            snippets = [line.description for line in inv.lines if line.source == wanted and line.description]
        if like:
            snippets = [s for s in snippets if item_q.lower() in s.lower()] or snippets[:1]
        for snippet in snippets:
            if snippet not in row["matches"] and len(row["matches"]) < 3:
                row["matches"].append(snippet)
    return list(grouped.values())


_SOURCE_LABELS = {
    "opd": "Consultation / OPD",
    "pharmacy": "Pharmacy",
    "radiology": "Radiology / X-ray",
    "lab": "Laboratory",
    "ipd_room": "IPD Room Charge",
}


def patient_medical_history(db: Session, *, patient_id: int, branch_id: int | None = None) -> dict:
    patient = db.get(Patient, patient_id)
    if not patient:
        raise ValueError("Patient not found")

    inv_q = (
        db.query(Invoice)
        .options(joinedload(Invoice.lines))
        .filter(Invoice.patient_id == patient_id)
    )
    if branch_id:
        inv_q = inv_q.filter(Invoice.branch_id == branch_id)
    invoices = inv_q.order_by(Invoice.created_at.desc()).limit(200).all()

    clinical = (
        db.query(Visit)
        .filter(Visit.patient_id == patient_id)
        .order_by(Visit.created_at.desc())
        .limit(200)
        .all()
    )

    doctor_ids = {inv.doctor_id for inv in invoices if inv.doctor_id}
    doctor_ids |= {v.doctor_id for v in clinical if v.doctor_id}
    doctors: dict[int, str] = {}
    if doctor_ids:
        doctors = {u.id: u.full_name for u in db.query(User).filter(User.id.in_(doctor_ids)).all()}

    rad_by_inv: dict[int, list] = {}
    for order in (
        db.query(RadiologyOrder)
        .filter(RadiologyOrder.patient_id == patient_id)
        .order_by(RadiologyOrder.id)
        .all()
    ):
        if order.invoice_id:
            rad_by_inv.setdefault(order.invoice_id, []).append(order)
    lab_by_inv: dict[int, list] = {}
    for order in (
        db.query(LabOrder)
        .filter(LabOrder.patient_id == patient_id)
        .order_by(LabOrder.id)
        .all()
    ):
        if order.invoice_id:
            lab_by_inv.setdefault(order.invoice_id, []).append(order)

    visits_out = []
    for inv in invoices:
        by_source: dict[str, list[dict]] = {}
        for line in inv.lines:
            src = line.source or "other"
            by_source.setdefault(src, []).append({"description": line.description, "qty": line.qty, "source": src})
        treatments = [
            {
                "category": src,
                "label": _SOURCE_LABELS.get(src, src.replace("_", " ").title()),
                "items": items,
            }
            for src, items in by_source.items()
        ]
        rads = rad_by_inv.get(inv.id, [])
        labs = lab_by_inv.get(inv.id, [])
        visits_out.append(
            {
                "invoice_id": inv.id,
                "bill_number": inv.number,
                "visit_date": inv.created_at.isoformat() if inv.created_at else None,
                "type": inv.kind,
                "status": inv.status,
                "doctor_name": doctors.get(inv.doctor_id, "") if inv.doctor_id else "",
                "treatments": treatments,
                "radiology_findings": "\n\n".join(o.findings for o in rads if o.findings),
                "lab_result": "\n\n".join(o.result for o in labs if o.result),
                "lab_tests": ", ".join(o.tests for o in labs if o.tests),
                "lab_orders": [{"order_id": o.id, "tests": o.tests, "status": o.status} for o in labs],
                "radiology_orders": [{"order_id": o.id, "modality": o.modality, "status": o.status} for o in rads],
            }
        )

    clinical_notes = []
    for v in clinical:
        if not (v.diagnosis or v.chief_complaint or v.notes):
            continue
        clinical_notes.append(
            {
                "date": v.created_at.isoformat() if v.created_at else None,
                "doctor_name": doctors.get(v.doctor_id, "") if v.doctor_id else "",
                "chief_complaint": v.chief_complaint,
                "diagnosis": v.diagnosis,
                "notes": v.notes,
            }
        )

    return {"patient": patient, "visits": visits_out, "clinical_notes": clinical_notes}

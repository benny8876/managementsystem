from datetime import date

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core import clock
from app.models.billing import Invoice, InvoiceLine, Payment
from app.models.catalog import CatalogItem
from app.models.ipd import Admission, Bed
from app.models.users import Commission, User
from app.services.accounting_service import post_payment, post_refund
from app.services.utils import next_number


EDIT_REFUND_TAG = "Bill edit:"


def recalc_invoice(inv: Invoice, db: Session | None = None):
    if inv.status == "void":  # a cancelled bill stays cancelled with nothing owed
        inv.total = 0
        inv.balance = 0
        return
    if db is not None:
        inv.subtotal = float(
            db.query(func.coalesce(func.sum(InvoiceLine.amount), 0.0))
            .filter(InvoiceLine.invoice_id == inv.id)
            .scalar()
            or 0
        )
        has_lines = (
            db.query(func.count(InvoiceLine.id)).filter(InvoiceLine.invoice_id == inv.id).scalar() or 0
        ) > 0
        gross = float(
            db.query(func.coalesce(func.sum(Payment.amount), 0.0))
            .filter(Payment.invoice_id == inv.id, Payment.method != "refund")
            .scalar()
            or 0
        )
        refunded = float(
            db.query(func.coalesce(func.sum(Payment.amount), 0.0))
            .filter(Payment.invoice_id == inv.id, Payment.method == "refund")
            .scalar()
            or 0
        )
        edit_ref = float(
            db.query(func.coalesce(func.sum(Payment.amount), 0.0))
            .filter(Payment.invoice_id == inv.id, Payment.method == "refund", Payment.notes.like(EDIT_REFUND_TAG + "%"))
            .scalar()
            or 0
        )
        inv.paid = max(gross - refunded, 0)
    else:
        inv.subtotal = sum(l.amount for l in inv.lines)
        has_lines = bool(inv.lines)
        gross = sum(p.amount for p in inv.payments if p.method != "refund")
        refunded = sum(p.amount for p in inv.payments if p.method == "refund")
        edit_ref = sum(p.amount for p in inv.payments if p.method == "refund" and (p.notes or "").startswith(EDIT_REFUND_TAG))
        inv.paid = max(gross - refunded, 0)
    # A refund is money given back. The part that only returns an overpayment
    # (paid more than the bill, e.g. an IPD deposit) leaves the bill as it is;
    # the rest takes that much off the bill (service cancelled / charged too
    # much), so the bill's total drops with it and it does not show as owing.
    # A refund made because a History edit lowered the bill (EDIT_REFUND_TAG) only
    # hands back what was overpaid — the edit itself already lowered the bill.
    charged = max(inv.subtotal - inv.discount, 0)
    overpaid = max(gross - edit_ref - charged, 0)
    inv.total = max(charged - max(refunded - edit_ref - overpaid, 0), 0)
    inv.balance = max(inv.total - inv.paid, 0)

    admission_ongoing = False
    if inv.admission_id and db is not None:
        admission = db.get(Admission, inv.admission_id)
        admission_ongoing = bool(admission and admission.status != "discharged")

    fully_discounted = has_lines and inv.total <= 0.01

    if inv.balance <= 0.01 and (inv.paid > 0.01 or fully_discounted) and not admission_ongoing:
        inv.status = "paid"
    elif inv.balance <= 0.01 and inv.total <= 0.01 and not has_lines:
        inv.status = "draft"
    elif getattr(inv, "credit_at", None) is not None and inv.balance > 0.01:
        inv.status = "credit"  # 📒 closed on credit: still owed, but not an Open Bill
    elif inv.paid > 0:
        inv.status = "partial"
    else:
        inv.status = "open" if has_lines else "draft"
    # Reports count a bill on the day it is fully paid (the last Pay).
    if inv.status == "paid":
        if inv.paid_at is None:
            inv.paid_at = clock.now()
    elif inv.status != "void":
        inv.paid_at = None


def add_line(
    db: Session,
    inv: Invoice,
    item: CatalogItem | None,
    qty: float,
    unit_price: float | None,
    source: str,
    description: str = "",
    doctor: User | None = None,
    discount: float = 0,
    batch_id: int | None = None,
    unit_cost: float = 0,
):
    price = unit_price if unit_price is not None else (item.price if item else 0)
    amount = max(qty * price - discount, 0)
    df_amount = 0.0
    doc_id = doctor.id if doctor else None
    if doctor and item and item.doctor_fee_pct:
        df_amount = amount * item.doctor_fee_pct / 100
    elif doctor and source == "opd" and doctor.consultation_fee:
        df_amount = doctor.consultation_fee
    line = InvoiceLine(
        invoice_id=inv.id,
        item_id=item.id if item else None,
        batch_id=batch_id,
        source=source,
        description=description or (item.name if item else ""),
        qty=qty,
        unit_price=price,
        unit_cost=unit_cost,
        discount=discount,
        amount=amount,
        doctor_id=doc_id,
        df_amount=df_amount,
    )
    inv.lines.append(line)
    db.flush()
    recalc_invoice(inv, db)
    if doctor and df_amount > 0:
        pct = doctor.commission_pct or 100
        db.add(Commission(user_id=doctor.id, invoice_id=inv.id, kind="doctor", amount=df_amount * pct / 100))
    return line


FEE_TYPES = ("", "night", "half", "foc")


def update_line(
    db: Session, inv: Invoice, line: InvoiceLine, unit_price: float | None = None, qty: float | None = None, fee_type: str | None = None,
    doctor_id: int | None = None,
) -> InvoiceLine:
    if unit_price is not None:
        if unit_price < 0:
            raise ValueError("Price cannot be negative")
        line.unit_price = unit_price
    if qty is not None:
        if qty <= 0:
            raise ValueError("Qty must be positive")
        _adjust_line_stock(db, inv, line, qty)
        line.qty = qty
    if fee_type is not None:
        if fee_type not in FEE_TYPES:
            raise ValueError("fee_type must be normal, night, half or foc")
        if not (line.doctor_id and line.source == "opd"):
            raise ValueError("Only a doctor-fee line has a fee type")
        line.fee_type = fee_type
    if doctor_id is not None:
        _set_lab_doctor(db, inv, line, doctor_id)
    line.amount = max(line.qty * line.unit_price - line.discount, 0)
    # A doctor-fee line's doctor share follows its price (FOC 0, back to normal = full fee).
    if line.doctor_id and line.source in ("opd", "round", "surgery"):
        line.df_amount = line.amount
    db.flush()
    recalc_invoice(inv, db)
    return line


def _set_lab_doctor(db: Session, inv: Invoice, line: InvoiceLine, doctor_id: int) -> None:
    """Which doctor ordered a lab test / X-ray / USG (0 = none / outside doctor) —
    for the Lab / X-ray / USG by Doctor reports. Its returns and its order follow."""
    from app.models.ancillary import LabOrder, RadiologyOrder

    if line.source not in ("lab", "radiology") or line.qty <= 0 or line.returned_from_line_id:
        raise ValueError("Only a lab / X-ray / USG line has an ordering doctor")
    if doctor_id:
        doctor = db.get(User, doctor_id)
        if not doctor or doctor.role != "doctor":
            raise ValueError("Doctor not found")
    new_id = doctor_id or None
    old_id = line.doctor_id
    line.doctor_id = new_id
    for l in inv.lines:
        if l.returned_from_line_id == line.id:
            l.doctor_id = new_id
    if line.source == "lab":
        orders = db.query(LabOrder).filter(LabOrder.invoice_id == inv.id, LabOrder.tests == line.description).all()
    else:
        item = line.item
        modality = "usg" if item is not None and item.department == "usg" else ((item.sku or "").lower() if item is not None else "")
        orders = db.query(RadiologyOrder).filter(RadiologyOrder.invoice_id == inv.id, RadiologyOrder.modality == modality).all()
    same = [l for l in inv.lines if l.source == line.source and l.qty > 0 and l.item_id == line.item_id]
    if len(same) == 1 and len(orders) == 1:
        orders[0].doctor_id = new_id
    else:
        match = next((o for o in orders if o.doctor_id == old_id), None)
        if match:
            match.doctor_id = new_id


def pay_invoice(db: Session, inv: Invoice, method: str, amount: float, user_id: int, shift_id: int | None = None, allow_overpay: bool = False, notes: str = "", is_deposit: bool = False):
    if amount <= 0:
        raise ValueError("Payment amount must be positive")
    recalc_invoice(inv, db)
    if not allow_overpay and amount > inv.balance + 0.01:
        raise ValueError(f"Amount exceeds balance ({inv.balance:.0f})")
    p = Payment(invoice_id=inv.id, shift_id=shift_id, method=method, amount=amount, received_by=user_id, notes=(notes or "").strip()[:200],
                is_deposit=bool(is_deposit or method == "deposit"))
    inv.payments.append(p)
    db.flush()
    recalc_invoice(inv, db)
    post_payment(db, inv, p)
    return p


def refund_payment(
    db: Session,
    inv: Invoice,
    *,
    amount: float,
    user_id: int,
    method: str = "",
    reason: str = "",
    shift_id: int | None = None,
) -> Payment:
    """Records money paid back to the patient — a correction to an existing
    payment (wrong amount entered, cancelled service already paid for), not a
    new charge. Stored as its own Payment row (method="refund", amount kept
    positive like every other payment) rather than mutating the original
    payment, so both the original collection and the refund stay visible in
    the invoice's payment history."""
    if amount <= 0:
        raise ValueError("Refund amount must be positive")
    recalc_invoice(inv, db)
    if amount > inv.paid + 0.01:
        raise ValueError(f"Refund cannot exceed amount paid ({inv.paid:.0f})")
    refund_method = method or "cash"
    p = Payment(invoice_id=inv.id, shift_id=shift_id, method="refund", refund_method=refund_method, amount=amount, received_by=user_id, notes=reason)
    inv.payments.append(p)
    db.flush()
    recalc_invoice(inv, db)
    post_refund(db, inv, p, refund_method)
    return p


def _adjust_line_stock(db: Session, inv: Invoice, line: InvoiceLine, new_qty: float) -> None:
    """A stock item's qty on a bill changed (or the line is removed, new_qty 0):
    put the difference back on the shelf, or take the extra out (oldest expiry
    first) from the store it was sold from, so stock always matches the bill."""
    from app.models.inventory import StockBatch
    from app.models.org import Warehouse
    from app.services.inventory_service import dispense

    item = line.item
    if item is None or not item.is_stock or line.qty <= 0 or line.returned_from_line_id:
        return
    if any(l.returned_from_line_id == line.id for l in inv.lines):
        raise ValueError("Lines with returns can't be changed — use Return")
    delta = new_qty - line.qty
    if abs(delta) < 1e-9:
        return
    if delta < 0:
        _restock_returned(db, inv, line, -delta, reason="bill_edit")
        return
    batch = db.get(StockBatch, line.batch_id) if line.batch_id else None
    wh_id = batch.warehouse_id if batch else None
    if wh_id is None:
        wh = (
            db.query(Warehouse)
            .filter(Warehouse.branch_id == inv.branch_id)
            .order_by(Warehouse.is_default.desc(), Warehouse.id)
            .first()
        )
        if not wh:
            raise ValueError("No store for this branch")
        wh_id = wh.id
    dispense(db, item, wh_id, delta, f"inv-{inv.id}", None)


def _cancel_service_orders(db: Session, inv: Invoice, line: InvoiceLine, count: float) -> None:
    """A lab / X-ray / USG test taken off the bill (removed or returned): cancel
    that many of its orders that have no result yet, so the counter doesn't do
    a test nobody pays for. Tests already done are left as they are."""
    from app.models.ancillary import LabOrder, RadiologyOrder

    n = int(round(count))
    if line.source not in ("lab", "radiology") or n <= 0:
        return
    if line.source == "lab":
        q = db.query(LabOrder).filter(LabOrder.invoice_id == inv.id, LabOrder.tests == line.description)
        q = q.filter(LabOrder.status.notin_(["completed", "cancelled"])).order_by(LabOrder.id.desc())
    else:
        item = line.item
        if item is None:
            return
        modality = "usg" if item.department == "usg" else (item.sku or "").lower()
        q = db.query(RadiologyOrder).filter(RadiologyOrder.invoice_id == inv.id, RadiologyOrder.modality == modality)
        q = q.filter(RadiologyOrder.status.notin_(["completed", "cancelled"])).order_by(RadiologyOrder.id.desc())
    for order in q.limit(n).all():
        order.status = "cancelled"


def _restock_returned(db: Session, inv: Invoice, line: InvoiceLine, qty: float, reason: str = "return") -> None:
    """Puts returned medicine back on the shelf: into the batch it was sold
    from, or this branch's newest batch of that item if that's gone."""
    from app.models.inventory import StockBatch, StockMovement
    from app.models.org import Warehouse

    batch = db.get(StockBatch, line.batch_id) if line.batch_id else None
    if batch is None:
        wh_ids = [w.id for w in db.query(Warehouse).filter(Warehouse.branch_id == inv.branch_id).all()]
        batch = (
            db.query(StockBatch)
            .filter(StockBatch.item_id == line.item_id, StockBatch.warehouse_id.in_(wh_ids))
            .order_by(StockBatch.id.desc())
            .first()
        )
    if batch is None:
        raise ValueError("No stock batch to return this medicine to")
    batch.qty += qty
    db.add(StockMovement(item_id=line.item_id, batch_id=batch.id, qty_delta=qty, reason=reason, ref=f"inv-{inv.id}"))


def return_line(
    db: Session,
    inv: Invoice,
    line: InvoiceLine,
    *,
    qty: float,
    reason: str,
    user_id: int,
    refund_method: str = "cash",
    restock: bool = True,
    shift_id: int | None = None,
    refund: bool = True,
) -> tuple[InvoiceLine, float]:
    """A patient gives back (or never collects) something already billed —
    medicine handed back, a test/service they decided not to have. The original
    line stays as it was; a negative "Return: ..." line records what came back
    (so DF and pharmacy cost reverse with it), medicine goes back into stock,
    and whatever the patient has now overpaid is refunded in the same step.
    Returns (return line, amount refunded)."""
    reason = (reason or "").strip()
    if not reason:
        raise ValueError("Return reason is required")
    if line.qty <= 0 or line.returned_from_line_id:
        raise ValueError("This line can't be returned")
    already = -sum(l.qty for l in inv.lines if l.returned_from_line_id == line.id)
    left = line.qty - already
    if qty <= 0 or qty > left + 1e-9:
        raise ValueError(f"Can return at most {left:g}")
    per_unit = (line.amount or 0) / line.qty
    per_df = (line.df_amount or 0) / line.qty
    ret = InvoiceLine(
        invoice_id=inv.id, item_id=line.item_id, batch_id=line.batch_id, source=line.source,
        description=f"Return: {line.description}", qty=-qty, unit_price=line.unit_price,
        unit_cost=line.unit_cost, discount=0, amount=-round(per_unit * qty, 2),
        doctor_id=line.doctor_id, df_amount=-round(per_df * qty, 2), returned_from_line_id=line.id,
    )
    inv.lines.append(ret)
    db.flush()
    if restock and line.item is not None and line.item.is_stock:
        _restock_returned(db, inv, line, qty)
    _cancel_service_orders(db, inv, line, qty)
    recalc_invoice(inv, db)
    if not refund:
        return ret, 0.0
    amount = round(min(inv.paid - inv.total, -ret.amount), 2)
    if amount > 0.01:
        refund_payment(
            db, inv, amount=amount, user_id=user_id, method=refund_method,
            reason=f"Return: {line.description} — {reason}", shift_id=shift_id,
        )
    else:
        amount = 0.0
    return ret, amount


def void_line(db: Session, inv: Invoice, line: InvoiceLine) -> None:
    """Removes a wrongly-billed line entirely (not a soft-delete — there's no
    voided/status column on InvoiceLine) and recalculates the invoice. Doctor
    commission tied to this line (Commission has no line_id, only
    invoice_id) is deliberately left untouched rather than guessed at —
    reversing it correctly would need a per-line link that doesn't exist."""
    if line.returned_from_line_id or line.qty < 0 or any(l.returned_from_line_id == line.id for l in inv.lines):
        raise ValueError("Lines with returns can't be removed")
    recalc_invoice(inv, db)
    # A patient still in the ward pays deposits, not for items: removing a line
    # just leaves more deposit, handed back (or used) at discharge.
    adm = db.get(Admission, inv.admission_id) if inv.kind == "ipd" and inv.admission_id else None
    in_ward = bool(adm and adm.status != "discharged")
    if not in_ward and inv.paid > 0.01 and line.amount > inv.balance + 0.01:
        # Removing it would leave the patient overpaid with no refund recorded
        # (and medicine never put back) — that's what Return is for.
        raise ValueError("This item is already paid for — use Return so the money is refunded")
    _adjust_line_stock(db, inv, line, 0)
    _cancel_service_orders(db, inv, line, line.qty)
    old_doc = line.doctor_id if line.source == "opd" else None
    inv.lines.remove(line)
    db.delete(line)
    db.flush()
    if old_doc and inv.doctor_id == old_doc:
        _hand_bill_to_next_doctor(db, inv, old_doc)
    recalc_invoice(inv, db)


def _hand_bill_to_next_doctor(db: Session, inv: Invoice, old_doc: int) -> None:
    """The bill's own doctor had their consultation removed (wrong doctor
    picked): unless they still have a fee line here, the bill — and the
    lab / X-ray / service lines that were credited to them — move to the next
    doctor on the bill, or to no doctor until one is added. Otherwise History
    and the doctor reports keep showing the bill under the wrong doctor."""
    from app.models.ancillary import LabOrder, RadiologyOrder

    fee_srcs = ("opd", "round", "surgery")
    if any(l.doctor_id == old_doc and l.source in fee_srcs and (l.qty or 0) > 0 for l in inv.lines):
        return
    new_doc = next((l.doctor_id for l in inv.lines if l.source == "opd" and l.doctor_id and (l.qty or 0) > 0), None)
    inv.doctor_id = new_doc
    for l in inv.lines:
        if l.doctor_id == old_doc and l.source not in fee_srcs and not (l.df_amount or 0):
            l.doctor_id = new_doc
    for model in (LabOrder, RadiologyOrder):
        db.query(model).filter(model.invoice_id == inv.id, model.doctor_id == old_doc).update(
            {model.doctor_id: new_doc}, synchronize_session=False)
    db.flush()


def pay_invoice_multi(db: Session, inv: Invoice, payments: list, user_id: int, shift_id: int | None = None, notes: str = ""):
    if not payments:
        raise ValueError("Add at least one payment")
    recalc_invoice(inv, db)
    total = sum(float(p.amount) for p in payments)
    if total <= 0:
        raise ValueError("Invalid payment total")
    if total > inv.balance + 0.01:
        raise ValueError(f"Payment total exceeds balance ({inv.balance:.0f})")
    created = []
    for p in payments:
        amt = float(p.amount)
        if amt <= 0:
            continue
        created.append(pay_invoice(db, inv, p.method, amt, user_id, shift_id, notes=getattr(p, "notes", "") or notes))
    return created


IPD_DEPOSIT_METHODS = frozenset({"deposit", "cash", "kpay", "wave", "kbzpay", "card"})


def collect_ipd_deposit(
    db: Session,
    *,
    admission_id: int,
    amount: float,
    method: str,
    user_id: int,
    shift_id: int | None = None,
    notes: str = "",
) -> tuple[Invoice, Admission]:
    from app.services.utils import audit

    adm = db.get(Admission, admission_id)
    if not adm:
        raise ValueError("Admission not found")
    if adm.status not in ("admitted", "transferred", "pending_bed"):
        raise ValueError("Patient is not an active IPD admission")
    m = (method or "deposit").lower()
    if m not in IPD_DEPOSIT_METHODS:
        raise ValueError("Invalid payment method for IPD deposit")
    inv = db.query(Invoice).filter(Invoice.admission_id == admission_id, Invoice.kind == "ipd").order_by(Invoice.id.desc()).first()
    if not inv:
        raise ValueError("No IPD invoice for this admission")
    pay_invoice(db, inv, m, float(amount), user_id, shift_id, allow_overpay=True, notes=notes, is_deposit=True)
    adm.deposit = float(adm.deposit or 0) + float(amount)
    audit(db, user_id, "ipd_deposit", "admission", str(admission_id), f"{m} amount={amount}")
    db.flush()
    return inv, adm


def add_round_fee(db: Session, inv: Invoice, doctor: User, unit_price: float, qty: float = 1) -> InvoiceLine:
    """IPD doctor round fee (ward round): a price set each time (not fixed per
    doctor) x number of rounds. Its own line each time it is added, so the
    day bill shows the rounds on the day they were charged; all of it is the
    doctor's share."""
    if doctor is None or doctor.role != "doctor":
        raise ValueError("Doctor not found")
    if unit_price is None or unit_price < 0:
        raise ValueError("Round fee ဈေး ထည့်ပါ")
    if not qty or qty <= 0:
        raise ValueError("အကြိမ်ရေ ထည့်ပါ")
    line = add_line(db, inv, None, qty, unit_price, "round", f"Round fee — {doctor.full_name}", doctor)
    line.df_amount = line.amount
    db.flush()
    recalc_invoice(inv, db)
    return line


# OT roles are free text (Surgeon, Anaesthetist, Scrub nurse, …); the first three
# were stored as short keys before, so those keys still read as their names.
SURGERY_ROLES = {"surgeon": "Surgeon", "anaesth": "Anaesthetist", "assistant": "Assistant"}


def surgery_role(role: str | None) -> str:
    r = " ".join((role or "").split())[:30] or "Surgeon"
    return SURGERY_ROLES.get(r.lower(), r)


def add_surgery_fee(db: Session, inv: Invoice, doctor: User, unit_price: float, role: str = "surgeon", procedure: str = "") -> InvoiceLine:
    """OT doctor fee (surgeon / anaesthetist / assistant) — a price set each
    time; all of it is the doctor's share. Shown under OT on the bill."""
    if doctor is None or doctor.role != "doctor":
        raise ValueError("Doctor not found")
    if unit_price is None or unit_price < 0:
        raise ValueError("OT ဆရာဝန်ခ ဈေး ထည့်ပါ")
    role = surgery_role(role)
    label = f"{role} fee"
    proc = (procedure or "").strip()
    line = add_line(db, inv, None, 1, unit_price, "surgery", f"{label} — {doctor.full_name}" + (f" ({proc})" if proc else ""), doctor)
    line.fee_type = role
    line.df_amount = line.amount
    db.flush()
    recalc_invoice(inv, db)
    return line


def add_room_charge(db: Session, inv: Invoice, label: str, rate: float, days: float = 1):
    """One bill line per bed stay — extra days raise that line's qty instead of
    adding a new "Room charge ..." line for every day of the admission."""
    for line in inv.lines:
        if line.source == "ipd_room" and line.description == label and line.unit_price == rate and not line.discount:
            line.qty = (line.qty or 0) + days
            line.amount = line.qty * line.unit_price
            db.flush()
            recalc_invoice(inv, db)
            return line
    return add_line(db, inv, None, days, rate, "ipd_room", label)


def ensure_daily_room_charges(db: Session, adm: Admission) -> int:
    """Catches up any room-charge days owed since `last_room_charge_date`,
    so ward staff no longer have to remember to click "Post room charge"
    every day. There's no background job runner in this codebase, so this
    is called opportunistically wherever an active admission is read (IPD
    dashboard, admissions list, discharge) rather than on a schedule.
    Idempotent per calendar day: advances `last_room_charge_date` to today,
    so calling it again the same day is a no-op. Only `billing_mode ==
    "daily"` accrues this way — "package" is a single flat fee already
    posted at admission, and "hourly" isn't billed per calendar day.
    Returns the number of days charged."""
    if adm.status not in ("admitted", "transferred") or not adm.bed_id or adm.billing_mode != "daily":
        return 0
    today = clock.today()
    if adm.discharge_approved_at:
        # Doctor already cleared the patient to go — no more room days after that.
        today = min(today, adm.discharge_approved_at.date())
    last = adm.last_room_charge_date or (adm.admitted_at.date() if adm.admitted_at else today)
    days_owed = (today - last).days
    if days_owed <= 0:
        return 0
    bed = db.get(Bed, adm.bed_id)
    inv = db.query(Invoice).filter(Invoice.admission_id == adm.id, Invoice.kind == "ipd").order_by(Invoice.id.desc()).first()
    if not bed or not inv:
        return 0
    label = f"Room charge {bed.code} ({adm.billing_mode})"
    add_room_charge(db, inv, label, bed.daily_rate, days_owed)
    adm.last_room_charge_date = today
    db.flush()
    return days_owed


def move_today_room_charge(db: Session, adm: Admission, old_bed: Bed | None, new_bed: Bed) -> bool:
    """Room change: the day of the move is charged at the NEW room, so the new
    room shows on the bill at once (it used to appear only the next day). Today
    was already charged at the old room, so that one day moves across — the
    number of room days stays the same. Daily billing only."""
    if adm.billing_mode != "daily" or adm.last_room_charge_date != clock.today():
        return False
    inv = db.query(Invoice).filter(Invoice.admission_id == adm.id, Invoice.kind == "ipd").order_by(Invoice.id.desc()).first()
    if not inv:
        return False
    if old_bed is not None:
        old_label = f"Room charge {old_bed.code} (daily)"
        old = [ln for ln in inv.lines if ln.source == "ipd_room" and ln.description == old_label and (ln.qty or 0) >= 1]
        # Only when the old room keeps at least one day: a room the patient
        # was in stays on the bill (moved the same day = both rooms charged).
        if old and (old[-1].qty or 0) >= 2:
            ln = old[-1]
            ln.qty = (ln.qty or 0) - 1
            ln.amount = ln.qty * ln.unit_price
            db.flush()
    add_room_charge(db, inv, f"Room charge {new_bed.code} (daily)", new_bed.daily_rate, 1)
    recalc_invoice(inv, db)
    return True


def create_invoice(db: Session, branch_id: int, patient_id: int | None, kind: str = "opd", doctor_id: int | None = None):
    inv = Invoice(
        number=next_number(db, "invoice", "INV-"),
        patient_id=patient_id,
        branch_id=branch_id,
        doctor_id=doctor_id,
        kind=kind,
    )
    db.add(inv)
    db.flush()
    return inv


def void_invoice(db: Session, inv: Invoice, *, user_id: int, reason: str, shift_id: int | None = None) -> float:
    """Cancel a whole bill: medicine back on the shelf, unfinished lab/X-ray
    orders cancelled, any money taken recorded as refunded. The bill stays in
    History as VOID with nothing owed. Returns the amount refunded."""
    from app.models.ipd import Admission
    from app.services.utils import audit

    if inv.status == "void":
        raise ValueError("ပယ်ပြီးသား bill ပါ")
    if inv.admission_id:
        adm = db.get(Admission, inv.admission_id)
        if adm and adm.status != "discharged":
            raise ValueError("IPD လူနာ တက်နေဆဲ bill ပါ — ဆေးရုံဆင်းပြီးမှ ပယ်ပါ")
    for line in list(inv.lines):
        if line.returned_from_line_id or line.qty <= 0:
            continue
        given_back = -sum(l.qty for l in inv.lines if l.returned_from_line_id == line.id)
        left = line.qty - given_back
        if left <= 1e-9:
            continue
        if line.item is not None and line.item.is_stock:
            _restock_returned(db, inv, line, left, reason="void")
        _cancel_service_orders(db, inv, line, left)
    recalc_invoice(inv, db)
    refunded = 0.0
    if inv.paid > 0.01:
        refunded = float(inv.paid)
        refund_payment(db, inv, amount=refunded, user_id=user_id, method="cash", reason=f"Bill ပယ်: {reason}"[:200], shift_id=shift_id)
    if inv.patient_id and inv.created_at:
        from app.models.clinical import QueueToken

        # The visit is cancelled, so its OPD token leaves the queue too.
        db.query(QueueToken).filter(
            QueueToken.patient_id == inv.patient_id,
            QueueToken.queue_date == inv.created_at.date(),
            QueueToken.status.in_(["waiting", "serving"]),
        ).update({QueueToken.status: "skipped"}, synchronize_session=False)
    inv.status = "void"
    inv.total = 0
    inv.balance = 0
    inv.paid_at = None
    audit(db, user_id, "invoice_void", "invoice", str(inv.id), f"{inv.number} refund={refunded:,.0f} — {reason}"[:200])
    db.flush()
    return refunded


def unvoid_invoice(db: Session, inv: Invoice, *, user_id: int, reason: str) -> None:
    """↩ Undo a Void done by mistake: the bill comes back as it was — medicine
    put back on the shelf by the Void is taken off again, the "Bill ပယ်" refund
    is removed (ledger reversed) and lab / X-ray orders the Void cancelled are
    ordered again. Then it can be edited like any bill."""

    from app.models.ancillary import LabOrder, RadiologyOrder
    from app.models.inventory import StockBatch, StockMovement
    from app.services.accounting_service import PAYMENT_ACCOUNTS, post_entry
    from app.services.utils import audit

    if inv.status != "void":
        raise ValueError("Void ဖြစ်ထားတဲ့ bill မဟုတ်ပါ")
    ref = f"inv-{inv.id}"
    # Stock: undo what the Void(s) put back (net of earlier un-voids), batch by batch.
    net: dict[int, float] = {}
    for m in db.query(StockMovement).filter(StockMovement.ref == ref, StockMovement.reason.in_(["void", "unvoid"])):
        if m.batch_id:
            net[m.batch_id] = net.get(m.batch_id, 0.0) + float(m.qty_delta or 0)
    for batch_id, q in net.items():
        if q <= 1e-9:
            continue
        b = db.get(StockBatch, batch_id)
        if b is None:
            continue
        b.qty -= q
        db.add(StockMovement(item_id=b.item_id, batch_id=b.id, qty_delta=-q, reason="unvoid", ref=ref))
    # Money: remove the refund(s) the Void recorded.
    removed = 0.0
    for p in list(inv.payments):
        if p.method == "refund" and (p.notes or "").startswith("Bill ပယ်:"):
            amt = float(p.amount or 0)
            code = PAYMENT_ACCOUNTS.get(p.refund_method or "cash", "1000")
            post_entry(db, ref=f"UNVOID-{p.id}", description=f"Void undone for {inv.number}", lines=[(code, amt, 0), ("4000", 0, amt)])
            removed += amt
            inv.payments.remove(p)
            db.delete(p)
    # Lab / X-ray orders the Void cancelled: order them again.
    for line in inv.lines:
        if line.returned_from_line_id or line.qty <= 0 or line.source not in ("lab", "radiology"):
            continue
        left = line.qty + sum(l.qty for l in inv.lines if l.returned_from_line_id == line.id)
        n = int(round(left))
        if n <= 0:
            continue
        if line.source == "lab":
            base = db.query(LabOrder).filter(LabOrder.invoice_id == inv.id, LabOrder.tests == line.description)
            Model = LabOrder
        else:
            if line.item is None:
                continue
            modality = "usg" if line.item.department == "usg" else (line.item.sku or "").lower()
            base = db.query(RadiologyOrder).filter(RadiologyOrder.invoice_id == inv.id, RadiologyOrder.modality == modality)
            Model = RadiologyOrder
        active = base.filter(Model.status != "cancelled").count()
        for o in base.filter(Model.status == "cancelled").order_by(Model.id).limit(max(n - active, 0)).all():
            o.status = "ordered"
    db.flush()
    inv.status = "open"
    inv.paid_at = None
    recalc_invoice(inv, db)
    if inv.status == "paid":
        inv.paid_at = max((p.created_at for p in inv.payments if p.method != "refund" and p.created_at), default=None) or inv.paid_at
    audit(db, user_id, "invoice_unvoid", "invoice", str(inv.id),
          f"{inv.number} void undone · refund removed {removed:,.0f} · now {inv.status} — {reason}"[:300])
    db.flush()


def delete_invoice(db: Session, inv: Invoice, *, user_id: int, reason: str) -> str:
    """🗑 Delete a whole voucher made by mistake (e.g. Pay pressed on the wrong
    bill) — unlike Void, nothing is left in History: medicine goes back on the
    shelf, unfinished lab/X-ray orders are cancelled, every payment is taken off
    (its ledger entry reversed, so the day's money and the shift don't count it)
    and the bill is removed. The Activity Log keeps what it was."""
    from app.models.ancillary import LabOrder, OTSchedule, RadiologyOrder, ServiceOrder
    from app.models.ipd import Admission, WardMedOrder, WardMedOrderItem
    from app.models.users import Commission
    from app.services.accounting_service import PAYMENT_ACCOUNTS, post_entry
    from app.services.utils import audit

    adm = db.get(Admission, inv.admission_id) if inv.admission_id else None
    if adm is not None and adm.status != "discharged":
        raise ValueError("IPD လူနာ တက်နေဆဲ bill ပါ — ဖျက်လို့ မရပါ")
    recalc_invoice(inv, db)
    detail = (f"{inv.number} · {inv.patient.name if inv.patient else '—'} · {inv.kind} · total {float(inv.total or 0):,.0f}"
              f" · paid {float(inv.paid or 0):,.0f} · items: "
              + ", ".join(f"{l.description} x{l.qty:g}" for l in inv.lines)[:500])
    if inv.status != "void":
        for line in list(inv.lines):
            if line.returned_from_line_id or line.qty <= 0:
                continue
            given_back = -sum(l.qty for l in inv.lines if l.returned_from_line_id == line.id)
            left = line.qty - given_back
            if left <= 1e-9:
                continue
            if line.item is not None and line.item.is_stock:
                _restock_returned(db, inv, line, left, reason="delete")
            _cancel_service_orders(db, inv, line, left)
    for p in list(inv.payments):
        amt = float(p.amount or 0)
        if p.method == "refund":
            code = PAYMENT_ACCOUNTS.get(p.refund_method or "cash", "1000")
            post_entry(db, ref=f"DEL-REF-{p.id}", description=f"Deleted bill {inv.number}", lines=[(code, amt, 0), ("4000", 0, amt)])
        else:
            code = PAYMENT_ACCOUNTS.get(p.method, "1000")
            post_entry(db, ref=f"DEL-PAY-{p.id}", description=f"Deleted bill {inv.number}", lines=[("4000", amt, 0), (code, 0, amt)])
            if adm is not None:
                adm.deposit = max(float(adm.deposit or 0) - amt, 0)
        inv.payments.remove(p)
        db.delete(p)
    if inv.patient_id and inv.created_at:
        from app.models.clinical import QueueToken

        db.query(QueueToken).filter(
            QueueToken.patient_id == inv.patient_id,
            QueueToken.queue_date == inv.created_at.date(),
            QueueToken.status.in_(["waiting", "serving"]),
        ).update({QueueToken.status: "skipped"}, synchronize_session=False)
    line_ids = [l.id for l in inv.lines]
    if line_ids:
        db.query(WardMedOrderItem).filter(WardMedOrderItem.invoice_line_id.in_(line_ids)).update(
            {WardMedOrderItem.invoice_line_id: None}, synchronize_session=False)
    for model in (LabOrder, RadiologyOrder, OTSchedule, ServiceOrder, WardMedOrder):
        db.query(model).filter(model.invoice_id == inv.id).update({model.invoice_id: None}, synchronize_session=False)
    db.query(Commission).filter(Commission.invoice_id == inv.id).delete(synchronize_session=False)
    for l in list(inv.lines):
        db.delete(l)
    # SQLite hands a deleted bill's id to the next new bill, so move this bill's
    # stock moves and change log off "its" id — they must not attach to that one.
    from app.models.inventory import StockMovement
    from app.models.org import AuditLog

    tag = f"del-{inv.number}"
    db.query(StockMovement).filter(StockMovement.ref == f"inv-{inv.id}").update({StockMovement.ref: tag}, synchronize_session=False)
    db.query(AuditLog).filter(AuditLog.entity == "invoice", AuditLog.entity_id == str(inv.id)).update(
        {AuditLog.entity_id: tag}, synchronize_session=False)
    if line_ids:
        db.query(AuditLog).filter(AuditLog.entity == "invoice_line", AuditLog.entity_id.in_([str(i) for i in line_ids])).update(
            {AuditLog.entity_id: tag}, synchronize_session=False)
    audit(db, user_id, "invoice_delete", "invoice", tag, f"{detail} — {reason}"[:900])
    number = inv.number
    db.flush()
    db.delete(inv)
    db.flush()
    return number


def void_payment(db: Session, inv: Invoice, payment: Payment, *, user_id: int, reason: str) -> None:
    """A payment entered by mistake (wrong method / amount / bill): remove it and
    reverse its ledger entry, as if it had never been taken."""
    from app.models.ipd import Admission
    from app.services.accounting_service import PAYMENT_ACCOUNTS, post_entry
    from app.services.utils import audit

    if inv.status == "void":
        raise ValueError("ပယ်ပြီးသား bill ပါ")
    if payment.method == "refund":
        raise ValueError("Refund ကို ဒီနေရာက ပယ်လို့ မရပါ")
    gross = sum(p.amount for p in inv.payments if p.method != "refund")
    refunded = sum(p.amount for p in inv.payments if p.method == "refund")
    if gross - payment.amount < refunded - 0.01:
        raise ValueError("ဒီ bill မှာ ပြန်အမ်းထားတာ ရှိလို့ ဒီငွေကို ပယ်လို့ မရပါ")
    cash_code = PAYMENT_ACCOUNTS.get(payment.method, "1000")
    post_entry(db, ref=f"VOID-PAY-{payment.id}", description=f"Payment voided for {inv.number}", lines=[("4000", payment.amount, 0), (cash_code, 0, payment.amount)])
    if inv.admission_id:
        adm = db.get(Admission, inv.admission_id)
        if adm:
            adm.deposit = max(float(adm.deposit or 0) - float(payment.amount), 0)
    audit(db, user_id, "payment_void", "invoice", str(inv.id), f"{inv.number} {payment.method} {payment.amount:,.0f} — {reason}"[:200])
    inv.payments.remove(payment)
    db.delete(payment)
    db.flush()
    recalc_invoice(inv, db)

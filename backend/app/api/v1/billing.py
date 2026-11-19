from datetime import date, datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from app.core import clock
from app.core.deps import get_current_user, require
from app.db.session import get_db
from app.models.billing import CashierShift, Invoice, Payment
from app.models.catalog import CatalogItem, Promotion
from app.models.users import User
from app.models.billing import InvoiceLine
from app.schemas.actions import IpdDepositIn
from pydantic import BaseModel


class DoctorFeeIn(BaseModel):
    doctor_id: int
    unit_price: float | None = None  # cashier's own price (else the doctor's usual fee)
from app.schemas.common import InvoiceCreateIn, InvoiceLineIn, InvoiceLineOut, InvoiceLineUpdateIn, InvoiceOut, MultiPaymentIn, PaymentIn, PharmacyLineIn, RefundIn, InvoiceNoteIn, ReturnLineIn, ExchangeLineIn, OrderItemIn
from app.services.billing_service import add_line, collect_ipd_deposit, create_invoice, pay_invoice, pay_invoice_multi, recalc_invoice, refund_payment, return_line, update_line, void_line
from app.services.counter_service import exchange_line, order_on_invoice, pharmacy_charge
from app.services.utils import audit

router = APIRouter(tags=["billing"])


@router.get("/catalog")
def catalog(q: str = "", db: Session = Depends(get_db), _: User = Depends(get_current_user)):
    query = db.query(CatalogItem).filter(CatalogItem.is_active == True)  # noqa: E712
    if q:
        like = f"%{q}%"
        query = query.filter((CatalogItem.name.ilike(like)) | (CatalogItem.barcode.ilike(like)) | (CatalogItem.sku.ilike(like)))
    return query.limit(100).all()


@router.get("/promotions")
def promotions(db: Session = Depends(get_db), _: User = Depends(get_current_user)):
    return db.query(Promotion).filter(Promotion.is_active == True).all()  # noqa: E712


@router.post("/shifts/open")
def open_shift(branch_id: int, opening_float: float = 0, db: Session = Depends(get_db), user: User = Depends(require("shifts", "pos"))):
    existing = db.query(CashierShift).filter(CashierShift.user_id == user.id, CashierShift.status == "open").first()
    if existing:
        return existing
    s = CashierShift(user_id=user.id, branch_id=branch_id, opening_float=opening_float)
    db.add(s)
    db.commit()
    db.refresh(s)
    return s


@router.post("/shifts/{shift_id}/close")
def close_shift(shift_id: int, closing_cash: float = 0, db: Session = Depends(get_db), user: User = Depends(require("shifts", "pos"))):
    s = db.get(CashierShift, shift_id)
    if not s or s.user_id != user.id:
        raise HTTPException(404)
    s.status = "closed"
    s.closing_cash = closing_cash
    s.closed_at = clock.now()
    db.commit()
    db.refresh(s)
    return s


@router.get("/shifts/current")
def current_shift(db: Session = Depends(get_db), user: User = Depends(require("pos", "shifts"))):
    return db.query(CashierShift).filter(CashierShift.user_id == user.id, CashierShift.status == "open").first()


@router.get("/shifts")
def list_shifts(branch_id: int, db: Session = Depends(get_db), _: User = Depends(require("shifts", "pos"))):
    # Across every cashier at this branch, not just the caller's own — a 24-hour
    # branch hands the till between several cashier logins per day, and the
    # whole point of this list is letting any of them (or a manager) review a
    # previous shift's handover, not just their own.
    shifts = (
        db.query(CashierShift)
        .options(joinedload(CashierShift.user))
        .filter(CashierShift.branch_id == branch_id)
        .order_by(CashierShift.opened_at.desc())
        .limit(200)
        .all()
    )
    return [
        {
            "id": s.id,
            "user_name": s.user.full_name if s.user else "",
            "opened_at": s.opened_at,
            "closed_at": s.closed_at,
            "opening_float": s.opening_float,
            "closing_cash": s.closing_cash,
            "status": s.status,
        }
        for s in shifts
    ]


@router.get("/shifts/{shift_id}/z-report")
def z_report(shift_id: int, db: Session = Depends(get_db), _: User = Depends(require("shifts", "pos"))):
    # Based on Payment rows (which /invoices/{id}/pay, /pay-multi and /refund
    # already tag with the cashier's currently-open shift_id), not Invoice —
    # most invoices are created by Reception/IPD/Pharmacy via
    # billing_service.create_invoice() directly, never through this file's
    # POST /invoices, so Invoice.shift_id is essentially never set. Payments
    # collected while the till is open are the real per-shift signal.
    from app.models.accounting import Expense
    from app.models.billing import Payment

    shift = db.get(CashierShift, shift_id)
    payments = db.query(Payment).filter(Payment.shift_id == shift_id).all()
    collected = sum(p.amount for p in payments if p.method != "refund")
    refunded = sum(p.amount for p in payments if p.method == "refund")
    invoice_count = len({p.invoice_id for p in payments})
    expenses = db.query(Expense).filter(Expense.shift_id == shift_id).all()
    expense_total = sum(e.amount for e in expenses)

    # Cash-drawer reconciliation: only payments/refunds/expenses that actually moved
    # physical cash affect it — a KPay/Wave collection or expense never touches the
    # till, so folding it in here would make "expected cash" wrong on day one.
    cash_in = sum(p.amount for p in payments if p.method == "cash")
    cash_out_refunds = sum(p.amount for p in payments if p.method == "refund" and p.refund_method == "cash")
    cash_out_expenses = sum(e.amount for e in expenses if e.paid_from == "cash")
    expected_closing_cash = (shift.opening_float if shift else 0) + cash_in - cash_out_refunds - cash_out_expenses
    cash_discrepancy = (shift.closing_cash - expected_closing_cash) if shift and shift.status == "closed" else None

    return {
        "shift_id": shift_id,
        "invoice_count": invoice_count,
        "collected": collected,
        "refunded": refunded,
        "net_collected": collected - refunded,
        "expense_total": expense_total,
        "expected_closing_cash": expected_closing_cash,
        "cash_discrepancy": cash_discrepancy,
    }


@router.post("/invoices", response_model=InvoiceOut)
def new_invoice(data: InvoiceCreateIn, db: Session = Depends(get_db), user: User = Depends(require("billing", "pos"))):
    shift = db.query(CashierShift).filter(CashierShift.user_id == user.id, CashierShift.status == "open").first()
    inv = create_invoice(db, data.branch_id, data.patient_id, data.kind, data.doctor_id)
    if shift:
        inv.shift_id = shift.id
    db.commit()
    db.refresh(inv)
    return inv


@router.get("/invoices/{invoice_id}", response_model=InvoiceOut)
def get_invoice(invoice_id: int, db: Session = Depends(get_db), _: User = Depends(require("billing.read", "billing", "pos", "ipd"))):
    head = db.get(Invoice, invoice_id)
    if head is not None and head.admission_id:
        from app.models.ipd import Admission
        from app.services.billing_service import ensure_daily_room_charges

        adm = db.get(Admission, head.admission_id)
        if adm is not None and ensure_daily_room_charges(db, adm):  # room days up to today
            db.commit()
            db.expire_all()
    inv = (
        db.query(Invoice)
        .options(joinedload(Invoice.lines), joinedload(Invoice.payments))
        .filter(Invoice.id == invoice_id)
        .first()
    )
    if not inv:
        raise HTTPException(404)
    return inv


@router.post("/invoices/{invoice_id}/lines", response_model=InvoiceLineOut)
def add_invoice_line(invoice_id: int, data: InvoiceLineIn, db: Session = Depends(get_db), user: User = Depends(require("billing", "pos"))):
    inv = db.get(Invoice, invoice_id)
    if not inv:
        raise HTTPException(404)
    item = db.get(CatalogItem, data.item_id) if data.item_id else None
    if item and item.is_stock:
        raise HTTPException(400, "Stock-tracked items must be added via /lines/pharmacy so stock is deducted")
    doctor = db.get(User, inv.doctor_id) if inv.doctor_id else None
    line = add_line(db, inv, item, data.qty, data.unit_price, data.source, data.description, doctor, data.discount)
    audit(db, user.id, "add_line", "invoice", str(inv.id))
    db.commit()
    return line


@router.post("/invoices/{invoice_id}/lines/pharmacy", response_model=InvoiceOut)
def add_invoice_pharmacy_line(
    invoice_id: int,
    data: PharmacyLineIn,
    db: Session = Depends(get_db),
    user: User = Depends(require("billing", "pos")),
):
    """Lets Cashier bill a stock-tracked (drug) item straight onto an already
    open bill — e.g. a missed item, or a patient buying medicine while
    settling up — without walking them to the Pharmacy counter first. Reuses
    pharmacy_charge() (the same dispense + add_line path Pharmacy's own Sale
    tab uses) so stock actually decrements instead of drifting out of sync
    with what physically left the shelf."""
    inv = db.get(Invoice, invoice_id)
    if not inv:
        raise HTTPException(404)
    if not inv.patient_id:
        raise HTTPException(400, "Invoice has no patient to dispense against")
    try:
        pharmacy_charge(
            db,
            branch_id=inv.branch_id,
            patient_id=inv.patient_id,
            item_id=data.item_id,
            warehouse_id=data.warehouse_id,
            qty=data.qty,
            user_id=user.id,
            invoice_id=inv.id,
        )
        audit(db, user.id, "cashier_add_pharmacy_line", "invoice", str(inv.id), f"item={data.item_id} qty={data.qty}")
        db.commit()
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    inv = (
        db.query(Invoice)
        .options(joinedload(Invoice.lines), joinedload(Invoice.payments))
        .filter(Invoice.id == invoice_id)
        .first()
    )
    return inv


@router.patch("/invoices/{invoice_id}/lines/{line_id}", response_model=InvoiceLineOut)
def edit_invoice_line(
    invoice_id: int,
    line_id: int,
    data: InvoiceLineUpdateIn,
    db: Session = Depends(get_db),
    user: User = Depends(require("billing", "pos")),
):
    inv = db.get(Invoice, invoice_id)
    if not inv:
        raise HTTPException(404, "Invoice not found")
    line = db.get(InvoiceLine, line_id)
    if not line or line.invoice_id != invoice_id:
        raise HTTPException(404, "Line not found")
    try:
        update_line(db, inv, line, unit_price=data.unit_price, qty=data.qty, fee_type=data.fee_type, doctor_id=data.doctor_id)
        detail = f"price={data.unit_price} qty={data.qty}" + (f" fee={data.fee_type or 'normal'}" if data.fee_type is not None else "")
        if data.doctor_id is not None:
            detail = f"lab doctor={data.doctor_id or 'none'}"
        audit(db, user.id, "edit_line", "invoice_line", str(line.id), detail)
        db.commit()
        return line
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@router.delete("/invoices/{invoice_id}/lines/{line_id}", response_model=InvoiceOut)
def void_invoice_line(
    invoice_id: int,
    line_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require("billing", "pos")),
):
    inv = db.get(Invoice, invoice_id)
    if not inv:
        raise HTTPException(404, "Invoice not found")
    line = db.get(InvoiceLine, line_id)
    if not line or line.invoice_id != invoice_id:
        raise HTTPException(404, "Line not found")
    try:
        void_line(db, inv, line)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    audit(db, user.id, "void_line", "invoice", str(inv.id), f"{line.description} amount={line.amount:g}")
    db.commit()
    inv = (
        db.query(Invoice)
        .options(joinedload(Invoice.lines), joinedload(Invoice.payments))
        .filter(Invoice.id == invoice_id)
        .first()
    )
    return inv


@router.post("/invoices/{invoice_id}/discount", response_model=InvoiceOut)
def apply_discount(invoice_id: int, amount: float, db: Session = Depends(get_db), _: User = Depends(require("billing", "pos"))):
    inv = db.get(Invoice, invoice_id)
    if not inv:
        raise HTTPException(404)
    recalc_invoice(inv, db)
    if amount < 0 or amount > inv.subtotal + 0.01:
        raise HTTPException(400, f"Discount must be between 0 and the subtotal ({inv.subtotal:.0f})")
    # An IPD stay's deposit legitimately exceeds the bill so far; everywhere else,
    # discounting below what's already been collected needs a Refund instead.
    # (against everything collected, refunds aside: a refund already took its
    # share off the bill, so the discount must still leave the bill at or above it)
    collected = float(
        db.query(func.coalesce(func.sum(Payment.amount), 0.0))
        .filter(Payment.invoice_id == inv.id, Payment.method != "refund")
        .scalar()
        or 0
    )
    if not inv.admission_id and inv.subtotal - amount < collected - 0.01:
        raise HTTPException(400, "Discount would drop the total below the amount already paid — use Refund instead")
    inv.discount = amount
    recalc_invoice(inv, db)
    db.commit()
    return inv


def _invoice_full(inv: Invoice) -> dict:
    """Every column of the bill plus what InvoiceOut adds (refunded, names, lines, payments)."""
    return {**{c.name: getattr(inv, c.name) for c in Invoice.__table__.columns}, **InvoiceOut.model_validate(inv).model_dump()}


@router.post("/invoices/{invoice_id}/pay")
def pay(invoice_id: int, data: PaymentIn, db: Session = Depends(get_db), user: User = Depends(require("billing", "pos"))):
    inv = db.get(Invoice, invoice_id)
    if not inv:
        raise HTTPException(404)
    shift = db.query(CashierShift).filter(CashierShift.user_id == user.id, CashierShift.status == "open").first()
    try:
        p = pay_invoice(db, inv, data.method, data.amount, user.id, shift.id if shift else None, notes=data.notes)
        audit(db, user.id, "payment", "invoice", str(inv.id), data.method)
        db.commit()
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    inv = (
        db.query(Invoice)
        .options(joinedload(Invoice.lines), joinedload(Invoice.payments))
        .filter(Invoice.id == invoice_id)
        .first()
    )
    return {"payment_id": p.id, "invoice": _invoice_full(inv)}


@router.post("/invoices/{invoice_id}/lines/{line_id}/return")
def return_invoice_line(
    invoice_id: int,
    line_id: int,
    data: ReturnLineIn,
    db: Session = Depends(get_db),
    user: User = Depends(require("billing", "pos", "pharmacy")),
):
    """Patient gives an already-billed item back (medicine returned, or a
    test/service they decided not to have): records it, refunds what they've
    now overpaid, and restocks medicine — see billing_service.return_line()."""
    inv = db.get(Invoice, invoice_id)
    if not inv:
        raise HTTPException(404, "Invoice not found")
    line = db.get(InvoiceLine, line_id)
    if not line or line.invoice_id != invoice_id:
        raise HTTPException(404, "Line not found")
    shift = db.query(CashierShift).filter(CashierShift.user_id == user.id, CashierShift.status == "open").first()
    try:
        ret, refunded = return_line(
            db, inv, line, qty=data.qty, reason=data.reason, user_id=user.id,
            refund_method=data.refund_method, restock=data.restock, shift_id=shift.id if shift else None,
        )
        audit(db, user.id, "return_line", "invoice_line", str(line.id), f"qty={data.qty} refund={refunded:.0f} {data.reason}".strip())
        db.commit()
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    inv = (
        db.query(Invoice)
        .options(joinedload(Invoice.lines), joinedload(Invoice.payments))
        .filter(Invoice.id == invoice_id)
        .first()
    )
    return {"refunded": refunded, "invoice": InvoiceOut.model_validate(inv)}


@router.post("/invoices/{invoice_id}/order", response_model=InvoiceOut)
def order_item(invoice_id: int, data: OrderItemIn, db: Session = Depends(get_db), user: User = Depends(require("billing", "pos"))):
    """Cashier search box: add a medicine / lab test / X-ray / USG / service to
    an open bill, creating the matching counter order — see order_on_invoice()."""
    inv = db.get(Invoice, invoice_id)
    if not inv:
        raise HTTPException(404, "Invoice not found")
    try:
        order_on_invoice(db, inv, db.get(CatalogItem, data.item_id), qty=data.qty, user_id=user.id, warehouse_id=data.warehouse_id)
        db.commit()
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return (
        db.query(Invoice)
        .options(joinedload(Invoice.lines), joinedload(Invoice.payments))
        .filter(Invoice.id == invoice_id)
        .first()
    )


@router.post("/invoices/{invoice_id}/doctor-fee", response_model=InvoiceOut)
def add_invoice_doctor_fee(invoice_id: int, data: DoctorFeeIn, db: Session = Depends(get_db), user: User = Depends(require("billing", "pos"))):
    """Cashier "+ ဆရာဝန် ထည့်": the patient was also seen by another doctor."""
    from app.services.counter_service import add_doctor_fee

    inv = db.get(Invoice, invoice_id)
    if not inv:
        raise HTTPException(404, "Invoice not found")
    doctor = db.get(User, data.doctor_id)
    if not doctor or not doctor.is_active:
        raise HTTPException(404, "Doctor not found")
    try:
        line = add_doctor_fee(db, inv, doctor)
        if line is not None and data.unit_price is not None:
            if data.unit_price < 0:
                raise ValueError("ဈေး မှန်အောင် ထည့်ပါ")
            update_line(db, inv, line, unit_price=float(data.unit_price))
        recalc_invoice(inv, db)
        audit(db, user.id, "add_doctor_fee", "invoice", str(inv.id),
              (doctor.full_name + (f" · {data.unit_price:,.0f}" if data.unit_price is not None else ""))[:100])
        db.commit()
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return (
        db.query(Invoice)
        .options(joinedload(Invoice.lines), joinedload(Invoice.payments))
        .filter(Invoice.id == invoice_id)
        .first()
    )


class RoundFeeIn(BaseModel):
    doctor_id: int
    unit_price: float
    qty: float = 1


@router.post("/invoices/{invoice_id}/reopen", response_model=InvoiceOut)
def reopen_bill(invoice_id: int, payload: dict, db: Session = Depends(get_db), user: User = Depends(require("billing", "pos"))):
    """Put a settled bill back in Open Bills. For IPD the discharge is undone too
    (the patient stays on — e.g. going home tomorrow instead): back in the same
    room if it is still free, otherwise waiting for a room; room charges carry on
    from the next day. Money already taken stays as it is."""
    from app.models.ipd import Admission, Bed

    inv = db.get(Invoice, invoice_id)
    if not inv:
        raise HTTPException(404, "Invoice not found")
    if inv.status != "paid":
        raise HTTPException(400, "ငွေရှင်းပြီးသား (paid) bill ကိုပဲ ပြန်ဖွင့်လို့ရပါတယ်")
    note = str(payload.get("note") or "").strip()
    if len(note) < 2:
        raise HTTPException(400, "Note ထည့်ပါ")
    extra = ""
    adm = db.get(Admission, inv.admission_id) if inv.kind == "ipd" and inv.admission_id else None
    if adm and adm.status == "discharged":
        bed = db.get(Bed, adm.bed_id) if adm.bed_id else None
        if bed and bed.status == "available":
            bed.status = "occupied"
            adm.status = "admitted"
            extra = f" · back in {bed.code}"
        else:
            adm.status = "pending_bed"
            adm.bed_id = None
            extra = " · waiting for a room"
        adm.discharged_at = None
        adm.discharge_approved_at = None
        adm.discharge_approved_by = None
        if not adm.last_room_charge_date:
            adm.last_room_charge_date = clock.today()
    inv.status = "partial" if float(inv.paid or 0) > 0.01 else "open"
    inv.paid_at = None
    if inv.credit_at is not None:  # back to a normal Open Bill
        extra += " · credit cleared"
        inv.credit_at = None
    db.flush()
    recalc_invoice(inv, db)
    if inv.status == "paid":  # OPD with nothing owed: keep it open until it is closed again
        inv.status = "partial" if float(inv.paid or 0) > 0.01 else "open"
        inv.paid_at = None
    audit(db, user.id, "reopen_bill", "invoice", str(inv.id), f"{note}{extra}"[:200])
    db.commit()
    db.expire_all()
    return (
        db.query(Invoice)
        .options(joinedload(Invoice.lines), joinedload(Invoice.payments))
        .filter(Invoice.id == invoice_id)
        .first()
    )


@router.post("/payments/{payment_id}/edit", response_model=InvoiceOut)
@router.post("/payments/{payment_id}/date", response_model=InvoiceOut)
def change_payment_date(payment_id: int, payload: dict, db: Session = Depends(get_db), user: User = Depends(require("billing", "pos"))):
    """Move one payment (deposit / refund / settlement) to another day — e.g. a
    deposit taken yesterday but keyed in today. The time of day is kept, so
    History, Daily Close and the Sale Report show the money on the new day.
    With "amount": also corrects a mistyped amount (bill paid / balance follow)."""
    from app.services.billing_service import recalc_invoice

    pay = db.get(Payment, payment_id)
    if not pay:
        raise HTTPException(404, "Payment not found")
    inv = db.get(Invoice, pay.invoice_id)
    if not inv or inv.status == "void":
        raise HTTPException(400, "ပယ်ထားတဲ့ bill ပါ")
    note = str(payload.get("note") or "").strip()
    if len(note) < 2:
        raise HTTPException(400, "Note ထည့်ပါ")
    old = pay.created_at or clock.now()
    try:
        d = date.fromisoformat(str(payload.get("date") or old.date().isoformat()))
    except ValueError as e:
        raise HTTPException(400, "ရက်စွဲ မှန်အောင် ရွေးပါ") from e
    if d > clock.today():
        raise HTTPException(400, "နောက်ရက်ကို မရွေးနိုင်ပါ")
    old_amt = float(pay.amount or 0)
    new_amt = old_amt
    if payload.get("amount") not in (None, ""):
        try:
            new_amt = round(float(payload.get("amount")), 2)
        except (TypeError, ValueError) as e:
            raise HTTPException(400, "ငွေပမာဏ မှန်အောင် ထည့်ပါ") from e
        if new_amt <= 0:
            raise HTTPException(400, "ငွေပမာဏ 0 ထက် များရပါမယ် (မှားသွင်းတာဆို ✕ နဲ့ ပယ်ပါ)")
    if d == old.date() and abs(new_amt - old_amt) < 0.005:
        raise HTTPException(400, "ဘာမှ မပြောင်းထားပါ")
    if d != old.date() and inv.status == "paid" and inv.paid_at and d > inv.paid_at.date():
        raise HTTPException(400, f"ငွေရှင်းတဲ့ရက် ({inv.paid_at:%d/%m/%Y}) ထက် နောက်ကျလို့ မရပါ")
    pay.created_at = datetime.combine(d, old.time())
    pay.amount = new_amt
    # Bill keyed in late (opened today, deposit taken days ago): open it on that day too.
    if inv.created_at and pay.created_at < inv.created_at:
        inv.created_at = pay.created_at
    was_paid_at = inv.paid_at
    db.flush()
    recalc_invoice(inv, db)
    if inv.status == "paid":
        # An edit is not a new payment: keep the bill on the day it was settled
        # (or, if the edit is what settles it, the day of its last payment).
        last = max((p.created_at for p in inv.payments if p.created_at), default=None)
        inv.paid_at = was_paid_at or last or inv.paid_at
    kind = "Refund" if pay.method == "refund" else "Deposit" if inv.kind == "ipd" else pay.method
    parts = []
    if abs(new_amt - old_amt) >= 0.005:
        parts.append(f"{kind} {old_amt:,.0f} → {new_amt:,.0f}")
    else:
        parts.append(f"{kind} {new_amt:,.0f}")
    if d != old.date():
        parts.append(f"{old:%d/%m/%Y} → {d:%d/%m/%Y}")
    audit(db, user.id, "change_payment_date" if abs(new_amt - old_amt) < 0.005 else "edit_payment", "invoice", str(inv.id),
          " · ".join(parts + [note])[:200])
    db.commit()
    db.expire_all()
    return (
        db.query(Invoice)
        .options(joinedload(Invoice.lines), joinedload(Invoice.payments))
        .filter(Invoice.id == inv.id)
        .first()
    )


@router.post("/invoices/{invoice_id}/paid-date", response_model=InvoiceOut)
def change_paid_date(invoice_id: int, payload: dict, db: Session = Depends(get_db), user: User = Depends(require("billing", "pos"))):
    """Move a settled bill to another day (e.g. a bill keyed in late). The money
    taken on the old paid day moves with it, so History, Daily Close and the Sale
    Report all show it on the new day. Earlier deposits stay on their own days."""
    from datetime import timedelta

    inv = db.get(Invoice, invoice_id)
    if not inv:
        raise HTTPException(404, "Invoice not found")
    if inv.status != "paid" or not inv.paid_at:
        raise HTTPException(400, "ငွေရှင်းပြီးသား bill ကိုပဲ ရက်ပြောင်းလို့ရပါတယ်")
    note = str(payload.get("note") or "").strip()
    if len(note) < 2:
        raise HTTPException(400, "Note ထည့်ပါ")
    try:
        d = date.fromisoformat(str(payload.get("date") or ""))
    except ValueError as e:
        raise HTTPException(400, "ရက်စွဲ မှန်အောင် ရွေးပါ") from e
    if d > clock.today():
        raise HTTPException(400, "နောက်ရက်ကို မရွေးနိုင်ပါ")
    old = inv.paid_at
    if d == old.date():
        raise HTTPException(400, "ရက် မပြောင်းထားပါ")
    delta = timedelta(days=(d - old.date()).days)
    move = payload.get("move_payments", True)
    # Payments that stay on their own day must not end up after the paid day.
    stay = [p for p in inv.payments if p.created_at and not (move and p.created_at.date() == old.date())]
    late = max((p.created_at for p in stay), default=None)
    if late and late.date() > d:
        raise HTTPException(400, f"{late:%d/%m/%Y} မှာ deposit / payment ရှိနေလို့ အဲဒီရက်ထက် စောလို့ မရပါ — အဲဒီ deposit ရဲ့ ရက်ကို ✏️ နဲ့ အရင် ပြောင်းပါ")
    moved = 0
    if move:
        for p in inv.payments:
            if p.created_at and p.created_at.date() == old.date():
                p.created_at = p.created_at + delta
                moved += 1
    inv.paid_at = old + delta
    # A bill keyed in late (opened today, paid "yesterday"): open it on that day too.
    if inv.created_at and inv.created_at > inv.paid_at:
        inv.created_at = inv.created_at + delta
        for ln in inv.lines:
            if ln.created_at and ln.created_at > inv.paid_at:
                ln.created_at = ln.created_at + delta
    audit(db, user.id, "change_paid_date", "invoice", str(inv.id),
          f"{old:%d/%m/%Y} → {d:%d/%m/%Y} · payments {moved} · {note}"[:200])
    db.commit()
    db.expire_all()
    return (
        db.query(Invoice)
        .options(joinedload(Invoice.lines), joinedload(Invoice.payments))
        .filter(Invoice.id == invoice_id)
        .first()
    )


@router.post("/invoices/{invoice_id}/line-order", response_model=InvoiceOut)
def set_line_order(invoice_id: int, payload: dict, db: Session = Depends(get_db), user: User = Depends(require("billing", "pos"))):
    """Cashier dragged the bill's lines into a new order: `ids` top to bottom.
    Only the order on screen and on the voucher changes — no amounts."""
    inv = db.get(Invoice, invoice_id)
    if not inv:
        raise HTTPException(404, "Invoice not found")
    want = [int(x) for x in (payload.get("ids") or []) if str(x).isdigit()]
    mine = {ln.id: ln for ln in inv.lines}
    pos = 0
    for lid in dict.fromkeys(want):
        if lid in mine:
            pos += 1
            mine[lid].sort_order = pos
    audit(db, user.id, "line_order", "invoice", str(inv.id), f"{pos} lines")
    db.commit()
    db.expire_all()
    return (
        db.query(Invoice)
        .options(joinedload(Invoice.lines), joinedload(Invoice.payments))
        .filter(Invoice.id == invoice_id)
        .first()
    )


@router.post("/invoices/{invoice_id}/round-fee", response_model=InvoiceOut)
def add_invoice_round_fee(invoice_id: int, data: RoundFeeIn, db: Session = Depends(get_db), user: User = Depends(require("billing", "pos"))):
    """Cashier "+ Round fee": a doctor's ward rounds on an IPD stay (price x rounds)."""
    from app.services.billing_service import add_round_fee

    inv = db.get(Invoice, invoice_id)
    if not inv:
        raise HTTPException(404, "Invoice not found")
    if inv.status == "void":
        raise HTTPException(400, "Void bill ကို ထည့်လို့ မရပါ")
    doctor = db.get(User, data.doctor_id)
    if not doctor or not doctor.is_active:
        raise HTTPException(404, "Doctor not found")
    try:
        was = inv.status
        if inv.status == "paid":
            inv.status = "partial"
        add_round_fee(db, inv, doctor, data.unit_price, data.qty)
        audit(db, user.id, "add_round_fee", "invoice", str(inv.id), f"{doctor.full_name} {data.qty:g} x {data.unit_price:g} (was {was})"[:200])
        db.commit()
    except ValueError as e:
        db.rollback()
        raise HTTPException(400, str(e)) from e
    return (
        db.query(Invoice)
        .options(joinedload(Invoice.lines), joinedload(Invoice.payments))
        .filter(Invoice.id == invoice_id)
        .first()
    )


class SurgeryFeeIn(BaseModel):
    doctor_id: int
    unit_price: float
    role: str = "surgeon"  # surgeon / anaesth / assistant
    procedure: str = ""


@router.post("/invoices/{invoice_id}/surgery-fee", response_model=InvoiceOut)
def add_invoice_surgery_fee(invoice_id: int, data: SurgeryFeeIn, db: Session = Depends(get_db), user: User = Depends(require("billing", "pos"))):
    """Cashier "+ OT Doctor": surgeon / anaesthetist / assistant fee for an operation."""
    from app.services.billing_service import add_surgery_fee

    inv = db.get(Invoice, invoice_id)
    if not inv:
        raise HTTPException(404, "Invoice not found")
    if inv.status == "void":
        raise HTTPException(400, "Void bill ကို ထည့်လို့ မရပါ")
    doctor = db.get(User, data.doctor_id)
    if not doctor or not doctor.is_active:
        raise HTTPException(404, "Doctor not found")
    try:
        was = inv.status
        if inv.status == "paid":
            inv.status = "partial"
        line = add_surgery_fee(db, inv, doctor, data.unit_price, data.role, data.procedure)
        audit(db, user.id, "add_surgery_fee", "invoice", str(inv.id), f"{line.description} {data.unit_price:g} (was {was})"[:200])
        db.commit()
    except ValueError as e:
        db.rollback()
        raise HTTPException(400, str(e)) from e
    return (
        db.query(Invoice)
        .options(joinedload(Invoice.lines), joinedload(Invoice.payments))
        .filter(Invoice.id == invoice_id)
        .first()
    )


@router.post("/invoices/{invoice_id}/lines/{line_id}/exchange")
def exchange_invoice_line(
    invoice_id: int,
    line_id: int,
    data: ExchangeLineIn,
    db: Session = Depends(get_db),
    user: User = Depends(require("billing", "pos", "pharmacy")),
):
    """Swap an already-billed item for a different one; only the price
    difference is collected/refunded — see counter_service.exchange_line()."""
    inv = db.get(Invoice, invoice_id)
    if not inv:
        raise HTTPException(404, "Invoice not found")
    line = db.get(InvoiceLine, line_id)
    if not line or line.invoice_id != invoice_id:
        raise HTTPException(404, "Line not found")
    shift = db.query(CashierShift).filter(CashierShift.user_id == user.id, CashierShift.status == "open").first()
    try:
        result = exchange_line(
            db, inv, line, qty=data.qty, reason=data.reason, new_item=db.get(CatalogItem, data.new_item_id),
            new_qty=data.new_qty, user_id=user.id, method=data.method,
            shift_id=shift.id if shift else None, warehouse_id=data.warehouse_id,
        )
        db.commit()
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    inv = (
        db.query(Invoice)
        .options(joinedload(Invoice.lines), joinedload(Invoice.payments))
        .filter(Invoice.id == invoice_id)
        .first()
    )
    return {**result, "invoice": InvoiceOut.model_validate(inv)}


@router.post("/invoices/{invoice_id}/refund")
def refund(invoice_id: int, data: RefundIn, db: Session = Depends(get_db), user: User = Depends(require("billing", "pos"))):
    inv = db.get(Invoice, invoice_id)
    if not inv:
        raise HTTPException(404)
    shift = db.query(CashierShift).filter(CashierShift.user_id == user.id, CashierShift.status == "open").first()
    try:
        p = refund_payment(db, inv, amount=data.amount, user_id=user.id, method=data.method, reason=data.reason, shift_id=shift.id if shift else None)
        audit(db, user.id, "refund", "invoice", str(inv.id), f"{data.amount} {data.reason}".strip())
        db.commit()
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    inv = (
        db.query(Invoice)
        .options(joinedload(Invoice.lines), joinedload(Invoice.payments))
        .filter(Invoice.id == invoice_id)
        .first()
    )
    return {"payment_id": p.id, "invoice": _invoice_full(inv)}


@router.post("/invoices/{invoice_id}/ipd-deposit", response_model=InvoiceOut)
def ipd_deposit_on_invoice(
    invoice_id: int,
    data: IpdDepositIn,
    db: Session = Depends(get_db),
    user: User = Depends(require("billing", "pos")),
):
    inv = db.get(Invoice, invoice_id)
    if not inv or inv.kind != "ipd" or not inv.admission_id:
        raise HTTPException(400, "Not an active IPD bill")
    shift = db.query(CashierShift).filter(CashierShift.user_id == user.id, CashierShift.status == "open").first()
    try:
        inv, _adm = collect_ipd_deposit(
            db,
            admission_id=inv.admission_id,
            amount=data.amount,
            method=data.method,
            user_id=user.id,
            shift_id=shift.id if shift else None,
            notes=data.notes,
        )
        db.commit()
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    inv = (
        db.query(Invoice)
        .options(joinedload(Invoice.lines), joinedload(Invoice.payments))
        .filter(Invoice.id == invoice_id)
        .first()
    )
    return inv


@router.post("/invoices/{invoice_id}/pay-multi", response_model=InvoiceOut)
def pay_multi(invoice_id: int, data: MultiPaymentIn, db: Session = Depends(get_db), user: User = Depends(require("billing", "pos"))):
    inv = (
        db.query(Invoice)
        .options(joinedload(Invoice.lines), joinedload(Invoice.payments))
        .filter(Invoice.id == invoice_id)
        .first()
    )
    if not inv:
        raise HTTPException(404)
    shift = db.query(CashierShift).filter(CashierShift.user_id == user.id, CashierShift.status == "open").first()
    try:
        pay_invoice_multi(db, inv, data.payments, user.id, shift.id if shift else None, notes=data.notes)
        if (data.notes or "").strip():
            # Keep the latest note on the bill too, so every reprint of the voucher shows it.
            inv.notes = data.notes.strip()[:500]
        audit(db, user.id, "multi_payment", "invoice", str(inv.id), f"{len(data.payments)} splits")
        db.commit()
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    inv = (
        db.query(Invoice)
        .options(joinedload(Invoice.lines), joinedload(Invoice.payments))
        .filter(Invoice.id == invoice_id)
        .first()
    )
    return inv


@router.post("/invoices/{invoice_id}/close", response_model=InvoiceOut)
def close_settled_bill(invoice_id: int, db: Session = Depends(get_db), user: User = Depends(require("billing", "pos"))):
    """"Close bill (nothing due)": a bill with nothing left to pay (e.g. put back
    with ↩ Open Bill and closed again without taking more money) becomes Paid and
    leaves Open Bills. Paid date = when its money last came in."""
    from app.models.ipd import Admission

    inv = _credit_bill(db, invoice_id)
    if inv.status in ("paid", "void"):
        return inv
    was = inv.status
    recalc_invoice(inv, db)
    if float(inv.balance or 0) > 0.01:
        raise HTTPException(400, f"ကျန်ငွေ {inv.balance:,.0f} ရှိနေသေးတယ် — Pay နဲ့ ရှင်းပါ")
    if inv.admission_id:
        adm = db.get(Admission, inv.admission_id)
        if adm and adm.status != "discharged":
            raise HTTPException(400, "ဆေးရုံတက်နေဆဲ — Discharge ကနေ ပိတ်ပါ")
    if not inv.lines:
        return inv  # empty bill: nothing to close
    inv.status = "paid"
    inv.paid_at = max((p.created_at for p in inv.payments if p.created_at), default=None) or clock.now()
    audit(db, user.id, "close_bill", "invoice", str(inv.id), f"{inv.number} closed (nothing due, was {was}) · paid date {inv.paid_at:%d/%m %H:%M}")
    db.commit()
    db.expire_all()
    return _credit_bill(db, invoice_id)


class CreditIn(BaseModel):
    name: str = ""
    phone: str = ""
    due: date | None = None
    note: str = ""
    payments: list[PaymentIn] = []


def _credit_bill(db: Session, invoice_id: int) -> Invoice:
    inv = (
        db.query(Invoice)
        .options(joinedload(Invoice.lines), joinedload(Invoice.payments))
        .filter(Invoice.id == invoice_id)
        .first()
    )
    if not inv:
        raise HTTPException(404, "Invoice not found")
    return inv


@router.post("/invoices/{invoice_id}/credit", response_model=InvoiceOut)
def close_on_credit(invoice_id: int, data: CreditIn, db: Session = Depends(get_db), user: User = Depends(require("billing", "pos"))):
    """📒 Close an OPD bill on credit: take what is paid now (if anything), the
    rest stays owed. The bill leaves Open Bills; income counts each payment on
    the day it comes in — nothing for the part still owed."""
    inv = _credit_bill(db, invoice_id)
    if inv.kind != "opd" or inv.admission_id:
        raise HTTPException(400, "OPD bill ကိုပဲ အကြွေးနဲ့ ပိတ်လို့ရပါတယ်")
    if inv.status not in ("open", "partial"):
        raise HTTPException(400, "Open bill ကိုပဲ အကြွေးနဲ့ ပိတ်လို့ရပါတယ်")
    name = (data.name or "").strip()
    if len(name) < 2:
        raise HTTPException(400, "အကြွေးယူသူ နာမည် ထည့်ပါ")
    shift = db.query(CashierShift).filter(CashierShift.user_id == user.id, CashierShift.status == "open").first()
    try:
        pays = [p for p in data.payments if float(p.amount or 0) > 0]
        recalc_invoice(inv, db)
        if sum(float(p.amount) for p in pays) > inv.balance - 0.01:
            raise ValueError("ပေးငွေက bill ကျန်ငွေ အပြည့် ဖြစ်နေပါတယ် — အကြွေး မလိုပါ၊ Pay နဲ့ပဲ ရှင်းပါ")
        if pays:
            pay_invoice_multi(db, inv, pays, user.id, shift.id if shift else None, notes=data.note)
        recalc_invoice(inv, db)
        if inv.balance <= 0.01:
            raise ValueError("ကျန်ငွေ မရှိပါ")
        inv.credit_at = clock.now()
        inv.credit_amount = round(float(inv.balance), 2)
        inv.credit_by = user.id
        inv.credit_name = name[:120]
        inv.credit_phone = (data.phone or "").strip()[:40]
        inv.credit_due = data.due
        inv.credit_note = (data.note or "").strip()[:300]
        recalc_invoice(inv, db)
        audit(db, user.id, "credit_close", "invoice", str(inv.id),
              f"{inv.number} credit {inv.credit_amount:,.0f} · {name}" + (f" · due {data.due}" if data.due else ""))
        db.commit()
    except ValueError as e:
        db.rollback()
        raise HTTPException(400, str(e)) from e
    db.expire_all()
    return _credit_bill(db, invoice_id)


@router.post("/invoices/{invoice_id}/credit-cancel", response_model=InvoiceOut)
def cancel_credit(invoice_id: int, db: Session = Depends(get_db), user: User = Depends(require("billing", "pos"))):
    """↩ Undo 📒 credit (keyed by mistake): the bill goes back to Open Bills.
    Money already taken stays as it is."""
    inv = _credit_bill(db, invoice_id)
    if inv.status != "credit" or inv.credit_at is None:
        raise HTTPException(400, "အကြွေး bill မဟုတ်ပါ")
    amt = float(inv.credit_amount or 0)
    inv.credit_at = None
    recalc_invoice(inv, db)
    audit(db, user.id, "credit_cancel", "invoice", str(inv.id), f"{inv.number} credit {amt:,.0f} cancelled")
    db.commit()
    db.expire_all()
    return _credit_bill(db, invoice_id)


@router.get("/invoices")
def list_invoices(branch_id: int | None = None, db: Session = Depends(get_db), _: User = Depends(require("billing.read", "billing", "pos"))):
    q = db.query(Invoice)
    if branch_id:
        q = q.filter(Invoice.branch_id == branch_id)
    return q.order_by(Invoice.id.desc()).limit(100).all()


@router.patch("/invoices/{invoice_id}/note", response_model=InvoiceOut)
def set_invoice_note(invoice_id: int, data: InvoiceNoteIn, db: Session = Depends(get_db), user: User = Depends(require("billing", "pos"))):
    """Voucher note printed on the receipt (KPay ref, who paid, remarks ...)."""
    inv = (
        db.query(Invoice)
        .options(joinedload(Invoice.lines), joinedload(Invoice.payments))
        .filter(Invoice.id == invoice_id)
        .first()
    )
    if not inv:
        raise HTTPException(404)
    old = inv.notes or ""
    inv.notes = (data.notes or "").strip()[:500]
    audit(db, user.id, "invoice_note", "invoice", str(inv.id), f"{old!r} -> {inv.notes!r}"[:400])
    db.commit()
    db.refresh(inv)
    return inv


@router.get("/invoices/{invoice_id}/day-bill")
def day_bill(invoice_id: int, day: date | None = None, db: Session = Depends(get_db), _: User = Depends(require("billing.read", "billing", "pos", "ipd"))):
    """IPD pre-bill for one day: that day's charges, plus the running total so far.
    Daily room charge is one merged bill line, so its days are rebuilt from the
    bed history (one day at the bed the patient was in at the end of that day)."""
    from app.models.ipd import Admission, Bed, BedTransferLog

    inv = (
        db.query(Invoice)
        .options(joinedload(Invoice.lines), joinedload(Invoice.payments))
        .filter(Invoice.id == invoice_id)
        .first()
    )
    if not inv:
        raise HTTPException(404)
    d = day or clock.today()
    opened = inv.created_at.date() if inv.created_at else d
    adm = db.get(Admission, inv.admission_id) if inv.admission_id else None
    room_daily = bool(adm and adm.billing_mode == "daily")
    start = (adm.admitted_at.date() if adm and adm.admitted_at else opened)
    if d < start:
        raise HTTPException(400, f"{d} က ဆေးရုံမတက်ခင် ရက်ပါ (တက်တဲ့နေ့ {start})")
    if d > clock.today():
        raise HTTPException(400, "နောက်ရက်အတွက် Pre-bill မထုတ်နိုင်သေးပါ")

    def row(line, qty=None, amount=None) -> dict:
        return {
            "id": line.id if line is not None else None,
            "ret": getattr(line, "returned_from_line_id", None),
            "source": getattr(line, "source", "ipd_room"),
            "fee_type": getattr(line, "fee_type", ""),
            "is_stock": bool(getattr(line, "is_stock", False)),
            "bill_group": getattr(line, "bill_group", "") or "",
            "sort_order": getattr(line, "sort_order", 0) or 0,
            "department": getattr(line, "department", ""),
            "description": line.description,
            "qty": line.qty if qty is None else qty,
            "unit_price": line.unit_price,
            "amount": line.amount if amount is None else amount,
        }

    items: list[dict] = []
    to_date = 0.0
    for line in inv.lines:
        if room_daily and line.source == "ipd_room":
            continue
        when = (line.created_at or inv.created_at).date() if (line.created_at or inv.created_at) else opened
        if when <= d:
            to_date += line.amount or 0
        if when == d:
            items.append(row(line))

    room_days = 0
    if room_daily:
        logs = (
            db.query(BedTransferLog).filter(BedTransferLog.admission_id == adm.id)
            .order_by(BedTransferLog.transferred_at, BedTransferLog.id).all()
        )
        first = logs[0].transferred_at.date() if logs else start
        last = adm.last_room_charge_date or first
        room_lines = [x for x in inv.lines if x.source == "ipd_room"]
        beds: dict[int, Bed | None] = {}

        def bed_on(x):
            bid = None
            for lg in logs:
                if lg.transferred_at.date() <= x:
                    bid = lg.to_bed_id
            bid = bid or adm.bed_id
            if bid not in beds:
                beds[bid] = db.get(Bed, bid) if bid else None
            return beds[bid]

        x = first
        while x <= min(d, last):
            bed = bed_on(x)
            label = f"Room charge {bed.code} (daily)" if bed else "Room charge"
            match = next((r for r in room_lines if r.description == label), None) or (room_lines[0] if room_lines else None)
            rate = match.unit_price if match else (bed.daily_rate if bed else 0)
            to_date += rate
            if x == d:
                room_days = 1
                items.insert(0, {"id": None, "ret": None, "source": "ipd_room", "fee_type": "", "description": label, "qty": 1, "unit_price": rate, "amount": rate})
            x = date.fromordinal(x.toordinal() + 1)

    paid = 0.0
    for p in inv.payments:
        if p.created_at and p.created_at.date() <= d:
            paid += -(p.amount or 0) if p.method == "refund" else (p.amount or 0)
    return {
        "invoice_id": inv.id,
        "number": inv.number,
        "kind": inv.kind,
        "day": d.isoformat(),
        "day_no": (d - start).days + 1,
        "admitted": start.isoformat(),
        "discharged": adm.discharged_at.date().isoformat() if adm and adm.discharged_at else None,
        "lines": items,
        "day_total": sum(i["amount"] for i in items),
        "total_to_date": to_date,
        "paid_to_date": paid,
        "balance_to_date": to_date - paid,
        "room_days": room_days,
    }


class HistoryAddIn(BaseModel):
    item_id: int
    qty: float = 1
    warehouse_id: int | None = None
    note: str = ""


@router.post("/invoices/{invoice_id}/history-add", response_model=InvoiceOut)
def history_add_item(invoice_id: int, data: HistoryAddIn, db: Session = Depends(get_db), user: User = Depends(require("billing", "pos"))):
    """Bill History: add a missed item to a bill, even one already paid — a note
    (why) is required. A paid bill reopens with the extra amount due."""
    inv = db.get(Invoice, invoice_id)
    if not inv:
        raise HTTPException(404, "Invoice not found")
    if inv.status == "void":
        raise HTTPException(400, "Void bill ကို ထပ်ထည့်လို့ မရပါ")
    note = (data.note or "").strip()
    if len(note) < 2:
        raise HTTPException(400, "Note (ဘာကြောင့် ထပ်ထည့်တာလဲ) ထည့်ပါ")
    item = db.get(CatalogItem, data.item_id)
    if not item:
        raise HTTPException(404, "Item not found")
    was = inv.status
    if was == "paid":
        inv.status = "partial"  # order_on_invoice only adds to unsettled bills
    try:
        order_on_invoice(db, inv, item, qty=data.qty, user_id=user.id, warehouse_id=data.warehouse_id)
        recalc_invoice(inv, db)
        audit(db, user.id, "history_add_item", "invoice", str(inv.id), f"{item.name} x{data.qty:g} (was {was}) — {note}"[:400])
        db.commit()
    except ValueError as e:
        db.rollback()
        raise HTTPException(400, str(e)) from e
    return (
        db.query(Invoice)
        .options(joinedload(Invoice.lines), joinedload(Invoice.payments))
        .filter(Invoice.id == invoice_id)
        .first()
    )


class HistoryEditIn(BaseModel):
    unit_price: float | None = None
    qty: float | None = None
    delete: bool = False
    refund_method: str = "cash"


@router.post("/invoices/{invoice_id}/lines/{line_id}/hedit", response_model=InvoiceOut)
def history_edit_line(invoice_id: int, line_id: int, data: HistoryEditIn, db: Session = Depends(get_db), user: User = Depends(require("billing", "pos"))):
    """Bill History: change a line's price / qty, or remove it, on any bill —
    paid ones too, just like an unpaid bill. If the bill now comes to less than
    was paid, the difference is recorded as a refund (money handed back) so the
    cash report stays right; if it comes to more, the bill reopens with the
    extra due (collected from Open Bills). An IPD bill still running keeps its
    deposit (no refund)."""
    from app.models.ipd import Admission
    from app.services.billing_service import EDIT_REFUND_TAG, _adjust_line_stock, _cancel_service_orders

    inv = db.get(Invoice, invoice_id)
    if not inv:
        raise HTTPException(404, "Invoice not found")
    if inv.status == "void":
        raise HTTPException(400, "Void bill ကို ပြင်လို့ မရပါ")
    line = db.get(InvoiceLine, line_id)
    if not line or line.invoice_id != invoice_id:
        raise HTTPException(404, "Line not found")
    if line.returned_from_line_id or (line.qty or 0) < 0 or any(l.returned_from_line_id == line.id for l in inv.lines):
        raise HTTPException(400, "Return လုပ်ထားတဲ့ item ကို ပြင်/ဖျက်လို့ မရပါ")
    was = inv.status
    before = float(inv.total or 0)
    desc = line.description
    try:
        if data.delete:
            _adjust_line_stock(db, inv, line, 0)
            _cancel_service_orders(db, inv, line, line.qty)
            inv.lines.remove(line)
            db.delete(line)
            db.flush()
            detail = f"removed {desc}"
        else:
            old = f"{line.qty:g}x{line.unit_price:g}"
            update_line(db, inv, line, unit_price=data.unit_price, qty=data.qty)
            detail = f"{desc}: {old} -> {line.qty:g}x{line.unit_price:g}"
        recalc_invoice(inv, db)
        ongoing = False
        if inv.admission_id:
            adm = db.get(Admission, inv.admission_id)
            ongoing = bool(adm and adm.status != "discharged")
        refunded = 0.0
        over = round((inv.paid or 0) - (inv.total or 0), 2)
        if over > 0.01 and not ongoing:
            shift = db.query(CashierShift).filter(CashierShift.user_id == user.id, CashierShift.status == "open").first()
            method = data.refund_method if data.refund_method in ("cash", "kpay", "wave", "ayapay", "cbpay", "bank", "card") else "cash"
            refund_payment(db, inv, amount=over, user_id=user.id, method=method, reason=f"{EDIT_REFUND_TAG} {desc}", shift_id=shift.id if shift else None)
            refunded = over
        audit(db, user.id, "history_edit_line", "invoice", str(inv.id),
              f"{detail} (was {was}, total {before:g} -> {float(inv.total or 0):g}" + (f", refund {refunded:g}" if refunded else "") + ")")
        db.commit()
    except ValueError as e:
        db.rollback()
        raise HTTPException(400, str(e)) from e
    return (
        db.query(Invoice)
        .options(joinedload(Invoice.lines), joinedload(Invoice.payments))
        .filter(Invoice.id == invoice_id)
        .first()
    )


class HistoryDoctorIn(BaseModel):
    doctor_id: int
    note: str = ""
    replace_line_id: int | None = None
    refund_method: str = "cash"


@router.post("/invoices/{invoice_id}/history-doctor", response_model=InvoiceOut)
def history_doctor(invoice_id: int, data: HistoryDoctorIn, db: Session = Depends(get_db), user: User = Depends(require("billing", "pos"))):
    """Bill History: add a doctor's consultation fee to a bill, or swap the
    doctor on an existing consultation line (replace_line_id) — paid bills too.
    Less than paid -> refund the difference; more -> the bill reopens with the
    extra due."""
    from app.models.ipd import Admission
    from app.services.billing_service import EDIT_REFUND_TAG
    from app.services.counter_service import add_doctor_fee

    inv = db.get(Invoice, invoice_id)
    if not inv:
        raise HTTPException(404, "Invoice not found")
    if inv.status == "void":
        raise HTTPException(400, "Void bill ကို ပြင်လို့ မရပါ")
    doctor = db.get(User, data.doctor_id)
    if not doctor or not doctor.is_active or doctor.role != "doctor":
        raise HTTPException(404, "Doctor not found")
    old = None
    if data.replace_line_id:
        old = db.get(InvoiceLine, data.replace_line_id)
        if not old or old.invoice_id != invoice_id or old.source != "opd" or not old.doctor_id:
            raise HTTPException(400, "ဆရာဝန်ခ line မဟုတ်ပါ")
        if old.returned_from_line_id or (old.qty or 0) <= 0 or any(l.returned_from_line_id == old.id for l in inv.lines):
            raise HTTPException(400, "Return လုပ်ထားတဲ့ item ကို ပြင်လို့ မရပါ")
        if old.doctor_id == doctor.id:
            raise HTTPException(400, "ဆရာဝန် အတူတူပါ")
    elif len((data.note or "").strip()) < 2:
        raise HTTPException(400, "Note (ဘာကြောင့် ထပ်ထည့်တာလဲ) ထည့်ပါ")
    was = inv.status
    before = float(inv.total or 0)
    try:
        if inv.status == "paid":
            inv.status = "partial"
        if old:
            old_doc = old.doctor_id
            old_name = old.description
            inv.lines.remove(old)
            db.delete(old)
            db.flush()
            # The visit now belongs to the new doctor: move the bill's doctor and
            # the lab / X-ray lines that counted for the old one.
            for l in inv.lines:
                if l.doctor_id == old_doc and l.source in ("lab", "radiology"):
                    l.doctor_id = doctor.id
            if inv.doctor_id == old_doc:
                inv.doctor_id = doctor.id
        add_doctor_fee(db, inv, doctor)
        recalc_invoice(inv, db)
        ongoing = False
        if inv.admission_id:
            adm = db.get(Admission, inv.admission_id)
            ongoing = bool(adm and adm.status != "discharged")
        refunded = 0.0
        over = round((inv.paid or 0) - (inv.total or 0), 2)
        if over > 0.01 and not ongoing:
            shift = db.query(CashierShift).filter(CashierShift.user_id == user.id, CashierShift.status == "open").first()
            method = data.refund_method if data.refund_method in ("cash", "kpay", "wave", "ayapay", "cbpay", "bank", "card") else "cash"
            refund_payment(db, inv, amount=over, user_id=user.id, method=method,
                           reason=f"{EDIT_REFUND_TAG} doctor -> {doctor.full_name}", shift_id=shift.id if shift else None)
            refunded = over
        what = f"doctor {old_name} -> {doctor.full_name}" if old else f"added Consultation — {doctor.full_name}"
        note = (data.note or "").strip()
        audit(db, user.id, "history_edit_line" if old else "history_add_item", "invoice", str(inv.id),
              (f"{what} (was {was}, total {before:g} -> {float(inv.total or 0):g}" + (f", refund {refunded:g}" if refunded else "") + ")"
               + (f" — {note}" if note else ""))[:400])
        db.commit()
    except ValueError as e:
        db.rollback()
        raise HTTPException(400, str(e)) from e
    return (
        db.query(Invoice)
        .options(joinedload(Invoice.lines), joinedload(Invoice.payments))
        .filter(Invoice.id == invoice_id)
        .first()
    )


class HistorySaveLine(BaseModel):
    id: int
    qty: float | None = None
    unit_price: float | None = None
    delete: bool = False


class HistorySaveAdd(BaseModel):
    item_id: int | None = None
    doctor_id: int | None = None
    qty: float = 1
    round: bool = False  # doctor round fee (price x rounds) instead of a consultation
    surgery: bool = False  # OT doctor fee (surgeon / anaesth / assistant)
    role: str = "surgeon"
    procedure: str = ""
    unit_price: float | None = None


class HistorySavePay(BaseModel):
    id: int | None = None  # None = a new payment
    method: str = "cash"
    amount: float = 0
    delete: bool = False


class HistorySaveIn(BaseModel):
    payments: list[HistorySavePay] = []
    lines: list[HistorySaveLine] = []
    add: list[HistorySaveAdd] = []
    discount: float | None = None
    note: str = ""
    refund_method: str = "cash"
    warehouse_id: int | None = None


@router.post("/invoices/{invoice_id}/history-save", response_model=InvoiceOut)
def history_save(invoice_id: int, data: HistorySaveIn, db: Session = Depends(get_db), user: User = Depends(require("billing", "pos"))):
    """Bill History "Bill ပြင်မယ်": every change made in the editor (qty / price /
    remove / add item or doctor / discount) saved at once with one note. Comes to
    less than paid -> the difference is refunded; more -> the bill reopens with
    the extra due."""
    from app.models.ipd import Admission
    from app.services.billing_service import EDIT_REFUND_TAG, _adjust_line_stock, _cancel_service_orders
    from app.services.counter_service import add_doctor_fee

    inv = db.get(Invoice, invoice_id)
    if not inv:
        raise HTTPException(404, "Invoice not found")
    if inv.status == "void":
        raise HTTPException(400, "Void bill ကို ပြင်လို့ မရပါ")
    note = (data.note or "").strip()
    if len(note) < 2:
        raise HTTPException(400, "Note (ဘာကြောင့် ပြင်တာလဲ) ထည့်ပါ")
    was = inv.status
    before = float(inv.total or 0)
    changes: list[str] = []
    removed_docs: list[int] = []
    try:
        if inv.status == "paid":
            inv.status = "partial"  # adding items needs an unsettled bill
        for e in data.lines:
            line = db.get(InvoiceLine, e.id)
            if not line or line.invoice_id != invoice_id:
                raise ValueError("Line not found")
            if line.returned_from_line_id or (line.qty or 0) < 0 or any(l.returned_from_line_id == line.id for l in inv.lines):
                raise ValueError(f"{line.description}: Return လုပ်ထားတဲ့ item ကို ပြင်/ဖျက်လို့ မရပါ")
            desc = line.description
            if e.delete:
                if line.source == "opd" and line.doctor_id:
                    removed_docs.append(line.doctor_id)
                _adjust_line_stock(db, inv, line, 0)
                _cancel_service_orders(db, inv, line, line.qty)
                inv.lines.remove(line)
                db.delete(line)
                db.flush()
                changes.append(f"removed {desc}")
                continue
            q = e.qty if e.qty is not None and abs(e.qty - (line.qty or 0)) > 1e-9 else None
            pr = e.unit_price if e.unit_price is not None and abs(e.unit_price - (line.unit_price or 0)) > 0.001 else None
            if q is None and pr is None:
                continue
            old = f"{line.qty:g}x{line.unit_price:g}"
            update_line(db, inv, line, unit_price=pr, qty=q)
            changes.append(f"{desc}: {old} -> {line.qty:g}x{line.unit_price:g}")
        new_docs: list[User] = []
        for a in data.add:
            if a.surgery and a.doctor_id:
                from app.services.billing_service import add_surgery_fee

                doctor = db.get(User, a.doctor_id)
                if not doctor or not doctor.is_active:
                    raise ValueError("Doctor not found")
                if inv.status == "paid":
                    inv.status = "partial"
                ln = add_surgery_fee(db, inv, doctor, a.unit_price if a.unit_price is not None else -1, a.role, a.procedure)
                changes.append(f"added {ln.description} {(a.unit_price or 0):g}")
                continue
            if a.round and a.doctor_id:
                from app.services.billing_service import add_round_fee

                doctor = db.get(User, a.doctor_id)
                if not doctor or not doctor.is_active:
                    raise ValueError("Doctor not found")
                if inv.status == "paid":
                    inv.status = "partial"
                add_round_fee(db, inv, doctor, a.unit_price if a.unit_price is not None else -1, a.qty)
                changes.append(f"added Round fee — {doctor.full_name} {a.qty:g} x {(a.unit_price or 0):g}")
                continue
            if a.doctor_id:
                doctor = db.get(User, a.doctor_id)
                if not doctor or not doctor.is_active:
                    raise ValueError("Doctor not found")
                if inv.status == "paid":
                    inv.status = "partial"
                ln = add_doctor_fee(db, inv, doctor)
                if ln is not None and a.unit_price is not None and a.unit_price >= 0 and abs(a.unit_price - (ln.unit_price or 0)) > 0.001:
                    update_line(db, inv, ln, unit_price=a.unit_price)
                new_docs.append(doctor)
                changes.append(f"added Consultation — {doctor.full_name}" + (f" {ln.unit_price:g}" if ln is not None else ""))
            elif a.item_id:
                item = db.get(CatalogItem, a.item_id)
                if not item:
                    raise ValueError("Item not found")
                if a.qty <= 0:
                    raise ValueError("Qty must be positive")
                if inv.status == "paid":
                    inv.status = "partial"
                prev_ids = {i for (i,) in db.query(InvoiceLine.id).filter(InvoiceLine.invoice_id == inv.id)}
                order_on_invoice(db, inv, item, qty=a.qty, user_id=user.id, warehouse_id=data.warehouse_id)
                if a.unit_price is not None:
                    if a.unit_price < 0:
                        raise ValueError("ဈေး မှန်အောင် ထည့်ပါ")
                    db.flush()
                    for ln in db.query(InvoiceLine).filter(InvoiceLine.invoice_id == inv.id, InvoiceLine.item_id == item.id,
                                                          ~InvoiceLine.id.in_(prev_ids or {0})).all():
                        if abs(float(ln.unit_price or 0) - a.unit_price) > 0.001:
                            update_line(db, inv, ln, unit_price=a.unit_price)
                changes.append(f"added {item.name} x{a.qty:g}" + (f" @ {a.unit_price:g}" if a.unit_price is not None else ""))
        # The bill's doctor was taken off: the visit goes to the doctor put on
        # instead (with the lab / X-ray lines that counted for the old one).
        if removed_docs and inv.doctor_id in removed_docs:
            new = new_docs[0] if new_docs else None
            if new is None:
                rest = next((l for l in inv.lines if l.source == "opd" and l.doctor_id and (l.qty or 0) > 0), None)
                new = db.get(User, rest.doctor_id) if rest else None
            if new is not None:
                old_doc = inv.doctor_id
                for l in inv.lines:
                    if l.doctor_id == old_doc and l.source in ("lab", "radiology"):
                        l.doctor_id = new.id
                inv.doctor_id = new.id
        db.flush()
        recalc_invoice(inv, db)
        if data.discount is not None and abs(data.discount - float(inv.discount or 0)) > 0.001:
            if data.discount < 0 or data.discount > inv.subtotal + 0.01:
                raise ValueError(f"Discount must be between 0 and the subtotal ({inv.subtotal:.0f})")
            changes.append(f"discount {float(inv.discount or 0):g} -> {data.discount:g}")
            inv.discount = data.discount
            recalc_invoice(inv, db)
        # Payments: a wrong method / amount is put right by replacing the payment
        # (same day, shift and cashier) and voiding the old one with its ledger entry.
        from app.services.accounting_service import post_payment
        from app.services.billing_service import void_payment

        methods = ("cash", "kpay", "wave", "ayapay", "cbpay", "bank", "card")
        adm_row = db.get(Admission, inv.admission_id) if inv.admission_id else None
        to_void: list[Payment] = []
        added_new = False
        for e in data.payments:
            if e.id is None:
                if e.delete:
                    continue
                if e.method not in methods or e.amount <= 0:
                    raise ValueError("Payment method / amount မှန်အောင် ထည့်ပါ")
                shift = db.query(CashierShift).filter(CashierShift.user_id == user.id, CashierShift.status == "open").first()
                np = Payment(invoice_id=inv.id, shift_id=shift.id if shift else None, method=e.method, amount=e.amount, received_by=user.id, notes=note[:200])
                inv.payments.append(np)
                db.flush()
                post_payment(db, inv, np)
                if adm_row:
                    adm_row.deposit = float(adm_row.deposit or 0) + e.amount
                added_new = True
                changes.append(f"payment added {e.method} {e.amount:g}")
                continue
            p = db.get(Payment, e.id)
            if not p or p.invoice_id != invoice_id or p.method == "refund":
                raise ValueError("Payment not found")
            if e.delete:
                to_void.append(p)
                changes.append(f"payment removed {p.method} {p.amount:g}")
                continue
            if e.method == p.method and abs(e.amount - p.amount) < 0.001:
                continue
            if e.method not in methods or e.amount <= 0:
                raise ValueError("Payment method / amount မှန်အောင် ထည့်ပါ")
            np = Payment(invoice_id=inv.id, shift_id=p.shift_id, method=e.method, amount=e.amount, received_by=p.received_by,
                         notes=p.notes, created_at=p.created_at)
            inv.payments.append(np)
            db.flush()
            post_payment(db, inv, np)
            if adm_row:
                adm_row.deposit = float(adm_row.deposit or 0) + e.amount
            to_void.append(p)
            changes.append(f"payment {p.method} {p.amount:g} -> {e.method} {e.amount:g}")
        for p in to_void:
            void_payment(db, inv, p, user_id=user.id, reason=note)
        recalc_invoice(inv, db)
        if added_new and (inv.paid or 0) > (inv.total or 0) + 0.01 and not (adm_row and adm_row.status != "discharged"):
            raise ValueError(f"ပေးငွေ ({inv.paid:,.0f}) က Total ({inv.total:,.0f}) ထက် များနေပါတယ်")
        if not changes:
            raise ValueError("ပြင်ထားတာ မရှိပါ")
        ongoing = bool(adm_row and adm_row.status != "discharged")
        refunded = 0.0
        over = round((inv.paid or 0) - (inv.total or 0), 2)
        if over > 0.01 and not ongoing:
            shift = db.query(CashierShift).filter(CashierShift.user_id == user.id, CashierShift.status == "open").first()
            method = data.refund_method if data.refund_method in methods else "cash"
            refund_payment(db, inv, amount=over, user_id=user.id, method=method, reason=f"{EDIT_REFUND_TAG} {note}"[:200],
                           shift_id=shift.id if shift else None)
            refunded = over
        audit(db, user.id, "history_edit_line", "invoice", str(inv.id),
              ("; ".join(changes) + f" (was {was}, total {before:g} -> {float(inv.total or 0):g}"
               + (f", refund {refunded:g}" if refunded else "") + f") — {note}")[:1000])
        db.commit()
    except ValueError as e:
        db.rollback()
        raise HTTPException(400, str(e)) from e
    return (
        db.query(Invoice)
        .options(joinedload(Invoice.lines), joinedload(Invoice.payments))
        .filter(Invoice.id == invoice_id)
        .first()
    )


@router.get("/invoices/{invoice_id}/log")
def invoice_log(invoice_id: int, db: Session = Depends(get_db), _: User = Depends(require("billing", "pos", "ipd"))):
    """What was changed on this bill after it was made: items edited / removed /
    added, payments voided, the bill voided — who, when, why."""
    from app.models.org import AuditLog
    from app.api.v1.counters import ACTIVITY_ACTIONS

    inv = db.get(Invoice, invoice_id)
    if not inv:
        raise HTTPException(404, "Invoice not found")
    line_ids = [str(l.id) for l in inv.lines]
    acts = ("history_edit_line", "history_add_item", "void_line", "edit_line", "return_line", "exchange_line",
            "invoice_void", "payment_void", "pharmacy_line_qty", "discount", "refund", "add_round_fee", "add_doctor_fee", "add_surgery_fee", "change_paid_date", "change_payment_date", "edit_payment", "reopen_bill", "credit_close", "credit_cancel", "close_bill", "invoice_delete", "invoice_unvoid")
    q = db.query(AuditLog).filter(AuditLog.action.in_(acts)).filter(
        ((AuditLog.entity == "invoice") & (AuditLog.entity_id == str(invoice_id)))
        | ((AuditLog.entity == "invoice_line") & (AuditLog.entity_id.in_(line_ids or ["-"])))
    )
    rows = q.order_by(AuditLog.id).limit(200).all()
    names = {u.id: u.full_name or u.username for u in db.query(User).filter(User.id.in_({r.user_id for r in rows if r.user_id}))} if rows else {}
    return [
        {
            "at": r.created_at.isoformat() if r.created_at else None,
            "user": names.get(r.user_id, ""),
            "action": r.action,
            "label": ACTIVITY_ACTIONS.get(r.action, ("", r.action, r.action))[1],
            "label_en": ACTIVITY_ACTIONS.get(r.action, ("", r.action, r.action))[2],
            "detail": r.detail or "",
        }
        for r in rows
    ]


class VoidIn(BaseModel):
    reason: str = ""


BILL_ADMINS = ("super_admin", "hospital_admin", "branch_admin")


@router.post("/invoices/{invoice_id}/void", response_model=InvoiceOut)
def void_bill(invoice_id: int, data: VoidIn, db: Session = Depends(get_db), user: User = Depends(require("billing", "pos"))):
    """Cancel a whole bill. Empty bills (nothing charged, nothing paid) any cashier
    may cancel; anything else only an admin."""
    from app.services.billing_service import void_invoice

    inv = db.get(Invoice, invoice_id)
    if not inv:
        raise HTTPException(404)
    reason = (data.reason or "").strip()
    if len(reason) < 2:
        raise HTTPException(400, "ပယ်ရတဲ့ အကြောင်းပြချက် ထည့်ပါ")
    recalc_invoice(inv, db)
    empty = (inv.subtotal or 0) <= 0.01 and (inv.paid or 0) <= 0.01
    if not empty and user.role not in BILL_ADMINS:
        raise HTTPException(403, "ငွေ/item ပါတဲ့ bill ကို Admin ပဲ ပယ်လို့ရပါတယ်")
    shift = db.query(CashierShift).filter(CashierShift.user_id == user.id, CashierShift.status == "open").first()
    try:
        void_invoice(db, inv, user_id=user.id, reason=reason, shift_id=shift.id if shift else None)
        db.commit()
    except ValueError as e:
        db.rollback()
        raise HTTPException(400, str(e)) from e
    return (
        db.query(Invoice)
        .options(joinedload(Invoice.lines), joinedload(Invoice.payments))
        .filter(Invoice.id == invoice_id)
        .first()
    )


@router.post("/invoices/{invoice_id}/unvoid", response_model=InvoiceOut)
def unvoid_bill(invoice_id: int, data: VoidIn, db: Session = Depends(get_db), user: User = Depends(require("billing", "pos"))):
    """↩ Undo a Void done by mistake (Admin; a cashier only for an empty bill)."""
    from app.services.billing_service import unvoid_invoice

    inv = db.get(Invoice, invoice_id)
    if not inv:
        raise HTTPException(404)
    reason = (data.reason or "").strip()
    if len(reason) < 2:
        raise HTTPException(400, "အကြောင်းပြချက် ထည့်ပါ")
    has_money = bool(inv.lines) or any(p.method != "refund" for p in inv.payments)
    if has_money and user.role not in BILL_ADMINS:
        raise HTTPException(403, "ငွေ/item ပါတဲ့ bill ကို Admin ပဲ Void ပြန်ဖျက်လို့ရပါတယ်")
    try:
        unvoid_invoice(db, inv, user_id=user.id, reason=reason)
        db.commit()
    except ValueError as e:
        db.rollback()
        raise HTTPException(400, str(e)) from e
    db.expire_all()
    return (
        db.query(Invoice)
        .options(joinedload(Invoice.lines), joinedload(Invoice.payments))
        .filter(Invoice.id == invoice_id)
        .first()
    )


@router.post("/invoices/{invoice_id}/delete")
def delete_bill(invoice_id: int, data: VoidIn, db: Session = Depends(get_db), user: User = Depends(require("billing", "pos"))):
    """🗑 Delete a whole voucher made by mistake — gone from History, Open Bills
    and the day's money (Void keeps it as VOID with a refund instead). Admins:
    any bill; cashiers: only a bill opened and paid today."""
    from app.services.billing_service import delete_invoice

    inv = db.get(Invoice, invoice_id)
    if not inv:
        raise HTTPException(404)
    reason = (data.reason or "").strip()
    if len(reason) < 2:
        raise HTTPException(400, "ဖျက်ရတဲ့ အကြောင်းပြချက် ထည့်ပါ")
    if user.role not in BILL_ADMINS:
        today = clock.today()
        old = (inv.created_at and inv.created_at.date() != today) or any(
            p.created_at and p.created_at.date() != today for p in inv.payments)
        if old:
            raise HTTPException(403, "ဒီနေ့ မဟုတ်တဲ့ voucher ကို Admin ပဲ ဖျက်လို့ရပါတယ်")
    try:
        number = delete_invoice(db, inv, user_id=user.id, reason=reason)
        db.commit()
    except ValueError as e:
        db.rollback()
        raise HTTPException(400, str(e)) from e
    return {"ok": True, "deleted": number}


@router.post("/invoices/{invoice_id}/payments/{payment_id}/void", response_model=InvoiceOut)
def void_bill_payment(invoice_id: int, payment_id: int, data: VoidIn, db: Session = Depends(get_db), user: User = Depends(require("billing", "pos"))):
    """Remove a payment entered by mistake. Cashiers: only today's payments from
    their still-open shift (or today's, if no shift); admins: any."""
    from app.services.billing_service import void_payment

    inv = db.query(Invoice).options(joinedload(Invoice.payments)).filter(Invoice.id == invoice_id).first()
    pay = next((p for p in (inv.payments if inv else []) if p.id == payment_id), None)
    if not inv or not pay:
        raise HTTPException(404)
    reason = (data.reason or "").strip()
    if len(reason) < 2:
        raise HTTPException(400, "ပယ်ရတဲ့ အကြောင်းပြချက် ထည့်ပါ")
    if user.role not in BILL_ADMINS:
        shift = db.get(CashierShift, pay.shift_id) if pay.shift_id else None
        same_day = pay.created_at and pay.created_at.date() == clock.today()
        if not same_day or (shift is not None and shift.status != "open"):
            raise HTTPException(403, "Shift ပိတ်ပြီး (ဒါမှမဟုတ် ရက်ကျော်ပြီး) ငွေကို Admin ပဲ ပယ်လို့ရပါတယ်")
    try:
        void_payment(db, inv, pay, user_id=user.id, reason=reason)
        db.commit()
    except ValueError as e:
        db.rollback()
        raise HTTPException(400, str(e)) from e
    return (
        db.query(Invoice)
        .options(joinedload(Invoice.lines), joinedload(Invoice.payments))
        .filter(Invoice.id == invoice_id)
        .first()
    )

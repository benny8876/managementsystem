"""Store / Pharmacy "ရောင်းအား" (Item Sales): how each stock item sells, so staff
can see what sells well, what has stopped selling, and which near-expiry stock
will not be sold before it expires.

Sales are the bill lines taken from this warehouse's batches (returns netted,
void bills left out) — a store that only sends stock on, like a central
warehouse, shows no sales of its own. The selling rate behind "days of stock left" and the expiry risk is
always the last 90 days (or since the first bill, if the system is newer), so
it does not jump around with the period picked on screen.
"""
from datetime import date, datetime, timedelta

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core import clock
from app.models.billing import BILL_DATE, Invoice, InvoiceLine
from app.models.catalog import CatalogItem
from app.models.inventory import StockBatch
from app.models.org import Warehouse
from app.models.patients import Patient
from app.services.inventory_service import apply_department, expiry_alert_date

RATE_DAYS = 90
SLOW_DAYS = 60


def _branch(db: Session, warehouse_id: int) -> int:
    wh = db.get(Warehouse, warehouse_id)
    if not wh:
        raise ValueError("Warehouse not found")
    return wh.branch_id


def _sold(db: Session, warehouse_id: int, branch_id: int, item_ids: list[int], since: datetime, until: datetime, paid_only: bool = True):
    # Sales figures count fully paid bills (on their paid day) — the selling
    # rate behind "days left" / expiry risk counts every bill, since the stock
    # has left the shelf either way.
    status = Invoice.status == "paid" if paid_only else Invoice.status != "void"
    return (
        db.query(InvoiceLine.item_id, func.sum(InvoiceLine.qty), func.sum(InvoiceLine.amount), func.count(func.distinct(Invoice.id)))
        .join(Invoice, Invoice.id == InvoiceLine.invoice_id)
        .join(StockBatch, StockBatch.id == InvoiceLine.batch_id)
        .filter(
            StockBatch.warehouse_id == warehouse_id,
            Invoice.branch_id == branch_id,
            status,
            BILL_DATE >= since,
            BILL_DATE <= until,
            InvoiceLine.item_id.in_(item_ids),
        )
        .group_by(InvoiceLine.item_id)
        .all()
    )


def item_sales(db: Session, *, warehouse_id: int, department: str, since: datetime, until: datetime) -> list[dict]:
    branch_id = _branch(db, warehouse_id)
    today = clock.today()
    items = apply_department(
        db.query(CatalogItem).filter(CatalogItem.is_active == True, CatalogItem.is_stock == True),  # noqa: E712
        department,
    ).order_by(CatalogItem.name).all()
    ids = [i.id for i in items]
    if not ids:
        return []

    window = {r[0]: r for r in _sold(db, warehouse_id, branch_id, ids, since, until)}
    first_bill = db.query(func.min(Invoice.created_at)).filter(Invoice.branch_id == branch_id).scalar()
    rate_days = min(RATE_DAYS, max((today - first_bill.date()).days + 1, 1)) if first_bill else RATE_DAYS
    rate_since = datetime.combine(today - timedelta(days=rate_days - 1), datetime.min.time())
    recent = {r[0]: float(r[1] or 0) for r in _sold(db, warehouse_id, branch_id, ids, rate_since, clock.now(), paid_only=False)}
    last_sold = dict(
        db.query(InvoiceLine.item_id, func.max(BILL_DATE))
        .join(Invoice, Invoice.id == InvoiceLine.invoice_id)
        .join(StockBatch, StockBatch.id == InvoiceLine.batch_id)
        .filter(
            StockBatch.warehouse_id == warehouse_id, Invoice.branch_id == branch_id, Invoice.status != "void",
            InvoiceLine.item_id.in_(ids), InvoiceLine.qty > 0,
        )
        .group_by(InvoiceLine.item_id)
        .all()
    )
    batches: dict[int, list[StockBatch]] = {}
    for b in db.query(StockBatch).filter(StockBatch.warehouse_id == warehouse_id, StockBatch.item_id.in_(ids), StockBatch.qty > 0):
        batches.setdefault(b.item_id, []).append(b)

    slow_before = today - timedelta(days=SLOW_DAYS)
    out = []
    for it in items:
        bs = sorted(batches.get(it.id, []), key=lambda b: (b.expiry_date is None, b.expiry_date or date.max, b.id))
        on_hand = sum(b.qty for b in bs)
        rate = recent.get(it.id, 0.0) / rate_days
        # Expiry risk: selling at the current rate, oldest batch first, how much of
        # the stock expiring within the item's alert window is left when it expires.
        alert = expiry_alert_date(it, today)
        sold_before = 0.0
        risk = 0.0
        for b in bs:
            if b.expiry_date is None:
                continue
            can_sell = max(0.0, min(b.qty, rate * max((b.expiry_date - today).days, 0) - sold_before))
            if b.expiry_date <= alert:
                risk += b.qty - can_sell
            sold_before += b.qty
        w = window.get(it.id)
        last = last_sold.get(it.id)
        nearest = next((b.expiry_date for b in bs if b.expiry_date), None)
        out.append({
            "id": it.id,
            "name": it.name,
            "sku": it.sku,
            "sub_category": it.sub_category or "",
            "unit": it.unit or "",
            "price": float(it.price or 0),
            "sold_qty": float(w[1] or 0) if w else 0.0,
            "amount": float(w[2] or 0) if w else 0.0,
            "bills": int(w[3] or 0) if w else 0,
            "on_hand": on_hand,
            "nearest_expiry": nearest.isoformat() if nearest else None,
            "expired": bool(nearest and nearest < today),
            "days_left": round(on_hand / rate) if rate > 0 and on_hand > 0 else None,
            "last_sold": last.isoformat() if last else None,
            "is_low": on_hand <= (it.min_stock or 0),
            "is_slow": on_hand > 0 and (last is None or last.date() < slow_before),
            "risk_qty": round(risk),
            "risk_value": round(round(risk) * float(it.price or 0)),
        })
    return out


def item_sales_detail(db: Session, *, item_id: int, warehouse_id: int, since: datetime, until: datetime) -> dict:
    branch_id = _branch(db, warehouse_id)
    item = db.get(CatalogItem, item_id)
    if not item:
        raise ValueError("Item not found")
    rows = (
        db.query(Invoice.id, Invoice.number, BILL_DATE, Patient.name, Patient.uhid, InvoiceLine.qty, InvoiceLine.unit_price, InvoiceLine.amount, InvoiceLine.returned_from_line_id)
        .join(InvoiceLine, InvoiceLine.invoice_id == Invoice.id)
        .join(StockBatch, StockBatch.id == InvoiceLine.batch_id)
        .outerjoin(Patient, Patient.id == Invoice.patient_id)
        .filter(
            StockBatch.warehouse_id == warehouse_id,
            Invoice.branch_id == branch_id,
            Invoice.status == "paid",
            BILL_DATE >= since,
            BILL_DATE <= until,
            InvoiceLine.item_id == item_id,
        )
        .order_by(Invoice.created_at.desc(), InvoiceLine.id.desc())
        .limit(1000)
        .all()
    )
    batches = (
        db.query(StockBatch)
        .filter(StockBatch.item_id == item_id, StockBatch.warehouse_id == warehouse_id, StockBatch.qty != 0)
        .order_by(StockBatch.expiry_date.asc().nullslast(), StockBatch.id)
        .all()
    )
    return {
        "item": {"id": item.id, "name": item.name, "sku": item.sku, "unit": item.unit or "", "price": float(item.price or 0)},
        "sales": [
            {
                "invoice_id": r[0], "invoice_number": r[1], "created_at": r[2].isoformat() if r[2] else None,
                "patient_name": r[3] or "—", "uhid": r[4] or "—", "qty": float(r[5] or 0),
                "unit_price": float(r[6] or 0), "amount": float(r[7] or 0), "is_return": bool(r[8]),
            }
            for r in rows
        ],
        "batches": [
            {"batch_no": b.batch_no, "expiry_date": b.expiry_date.isoformat() if b.expiry_date else None, "qty": float(b.qty), "notes": b.notes or ""}
            for b in batches
        ],
    }


def daily_dispensed(db: Session, *, warehouse_id: int, day: date, date_to: date | None = None) -> dict:
    """Pharmacy "📅 Daily": what left this store on one day — qty only, no money
    (pharmacy staff check stock, not cash). Counts every bill (paid or not, void
    left out) by the day the medicine was given; returns that day net off."""
    branch_id = _branch(db, warehouse_id)
    # One day, or day .. date_to (a range: 7 days, a month, three months …).
    last = date_to if date_to and date_to >= day else day
    start = datetime.combine(day, datetime.min.time())
    end = datetime.combine(last, datetime.min.time()) + timedelta(days=1)
    when = func.coalesce(InvoiceLine.created_at, Invoice.created_at)
    lines = (
        db.query(InvoiceLine.item_id, InvoiceLine.qty, Invoice.id, Invoice.kind)
        .join(Invoice, Invoice.id == InvoiceLine.invoice_id)
        .join(StockBatch, StockBatch.id == InvoiceLine.batch_id)
        .filter(
            StockBatch.warehouse_id == warehouse_id,
            Invoice.branch_id == branch_id,
            Invoice.status != "void",
            when >= start,
            when < end,
            InvoiceLine.item_id.isnot(None),
        )
        .all()
    )
    qty: dict[int, float] = {}
    bills_by_item: dict[int, set] = {}
    walkin, patient = set(), set()
    for item_id, q, inv_id, kind in lines:
        qty[item_id] = qty.get(item_id, 0.0) + float(q or 0)
        bills_by_item.setdefault(item_id, set()).add(inv_id)
        (walkin if kind == "pos" else patient).add(inv_id)
    ids = [i for i, q in qty.items() if abs(q) > 1e-9]
    items = {i.id: i for i in db.query(CatalogItem).filter(CatalogItem.id.in_(ids))} if ids else {}
    on_hand = dict(
        db.query(StockBatch.item_id, func.coalesce(func.sum(StockBatch.qty), 0))
        .filter(StockBatch.warehouse_id == warehouse_id, StockBatch.item_id.in_(ids))
        .group_by(StockBatch.item_id)
        .all()
    ) if ids else {}
    out = []
    for i in ids:
        it = items.get(i)
        if not it:
            continue
        oh = float(on_hand.get(i, 0) or 0)
        out.append({
            "id": i, "name": it.name, "sku": it.sku, "unit": it.unit or "", "sub_category": it.sub_category or "",
            "department": it.department, "qty": round(qty[i], 2), "bills": len(bills_by_item.get(i, ())),
            "on_hand": oh, "min_stock": float(it.min_stock or 0), "is_low": oh <= float(it.min_stock or 0),
        })
    out.sort(key=lambda r: (-r["qty"], r["name"]))
    return {
        "day": day.isoformat(),
        "date_to": last.isoformat(),
        "days": (last - day).days + 1,
        "items": out,
        "total_qty": round(sum(r["qty"] for r in out), 2),
        "bills": len(walkin | patient),
        "walkin_bills": len(walkin),
        "patient_bills": len(patient),
    }

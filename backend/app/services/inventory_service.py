from datetime import date, timedelta

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core import clock
from app.models.catalog import CatalogItem, LabTestReagent
from app.models.inventory import StockBatch, StockMovement, WastageLog
from app.services.utils import audit


def stock_on_hand(db: Session, item_id: int, warehouse_id: int | list[int] | None = None) -> float:
    q = db.query(func.coalesce(func.sum(StockBatch.qty), 0)).filter(StockBatch.item_id == item_id)
    if isinstance(warehouse_id, list):
        q = q.filter(StockBatch.warehouse_id.in_(warehouse_id))
    elif warehouse_id:
        q = q.filter(StockBatch.warehouse_id == warehouse_id)
    return float(q.scalar() or 0)


def _scope_batches(q, warehouse_id: int | list[int] | None):
    if isinstance(warehouse_id, list):
        return q.filter(StockBatch.warehouse_id.in_(warehouse_id))
    if warehouse_id:
        return q.filter(StockBatch.warehouse_id == warehouse_id)
    return q


def stock_summary(
    db: Session, item_ids: list[int], warehouse_id: int | list[int] | None = None,
) -> tuple[dict[int, float], dict[int, date]]:
    """On-hand qty and nearest (in-stock) expiry for many items in two grouped
    queries, instead of two queries per item."""
    if not item_ids:
        return {}, {}
    totals = dict(
        _scope_batches(
            db.query(StockBatch.item_id, func.coalesce(func.sum(StockBatch.qty), 0)).filter(StockBatch.item_id.in_(item_ids)),
            warehouse_id,
        ).group_by(StockBatch.item_id).all()
    )
    expiries = dict(
        _scope_batches(
            db.query(StockBatch.item_id, func.min(StockBatch.expiry_date)).filter(
                StockBatch.item_id.in_(item_ids), StockBatch.qty > 0, StockBatch.expiry_date.isnot(None),
            ),
            warehouse_id,
        ).group_by(StockBatch.item_id).all()
    )
    return {k: float(v or 0) for k, v in totals.items()}, expiries


def pick_fefo(
    db: Session, item_id: int, warehouse_id: int, qty: float, allow_negative: bool = False,
) -> list[tuple[StockBatch, float]]:
    batches = (
        db.query(StockBatch)
        .filter(StockBatch.item_id == item_id, StockBatch.warehouse_id == warehouse_id, StockBatch.qty > 0)
        .order_by(StockBatch.expiry_date.asc().nullslast(), StockBatch.id.asc())
        .all()
    )
    remaining = qty
    picks: list[tuple[StockBatch, float]] = []
    for b in batches:
        if remaining <= 0:
            break
        take = min(b.qty, remaining)
        b.qty -= take
        remaining -= take
        picks.append((b, take))
    if remaining > 0:
        if not allow_negative:
            raise ValueError("Insufficient stock")
        # Patient care can't wait for a stock-in entry: overdraw the newest batch
        # (or open a placeholder one) so on-hand goes negative. The next receive
        # settles it first, and the alerts list flags it until then.
        overdraft = (
            db.query(StockBatch)
            .filter(StockBatch.item_id == item_id, StockBatch.warehouse_id == warehouse_id)
            .order_by(StockBatch.id.desc())
            .first()
        )
        if not overdraft:
            item = db.get(CatalogItem, item_id)
            overdraft = StockBatch(
                item_id=item_id, warehouse_id=warehouse_id, batch_no="NEGATIVE", qty=0,
                unit_cost=item.cost if item else 0,
            )
            db.add(overdraft)
            db.flush()
        overdraft.qty -= remaining
        picks.append((overdraft, remaining))
    return picks


def dispense(db: Session, item: CatalogItem, warehouse_id: int, qty: float, ref: str, user_id: int | None):
    picks = pick_fefo(db, item.id, warehouse_id, qty, allow_negative=True)
    for batch, take in picks:
        db.add(StockMovement(item_id=item.id, batch_id=batch.id, qty_delta=-take, reason="dispense", ref=ref))
    audit(db, user_id, "dispense", "catalog_item", str(item.id), f"qty={qty} ref={ref}")
    return picks


def receive_stock(
    db: Session,
    item_id: int,
    warehouse_id: int,
    qty: float,
    unit_cost: float,
    batch_no: str,
    expiry: date | None,
    user_id: int | None,
    selling_price: float | None = None,
    min_stock: float | None = None,
    notes: str = "",
):
    item = db.get(CatalogItem, item_id)
    if not item or not item.is_stock:
        raise ValueError("Medicine not found")
    if qty <= 0:
        raise ValueError("Quantity must be positive")
    if selling_price is not None:
        item.price = selling_price
    if min_stock is not None:
        item.min_stock = min_stock
    if unit_cost > 0:
        item.cost = unit_cost
    batch = receive_po_line(db, item_id, warehouse_id, batch_no, expiry, qty, unit_cost)
    batch.notes = (notes or "").strip()[:200]
    audit(db, user_id, "stock_receive", "catalog_item", str(item_id), f"qty={qty} batch={batch_no}")
    return batch


STOCK_DEPARTMENTS = ("pharmacy", "lab", "xray", "usg", "cashier", "ot")
# OT (operating theatre): its medicines / supplies (stock) and its charges (no stock).
OT_DEPARTMENT = "ot"
# Seeded medicines use department="drug"; later ones use "pharmacy".
PHARMACY_DEPARTMENTS = ("pharmacy", "drug")
_SKU_PREFIX = {"pharmacy": "MED-", "lab": "LABS-", "xray": "XRS-", "usg": "USGS-", "cashier": "CSH-", "ot": "OT-"}
_CATEGORY = {"pharmacy": "drug", "lab": "supply", "xray": "supply", "usg": "supply", "cashier": "cashier", "ot": "drug"}


def stock_department(value: str | None) -> str:
    dept = (value or "pharmacy").strip().lower()
    if dept not in STOCK_DEPARTMENTS:
        raise ValueError("department must be pharmacy, lab, xray, usg, cashier or ot")
    return dept


def apply_department(query, department: str | None):
    """Limit a CatalogItem query to one Store segment (or several, comma-separated,
    e.g. "pharmacy,ot" for the Pharmacy counter). pharmacy includes legacy department=drug."""
    if not department:
        return query
    if "," in department:
        depts: list[str] = []
        for d in department.split(","):
            if d.strip():
                d = stock_department(d)
                depts.extend(PHARMACY_DEPARTMENTS if d == "pharmacy" else (d,))
        return query.filter(CatalogItem.department.in_(depts))
    dept = stock_department(department)
    if dept == "pharmacy":
        return query.filter(CatalogItem.department.in_(PHARMACY_DEPARTMENTS))
    return query.filter(CatalogItem.department == dept)


def create_medicine(
    db: Session,
    name: str,
    sku: str,
    barcode: str,
    price: float,
    cost: float,
    min_stock: float,
    user_id: int | None,
    department: str = "pharmacy",
    sub_category: str = "",
    unlimited: bool = False,
    expiry_alert_months: int = 1,
):
    """unlimited (Cashier / OT charges only): sold without stock tracking — never
    runs out, no batches."""
    from app.services.utils import next_number

    dept = stock_department(department)
    code = sku.strip() or next_number(db, f"sku_{dept}", _SKU_PREFIX[dept])
    existing = db.query(CatalogItem).filter(CatalogItem.sku == code).first()
    if existing:
        if not existing.is_active:
            raise ValueError(f"SKU {code} ({existing.name}) က Deleted Items ထဲမှာ ရှိပါတယ် — အဲ့မှာ Restore လုပ်ပါ")
        raise ValueError("SKU already exists")
    item = CatalogItem(
        sku=code,
        barcode=barcode or code,
        name=name.strip(),
        name_mm=name.strip(),
        category=_CATEGORY[dept],
        department=dept,
        sub_category=sub_category.strip(),
        price=price,
        cost=cost,
        min_stock=min_stock,
        is_stock=not (unlimited and dept in ("cashier", "ot")),
        expiry_alert_months=max(int(expiry_alert_months or 1), 1),
    )
    db.add(item)
    db.flush()
    audit(db, user_id, "medicine_create", "catalog_item", str(item.id), name)
    return item


def set_reagent_test_links(db: Session, reagent_item_id: int, links: list[tuple[int, float]]) -> None:
    """Reagent-centric recipe: which billable lab tests consume this supply, and how much per run."""
    reagent = db.get(CatalogItem, reagent_item_id)
    if not reagent or not reagent.is_stock or reagent.department != "lab":
        raise ValueError("Lab stock item not found")
    cleaned: list[tuple[int, float]] = []
    seen_tests: set[int] = set()
    for test_id, qty in links:
        if test_id in seen_tests:
            continue
        seen_tests.add(test_id)
        if qty <= 0:
            raise ValueError("Qty per test must be positive")
        test = db.get(CatalogItem, test_id)
        if not test or test.category != "lab" or test.is_stock:
            raise ValueError("Invalid lab test for recipe link")
        cleaned.append((test_id, qty))
    db.query(LabTestReagent).filter(LabTestReagent.reagent_item_id == reagent_item_id).delete()
    for test_id, qty in cleaned:
        db.add(LabTestReagent(test_item_id=test_id, reagent_item_id=reagent_item_id, qty=qty))


def reagent_test_links_map(db: Session, reagent_ids: list[int]) -> dict[int, list[dict]]:
    if not reagent_ids:
        return {}
    rows = (
        db.query(LabTestReagent, CatalogItem)
        .join(CatalogItem, LabTestReagent.test_item_id == CatalogItem.id)
        .filter(LabTestReagent.reagent_item_id.in_(reagent_ids))
        .order_by(CatalogItem.name)
        .all()
    )
    out: dict[int, list[dict]] = {}
    for link, test in rows:
        out.setdefault(link.reagent_item_id, []).append({
            "test_item_id": test.id,
            "test_sku": test.sku,
            "test_name": test.name,
            "qty": link.qty,
        })
    return out


def nearest_expiry(db: Session, item_id: int, warehouse_id: int | list[int] | None = None) -> date | None:
    q = db.query(StockBatch).filter(StockBatch.item_id == item_id, StockBatch.qty > 0, StockBatch.expiry_date.isnot(None))
    if isinstance(warehouse_id, list):
        q = q.filter(StockBatch.warehouse_id.in_(warehouse_id))
    elif warehouse_id:
        q = q.filter(StockBatch.warehouse_id == warehouse_id)
    batch = q.order_by(StockBatch.expiry_date.asc()).first()
    return batch.expiry_date if batch else None


def receive_po_line(db: Session, item_id: int, warehouse_id: int, batch_no: str, expiry: date | None, qty: float, unit_cost: float):
    batch = StockBatch(
        item_id=item_id,
        warehouse_id=warehouse_id,
        batch_no=batch_no or "BATCH",
        expiry_date=expiry,
        qty=qty,
        unit_cost=unit_cost,
    )
    db.add(batch)
    db.flush()
    db.add(StockMovement(item_id=item_id, batch_id=batch.id, qty_delta=qty, reason="purchase", ref=batch_no))
    # Settle any stock that was used beyond zero before this delivery arrived.
    overdrawn = (
        db.query(StockBatch)
        .filter(StockBatch.item_id == item_id, StockBatch.warehouse_id == warehouse_id, StockBatch.qty < 0)
        .order_by(StockBatch.id.asc())
        .all()
    )
    for neg in overdrawn:
        if batch.qty <= 0:
            break
        fill = min(-neg.qty, batch.qty)
        neg.qty += fill
        batch.qty -= fill
        db.add(StockMovement(item_id=item_id, batch_id=batch.id, qty_delta=-fill, reason="settle_negative", ref=batch_no))
        db.add(StockMovement(item_id=item_id, batch_id=neg.id, qty_delta=fill, reason="settle_negative", ref=batch_no))
    return batch


def expiry_alert_date(item: CatalogItem, today: date) -> date:
    """Batches expiring on or before this date count as expiring soon; each
    medicine sets its own lead time (expiry_alert_months, ~30 days a month)."""
    return today + timedelta(days=30 * max(int(item.expiry_alert_months or 1), 1))


def alerts(db: Session, warehouse_id: int | None = None, department: str | None = None):
    today = clock.today()
    # Emptied batches pile up forever (one per delivery) and can't raise any alert.
    q = db.query(StockBatch).join(CatalogItem).filter(CatalogItem.is_stock == True, StockBatch.qty != 0)  # noqa: E712
    if warehouse_id:
        q = q.filter(StockBatch.warehouse_id == warehouse_id)
    if department:
        q = apply_department(q, department)
    batches = q.all()
    low, near_exp, expired, negative = [], [], [], []
    seen = set()

    def classify(item: CatalogItem, total: float) -> None:
        if total < 0:
            negative.append({"item_id": item.id, "name": item.name, "qty": total, "used_beyond_zero": -total})
        elif total <= item.min_stock:
            low.append({"item_id": item.id, "name": item.name, "qty": total, "min": item.min_stock})

    items_q = db.query(CatalogItem).filter(CatalogItem.is_stock == True, CatalogItem.is_active == True)  # noqa: E712
    if department:
        items_q = apply_department(items_q, department)
    all_items = {i.id: i for i in items_q.all()}
    for b in batches:
        all_items.setdefault(b.item_id, b.item)
    totals, _ = stock_summary(db, list(all_items), warehouse_id)
    for b in batches:
        item = b.item
        if item.id not in seen:
            seen.add(item.id)
            classify(item, totals.get(item.id, 0.0))
        if b.expiry_date and b.qty > 0:
            if b.expiry_date < today:
                expired.append({"batch_id": b.id, "item": item.name, "batch_no": b.batch_no, "expiry": str(b.expiry_date), "qty": b.qty})
            elif b.expiry_date <= expiry_alert_date(item, today):
                near_exp.append({
                    "batch_id": b.id, "item": item.name, "batch_no": b.batch_no, "expiry": str(b.expiry_date), "qty": b.qty,
                    "days_left": (b.expiry_date - today).days, "alert_months": item.expiry_alert_months or 1,
                })
    # Items that have never been received still count as low stock.
    for item in all_items.values():
        if item.id in seen:
            continue
        classify(item, totals.get(item.id, 0.0))
    return {"low_stock": low, "near_expiry": near_exp, "expired": expired, "negative_stock": negative}


def approve_wastage(db: Session, log: WastageLog, approver_id: int):
    if log.status != "pending":
        raise ValueError("Already processed")
    batch = log.batch
    if batch.qty < log.qty:
        raise ValueError("Insufficient batch qty")
    batch.qty -= log.qty
    db.add(StockMovement(item_id=batch.item_id, batch_id=batch.id, qty_delta=-log.qty, reason="wastage", ref=str(log.id)))
    log.status = "approved"
    log.approved_by = approver_id

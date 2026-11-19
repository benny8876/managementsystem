from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from app.core import clinic_view, clock
from app.core.config import settings
from app.core.deps import ensure_catalog_editable, require
from app.db.session import get_db
from app.models.catalog import CatalogItem
from app.models.inventory import NarcoticLog, PurchaseOrder, PurchaseOrderLine, StockTransfer, Supplier, WastageLog
from app.models.org import Warehouse
from app.models.users import User
from app.schemas.actions import DispenseIn, MedicineIn, MedicineUpdateIn, StockReceiveIn, WastageIn
from app.services.inventory_service import (
    alerts,
    apply_department,
    approve_wastage,
    create_medicine,
    dispense,
    receive_po_line,
    receive_stock,
    reagent_test_links_map,
    set_reagent_test_links,
    stock_department,
    stock_summary,
    expiry_alert_date,
)
from app.services.item_sales_service import item_sales, item_sales_detail
from app.services.utils import next_number

router = APIRouter(prefix="/inventory", tags=["inventory"])


def _stock_rows(
    db: Session, *, warehouse_id: int | None, branch_id: int | None, department: str | None, sub_category: str | None = None,
) -> list[dict]:
    # warehouse_id (a single warehouse) takes priority if both are given; otherwise
    # branch_id scopes to every warehouse that branch owns (Owner Panel's per-branch
    # stock view — catalog items themselves are global, only StockBatch is
    # per-warehouse, so "this branch's stock" means "summed over its warehouses").
    # Neither given = combined across every branch, same as before this param existed.
    scope: int | list[int] | None = warehouse_id
    if scope is None and branch_id is not None:
        scope = [w.id for w in db.query(Warehouse).filter(Warehouse.branch_id == branch_id)]
    q = db.query(CatalogItem).filter(
        # Cashier's unlimited items have no stock but still belong on its list.
        # Deleted (deactivated) items are left out.
        CatalogItem.is_active == True,  # noqa: E712
        or_(CatalogItem.is_stock == True, CatalogItem.department.in_(("cashier", "ot"))),  # noqa: E712
    )
    if department:
        q = apply_department(q, department)
    if sub_category is not None:
        q = q.filter(CatalogItem.sub_category == sub_category.strip())
    rows = q.order_by(CatalogItem.name).all()
    link_map = reagent_test_links_map(db, [r.id for r in rows]) if department == "lab" else {}
    totals, expiries = stock_summary(db, [r.id for r in rows], scope)
    out = []
    for r in rows:
        on_hand = totals.get(r.id, 0.0)
        exp = expiries.get(r.id)
        row = {
            "item": r,
            "on_hand": on_hand,
            "nearest_expiry": str(exp) if exp else None,
            "is_low": r.is_stock and on_hand <= r.min_stock,
            "unlimited": not r.is_stock,
            "is_expired": bool(exp and exp < clock.today()),
            "expiring_soon": bool(exp and clock.today() <= exp <= expiry_alert_date(r, clock.today())),
        }
        if department == "lab":
            row["lab_tests"] = link_map.get(r.id, [])
        out.append(row)
    return out


@router.get("/items")
def items(
    warehouse_id: int | None = None,
    branch_id: int | None = None,
    department: str | None = None,
    sub_category: str | None = None,
    sites_only: bool = False,
    db: Session = Depends(get_db),
    _: User = Depends(require("inventory", "inventory.read", "pharmacy", "pos", "nursing", "reports")),
):
    try:
        site = clinic_view.site_for(branch_id)
        if site:  # Owner Panel: an offline site's stock
            return _clinic_stock_rows(site, department, sub_category)
        if sites_only and branch_id is None and warehouse_id is None:
            # Owner Panel "All Branches" on the cloud hub: the sites' stock, not the Central Warehouse.
            return [r for s in clinic_view.available_sites() for r in _clinic_stock_rows(s, department, sub_category)]
        rows = _stock_rows(db, warehouse_id=warehouse_id, branch_id=branch_id, department=department, sub_category=sub_category)
        if branch_id is None and warehouse_id is None:
            for s in clinic_view.available_sites():
                rows = rows + _clinic_stock_rows(s, department, sub_category)
        return rows
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


def _clinic_stock_rows(site: str, department: str | None, sub_category: str | None) -> list[dict]:
    if not clinic_view.available(site):
        return []
    try:
        with clinic_view.clinic_session(site) as cdb:
            rows = _stock_rows(cdb, warehouse_id=None, branch_id=clinic_view.LOCAL_ID, department=department, sub_category=sub_category)
            # CatalogItem objects must be read while the session is open.
            rows = [{**r, "item": _item_dict(r["item"])} for r in rows]
    except ValueError:
        raise
    except Exception:  # noqa: BLE001 — a bad site upload must not break the stock screen
        return []
    for row in rows:  # keep ids distinct from other branches' in a combined list
        row["item"]["id"] = row["item"]["id"] + clinic_view.offset(site)
        row["offline_clinic"] = True
        row["site"] = site
    return rows


def _item_dict(item) -> dict:
    if isinstance(item, dict):
        return dict(item)
    return {c.name: getattr(item, c.name) for c in CatalogItem.__table__.columns}


def _sales_window(days: int, date_from: str | None, date_to: str | None) -> tuple[datetime, datetime]:
    if date_from and date_to:
        try:
            since = datetime.fromisoformat(date_from)
            until = datetime.fromisoformat(date_to).replace(hour=23, minute=59, second=59)
        except ValueError as e:
            raise HTTPException(400, "date_from/date_to must be YYYY-MM-DD") from e
        return since, until
    if days < 1 or days > 3660:
        raise HTTPException(400, "days must be 1-3660")
    return datetime.combine(clock.today() - timedelta(days=days - 1), datetime.min.time()), clock.now()


@router.get("/item-sales")
def item_sales_list(
    warehouse_id: int,
    department: str = "pharmacy",
    days: int = 30,
    date_from: str | None = None,
    date_to: str | None = None,
    db: Session = Depends(get_db),
    _: User = Depends(require("inventory", "inventory.read", "pharmacy", "pos", "reports")),
):
    since, until = _sales_window(days, date_from, date_to)
    try:
        return item_sales(db, warehouse_id=warehouse_id, department=department, since=since, until=until)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@router.get("/daily-dispensed")
def daily_dispensed_view(
    warehouse_id: int,
    day: date | None = None,
    date_to: date | None = None,
    db: Session = Depends(get_db),
    _: User = Depends(require("inventory", "inventory.read", "pharmacy", "pos", "reports")),
):
    from app.services.item_sales_service import daily_dispensed

    try:
        return daily_dispensed(db, warehouse_id=warehouse_id, day=day or clock.today(), date_to=date_to)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


_SALES_FILTERS = {
    "all": (lambda r: True, lambda r: (-r["amount"], r["name"])),
    "top": (lambda r: r["sold_qty"] > 0, lambda r: -r["sold_qty"]),
    "slow": (lambda r: r["is_slow"], lambda r: -(r["on_hand"] * r["price"])),
    "risk": (lambda r: r["risk_qty"] > 0, lambda r: -r["risk_value"]),
    "low": (lambda r: r["is_low"], lambda r: r["on_hand"]),
}
_SALES_LABELS = {"all": "All items", "top": "Selling", "slow": "Slow (no sale 60 days)", "risk": "Expiry risk", "low": "Low stock"}


def _sales_export_rows(db: Session, warehouse_id: int, department: str, days: int, flt: str):
    since, until = _sales_window(days, None, None)
    try:
        rows = item_sales(db, warehouse_id=warehouse_id, department=department, since=since, until=until)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    keep, order = _SALES_FILTERS.get(flt, _SALES_FILTERS["all"])
    rows = sorted([r for r in rows if keep(r)], key=order)
    w = db.get(Warehouse, warehouse_id)
    period = "Today" if days == 1 else f"Last {days} days"
    return rows, (w.name if w else ""), period


def _sales_status(r: dict) -> str:
    return ", ".join(x for x in (
        "LOW" if r["is_low"] else "", "SLOW" if r["is_slow"] else "",
        f"EXPIRY RISK {r['risk_qty']:g}" if r["risk_qty"] > 0 else "", "EXPIRED" if r["expired"] else "",
    ) if x)


@router.get("/item-sales/export")
def item_sales_export_xlsx(
    warehouse_id: int, department: str = "pharmacy", days: int = 30, filter: str = "all", no_money: bool = False,
    db: Session = Depends(get_db), _: User = Depends(require("inventory", "inventory.read", "pharmacy", "pos", "reports")),
):
    """Store / Pharmacy 'ရောင်းအား' as Excel; no_money=1 (Pharmacy) leaves the money columns out."""
    rows, wn, period = _sales_export_rows(db, warehouse_id, department, days, filter)
    head = ["No.", "Item", "SKU", "Category", "Sold qty", "Unit"] + ([] if no_money else ["Sales (MMK)"]) + ["Stock", "Nearest expiry", "Days of stock", "Last sold", "Status"]
    data = [
        [i + 1, r["name"], r["sku"], r["sub_category"], r["sold_qty"], r["unit"]] + ([] if no_money else [r["amount"]])
        + [r["on_hand"], r["nearest_expiry"] or "", r["days_left"] if r["days_left"] is not None else "", (r["last_sold"] or "")[:10], _sales_status(r)]
        for i, r in enumerate(rows)
    ]
    return _xlsx(f"item-sales-{filter}-{days}d", head, data, [["Item sales", wn, period, _SALES_LABELS.get(filter, filter)]])


@router.get("/item-sales/export/pdf")
def item_sales_export_pdf(
    warehouse_id: int, department: str = "pharmacy", days: int = 30, filter: str = "all", no_money: bool = False,
    db: Session = Depends(get_db), _: User = Depends(require("inventory", "inventory.read", "pharmacy", "pos", "reports")),
):
    from fastapi.responses import StreamingResponse
    from reportlab.lib import colors

    from app.services.pdf_service import build_table_pdf

    rows, wn, period = _sales_export_rows(db, warehouse_id, department, days, filter)
    cols = ["No.", "Item", "Sold"] + ([] if no_money else ["Sales"]) + ["Stock", "Nearest expiry", "Days left", "Last sold", "Status"]
    data = [
        [i + 1, r["name"], f"{r['sold_qty']:g} {r['unit']}".strip()] + ([] if no_money else [f"{r['amount']:,.0f}"])
        + [f"{r['on_hand']:g}", r["nearest_expiry"] or "—", "—" if r["days_left"] is None else (">1 yr" if r["days_left"] > 365 else f"{r['days_left']}"),
           (r["last_sold"] or "—")[:10], _sales_status(r) or "OK"]
        for i, r in enumerate(rows)
    ]
    kpis = [("Items", f"{len(rows):,}"), ("Sold qty", f"{sum(r['sold_qty'] for r in rows):g}"), ("Low stock", f"{sum(1 for r in rows if r['is_low']):,}")]
    if not no_money:
        kpis.insert(2, ("Sales (MMK)", f"{sum(r['amount'] for r in rows):,.0f}"))
    buf = build_table_pdf(
        title="Item Sales", meta=[("Store", wn), ("Period", period), ("Filter", _SALES_LABELS.get(filter, filter))],
        kpis=kpis, columns=cols, rows=data, landscape_mode=True,
        highlight={i: colors.HexColor("#b91c1c") for i, r in enumerate(rows) if r["is_low"] or r["expired"]},
    )
    return StreamingResponse(buf, media_type="application/pdf", headers={"Content-Disposition": f'attachment; filename="item-sales-{filter}-{days}d.pdf"'})


@router.get("/item-sales/{item_id}")
def item_sales_one(
    item_id: int,
    warehouse_id: int,
    days: int = 30,
    date_from: str | None = None,
    date_to: str | None = None,
    db: Session = Depends(get_db),
    _: User = Depends(require("inventory", "inventory.read", "pharmacy", "pos", "reports")),
):
    since, until = _sales_window(days, date_from, date_to)
    try:
        return item_sales_detail(db, item_id=item_id, warehouse_id=warehouse_id, since=since, until=until)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@router.get("/sub-categories")
def sub_categories(
    department: str | None = None,
    db: Session = Depends(get_db),
    _: User = Depends(require("inventory", "inventory.read", "pharmacy", "pos", "nursing", "reports")),
):
    q = db.query(CatalogItem.sub_category).filter(CatalogItem.is_stock == True, CatalogItem.sub_category != "")  # noqa: E712
    if department:
        try:
            q = apply_department(q, department)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
    return [r[0] for r in q.distinct().order_by(CatalogItem.sub_category).all()]


@router.get("/items/export/pdf")
def items_export_pdf(
    warehouse_id: int | None = None,
    branch_id: int | None = None,
    department: str | None = None,
    sub_category: str | None = None,
    count_sheet: bool = False,
    db: Session = Depends(get_db),
    _: User = Depends(require("inventory", "inventory.read", "pharmacy", "pos", "nursing", "reports")),
):
    from fastapi.responses import StreamingResponse

    from app.models.org import Branch
    from app.services.pdf_service import build_table_pdf

    try:
        rows = _stock_rows(db, warehouse_id=warehouse_id, branch_id=branch_id, department=department, sub_category=sub_category)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e

    scope_label = "All Branches"
    if branch_id is not None:
        b = db.get(Branch, branch_id)
        scope_label = b.name if b else f"Branch #{branch_id}"
    elif warehouse_id is not None:
        w = db.get(Warehouse, warehouse_id)
        scope_label = w.name if w else f"Warehouse #{warehouse_id}"

    # Grouped by category so a printed sheet can be checked shelf-by-shelf.
    rows = sorted(rows, key=lambda r: ((r["item"].sub_category or "~").lower(), r["item"].name.lower()))
    table_rows = [
        [
            r["item"].sub_category or "Uncategorized",
            r["item"].sku or "—",
            r["item"].name,
            r["on_hand"],
            "LOW" if r["is_low"] else "OK",
            r["nearest_expiry"] or "—",
        ]
        for r in rows
    ]
    from reportlab.lib import colors

    low = sum(1 for r in rows if r["is_low"])
    out = sum(1 for r in rows if (r["on_hand"] or 0) <= 0)
    if count_sheet:
        # Stock Count sheet (every few months): staff write the counted qty by hand.
        buf = build_table_pdf(
            title="Stock Count Sheet",
            meta=[("Store", scope_label), ("Category", sub_category or "All"), ("Date", clock.today().isoformat()), ("Counted by", "________________")],
            columns=["Category", "SKU", "Medicine / Item", "System qty", "Counted", "Difference"],
            rows=[[r[0], r[1], r[2], f"{float(r[3] or 0):g}", "", ""] for r in table_rows],
        )
        return StreamingResponse(buf, media_type="application/pdf", headers={"Content-Disposition": 'attachment; filename="stock-count-sheet.pdf"'})
    buf = build_table_pdf(
        title="Stock Levels",
        meta=[("Scope", scope_label), ("Category", sub_category or "All")],
        kpis=[("Items", f"{len(rows):,}"), ("Low stock", f"{low:,}"), ("Out of stock (≤ 0)", f"{out:,}"), ("OK", f"{len(rows) - low:,}")],
        columns=["Category", "SKU", "Medicine / Item", "On Hand", "Status", "Nearest Expiry"],
        rows=table_rows,
        highlight={i: colors.HexColor("#b91c1c") for i, r in enumerate(rows) if r["is_low"]},
    )
    return StreamingResponse(
        buf,
        media_type="application/pdf",
        headers={"Content-Disposition": 'attachment; filename="stock-levels.pdf"'},
    )


def _xlsx(name: str, header: list[str], rows: list[list], title_rows: list[list] | None = None):
    import openpyxl
    from io import BytesIO
    from fastapi.responses import StreamingResponse

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = name[:30]
    for t in title_rows or []:
        ws.append(t)
    if title_rows:
        ws.append([])
    ws.append(header)
    for r in rows:
        ws.append(r)
    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{name}.xlsx"'},
    )


@router.get("/items/export")
def items_export_xlsx(
    warehouse_id: int | None = None,
    department: str | None = None,
    count_sheet: bool = False,
    db: Session = Depends(get_db),
    _: User = Depends(require("inventory", "inventory.read", "pharmacy", "pos", "nursing", "reports")),
):
    """Stock list as Excel; count_sheet=1 adds empty Counted / Difference columns."""
    try:
        rows = _stock_rows(db, warehouse_id=warehouse_id, branch_id=None, department=department)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    rows = sorted(rows, key=lambda r: ((r["item"].sub_category or "~").lower(), r["item"].name.lower()))
    w = db.get(Warehouse, warehouse_id) if warehouse_id else None
    head = [["Stock Count Sheet" if count_sheet else "Stock Levels", w.name if w else "All stores", clock.today().isoformat()]]
    if count_sheet:
        return _xlsx("stock-count-sheet", ["Category", "SKU", "Medicine / Item", "System qty", "Counted", "Difference"],
                     [[r["item"].sub_category or "Uncategorized", r["item"].sku or "", r["item"].name, r["on_hand"], None, None] for r in rows], head)
    return _xlsx("stock-levels", ["Category", "SKU", "Medicine / Item", "On Hand", "Status", "Nearest Expiry"],
                 [[r["item"].sub_category or "Uncategorized", r["item"].sku or "", r["item"].name, r["on_hand"], "LOW" if r["is_low"] else "OK", r["nearest_expiry"] or ""] for r in rows], head)


def _daily_rows(db: Session, warehouse_id: int, day: date | None, date_to: date | None = None):
    from app.services.item_sales_service import daily_dispensed

    try:
        d = daily_dispensed(db, warehouse_id=warehouse_id, day=day or clock.today(), date_to=date_to)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    w = db.get(Warehouse, warehouse_id)
    return d, (w.name if w else "")


@router.get("/daily-dispensed/export")
def daily_dispensed_xlsx(warehouse_id: int, day: date | None = None, date_to: date | None = None, db: Session = Depends(get_db),
                         _: User = Depends(require("inventory", "inventory.read", "pharmacy", "pos", "reports"))):
    d, wn = _daily_rows(db, warehouse_id, day, date_to)
    period = d["day"] if d["days"] == 1 else f"{d['day']} – {d['date_to']}"
    return _xlsx(f"pharmacy-given-{d['day']}" + ("" if d["days"] == 1 else f"_{d['date_to']}"), ["No.", "Medicine / Item", "SKU", "Category", "Given qty", "Unit", "Bills", "Stock left", "Low"],
                 [[i + 1, r["name"], r["sku"], r["sub_category"], r["qty"], r["unit"], r["bills"], r["on_hand"], "LOW" if r["is_low"] else ""] for i, r in enumerate(d["items"])],
                 [["Pharmacy — medicine given", wn, period], ["Items", len(d["items"]), "Total qty", d["total_qty"], "Bills", d["bills"], "Walk-in", d["walkin_bills"], "Patients", d["patient_bills"]]])


@router.get("/daily-dispensed/export/pdf")
def daily_dispensed_pdf(warehouse_id: int, day: date | None = None, date_to: date | None = None, db: Session = Depends(get_db),
                        _: User = Depends(require("inventory", "inventory.read", "pharmacy", "pos", "reports"))):
    from fastapi.responses import StreamingResponse
    from reportlab.lib import colors

    from app.services.pdf_service import build_table_pdf

    d, wn = _daily_rows(db, warehouse_id, day, date_to)
    period = d["day"] if d["days"] == 1 else f"{d['day']} – {d['date_to']}"
    items = d["items"]
    buf = build_table_pdf(
        title="Pharmacy — Medicine Given",
        meta=[("Store", wn), ("Date" if d["days"] == 1 else "Period", period)],
        kpis=[("Items", f"{len(items):,}"), ("Total qty", f"{d['total_qty']:g}"), ("Walk-in bills", f"{d['walkin_bills']:,}"), ("Patient bills", f"{d['patient_bills']:,}")],
        columns=["No.", "Medicine / Item", "Category", "Given qty", "Bills", "Stock left", "Status"],
        rows=[[i + 1, r["name"], r["sub_category"] or "—", f"{r['qty']:g} {r['unit']}".strip(), r["bills"], f"{r['on_hand']:g}", "LOW" if r["is_low"] else "OK"] for i, r in enumerate(items)],
        highlight={i: colors.HexColor("#b91c1c") for i, r in enumerate(items) if r["is_low"]},
    )
    return StreamingResponse(buf, media_type="application/pdf", headers={"Content-Disposition": f'attachment; filename="pharmacy-given-{d["day"]}.pdf"'})


@router.get("/lab-tests")
def list_billable_lab_tests(db: Session = Depends(get_db), _: User = Depends(require("inventory", "inventory.read", "lab"))):
    """Billable lab panels/tests (not stock supplies) — for linking reagents on the Store counter."""
    rows = (
        db.query(CatalogItem)
        .filter(CatalogItem.category == "lab", CatalogItem.is_active == True)  # noqa: E712
        .order_by(CatalogItem.name)
        .all()
    )
    return [{"id": r.id, "sku": r.sku, "name": r.name} for r in rows]


@router.post("/medicines")
def add_medicine(data: MedicineIn, db: Session = Depends(get_db), user: User = Depends(require("inventory", "po", "pos"))):
    ensure_catalog_editable()
    try:
        item = create_medicine(
            db, data.name, data.sku, data.barcode, data.price, data.cost, data.min_stock, user.id, data.department,
            data.sub_category, unlimited=data.unlimited, expiry_alert_months=data.expiry_alert_months,
        )
        if data.unit.strip():
            item.unit = data.unit.strip()[:20]
        if item.department == "ot" and data.bill_group in ("ot", "otm"):
            item.bill_group = data.bill_group
        if data.department == "lab" and data.lab_tests:
            set_reagent_test_links(db, item.id, [(l.test_item_id, l.qty) for l in data.lab_tests])
        if data.initial_qty < 0:
            raise ValueError("Opening stock quantity can't be negative")
        if data.initial_qty > 0 and item.is_stock:
            if not data.warehouse_id or not db.get(Warehouse, data.warehouse_id):
                raise ValueError("Select a warehouse for the opening stock")
            receive_stock(
                db, item.id, data.warehouse_id, data.initial_qty, data.cost,
                data.batch_no, data.expiry_date, user.id,
            )
        db.commit()
        db.refresh(item)
        return item
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


def _item_usage(db: Session, item_id: int) -> dict[str, int]:
    """Rows in other tables that point at this catalog item (bills, stock
    batches, orders, ...), found from the schema's foreign keys so a new table
    is covered automatically. Reagent<->test links don't count — they go with it."""
    from sqlalchemy import text

    from app.db.session import Base

    used: dict[str, int] = {}
    for table in Base.metadata.sorted_tables:
        if table.name == "lab_test_reagents":
            continue
        for col in table.columns:
            if any(fk.column.table.name == "catalog_items" for fk in col.foreign_keys):
                n = db.execute(text(f'SELECT COUNT(*) FROM "{table.name}" WHERE "{col.name}" = :i'), {"i": item_id}).scalar() or 0
                if n:
                    used[table.name] = used.get(table.name, 0) + n
    return used


@router.delete("/items/{item_id}")
def delete_item(
    item_id: int,
    permanent: bool = False,
    db: Session = Depends(get_db),
    user: User = Depends(require("inventory", "po", "pos", "management")),
):
    """Delete a stock / service item (🗑). It moves to Store > Deleted Items
    (hidden from every list and search, restorable). Already on a bill, in stock
    history or an order -> kept as "inactive" instead, so old bills and reports
    stay intact. permanent=true removes a never-used item for good."""
    from app.models.catalog import LabTestReagent
    from app.services.utils import audit

    ensure_catalog_editable()
    item = db.get(CatalogItem, item_id)
    if not item:
        raise HTTPException(404, "Item not found")
    used = _item_usage(db, item_id)
    if permanent:
        if used:
            raise HTTPException(400, "Bill / stock မှာ သုံးပြီးသား item မို့ အပြီးဖျက်လို့ မရပါ")
        db.query(LabTestReagent).filter(
            (LabTestReagent.test_item_id == item_id) | (LabTestReagent.reagent_item_id == item_id)
        ).delete(synchronize_session=False)
        audit(db, user.id, "item_delete", "catalog_item", str(item_id), f"permanent: {item.name}"[:100])
        db.delete(item)
        db.commit()
        return {"ok": True, "deleted": True, "permanent": True, "deactivated": False}
    item.is_active = False
    if used:
        item.deleted_at = None
        audit(db, user.id, "item_deactivate", "catalog_item", str(item_id), item.name[:100])
    else:
        item.deleted_at = clock.now()
        audit(db, user.id, "item_delete", "catalog_item", str(item_id), item.name[:100])
    db.commit()
    return {"ok": True, "deleted": not used, "deactivated": bool(used), "used_in": used}


@router.get("/items/removed")
def removed_items(
    department: str | None = None,
    db: Session = Depends(get_db),
    _: User = Depends(require("inventory", "inventory.read", "po", "pos", "management")),
):
    """Store > Deleted Items: deleted (🗑) and inactive items, newest first."""
    q = db.query(CatalogItem).filter(CatalogItem.is_active == False)  # noqa: E712
    if department:
        q = apply_department(q, department)
    rows = q.order_by(CatalogItem.deleted_at.desc(), CatalogItem.name).all()
    return [
        {
            "id": r.id, "sku": r.sku, "name": r.name, "department": r.department, "category": r.category,
            "sub_category": r.sub_category, "price": r.price, "unit": r.unit, "is_stock": r.is_stock,
            "status": "deleted" if r.deleted_at else "inactive",
            "deleted_at": r.deleted_at.isoformat() if r.deleted_at else None,
        }
        for r in rows
    ]


@router.post("/items/{item_id}/restore")
def restore_item(item_id: int, db: Session = Depends(get_db), user: User = Depends(require("inventory", "po", "pos", "management"))):
    from app.services.utils import audit

    ensure_catalog_editable()
    item = db.get(CatalogItem, item_id)
    if not item:
        raise HTTPException(404, "Item not found")
    item.is_active = True
    item.deleted_at = None
    audit(db, user.id, "item_restore", "catalog_item", str(item_id), item.name[:100])
    db.commit()
    return {"ok": True}


@router.patch("/medicines/{item_id}")
def update_medicine(item_id: int, data: MedicineUpdateIn, db: Session = Depends(get_db), user: User = Depends(require("inventory", "po", "pos"))):
    item = db.get(CatalogItem, item_id)
    if not item or not (item.is_stock or item.department in ("cashier", "ot")):
        raise HTTPException(404)
    if settings.site:
        # Site server: name, price, unit, ... come from the cloud; min stock, cost
        # and lab reagent links stay this site's own.
        changes = {
            "name": (data.name or "").strip() not in ("", item.name) if data.name is not None else False,
            "sub_category": data.sub_category is not None and data.sub_category.strip() != (item.sub_category or ""),
            "price": data.price is not None and float(data.price) != float(item.price or 0),
            "unit": data.unit is not None and data.unit.strip() not in ("", item.unit),
            "expiry_alert_months": data.expiry_alert_months is not None and max(int(data.expiry_alert_months), 1) != item.expiry_alert_months,
            "bill_group": data.bill_group is not None and data.bill_group != (item.bill_group or ""),
            "unlimited": data.unlimited is not None and bool(data.unlimited) == bool(item.is_stock),
        }
        if any(changes.values()):
            ensure_catalog_editable()
    if data.unit is not None and data.unit.strip():
        item.unit = data.unit.strip()[:20]
    if data.expiry_alert_months is not None:
        item.expiry_alert_months = max(int(data.expiry_alert_months), 1)
    if data.name is not None:
        item.name = data.name.strip()
        item.name_mm = data.name.strip()
    if data.sub_category is not None:
        item.sub_category = data.sub_category.strip()
    if data.price is not None:
        item.price = data.price
    if data.cost is not None:
        item.cost = data.cost
    if data.min_stock is not None:
        item.min_stock = data.min_stock
    if data.unlimited is not None and bool(data.unlimited) == bool(item.is_stock):
        if item.department not in ("cashier", "ot"):
            raise HTTPException(400, "Unlimited is only for Cashier / OT items")
        from app.models.inventory import StockBatch, StockMovement
        from app.services.utils import audit

        if data.unlimited:
            # No stock any more: clear what the batches still show (incl. sold-below-zero).
            cleared = 0.0
            for b in db.query(StockBatch).filter(StockBatch.item_id == item.id, StockBatch.qty != 0).all():
                db.add(StockMovement(item_id=item.id, batch_id=b.id, qty_delta=-b.qty, reason="set_unlimited", ref=b.batch_no))
                cleared += b.qty
                b.qty = 0
            item.is_stock = False
            audit(db, user.id, "item_unlimited", "catalog_item", str(item.id), f"{item.name} → Unlimited (stock {cleared:g} cleared)")
        else:
            item.is_stock = True
            audit(db, user.id, "item_unlimited", "catalog_item", str(item.id), f"{item.name} → Stock")
    if data.bill_group is not None:
        if item.department != "ot" or data.bill_group not in ("ot", "otm", ""):
            raise HTTPException(400, "bill_group only applies to OT items (ot / otm)")
        item.bill_group = data.bill_group
    if data.lab_tests is not None:
        if item.department != "lab":
            raise HTTPException(400, "lab_tests only applies to lab department items")
        try:
            set_reagent_test_links(db, item.id, [(l.test_item_id, l.qty) for l in data.lab_tests])
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
    db.commit()
    db.refresh(item)
    return item


@router.post("/receive")
def receive_stock_batch(data: StockReceiveIn, db: Session = Depends(get_db), user: User = Depends(require("inventory", "po", "pos"))):
    try:
        batch = receive_stock(
            db,
            data.item_id,
            data.warehouse_id,
            data.qty,
            data.unit_cost,
            data.batch_no,
            data.expiry_date,
            user.id,
            None if settings.site else data.selling_price,
            data.min_stock,
            notes=data.notes,
        )
        if data.unit.strip() and not settings.site:
            item = db.get(CatalogItem, data.item_id)
            item.unit = data.unit.strip()[:20]
        db.commit()
        return {"ok": True, "batch_id": batch.id}
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@router.get("/alerts")
def inventory_alerts(
    warehouse_id: int | None = None,
    department: str | None = None,
    db: Session = Depends(get_db),
    _: User = Depends(require("inventory", "pharmacy", "pos")),
):
    try:
        if department:
            stock_department(department)
        return alerts(db, warehouse_id, department)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@router.get("/suppliers")
def suppliers(db: Session = Depends(get_db), _: User = Depends(require("inventory", "po"))):
    return [{"id": s.id, "name": s.name, "phone": s.phone, "address": s.address} for s in db.query(Supplier).order_by(Supplier.name).all()]


class SupplierIn(BaseModel):
    name: str = ""
    phone: str = ""


@router.post("/suppliers")
def create_supplier(data: SupplierIn | None = None, name: str = "", phone: str = "", db: Session = Depends(get_db), _: User = Depends(require("po", "inventory"))):
    # The Store screen sends JSON; older callers sent query parameters.
    name = ((data.name if data else "") or name).strip()
    phone = ((data.phone if data else "") or phone).strip()
    if not name:
        raise HTTPException(400, "Supplier name required")
    s = Supplier(name=name, phone=phone)
    db.add(s)
    db.commit()
    return {"id": s.id, "name": s.name, "phone": s.phone}


@router.delete("/suppliers/{supplier_id}")
def delete_supplier(supplier_id: int, db: Session = Depends(get_db), user: User = Depends(require("po", "inventory"))):
    from app.services.utils import audit

    s = db.get(Supplier, supplier_id)
    if not s:
        raise HTTPException(404)
    if db.query(PurchaseOrder.id).filter(PurchaseOrder.supplier_id == supplier_id).first():
        raise HTTPException(400, "ဒီ Supplier နဲ့ Purchase order ရှိနေလို့ ဖျက်လို့ မရပါ")
    audit(db, user.id, "supplier_delete", "supplier", str(s.id), s.name)
    db.delete(s)
    db.commit()
    return {"ok": True}


@router.post("/purchase-orders")
def create_po(supplier_id: int, warehouse_id: int, db: Session = Depends(get_db), _: User = Depends(require("po"))):
    po = PurchaseOrder(number=next_number(db, "po", "PO-"), supplier_id=supplier_id, warehouse_id=warehouse_id, status="draft")
    db.add(po)
    db.commit()
    return po


@router.post("/purchase-orders/{po_id}/lines")
def add_po_line(po_id: int, item_id: int, qty: float, unit_cost: float, batch_no: str = "", expiry_date: date | None = None, db: Session = Depends(get_db), _: User = Depends(require("po"))):
    po = db.get(PurchaseOrder, po_id)
    if not po:
        raise HTTPException(404)
    line = PurchaseOrderLine(po_id=po_id, item_id=item_id, qty=qty, unit_cost=unit_cost, batch_no=batch_no, expiry_date=expiry_date)
    db.add(line)
    po.total += qty * unit_cost
    db.commit()
    return line


@router.post("/purchase-orders/{po_id}/receive")
def receive_po(po_id: int, db: Session = Depends(get_db), user: User = Depends(require("po"))):
    po = db.get(PurchaseOrder, po_id)
    if not po:
        raise HTTPException(404)
    for line in po.lines:
        receive_po_line(db, line.item_id, po.warehouse_id, line.batch_no, line.expiry_date, line.qty, line.unit_cost)
    po.status = "received"
    db.commit()
    return po


@router.get("/purchase-orders")
def list_po(db: Session = Depends(get_db), _: User = Depends(require("po", "inventory"))):
    return db.query(PurchaseOrder).order_by(PurchaseOrder.id.desc()).limit(50).all()


@router.get("/batches")
def list_batches(
    department: str | None = None,
    warehouse_id: int | None = None,
    limit: int = 100,
    db: Session = Depends(get_db),
    _: User = Depends(require("inventory", "pharmacy")),
):
    """Batches with stock. Stock In's list shows the newest 100; Stock Out's
    picker asks for limit=0 — every batch, so any item can be taken out."""
    from app.models.inventory import StockBatch

    q = db.query(StockBatch).join(CatalogItem).filter(
        StockBatch.qty > 0, CatalogItem.is_stock == True, CatalogItem.deleted_at.is_(None)  # noqa: E712
    )
    if warehouse_id:
        q = q.filter(StockBatch.warehouse_id == warehouse_id)
    if department:
        try:
            q = apply_department(q, department)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
    q = q.order_by(StockBatch.id.desc())
    rows = (q.limit(limit) if limit > 0 else q).all()
    from app.models.inventory import StockMovement

    ids = [b.id for b in rows]
    received = {m.batch_id: m.qty_delta for m in db.query(StockMovement).filter(StockMovement.batch_id.in_(ids), StockMovement.reason == "purchase")} if ids else {}
    out = []
    for b in rows:
        out.append({
            "id": b.id,
            "item_id": b.item_id,
            "item_name": b.item.name if b.item else "",
            "warehouse_id": b.warehouse_id,
            "batch_no": b.batch_no,
            "expiry_date": str(b.expiry_date) if b.expiry_date else "",
            "qty": b.qty,
            "unit_cost": b.unit_cost,
            "notes": b.notes or "",
            "received_qty": received.get(b.id, 0),
            # Stock In can be undone while none of it has been used yet.
            "can_undo": b.id in received and b.qty >= received[b.id] - 1e-9,
        })
    return out


class UndoIn(BaseModel):
    reason: str = ""


@router.post("/batches/{batch_id}/undo-receive")
def undo_receive(batch_id: int, data: UndoIn, db: Session = Depends(get_db), user: User = Depends(require("inventory", "po"))):
    """Take back a wrong Stock In — only while none of that batch has been used."""
    from app.models.inventory import StockBatch, StockMovement
    from app.services.utils import audit

    reason = (data.reason or "").strip()
    if len(reason) < 2:
        raise HTTPException(400, "ပယ်ရတဲ့ အကြောင်းပြချက် ထည့်ပါ")
    b = db.get(StockBatch, batch_id)
    mv = db.query(StockMovement).filter(StockMovement.batch_id == batch_id, StockMovement.reason == "purchase").first() if b else None
    if not b or not mv:
        raise HTTPException(404, "Stock In record not found")
    if b.qty < mv.qty_delta - 1e-9:
        raise HTTPException(400, f"ဒီ batch ထဲက {mv.qty_delta - b.qty:g} ခု သုံးပြီး/ရောင်းပြီးသွားလို့ ပယ်လို့ မရပါ — Stock Count နဲ့ ညှိပါ")
    b.qty -= mv.qty_delta
    mv.reason = "purchase_undone"
    db.add(StockMovement(item_id=b.item_id, batch_id=b.id, qty_delta=-mv.qty_delta, reason="undo_purchase", ref=b.batch_no or ""))
    audit(db, user.id, "stock_receive_undo", "catalog_item", str(b.item_id), f"batch={b.batch_no} qty={mv.qty_delta:g} — {reason}"[:200])
    db.commit()
    return {"ok": True}


@router.get("/wastage")
def list_wastage(status: str = "pending", db: Session = Depends(get_db), _: User = Depends(require("wastage", "inventory"))):
    q = db.query(WastageLog)
    if status != "all":
        q = q.filter(WastageLog.status == status)
    out = []
    for w in q.order_by(WastageLog.id.desc()).limit(80).all():
        b = w.batch
        out.append({
            "id": w.id, "batch_id": w.batch_id, "qty": w.qty, "reason": w.reason, "status": w.status, "notes": w.notes or "",
            "item_name": b.item.name if b and b.item else "", "batch_no": b.batch_no if b else "",
            "created_at": w.created_at.isoformat() if getattr(w, "created_at", None) else None,
        })
    return out


@router.post("/dispense")
def dispense_item(data: DispenseIn, db: Session = Depends(get_db), user: User = Depends(require("dispense", "pharmacy"))):
    item = db.get(CatalogItem, data.item_id)
    if not item:
        raise HTTPException(404)
    picks = dispense(db, item, data.warehouse_id, data.qty, data.ref, user.id)
    if item.is_controlled:
        for batch, take in picks:
            db.add(NarcoticLog(item_id=item.id, batch_id=batch.id, patient_id=data.patient_id, qty=take, direction="out"))
    db.commit()
    return {"ok": True}


@router.post("/wastage")
def request_wastage(data: WastageIn, db: Session = Depends(get_db), user: User = Depends(require("wastage"))):
    if not data.qty or data.qty <= 0:
        raise HTTPException(400, "Qty ထည့်ပါ")
    log = WastageLog(batch_id=data.batch_id, qty=data.qty, reason=data.reason, requested_by=user.id, notes=(data.notes or "").strip())
    db.add(log)
    db.flush()
    try:
        # Stock Out takes the stock off straight away — the Store has no separate approval screen.
        approve_wastage(db, log, user.id)
    except ValueError as e:
        db.rollback()
        raise HTTPException(400, "Batch ထဲမှာ အဲ့လောက် မရှိပါ" if "Insufficient" in str(e) else str(e)) from e
    db.commit()
    return {"ok": True, "id": log.id}


@router.post("/wastage/{log_id}/undo")
def undo_wastage(log_id: int, data: UndoIn, db: Session = Depends(get_db), user: User = Depends(require("wastage", "inventory"))):
    """Take back a wrong Stock Out: the quantity goes back into its batch."""
    from app.models.inventory import StockMovement
    from app.services.utils import audit

    reason = (data.reason or "").strip()
    if len(reason) < 2:
        raise HTTPException(400, "ပယ်ရတဲ့ အကြောင်းပြချက် ထည့်ပါ")
    log = db.get(WastageLog, log_id)
    if not log or log.status in ("reversed", "cancelled", "rejected"):
        raise HTTPException(404, "Stock Out record not found")
    if log.status == "approved":
        log.batch.qty += log.qty
        db.add(StockMovement(item_id=log.batch.item_id, batch_id=log.batch_id, qty_delta=log.qty, reason="undo_wastage", ref=str(log.id)))
        log.status = "reversed"
    else:
        log.status = "cancelled"
    audit(db, user.id, "stock_out_undo", "wastage", str(log.id), f"qty={log.qty:g} — {reason}"[:200])
    db.commit()
    return {"ok": True}


@router.post("/wastage/{log_id}/approve")
def approve(log_id: int, db: Session = Depends(get_db), user: User = Depends(require("wastage", "inventory"))):
    log = db.get(WastageLog, log_id)
    if not log:
        raise HTTPException(404)
    approve_wastage(db, log, user.id)
    db.commit()
    return log


@router.post("/transfer")
def transfer(from_warehouse_id: int, to_warehouse_id: int, batch_id: int, qty: float, db: Session = Depends(get_db), _: User = Depends(require("transfers"))):
    from app.models.inventory import StockBatch

    batch = db.get(StockBatch, batch_id)
    if not batch or batch.qty < qty:
        raise HTTPException(400, "Insufficient qty")
    batch.qty -= qty
    dest = StockBatch(item_id=batch.item_id, warehouse_id=to_warehouse_id, batch_no=batch.batch_no, expiry_date=batch.expiry_date, qty=qty, unit_cost=batch.unit_cost)
    db.add(dest)
    db.flush()
    db.add(StockTransfer(from_warehouse_id=from_warehouse_id, to_warehouse_id=to_warehouse_id, item_id=batch.item_id, batch_id=batch.id, qty=qty))
    db.commit()
    return {"ok": True}


@router.get("/narcotics")
def narcotics(db: Session = Depends(get_db), _: User = Depends(require("narcotics", "pharmacy"))):
    return db.query(NarcoticLog).order_by(NarcoticLog.id.desc()).limit(100).all()


@router.get("/warehouses")
def warehouses(db: Session = Depends(get_db), _: User = Depends(require("inventory.read", "pharmacy", "pos"))):
    return db.query(Warehouse).all()


@router.post("/warehouses")
def create_warehouse(name: str, branch_id: int, db: Session = Depends(get_db), _: User = Depends(require("inventory", "po", "pos"))):
    wh = Warehouse(name=name, branch_id=branch_id)
    db.add(wh)
    db.commit()
    db.refresh(wh)
    return wh

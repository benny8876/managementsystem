"""Site servers: bring the local item list in line with the cloud's item master
(GET /sync/catalog — see api/v1/sync.py and windows/app/site_sync.py).

Items are matched by SKU. New items are added; name, price, unit, category,
active/deleted state, ... follow the cloud. Each site keeps its own stock,
batches, cost and min stock (min stock only starts from the cloud's value).
Items that exist only on this site are left alone.
"""
import json
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.models.catalog import CatalogItem

FOLLOW = (
    "barcode", "name", "name_mm", "category", "department", "sub_category", "unit",
    "expiry_alert_months", "price", "doctor_fee_pct", "is_stock", "is_controlled", "is_package", "is_active",
    "bill_group",
)
VERSION_KEY = "catalog_version"


def current_version(db: Session) -> str:
    return db.execute(text("SELECT value FROM app_meta WHERE key = :k"), {"k": VERSION_KEY}).scalar() or ""


def apply_catalog(db: Session, items: list[dict], version: str = "") -> dict:
    local = {i.sku: i for i in db.query(CatalogItem).all()}
    added = updated = 0
    for row in items:
        sku = (row.get("sku") or "").strip()
        if not sku:
            continue
        item = local.get(sku)
        deleted_at = datetime.fromisoformat(row["deleted_at"]) if row.get("deleted_at") else None
        if item is None:
            item = CatalogItem(sku=sku, cost=0, min_stock=float(row.get("min_stock") or 0), package_items="")
            for f in FOLLOW:
                setattr(item, f, row.get(f))
            item.deleted_at = deleted_at
            db.add(item)
            local[sku] = item
            added += 1
            continue
        changed = False
        for f in FOLLOW:
            if row.get(f) is not None and getattr(item, f) != row.get(f):
                setattr(item, f, row.get(f))
                changed = True
        if item.deleted_at != deleted_at:
            item.deleted_at = deleted_at
            changed = True
        updated += changed
    # Items deleted for good on the cloud are no longer in its list. Sites can't
    # add items of their own (the cloud owns the item master), so anything here
    # the cloud no longer has was deleted there: hide it the same way (🗑 →
    # Deleted Items; stock / bills that used it stay). Skipped when the list
    # looks partial, so a bad download can never hide the whole catalog.
    cloud_skus = {(r.get("sku") or "").strip() for r in items}
    gone = [i for s, i in local.items() if s and s not in cloud_skus and i.deleted_at is None]
    removed = moved = kept = 0
    if len(cloud_skus) >= 50 and len(gone) <= max(30, len(local) // 10):
        from sqlalchemy import func

        from app.models.inventory import StockBatch

        now = datetime.now()
        by_name: dict[str, CatalogItem] = {}
        for s, i in local.items():
            if s in cloud_skus and i.deleted_at is None and i.is_active:
                by_name.setdefault((i.name or "").strip().lower(), i)
        for i in gone:
            stock = float(db.query(func.coalesce(func.sum(StockBatch.qty), 0)).filter(StockBatch.item_id == i.id).scalar() or 0)
            if abs(stock) > 0.0001:
                # Stock still here: move it to the cloud's item of the same name
                # (a duplicate the cloud cleaned up); none -> keep it visible.
                twin = by_name.get((i.name or "").strip().lower())
                if twin is None:
                    kept += 1
                    continue
                db.query(StockBatch).filter(StockBatch.item_id == i.id).update({"item_id": twin.id}, synchronize_session=False)
                moved += 1
            i.is_active = False
            i.deleted_at = now
            removed += 1
    db.flush()
    # Packages list their contents by id; the cloud sends SKUs (ids differ per server).
    for row in items:
        if not row.get("is_package"):
            continue
        item = local.get(row.get("sku"))
        ids = [local[s].id for s in row.get("package_skus") or [] if s in local]
        value = json.dumps(ids) if ids else ""
        if item is not None and item.package_items != value:
            item.package_items = value
    if version:
        db.execute(text("DELETE FROM app_meta WHERE key = :k"), {"k": VERSION_KEY})
        db.execute(text("INSERT INTO app_meta (key, value) VALUES (:k, :v)"), {"k": VERSION_KEY, "v": version})
    db.commit()
    return {"added": added, "updated": updated, "removed": removed, "stock_moved": moved, "kept_with_stock": kept, "total": len(items)}

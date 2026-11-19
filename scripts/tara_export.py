#!/usr/bin/env python3
"""Export the hospital's item list from the old Tara Shop System database into
the JSON that scripts/import_catalog.py loads (cloud and clinic alike).

Keeps the items the way Tara used them: same item code (-> SKU), name, price,
unit and barcode, with the Tara top category kept as the sub-category so the
same names show in the Store / catalog filters. Each item goes to the counter
that matches how Tara used it:

  stock items (medicines, OT instruments, supplies)  -> Pharmacy stock
  lab tests (Laboratory Test, Dr.Phy/OG/Child/...)   -> Lab tests (price only)
  lab "(stock)" reagents / slides / stains           -> Lab stock
  X-ray / USG / Echo                                 -> X-Ray / USG
  charges (Clinic Charges, InPatient D/C, Doctor Charges, OPD Fee, ...)
                                                     -> Cashier services (no stock)

Also writes the doctor list (Tara kept doctors only as "Doctor Charges" items)
so Reception can pick a doctor and the Doctor Income report works.
No patient data is read or written.

Usage:
    python3 scripts/tara_export.py <Tara_Server.db> <out.json>
"""
import json
import re
import sqlite3
import sys
from collections import Counter

LAB_TOPS = {"Laboratory Test", "Dr.Phy", "Dr.OG", "Dr.Child", "Dr.Sur", "Dr.Ortho", "ENT"}
CHARGE_TOPS = {"Clinic Charges", "InPatient D/C", "OPD Fee", "Other Payment", "Other Income", "Return TopCategory", "Doctor Charges", "Deposit"}
USG_RE = re.compile(r"usg|ultrasound|echo|anomaly|doppler|\b4d\b", re.I)
XRAY_RE = re.compile(r"x[\s-]?ray", re.I)
LAB_STOCK_RE = re.compile(r"\(stock\)|glass slide|stain", re.I)

# Doctors as Tara named them (from the "Doctor Charges" items), with their fee.
DOCTORS = [
    ("Dr.Physician", "Physician", 12000),
    ("Dr.Sur (M)", "Surgeon", 12000),
    ("Dr. Surgeon", "Surgeon", 10000),
    ("Dr.OG", "OG", 12000),
    ("Dr.Child", "Child", 12000),
    ("Dr.Eye", "Eye", 13000),
    ("Dr.Ortho", "Ortho", 12000),
    ("Dr.ENT", "ENT", 15000),
    ("Dr.Oncologist", "Oncology", 35000),
    ("Dr.Psy (dr ye)", "Psychiatry", 12000),
    ("neuro surg", "Neuro Surgeon", 15000),
    ("Dr.Ana", "Anaesthetist", 80000),
    ("Dr.Mo", "MO", 10000),
    ("Dr.Mo(T)", "MO", 10000),
    ("Dr.Mo(YeWint)", "MO", 10000),
    ("Dr.Mo(THA)", "MO", 10000),
    ("Dr.Mo(YNO)", "MO", 10000),
    ("Dr.Mo(ZLH)", "MO", 10000),
]


def num(value, default=0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def place(top: str, item_type: int, name: str) -> tuple[str, str, bool]:
    """(department, category, is_stock) for a Tara item."""
    if top in LAB_TOPS:
        if item_type == 1 and LAB_STOCK_RE.search(name):
            return "lab", "supply", True
        return "lab", "lab", False
    if top in ("Medical Equipment", "usg"):
        if USG_RE.search(name):
            return "usg", "radiology", False
        if XRAY_RE.search(name):
            return "xray", "radiology", False
    if top in CHARGE_TOPS or item_type != 1:
        return "cashier", "cashier", False
    return "pharmacy", "drug", True


def main(src: str, out: str) -> None:
    db = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    tops = dict(db.execute("SELECT top_id, top_category_name FROM topCategoryTable"))
    # Expiry warning lives on the category (days); the item's category is (cid, tid).
    warn = {(c, t): d for c, t, d in db.execute("SELECT cat_id, tid, warning_for_expiry_duration FROM categoryTable")}
    items = []
    for code, name, price, unit, barcode, item_type, cid, tid, min_stock, last_cost in db.execute(
        "SELECT item_code, item_name, item_price, unit, barcode, item_type, cid, tid, minimum_stock_limit, last_bought_price FROM itemTable ORDER BY db_id"
    ):
        name = (name or "").strip()
        code = (code or "").strip()
        if not name or not code:
            continue
        top = tops.get(tid, "Other")
        dept, category, is_stock = place(top, int(item_type or 0), name)
        days = warn.get((cid, tid)) or 30
        items.append({
            "sku": code,
            "barcode": (barcode or "").strip() or code,
            "name": name[:160],
            "category": category,
            "department": dept,
            "sub_category": top[:60],
            "unit": ((unit or "").strip() or "ea")[:20],
            "price": num(price),
            "cost": num(last_cost),
            "min_stock": num(min_stock, 10.0) if is_stock else 0.0,
            "expiry_alert_months": max(1, round(num(days, 30) / 30)) if is_stock else 1,
            "is_stock": is_stock,
        })
    doctors = [{"full_name": n, "specialty": s, "consultation_fee": float(f)} for n, s, f in DOCTORS]
    with open(out, "w", encoding="utf-8") as fh:
        json.dump({"source": "Tara Shop System", "items": items, "doctors": doctors}, fh, ensure_ascii=False, indent=1)
    counts = Counter((i["department"], i["is_stock"]) for i in items)
    print(f"{len(items)} items, {len(doctors)} doctors ->", out)
    for (dept, stock), n in sorted(counts.items()):
        print(f"  {dept:9} {'stock' if stock else 'service'}: {n}")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    main(*sys.argv[1:])

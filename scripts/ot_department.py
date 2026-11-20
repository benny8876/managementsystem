"""Move every OT item into the Store's "OT" section (department = "ot").

An OT item is one whose name has the word "OT" in capitals — "Gauze(OT)",
"Spinal needle 25g (OT)", "OT Room Fee", "Instrument Fee OT", "service OT".
Names that only contain the letters (Cotton, Potassium, "(EyeOT)") are left alone.

Run it on the cloud (the item master): site servers follow the cloud's
department with their next catalog pull.

    python scripts/ot_department.py /path/to/hospital.db          # list only
    python scripts/ot_department.py /path/to/hospital.db --apply  # change
"""
import re
import sqlite3
import sys

OT_WORD = re.compile(r"\bOT\b")
FROM_DEPARTMENTS = ("pharmacy", "drug", "cashier")


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    path, apply = sys.argv[1], "--apply" in sys.argv
    con = sqlite3.connect(path)
    rows = [
        r for r in con.execute("SELECT id, sku, name, department, is_stock, is_active FROM catalog_items")
        if r[3] in FROM_DEPARTMENTS and OT_WORD.search(r[2] or "")
    ]
    by_dept: dict[str, int] = {}
    for r in rows:
        by_dept[r[3]] = by_dept.get(r[3], 0) + 1
    print(f"{len(rows)} OT items found: {by_dept}")
    for r in rows[:400]:
        print(f"  {r[1]:<12} {r[3]:<9} {'stock' if r[4] else 'charge':<6} {'' if r[5] else '(inactive) '}{r[2]}")
    if not apply:
        print("\nList only — add --apply to move them to the OT section.")
        return
    con.executemany("UPDATE catalog_items SET department = 'ot' WHERE id = ?", [(r[0],) for r in rows])
    con.commit()
    print(f"\nMoved {len(rows)} items to department 'ot'.")


if __name__ == "__main__":
    main()

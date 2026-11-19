"""Go-live: clear the test history but keep the set-up.

Up to a chosen date (inclusive), or everything. Deletes: patients, visits, tokens, bills, payments, lab / X-ray orders,
prescriptions, admissions, expenses, shifts, accounting entries, logs and the
bill-number sequences (only when nothing is left, so numbers start again at
000001; a partial clear keeps counting on).

Keeps: user accounts and passwords, doctors, branches, warehouses, settings,
the item list and prices, lab templates, wards and beds (emptied), suppliers.

Stock: what was taken out by test bills / ward orders (and put back by
returns or bill edits) is reversed, so each batch goes back to what was
stocked in (stock in, stock out, counts and wastage stay as they are).

Used by the cloud (Owner Panel -> its own branch) and by a site server when the
cloud asks it to (see api/v1/sync.py "commands" and windows/app/site_sync.py).
"""
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

from sqlalchemy import inspect, text
from sqlalchemy.orm import Session

# (table, date column) — rows dated before the cut-off go. Child rows without a
# date of their own go with their parent (see reset_history).
DATED = (
    ("invoices", "created_at"), ("lab_orders", "created_at"), ("radiology_orders", "created_at"),
    ("service_orders", "created_at"), ("prescriptions", "created_at"), ("admissions", "admitted_at"),
    ("ward_med_orders", "created_at"), ("nursing_notes", "created_at"), ("vital_signs", "recorded_at"),
    ("bed_transfer_logs", "transferred_at"), ("ot_schedules", "scheduled_at"), ("queue_tokens", "created_at"),
    ("appointments", "created_at"), ("visits", "created_at"), ("expenses", "COALESCE(expense_date, created_at)"),
    ("cashier_shifts", "opened_at"), ("journal_entries", "created_at"), ("commissions", "created_at"),
    ("narcotic_logs", "created_at"), ("audit_logs", "created_at"), ("sms_logs", "created_at"),
    ("otp_codes", "expires_at"), ("sync_snapshots", "received_at"), ("attendance", "work_date"),
    ("cash_closes", "period_to"), ("petty_topups", "created_at"),
)
# (child table, its parent link) — deleted when the parent row is gone.
CHILDREN = (
    ("invoice_lines", "invoice_id", "invoices"), ("payments", "invoice_id", "invoices"),
    ("commissions", "invoice_id", "invoices"), ("lab_orders", "invoice_id", "invoices"),
    ("radiology_orders", "invoice_id", "invoices"), ("service_orders", "invoice_id", "invoices"),
    ("ot_schedules", "invoice_id", "invoices"), ("ward_med_orders", "invoice_id", "invoices"),
    ("ward_med_orders", "admission_id", "admissions"), ("nursing_notes", "admission_id", "admissions"),
    ("vital_signs", "admission_id", "admissions"), ("bed_transfer_logs", "admission_id", "admissions"),
    ("prescription_items", "prescription_id", "prescriptions"), ("ward_med_order_items", "order_id", "ward_med_orders"),
    ("journal_lines", "entry_id", "journal_entries"),
)
# Tables that point at a patient; a patient still used by a kept row stays.
PATIENT_USERS = ("invoices", "visits", "admissions", "queue_tokens", "appointments", "lab_orders",
                 "radiology_orders", "service_orders", "prescriptions", "ward_med_orders", "ot_schedules", "narcotic_logs")
# Stock movements made by bills / ward orders; everything else is a stock action
# staff did on purpose (stock in / out, counts, wastage) and stays.
BILL_STOCK_REASONS = ("dispense", "return", "bill_edit")
MARKER = "history_reset_at"


def reset_history(db: Session, until: date | None = None) -> dict:
    """Clear the test history dated up to and including `until` (everything when None)."""
    tables = set(inspect(db.get_bind()).get_table_names())
    cut = (until + timedelta(days=1)).isoformat() if until else None
    before = (lambda col: f"({col}) < :cut") if cut else (lambda col: "1=1")
    p = {"cut": cut} if cut else {}
    x = lambda sql: db.execute(text(sql), p)  # noqa: E731

    # Stock taken out / put back by the bills that go (and, by date, ward orders) is reversed.
    marks = ",".join(f"'{r}'" for r in BILL_STOCK_REASONS)
    x("CREATE TEMP TABLE IF NOT EXISTS _gone_inv (id INTEGER PRIMARY KEY)")
    x("DELETE FROM _gone_inv")
    x(f"INSERT INTO _gone_inv SELECT id FROM invoices WHERE {before('created_at')}")
    moves = (f"reason IN ({marks}) AND batch_id IS NOT NULL AND ("
             f"ref IN (SELECT 'inv-' || id FROM _gone_inv) OR (ref NOT LIKE 'inv-%' AND {before('created_at')}))")
    put_back = x(f"SELECT COALESCE(SUM(-qty_delta), 0) FROM stock_movements WHERE {moves}").scalar() or 0
    # (in the sub-query the bare column names are the movement's own)
    x(f"UPDATE stock_batches SET qty = qty - (SELECT SUM(qty_delta) FROM stock_movements "
      f"WHERE batch_id = stock_batches.id AND {moves}) "
      f"WHERE id IN (SELECT batch_id FROM stock_movements WHERE {moves})")
    x(f"DELETE FROM stock_movements WHERE {moves}")

    deleted: dict[str, int] = {}

    def gone(table: str, n: int) -> None:
        if n:
            deleted[table] = deleted.get(table, 0) + n

    for table, col in DATED:
        if table in tables:
            gone(table, x(f'DELETE FROM "{table}" WHERE {before(col)}').rowcount)
    for _ in range(2):  # twice: an order removed with its bill can take its items with it
        for child, link, parent in CHILDREN:
            if child in tables and parent in tables:
                gone(child, x(f'DELETE FROM "{child}" WHERE "{link}" IS NOT NULL AND "{link}" NOT IN (SELECT id FROM "{parent}")').rowcount)
    if "patients" in tables:
        keep = " UNION ".join(f'SELECT patient_id FROM "{t}" WHERE patient_id IS NOT NULL' for t in PATIENT_USERS if t in tables)
        gone("patients", x(f"DELETE FROM patients WHERE {before('created_at')} AND id NOT IN ({keep})").rowcount)
    x("DROP TABLE _gone_inv")

    everything = all(
        t not in tables or not db.execute(text(f'SELECT 1 FROM "{t}" LIMIT 1')).first()
        for t in ("invoices", "patients", "queue_tokens", "expenses")
    )
    if everything:
        # Nothing left to clash with: bill / patient numbers start again at 000001.
        if "sequences" in tables:
            gone("sequences", db.execute(text("DELETE FROM sequences")).rowcount)
        if "petty_cash" in tables:
            db.execute(text("UPDATE petty_cash SET balance = 0"))
    if "beds" in tables and "admissions" in tables:
        db.execute(text("UPDATE beds SET status = 'available' WHERE status != 'available' AND id NOT IN "
                        "(SELECT bed_id FROM admissions WHERE bed_id IS NOT NULL AND discharged_at IS NULL)"))
    db.execute(text("DELETE FROM app_meta WHERE key = :k"), {"k": MARKER})
    db.execute(text("INSERT INTO app_meta (key, value) VALUES (:k, :v)"),
               {"k": MARKER, "v": f"{datetime.now().isoformat(timespec='seconds')} until {until or 'all'}"})
    db.commit()
    return {"until": until.isoformat() if until else None, "deleted": deleted, "stock_put_back": float(put_back), "numbers_restart": everything}


def backup(db_path: Path, folder: Path, tag: str) -> Path:
    """A consistent copy of the live database before anything is deleted."""
    folder.mkdir(parents=True, exist_ok=True)
    stamp = f"before-history-reset_{tag}_{datetime.now():%Y%m%d_%H%M%S}"
    out = folder / f"{stamp}.db"
    n = 1
    while out.exists():  # two clears in the same second must not share a backup
        n += 1
        out = folder / f"{stamp}_{n}.db"
    src = sqlite3.connect(db_path)
    dst = sqlite3.connect(out)
    try:
        with dst:
            src.backup(dst)
    finally:
        dst.close()
        src.close()
    return out


def sqlite_path(database_url: str) -> Path:
    return Path(database_url.split("sqlite:///", 1)[1]).resolve()

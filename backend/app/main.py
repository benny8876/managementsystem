# Copyright (c) 2026 Luxx Digital Solution. All Rights Reserved.
# Unauthorized use, copying, or distribution is strictly prohibited.

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from sqlalchemy import inspect, text

from app.core import clock
from app.api.v1.router import api_router
from app.core.config import settings
from app.core.branch_scope import BranchScopeMiddleware
from app.core.mirror import ReadOnlyMirrorMiddleware
from app.db.demo_seed import ensure_demo_seed
from app.db.seed import ensure_extra_seed, seed
from app.db.session import Base, SessionLocal, engine
import app.models  # noqa: F401


def _ensure_schema() -> None:
    insp = inspect(engine)
    if "catalog_items" in insp.get_table_names():
        cols = {c["name"] for c in insp.get_columns("catalog_items")}
        if "sub_category" not in cols:
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE catalog_items ADD COLUMN sub_category VARCHAR(60) DEFAULT ''"))
    if "wards" in insp.get_table_names() and "is_active" not in {c["name"] for c in insp.get_columns("wards")}:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE wards ADD COLUMN is_active BOOLEAN DEFAULT 1"))
            conn.execute(text("UPDATE wards SET is_active = 1 WHERE is_active IS NULL"))
    if "invoices" in insp.get_table_names():
        if "paid_at" not in {c["name"] for c in insp.get_columns("invoices")}:
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE invoices ADD COLUMN paid_at DATETIME"))
                # Bills already paid: the day of their last payment.
                conn.execute(text(
                    "UPDATE invoices SET paid_at = (SELECT MAX(created_at) FROM payments "
                    "WHERE payments.invoice_id = invoices.id AND payments.method != 'refund') "
                    "WHERE status = 'paid'"
                ))
                conn.execute(text("UPDATE invoices SET paid_at = created_at WHERE status = 'paid' AND paid_at IS NULL"))
    if "catalog_items" in insp.get_table_names():
        if "deleted_at" not in {c["name"] for c in insp.get_columns("catalog_items")}:
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE catalog_items ADD COLUMN deleted_at DATETIME"))
    if "catalog_items" in insp.get_table_names():
        if "bill_group" not in {c["name"] for c in insp.get_columns("catalog_items")}:
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE catalog_items ADD COLUMN bill_group VARCHAR(10) DEFAULT ''"))
        if "expiry_alert_months" not in {c["name"] for c in insp.get_columns("catalog_items")}:
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE catalog_items ADD COLUMN expiry_alert_months INTEGER DEFAULT 1"))
    if "admissions" in insp.get_table_names():
        if "requested_ward_id" not in {c["name"] for c in insp.get_columns("admissions")}:
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE admissions ADD COLUMN requested_ward_id INTEGER"))
        adm_cols = {c["name"] for c in insp.get_columns("admissions")}
        with engine.begin() as conn:
            if "discharge_approved_at" not in adm_cols:
                conn.execute(text("ALTER TABLE admissions ADD COLUMN discharge_approved_at DATETIME"))
            if "discharge_approved_by" not in adm_cols:
                conn.execute(text("ALTER TABLE admissions ADD COLUMN discharge_approved_by INTEGER"))
            if "home_stay" not in adm_cols:
                conn.execute(text("ALTER TABLE admissions ADD COLUMN home_stay BOOLEAN DEFAULT 0"))
    if "invoices" in insp.get_table_names():
        inv_cols = {c["name"] for c in insp.get_columns("invoices")}
        with engine.begin() as conn:
            for col, ddl in (
                ("credit_at", "DATETIME"), ("credit_amount", "FLOAT DEFAULT 0"), ("credit_by", "INTEGER"),
                ("credit_name", "VARCHAR(120) DEFAULT ''"), ("credit_phone", "VARCHAR(40) DEFAULT ''"),
                ("credit_due", "DATE"), ("credit_note", "VARCHAR(300) DEFAULT ''"),
            ):
                if col not in inv_cols:
                    conn.execute(text(f"ALTER TABLE invoices ADD COLUMN {col} {ddl}"))
    if "payments" in insp.get_table_names() and "is_deposit" not in {c["name"] for c in insp.get_columns("payments")}:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE payments ADD COLUMN is_deposit BOOLEAN DEFAULT 0"))
            # Mark the deposits already taken: method "deposit", or IPD money
            # taken while still admitted (before the bill was settled).
            conn.execute(text("UPDATE payments SET is_deposit = 1 WHERE method = 'deposit'"))
            conn.execute(text(
                "UPDATE payments SET is_deposit = 1 WHERE method != 'refund' AND invoice_id IN "
                "(SELECT id FROM invoices WHERE kind = 'ipd') AND EXISTS (SELECT 1 FROM audit_logs a WHERE "
                "a.action = 'ipd_deposit' AND ABS(julianday(a.created_at) - julianday(payments.created_at)) * 86400 < 2)"))
    if "cash_closes" in insp.get_table_names():
        if "status" not in {c["name"] for c in insp.get_columns("cash_closes")}:
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE cash_closes ADD COLUMN status VARCHAR(12) DEFAULT 'closed'"))
    if "stock_batches" in insp.get_table_names():
        if "notes" not in {c["name"] for c in insp.get_columns("stock_batches")}:
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE stock_batches ADD COLUMN notes VARCHAR(200) DEFAULT ''"))
    if "invoice_lines" in insp.get_table_names():
        cols = {c["name"] for c in insp.get_columns("invoice_lines")}
        if "unit_cost" not in cols:
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE invoice_lines ADD COLUMN unit_cost FLOAT DEFAULT 0"))
        if "returned_from_line_id" not in cols:
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE invoice_lines ADD COLUMN returned_from_line_id INTEGER"))
        if "fee_type" not in cols:
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE invoice_lines ADD COLUMN fee_type VARCHAR(10) DEFAULT ''"))
        if "sort_order" not in cols:
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE invoice_lines ADD COLUMN sort_order INTEGER DEFAULT 0"))
        if "created_at" not in cols:
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE invoice_lines ADD COLUMN created_at DATETIME"))
                conn.execute(text(
                    "UPDATE invoice_lines SET created_at = (SELECT created_at FROM invoices WHERE invoices.id = invoice_lines.invoice_id) "
                    "WHERE created_at IS NULL"
                ))
    if "patients" in insp.get_table_names():
        cols = {c["name"] for c in insp.get_columns("patients")}
        with engine.begin() as conn:
            if "age_years" not in cols:
                conn.execute(text("ALTER TABLE patients ADD COLUMN age_years INTEGER"))
            if "age_months" not in cols:
                conn.execute(text("ALTER TABLE patients ADD COLUMN age_months INTEGER"))
            if "age_days" not in cols:
                conn.execute(text("ALTER TABLE patients ADD COLUMN age_days INTEGER"))
            if "referring_doctor" not in cols:
                conn.execute(text("ALTER TABLE patients ADD COLUMN referring_doctor VARCHAR(160) DEFAULT ''"))
            if "father_name" not in cols:
                conn.execute(text("ALTER TABLE patients ADD COLUMN father_name VARCHAR(160) DEFAULT ''"))
    if "queue_tokens" in insp.get_table_names():
        cols = {c["name"] for c in insp.get_columns("queue_tokens")}
        with engine.begin() as conn:
            if "doctor_id" not in cols:
                conn.execute(text("ALTER TABLE queue_tokens ADD COLUMN doctor_id INTEGER REFERENCES users(id)"))
            if "queue_date" not in cols:
                conn.execute(text("ALTER TABLE queue_tokens ADD COLUMN queue_date DATE"))
            if "seq" not in cols:
                conn.execute(text("ALTER TABLE queue_tokens ADD COLUMN seq INTEGER DEFAULT 0"))
            if "public_code" not in cols:
                conn.execute(text("ALTER TABLE queue_tokens ADD COLUMN public_code VARCHAR(32) DEFAULT ''"))
            if "done_at" not in cols:
                conn.execute(text("ALTER TABLE queue_tokens ADD COLUMN done_at DATETIME"))
    if "users" in insp.get_table_names():
        cols = {c["name"] for c in insp.get_columns("users")}
        with engine.begin() as conn:
            if "allowed_counters" not in cols:
                conn.execute(text("ALTER TABLE users ADD COLUMN allowed_counters TEXT DEFAULT ''"))
            if "extra_permissions" not in cols:
                conn.execute(text("ALTER TABLE users ADD COLUMN extra_permissions TEXT DEFAULT ''"))
            if "allowed_features" not in cols:
                conn.execute(text("ALTER TABLE users ADD COLUMN allowed_features TEXT DEFAULT ''"))
    if "ward_med_orders" in insp.get_table_names():
        cols = {c["name"] for c in insp.get_columns("ward_med_orders")}
        if "invoice_id" not in cols:
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE ward_med_orders ADD COLUMN invoice_id INTEGER"))
    if "expenses" in insp.get_table_names():
        cols = {c["name"] for c in insp.get_columns("expenses")}
        with engine.begin() as conn:
            if "name" not in cols:
                conn.execute(text("ALTER TABLE expenses ADD COLUMN name VARCHAR(120) DEFAULT ''"))
            if "paid_by" not in cols:
                conn.execute(text("ALTER TABLE expenses ADD COLUMN paid_by VARCHAR(80) DEFAULT ''"))
            if "shift_id" not in cols:
                conn.execute(text("ALTER TABLE expenses ADD COLUMN shift_id INTEGER"))
    if "payments" in insp.get_table_names():
        cols = {c["name"] for c in insp.get_columns("payments")}
        if "refund_method" not in cols:
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE payments ADD COLUMN refund_method VARCHAR(20) DEFAULT ''"))
    if "appointments" in insp.get_table_names():
        cols = {c["name"] for c in insp.get_columns("appointments")}
        if "duration_minutes" not in cols:
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE appointments ADD COLUMN duration_minutes INTEGER DEFAULT 15"))
    admissions_needed_backfill = False
    if "admissions" in insp.get_table_names():
        cols = {c["name"] for c in insp.get_columns("admissions")}
        if "last_room_charge_date" not in cols:
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE admissions ADD COLUMN last_room_charge_date DATE"))
            admissions_needed_backfill = True
        if "discharge_type" not in cols:
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE admissions ADD COLUMN discharge_type VARCHAR(20) DEFAULT ''"))

    # Indexes added to models after their tables already shipped — CREATE
    # INDEX IF NOT EXISTS is idempotent (unlike ADD COLUMN) so this can just
    # run unconditionally every startup rather than checking column/index
    # existence first. Without these, hot-path lookups like "find this
    # patient's open invoice" or "load this invoice's lines" full-table-scan
    # invoices/invoice_lines/payments etc., which is invisible on a small
    # demo DB but gets slower as real operational history accumulates.
    _INDEXES = [
        ("invoices", "patient_id"), ("invoices", "branch_id"), ("invoices", "admission_id"),
        ("invoices", "shift_id"), ("invoices", "status"),
        ("invoice_lines", "invoice_id"),
        ("payments", "invoice_id"), ("payments", "shift_id"),
        ("lab_orders", "patient_id"), ("lab_orders", "branch_id"), ("lab_orders", "invoice_id"), ("lab_orders", "status"),
        ("radiology_orders", "patient_id"), ("radiology_orders", "branch_id"), ("radiology_orders", "invoice_id"), ("radiology_orders", "status"),
        ("ot_schedules", "patient_id"), ("ot_schedules", "branch_id"), ("ot_schedules", "invoice_id"), ("ot_schedules", "status"),
        ("service_orders", "patient_id"), ("service_orders", "branch_id"), ("service_orders", "invoice_id"), ("service_orders", "status"),
        ("beds", "ward_id"),
        ("admissions", "patient_id"), ("admissions", "branch_id"), ("admissions", "bed_id"), ("admissions", "status"),
        ("nursing_notes", "admission_id"),
        ("vital_signs", "admission_id"),
        ("ward_med_orders", "patient_id"), ("ward_med_orders", "branch_id"), ("ward_med_orders", "admission_id"),
        ("ward_med_order_items", "order_id"), ("ward_med_order_items", "status"),
        ("appointments", "patient_id"), ("appointments", "doctor_id"), ("appointments", "branch_id"), ("appointments", "status"),
        ("queue_tokens", "branch_id"), ("queue_tokens", "patient_id"), ("queue_tokens", "status"),
        ("queue_tokens", "branch_id, queue_date, doctor_id, status"), ("queue_tokens", "public_code"),
        ("visits", "patient_id"), ("visits", "branch_id"),
        ("expenses", "branch_id"), ("expenses", "shift_id"),
        # Date-window reports (Analyze, Owner Panel, Bill/Expense History, Doctor
        # Income) filter "this branch, between these dates" — composite indexes let
        # them seek straight to the window instead of scanning all history.
        # The analytics scans also carry every column they aggregate, so a year's
        # window is read from the index alone without a table lookup per row.
        ("invoices", "branch_id, created_at, kind, status, total, balance"), ("invoices", "branch_id, status"),
        ("payments", "created_at, invoice_id, method, amount"),
        ("invoice_lines", "invoice_id, source, doctor_id, amount, df_amount, qty, unit_cost, item_id"),
        ("invoice_lines", "doctor_id"), ("invoice_lines", "item_id"),
        ("expenses", "branch_id, expense_date"), ("expenses", "branch_id, created_at"),
        ("stock_batches", "item_id, warehouse_id"),
        ("stock_movements", "item_id"),
        ("lab_orders", "branch_id, created_at"), ("radiology_orders", "branch_id, created_at"),
    ]
    table_names = set(insp.get_table_names())
    with engine.begin() as conn:
        for table, cols in _INDEXES:
            col_list = [c.strip() for c in cols.split(",")]
            if table in table_names and set(col_list) <= {c["name"] for c in insp.get_columns(table)}:
                name = f"ix_{table}_" + "_".join(col_list)
                conn.execute(text(f"CREATE INDEX IF NOT EXISTS {name} ON {table} ({cols})"))
        if engine.dialect.name == "sqlite":
            # Refresh the query planner's statistics so it actually picks these.
            conn.execute(text("PRAGMA optimize"))

    db = SessionLocal()
    try:
        db.execute(
            text(
                """
                UPDATE invoice_lines
                SET unit_cost = (
                    SELECT cost FROM catalog_items WHERE catalog_items.id = invoice_lines.item_id
                )
                WHERE source = 'pharmacy' AND (unit_cost IS NULL OR unit_cost = 0) AND item_id IS NOT NULL
                """
            )
        )
        if admissions_needed_backfill:
            # Start the auto room-charge clock from today for admissions that
            # predate this column, rather than from admitted_at — otherwise a
            # long-open admission would suddenly get billed for every day it
            # was already open the first time this ran.
            from datetime import date

            from app.models.ipd import Admission

            for adm in db.query(Admission).filter(Admission.status.in_(["admitted", "transferred"])).all():
                adm.last_room_charge_date = clock.today()
        db.commit()
    finally:
        db.close()


# Timestamps written by code that used datetime.utcnow() (the column defaults
# all go through app.core.clock now). User-entered times — appointment/OT
# slots, follow-up dates — were always local and are left alone.
_UTC_WRITTEN_COLUMNS = {
    ("otp_codes", "expires_at"), ("attendance", "check_in"), ("cashier_shifts", "closed_at"),
    ("queue_tokens", "called_at"), ("admissions", "discharged_at"),
}


def _shift_timestamps_to_local() -> None:
    """One-time move of history recorded in UTC onto Myanmar local time
    (see app/core/clock.py), so "today"/day reports and on-screen times match
    the wall clock. Recorded in app_meta so it never runs twice."""
    from sqlalchemy import DateTime

    from app.core.clock import UTC_OFFSET_MINUTES

    sqlite = engine.dialect.name == "sqlite"
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE IF NOT EXISTS app_meta (key VARCHAR(60) PRIMARY KEY, value VARCHAR(200))"))
        if conn.execute(text("SELECT value FROM app_meta WHERE key = 'timestamps_local_tz'")).scalar():
            return
        insp = inspect(conn)
        existing = set(insp.get_table_names())
        for table in Base.metadata.sorted_tables:
            if table.name not in existing:
                continue
            present = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if not isinstance(col.type, DateTime) or col.name not in present:
                    continue
                if col.default is None and (table.name, col.name) not in _UTC_WRITTEN_COLUMNS:
                    continue
                shifted = f"datetime({col.name}, '+{UTC_OFFSET_MINUTES} minutes')" if sqlite else f"{col.name} + interval '{UTC_OFFSET_MINUTES} minutes'"
                conn.execute(text(f"UPDATE {table.name} SET {col.name} = {shifted} WHERE {col.name} IS NOT NULL"))
        if "expenses" in existing:
            # expense_date defaulted to the UTC date of created_at; re-derive it from
            # the now-local created_at wherever it still matches that default.
            if sqlite:
                conn.execute(text(
                    f"UPDATE expenses SET expense_date = date(created_at) "
                    f"WHERE expense_date = date(datetime(created_at, '-{UTC_OFFSET_MINUTES} minutes'))"
                ))
            else:
                conn.execute(text(
                    f"UPDATE expenses SET expense_date = created_at::date "
                    f"WHERE expense_date = (created_at - interval '{UTC_OFFSET_MINUTES} minutes')::date"
                ))
        if "admissions" in existing:
            # Room charges already posted for the current UTC day count as today's,
            # so switching day boundaries can't bill an admission twice for one day.
            from datetime import datetime as _dt

            conn.execute(
                text("UPDATE admissions SET last_room_charge_date = :local WHERE last_room_charge_date = :utc"),
                {"local": clock.today(), "utc": _dt.utcnow().date()},
            )
        conn.execute(text("INSERT INTO app_meta (key, value) VALUES ('timestamps_local_tz', 'done')"))


def _merge_room_charge_lines() -> None:
    """One-time collapse of the per-day "Room charge ..." lines that daily
    accrual used to add into one line per bed (qty = days), matching how
    billing_service.add_room_charge() records them now. Totals don't change."""
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE IF NOT EXISTS app_meta (key VARCHAR(60) PRIMARY KEY, value VARCHAR(200))"))
        if conn.execute(text("SELECT value FROM app_meta WHERE key = 'room_charges_merged'")).scalar():
            return
        if "invoice_lines" in set(inspect(conn).get_table_names()):
            rows = conn.execute(text(
                "SELECT id, invoice_id, description, unit_price, qty, amount FROM invoice_lines "
                "WHERE source = 'ipd_room' AND (discount IS NULL OR discount = 0) ORDER BY id"
            )).all()
            groups: dict[tuple, list] = {}
            for r in rows:
                groups.setdefault((r.invoice_id, r.description, r.unit_price), []).append(r)
            for lines in groups.values():
                if len(lines) < 2:
                    continue
                keep, extra = lines[0], lines[1:]
                conn.execute(
                    text("UPDATE invoice_lines SET qty = :q, amount = :a WHERE id = :id"),
                    {"q": sum(float(r.qty or 0) for r in lines), "a": sum(float(r.amount or 0) for r in lines), "id": keep.id},
                )
                conn.execute(text("DELETE FROM invoice_lines WHERE id IN (" + ",".join(str(r.id) for r in extra) + ")"))
        conn.execute(text("INSERT INTO app_meta (key, value) VALUES ('room_charges_merged', 'done')"))


def _grant_opd_queue_counter() -> None:
    """One-time: OPD Queue moved out of the Nurse station into its own counter,
    so staff with a custom counter list that includes "nurse" keep queue access."""
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE IF NOT EXISTS app_meta (key VARCHAR(60) PRIMARY KEY, value VARCHAR(200))"))
        if conn.execute(text("SELECT value FROM app_meta WHERE key = 'opd_queue_counter'")).scalar():
            return
        if "users" in set(inspect(conn).get_table_names()):
            from app.core.access import dump_json_list, parse_json_list

            for uid, raw in conn.execute(text("SELECT id, allowed_counters FROM users WHERE allowed_counters != ''")).all():
                counters = parse_json_list(raw)
                if "nurse" in counters and "opd-queue" not in counters:
                    counters.append("opd-queue")
                    conn.execute(text("UPDATE users SET allowed_counters = :c WHERE id = :id"), {"c": dump_json_list(counters), "id": uid})
        conn.execute(text("INSERT INTO app_meta (key, value) VALUES ('opd_queue_counter', 'done')"))


def _recalc_refunded_bills() -> None:
    """One-time: a refund now takes its amount off the bill's total (it used to
    leave the bill showing that amount as still owing), so bills that already
    have a refund are worked out again. A bill this makes fully paid counts on
    the day of its last refund."""
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE IF NOT EXISTS app_meta (key VARCHAR(60) PRIMARY KEY, value VARCHAR(200))"))
        if conn.execute(text("SELECT value FROM app_meta WHERE key = 'refund_lowers_total'")).scalar():
            return
    from app.models.billing import Invoice, Payment
    from app.services.billing_service import recalc_invoice

    db = SessionLocal()
    try:
        ids = [r[0] for r in db.query(Payment.invoice_id).filter(Payment.method == "refund").distinct()]
        for inv in db.query(Invoice).filter(Invoice.id.in_(ids), Invoice.status != "void"):
            was_paid = inv.status == "paid"
            recalc_invoice(inv, db)
            if inv.status == "paid" and not was_paid:
                inv.paid_at = max(p.created_at for p in inv.payments)
        db.execute(text("INSERT INTO app_meta (key, value) VALUES ('refund_lowers_total', 'done')"))
        db.commit()
    finally:
        db.close()


@asynccontextmanager
async def lifespan(_: FastAPI):
    Base.metadata.create_all(bind=engine)
    _shift_timestamps_to_local()
    _ensure_schema()
    _merge_room_charge_lines()
    _grant_opd_queue_counter()
    _recalc_refunded_bills()
    if settings.seed_on_start:
        db = SessionLocal()
        try:
            seed(db)
            ensure_extra_seed(db)
            ensure_demo_seed(db)
        finally:
            db.close()
    task = None
    if not settings.mirror_mode:
        import asyncio

        from app.services.auto_close import auto_close_loop

        task = asyncio.create_task(auto_close_loop())  # 🕛 midnight auto close
    yield
    if task is not None:
        task.cancel()


app = FastAPI(title=settings.app_name, lifespan=lifespan)
app.add_middleware(BranchScopeMiddleware)
if settings.mirror_mode:
    app.add_middleware(ReadOnlyMirrorMiddleware)
app.add_middleware(GZipMiddleware, minimum_size=1024)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(api_router)


@app.get("/health")
def health():
    return {"status": "ok", "app": settings.app_name}

from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from io import BytesIO
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.deps import require
from app.db.session import get_db
from app.models.accounting import Expense, JournalEntry, LedgerAccount, PettyCash
from app.models.billing import Payment
from app.models.patients import CorporateAccount
from app.models.users import User
from app.schemas.actions import ExpenseIn, ExpenseUpdateIn
from app.services.accounting_service import post_expense, reverse_expense
from app.services.utils import audit

router = APIRouter(prefix="/accounts", tags=["accounts"])


@router.get("/ledger")
def ledger(db: Session = Depends(get_db), _: User = Depends(require("accounts", "reports"))):
    return db.query(LedgerAccount).order_by(LedgerAccount.code).all()


@router.get("/journal")
def journal(db: Session = Depends(get_db), _: User = Depends(require("accounts", "reports"))):
    return db.query(JournalEntry).order_by(JournalEntry.id.desc()).limit(100).all()


@router.get("/cash-book")
def cash_book(db: Session = Depends(get_db), _: User = Depends(require("accounts", "reports"))):
    return db.query(Payment).order_by(Payment.id.desc()).limit(200).all()


@router.get("/ar")
def accounts_receivable(db: Session = Depends(get_db), _: User = Depends(require("accounts", "reports"))):
    from app.models.billing import Invoice

    return db.query(Invoice).filter(Invoice.balance > 0).order_by(Invoice.id.desc()).limit(100).all()


@router.get("/ap")
def accounts_payable(db: Session = Depends(get_db), _: User = Depends(require("accounts", "reports"))):
    from app.models.inventory import Supplier

    return db.query(Supplier).filter(Supplier.balance > 0).all()


@router.get("/corporate")
def corporate_accounts(db: Session = Depends(get_db), _: User = Depends(require("accounts", "billing.read"))):
    return db.query(CorporateAccount).all()


@router.post("/expenses")
def create_expense(data: ExpenseIn, db: Session = Depends(get_db), user: User = Depends(require("accounts", "pos"))):
    from app.models.billing import CashierShift

    shift = db.query(CashierShift).filter(CashierShift.user_id == user.id, CashierShift.status == "open").first()
    exp = Expense(
        branch_id=data.branch_id,
        shift_id=shift.id if shift else None,
        category=data.category,
        name=data.name,
        amount=data.amount,
        paid_from=data.paid_from,
        paid_by=data.paid_by,
        notes=data.notes,
    )
    db.add(exp)
    post_expense(db, data.category, data.amount, data.paid_from)
    # The petty-cash balance tracks the physical cash box specifically — an
    # expense paid via KPay/Wave moves money out of that e-wallet, not the
    # till, so it shouldn't touch this balance (it already posts to its own
    # ledger account — 1010/1011 — via post_expense() above).
    petty = db.query(PettyCash).filter(PettyCash.branch_id == data.branch_id).first()
    if petty and data.paid_from == "cash":
        petty.balance -= data.amount
    db.commit()
    db.refresh(exp)
    return exp


# Staff can change / delete an expense only while the shift it was recorded in
# is still open (a closed shift's cash count must not move afterwards).
EXPENSE_ADMINS = ("super_admin", "hospital_admin", "branch_admin", "accountant")


def _editable_expense(db: Session, expense_id: int, user: User) -> Expense:
    from app.models.billing import CashierShift

    exp = db.get(Expense, expense_id)
    if not exp:
        raise HTTPException(404, "Expense not found")
    if user.role not in EXPENSE_ADMINS:
        from app.core import clock

        shift = db.get(CashierShift, exp.shift_id) if exp.shift_id else None
        # (recorded with no shift open: same day only)
        ok = shift.status == "open" if shift else exp.created_at is not None and exp.created_at.date() == clock.today()
        if not ok:
            raise HTTPException(403, "Shift ပိတ်ပြီးသား (ဒါမှမဟုတ် ရက်ကျော်ပြီးသား) expense ကို Admin / Accountant ပဲ ပြင်/ဖျက်လို့ရပါတယ်")
    return exp


def _petty(db: Session, branch_id: int, paid_from: str, delta: float) -> None:
    """Petty cash only moves for expenses paid in cash (see create_expense)."""
    petty = db.query(PettyCash).filter(PettyCash.branch_id == branch_id).first()
    if petty and paid_from == "cash":
        petty.balance += delta


@router.patch("/expenses/{expense_id}")
def update_expense(expense_id: int, data: ExpenseUpdateIn, db: Session = Depends(get_db), user: User = Depends(require("accounts", "pos"))):
    exp = _editable_expense(db, expense_id, user)
    if data.amount is not None and data.amount <= 0:
        raise HTTPException(400, "Amount must be more than 0")
    if data.category is not None and not data.category.strip():
        raise HTTPException(400, "Category required")
    if data.name is not None and not data.name.strip():
        raise HTTPException(400, "Name required")
    before = f"{exp.category}/{exp.name} {exp.amount:.0f} {exp.paid_from} {exp.expense_date}"
    # Money side: take the old expense back out of the books, then post the new one.
    reverse_expense(db, exp.category, exp.amount, exp.paid_from)
    _petty(db, exp.branch_id, exp.paid_from, exp.amount)
    if data.category is not None:
        exp.category = data.category.strip()
    if data.name is not None:
        exp.name = data.name.strip()
    if data.amount is not None:
        exp.amount = data.amount
    if data.paid_from is not None and data.paid_from.strip():
        exp.paid_from = data.paid_from.strip()
    if data.notes is not None:
        exp.notes = data.notes
    if data.expense_date is not None:
        exp.expense_date = data.expense_date
    post_expense(db, exp.category, exp.amount, exp.paid_from)
    _petty(db, exp.branch_id, exp.paid_from, -exp.amount)
    after = f"{exp.category}/{exp.name} {exp.amount:.0f} {exp.paid_from} {exp.expense_date}"
    audit(db, user.id, "expense_edit", "expense", str(exp.id), f"{before} -> {after}")
    db.commit()
    db.refresh(exp)
    return exp


@router.delete("/expenses/{expense_id}")
def delete_expense(expense_id: int, db: Session = Depends(get_db), user: User = Depends(require("accounts", "pos"))):
    exp = _editable_expense(db, expense_id, user)
    reverse_expense(db, exp.category, exp.amount, exp.paid_from)
    _petty(db, exp.branch_id, exp.paid_from, exp.amount)
    audit(db, user.id, "expense_delete", "expense", str(exp.id),
          f"{exp.category}/{exp.name} {exp.amount:.0f} {exp.paid_from} {exp.expense_date} {exp.notes or ''}".strip())
    db.delete(exp)
    db.commit()
    return {"ok": True, "id": expense_id}


def _expense_query(db: Session, branch_id: int, date_from, date_to, category: str, name: str, search: str):
    """Expense History filters: date range, category, name (both exact, picked
    from lists) and free text in the name / notes."""
    q = db.query(Expense).filter(Expense.branch_id == branch_id)
    if date_from:
        q = q.filter(Expense.expense_date >= date_from)
    if date_to:
        q = q.filter(Expense.expense_date <= date_to)
    if category.strip():
        q = q.filter(Expense.category == category.strip())
    if name.strip():
        q = q.filter(Expense.name == name.strip())
    if search.strip():
        like = f"%{search.strip()}%"
        q = q.filter(Expense.notes.ilike(like) | Expense.name.ilike(like))
    return q


@router.get("/expenses")
def list_expenses(
    branch_id: int,
    date_from: str | None = None,
    date_to: str | None = None,
    category: str = "",
    name: str = "",
    search: str = "",
    db: Session = Depends(get_db),
    _: User = Depends(require("accounts", "pos")),
):
    q = _expense_query(db, branch_id, date_from, date_to, category, name, search)
    # Totals per category, and per name inside it, over everything that matches
    # (the row list itself is capped).
    groups = (
        q.with_entities(Expense.category, Expense.name, func.count(Expense.id), func.coalesce(func.sum(Expense.amount), 0))
        .group_by(Expense.category, Expense.name)
        .all()
    )
    summary: dict[str, dict] = {}
    for cat, nm, n, amt in groups:
        c = summary.setdefault(cat or "", {"category": cat or "", "count": 0, "total": 0.0, "names": []})
        c["count"] += int(n)
        c["total"] += float(amt or 0)
        c["names"].append({"name": nm or "", "count": int(n), "total": float(amt or 0)})
    for c in summary.values():
        c["names"].sort(key=lambda x: -x["total"])
    rows = q.order_by(Expense.expense_date.desc(), Expense.id.desc()).limit(500).all()
    return {
        "rows": rows,
        "total": sum(c["total"] for c in summary.values()),
        "count": sum(c["count"] for c in summary.values()),
        "summary": sorted(summary.values(), key=lambda c: -c["total"]),
    }


@router.get("/expenses/categories")
def expense_categories(branch_id: int, db: Session = Depends(get_db), _: User = Depends(require("accounts", "pos"))):
    rows = (
        db.query(Expense.category)
        .filter(Expense.branch_id == branch_id, Expense.category != "")
        .distinct()
        .order_by(Expense.category)
        .all()
    )
    # Only categories the hospital has used — new ones are typed in Record Expense.
    return [r[0] for r in rows]


@router.get("/expenses/names")
def expense_names(branch_id: int, db: Session = Depends(get_db), _: User = Depends(require("accounts", "pos"))):
    """Names already used on this branch's expenses (newest first) with the
    category they were last filed under — Record Expense suggests them so the
    same expense doesn't have to be typed again."""
    rows = (
        db.query(Expense.name, Expense.category)
        .filter(Expense.branch_id == branch_id, Expense.name != "")
        .order_by(Expense.id.desc())
        .limit(500)
        .all()
    )
    # One entry per (name, category): the same name can be used in two categories.
    seen: dict[tuple[str, str], None] = {}
    for name, category in rows:
        key = ((name or "").strip(), category or "")
        if key[0] and key not in seen:
            seen[key] = None
    return [{"name": n, "category": c} for n, c in seen]


@router.get("/expenses/export")
def export_expenses(
    branch_id: int,
    date_from: str | None = None,
    date_to: str | None = None,
    category: str = "",
    name: str = "",
    search: str = "",
    db: Session = Depends(get_db),
    _: User = Depends(require("accounts", "pos")),
):
    import openpyxl
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

    q = _expense_query(db, branch_id, date_from, date_to, category, name, search)
    rows = q.order_by(Expense.category, Expense.expense_date.desc(), Expense.id.desc()).limit(2000).all()

    by_category: dict[str, list[Expense]] = {}
    for r in rows:
        by_category.setdefault(r.category or "Uncategorized", []).append(r)
    sorted_categories = sorted(by_category.items(), key=lambda kv: -sum(i.amount for i in kv[1]))
    grand_total = sum(r.amount for r in rows)

    thin = Side(style="thin", color="D0D5DD")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    header_fill = PatternFill("solid", fgColor="005293")
    header_font = Font(bold=True, color="FFFFFF")
    subtotal_fill = PatternFill("solid", fgColor="EAF2FA")
    total_fill = PatternFill("solid", fgColor="FDECEC")
    bold = Font(bold=True)
    money_fmt = "#,##0"
    COLS = ["No", "Date", "Category", "Name", "Paid From", "Paid By", "Amount"]

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Expenses"

    ws.append(["Expense History Report"])
    ws["A1"].font = Font(bold=True, size=14)
    ws.append([f"Period: {date_from or 'all'} — {date_to or 'all'}" + (f"  ·  Category: {category}" if category.strip() else "")])
    ws["A2"].font = Font(italic=True, color="666666")
    ws.append([None] * len(COLS))

    ws.append(COLS)
    header_row = ws.max_row
    for c in ws[header_row]:
        c.font = header_font
        c.fill = header_fill
        c.border = border
        c.alignment = Alignment(horizontal="center")

    no = 0
    for cat, items in sorted_categories:
        for r in items:
            no += 1
            ws.append([no, str(r.expense_date), cat, r.name, r.paid_from, r.paid_by, r.amount])
            row = ws.max_row
            for c in ws[row]:
                c.border = border
            ws.cell(row, 7).number_format = money_fmt

        ws.append(["", "", "", "", "", f"{cat} Subtotal", sum(i.amount for i in items)])
        row = ws.max_row
        for c in ws[row]:
            c.border = border
            c.fill = subtotal_fill
            c.font = bold
        ws.cell(row, 7).number_format = money_fmt

    ws.append(["", "", "", "", "", "GRAND TOTAL", grand_total])
    row = ws.max_row
    for c in ws[row]:
        c.border = border
        c.fill = total_fill
        c.font = bold
    ws.cell(row, 7).number_format = money_fmt

    widths = [6, 14, 18, 20, 12, 16, 16]
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[openpyxl.utils.get_column_letter(i)].width = w
    ws.freeze_panes = ws.cell(header_row + 1, 1)

    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=expense-history.xlsx"},
    )


@router.get("/expenses/export/pdf")
def export_expenses_pdf(
    branch_id: int,
    date_from: str | None = None,
    date_to: str | None = None,
    category: str = "",
    name: str = "",
    search: str = "",
    db: Session = Depends(get_db),
    _: User = Depends(require("accounts", "pos")),
):
    from app.services.pdf_service import build_table_pdf

    q = _expense_query(db, branch_id, date_from, date_to, category, name, search)
    rows = q.order_by(Expense.category, Expense.expense_date.desc(), Expense.id.desc()).limit(2000).all()
    grand_total = sum(r.amount for r in rows)

    table_rows = [[str(r.expense_date or ""), r.category or "Uncategorized", r.name, (r.paid_from or "").upper(), r.paid_by, f"{r.amount:,.0f}"] for r in rows]
    by_cat: dict[str, float] = {}
    for r in rows:
        by_cat[r.category or "Uncategorized"] = by_cat.get(r.category or "Uncategorized", 0) + (r.amount or 0)
    top = sorted(by_cat.items(), key=lambda kv: -kv[1])[:2]

    buf = build_table_pdf(
        title="Expense Report",
        meta=[("Period", f"{date_from or 'all'} — {date_to or 'all'}"), ("Category", category or "All"), ("Name", name or "All")],
        kpis=[("Expenses", f"{len(rows):,}"), ("Total", f"{grand_total:,.0f}")] + [(f"Top: {k}", f"{v:,.0f}") for k, v in top],
        columns=["Date", "Category", "Name", "Paid From", "Paid By", "Amount"],
        rows=table_rows,
        total=["Total", "", f"{len(rows)} items", "", "", f"{grand_total:,.0f}"],
    )
    return StreamingResponse(
        buf,
        media_type="application/pdf",
        headers={"Content-Disposition": 'attachment; filename="expense-history.pdf"'},
    )


@router.get("/petty-cash")
def petty(branch_id: int, db: Session = Depends(get_db), _: User = Depends(require("accounts", "pos"))):
    """Petty-cash box = what was put in (top-ups) − expenses paid "from Petty"."""
    from app.models.accounting import PettyTopup

    put_in = float(db.query(func.coalesce(func.sum(PettyTopup.amount), 0)).filter(PettyTopup.branch_id == branch_id).scalar() or 0)
    spent = float(
        db.query(func.coalesce(func.sum(Expense.amount), 0))
        .filter(Expense.branch_id == branch_id, func.lower(Expense.paid_from) == "petty")
        .scalar() or 0
    )
    from app.core import clock

    first = clock.today().replace(day=1)
    month_spent = float(
        db.query(func.coalesce(func.sum(Expense.amount), 0))
        .filter(Expense.branch_id == branch_id, func.lower(Expense.paid_from) == "petty", Expense.expense_date >= first)
        .scalar() or 0
    )
    last = db.query(PettyTopup).filter(PettyTopup.branch_id == branch_id).order_by(PettyTopup.id.desc()).first()
    return {
        "month_spent": round(month_spent, 2),
        "branch_id": branch_id,
        "balance": round(put_in - spent, 2),
        "put_in": round(put_in, 2),
        "spent": round(spent, 2),
        "last_topup": {"amount": last.amount, "source": last.source, "at": last.created_at.isoformat()} if last else None,
    }


@router.post("/petty-cash/topup")
def petty_topup(payload: dict, db: Session = Depends(get_db), user: User = Depends(require("accounts", "pos"))):
    """Put money in the petty-cash box — from the owner / office, or out of the
    cashier's drawer (then the drawer's expected cash drops by it)."""
    from app.models.accounting import PettyTopup

    try:
        branch_id = int(payload.get("branch_id"))
        amount = float(payload.get("amount"))
    except (TypeError, ValueError) as e:
        raise HTTPException(400, "Amount ထည့်ပါ") from e
    if amount <= 0:
        raise HTTPException(400, "Amount ထည့်ပါ")
    source = "drawer" if payload.get("source") == "drawer" else "owner"
    t = PettyTopup(branch_id=branch_id, amount=round(amount, 2), source=source,
                   note=str(payload.get("note") or "").strip()[:200], user_id=user.id)
    db.add(t)
    audit(db, user.id, "petty_topup", "petty_cash", str(branch_id), f"{amount:,.0f} from {source} {t.note}".strip())
    db.commit()
    return petty(branch_id, db, user)


@router.get("/reports/pl")
def pl(db: Session = Depends(get_db), _: User = Depends(require("reports", "accounts"))):
    rev = db.query(LedgerAccount).filter(LedgerAccount.code == "4000").first()
    exp = db.query(LedgerAccount).filter(LedgerAccount.code == "5000").first()
    return {"revenue": rev.balance if rev else 0, "expense": exp.balance if exp else 0, "profit": (rev.balance if rev else 0) - (exp.balance if exp else 0)}


@router.get("/export/excel")
def export_excel(db: Session = Depends(get_db), _: User = Depends(require("reports", "accounts"))):
    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Ledger"
    ws.append(["Code", "Name", "Kind", "Balance"])
    for acc in db.query(LedgerAccount).all():
        ws.append([acc.code, acc.name, acc.kind, acc.balance])
    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)
    return StreamingResponse(buf, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", headers={"Content-Disposition": "attachment; filename=ledger.xlsx"})

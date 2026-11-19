"""Activity Log, readable: every line is tied to its patient and bill (INV-…),
system words (item=962 qty=2.0, inv=81, 1 splits) are turned into names and
money, and the second copy written for one action (stock `dispense` next to
`pharmacy_dispense`, `cashier_order` next to `lab_order`) is hidden.

Nothing is stored differently — old logs read the same way — so the clinic
mirror and an older site database work unchanged."""
import re
from datetime import datetime, timedelta

from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from app.models.billing import Invoice, InvoiceLine, Payment
from app.models.catalog import CatalogItem
from app.models.ipd import Admission
from app.models.org import AuditLog
from app.models.patients import Patient
from app.models.users import User

# ⚠ — things an owner wants to see: money taken back, bills removed, prices
# changed, closed books re-opened.
ALERT_ACTIONS = {
    "invoice_void", "invoice_delete", "invoice_unvoid", "void_line", "history_edit_line", "discount",
    "refund", "payment_void", "edit_payment", "change_paid_date", "change_payment_date", "reopen_bill",
    "cash_close_cancel", "cash_close_edit", "cash_close_reopen", "credit_cancel", "patient_delete",
    "patient_merge", "item_delete", "stock_receive_undo", "stock_out_undo", "expense_delete", "expense_edit",
    "return_line", "exchange_line", "pharmacy_line_remove", "staff_reset_password", "close_bill",
}
# Only a price change is worth a ⚠ for these (qty-only edits are routine).
ALERT_IF_PRICE = {"edit_line"}
ORDER_TWINS = {"lab_order", "xray_order", "usg_order", "pharmacy_dispense"}

_INV = re.compile(r"\b(?:ref=)?inv[=-](\d+)\b")
_ITEM = re.compile(r"\bitem=(\d+)\b")
_PAT = re.compile(r"\bpatient=(\d+)\b")


def _hidden_twin():
    # stock movement logged next to the order that caused it (ref=inv-N)
    return ~and_(AuditLog.action == "dispense", AuditLog.detail.like("%ref=inv-%"))


def _alert_filter():
    return or_(
        AuditLog.action.in_(ALERT_ACTIONS),
        and_(AuditLog.action.in_(ALERT_IF_PRICE), ~AuditLog.detail.like("price=None%")),
        and_(AuditLog.action == "ipd_discharge", AuditLog.detail.like("%override%")),
    )


def _inv_like(ids):
    out = []
    for i in ids:
        out += [AuditLog.detail.like(f"%inv={i}"), AuditLog.detail.like(f"%inv={i} %"), AuditLog.detail.like(f"%ref=inv-{i}%")]
    return out


def _bill_conds(db: Session, inv_ids: list[int], with_lines: bool = True):
    """Every log that belongs to these bills."""
    if not inv_ids:
        return []
    s_ids = [str(i) for i in inv_ids]
    conds = [and_(AuditLog.entity == "invoice", AuditLog.entity_id.in_(s_ids)), *_inv_like(inv_ids)]
    if with_lines:
        line_ids = [str(x) for (x,) in db.query(InvoiceLine.id).filter(InvoiceLine.invoice_id.in_(inv_ids)).limit(3000)]
        if line_ids:
            conds.append(and_(AuditLog.entity == "invoice_line", AuditLog.entity_id.in_(line_ids)))
    adm_ids = [str(a) for (a,) in db.query(Invoice.admission_id).filter(Invoice.id.in_(inv_ids), Invoice.admission_id.isnot(None))]
    if adm_ids:
        conds.append(and_(AuditLog.entity == "admission", AuditLog.entity_id.in_(adm_ids)))
    nums = [n for (n,) in db.query(Invoice.number).filter(Invoice.id.in_(inv_ids))]
    for n in nums:
        conds.append(AuditLog.detail.like(f"%{n}%"))
    return conds


def _search_conds(db: Session, q: str):
    like = f"%{q}%"
    conds = [
        AuditLog.detail.ilike(like), AuditLog.action.ilike(like), AuditLog.entity_id.ilike(like),
        User.full_name.ilike(like), User.username.ilike(like),
    ]
    pq = db.query(Patient.id).filter(or_(
        Patient.name.ilike(like), Patient.name_mm.ilike(like), Patient.uhid.ilike(like), Patient.phone.ilike(like),
    ))
    pids = [p for (p,) in pq.limit(40)]
    if pids:
        sp = [str(p) for p in pids]
        conds.append(and_(AuditLog.entity == "patient", AuditLog.entity_id.in_(sp)))
        for p in pids:
            conds += [AuditLog.detail.like(f"%patient={p}"), AuditLog.detail.like(f"%patient={p} %")]
        adm = [str(a) for (a,) in db.query(Admission.id).filter(Admission.patient_id.in_(pids)).limit(200)]
        if adm:
            conds.append(and_(AuditLog.entity == "admission", AuditLog.entity_id.in_(adm)))
    ic = [Invoice.number.ilike(like)]
    if pids:
        ic.append(Invoice.patient_id.in_(pids))
    inv_ids = [i for (i,) in db.query(Invoice.id).filter(or_(*ic)).order_by(Invoice.id.desc()).limit(60)]
    conds += _bill_conds(db, inv_ids, with_lines=len(inv_ids) <= 20)
    items = [i for (i,) in db.query(CatalogItem.id).filter(CatalogItem.name.ilike(like)).limit(30)]
    for i in items:
        conds += [AuditLog.detail.like(f"%item={i}"), AuditLog.detail.like(f"%item={i} %")]
    return conds


def _money(v) -> str:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    return f"{f:,.0f}" if abs(f - round(f)) < 0.005 else f"{f:,.2f}"


def _num(v) -> str:
    f = float(v)
    return str(int(f)) if f == int(f) else f"{f:g}"


def activity_rows(
    db: Session, *, branch_id: int | None, q: str = "", group: str = "", date_from: str = "", date_to: str = "",
    user_id: int | None = None, invoice_id: int | None = None, limit: int = 200, offset: int = 0,
    labels: dict,
):
    query = db.query(AuditLog, User).outerjoin(User, AuditLog.user_id == User.id)
    if branch_id is not None and invoice_id is None:
        query = query.filter(User.branch_id == branch_id)
    if invoice_id:
        conds = _bill_conds(db, [invoice_id])
        query = query.filter(or_(*conds)) if conds else query.filter(False)
    else:
        query = query.filter(_hidden_twin())
    if group == "alert":
        query = query.filter(_alert_filter())
    elif group:
        actions = [a for a, (g, *_l) in labels.items() if g == group]
        query = query.filter(AuditLog.action.in_(actions)) if group != "other" else query.filter(~AuditLog.action.in_(list(labels)))
    if user_id:
        query = query.filter(AuditLog.user_id == user_id)
    if date_from:
        query = query.filter(AuditLog.created_at >= datetime.fromisoformat(date_from[:10]))
    if date_to:
        query = query.filter(AuditLog.created_at < datetime.fromisoformat(date_to[:10]) + timedelta(days=1))
    q = q.strip()
    if q:
        label_hits = [a for a, (_g, *lb) in labels.items() if any(q.lower() in x.lower() for x in lb)]
        conds = _search_conds(db, q)
        if label_hits:
            conds.append(AuditLog.action.in_(label_hits))
        query = query.filter(or_(*conds))
    total = query.order_by(None).count()
    limit = max(1, min(int(limit or 200), 1000))
    order = AuditLog.id.asc() if invoice_id else AuditLog.id.desc()
    rows = query.order_by(order).offset(max(0, int(offset or 0))).limit(limit + 1).all()
    more = len(rows) > limit
    rows = rows[:limit]

    # ── what each line points at ──
    def ids_of(rx, txt):
        m = rx.search(txt or "")
        return int(m.group(1)) if m else None

    need_inv, need_line, need_adm, need_pat, need_item = set(), set(), set(), set(), set()
    for log, _u in rows:
        e, eid, d = log.entity or "", log.entity_id or "", log.detail or ""
        if e == "invoice" and eid.isdigit():
            need_inv.add(int(eid))
        if e == "invoice_line" and eid.isdigit():
            need_line.add(int(eid))
        if e == "admission" and eid.isdigit():
            need_adm.add(int(eid))
        if e == "patient" and eid.isdigit():
            need_pat.add(int(eid))
        if e == "catalog_item" and eid.isdigit():
            need_item.add(int(eid))
        for rx, bag in ((_INV, need_inv), (_PAT, need_pat)):
            v = ids_of(rx, d)
            if v:
                bag.add(v)
        need_item.update(int(x) for x in _ITEM.findall(d))
    line_rows = db.query(InvoiceLine.id, InvoiceLine.invoice_id, InvoiceLine.description).filter(InvoiceLine.id.in_(need_line)).all() if need_line else []
    lines = {ln.id: ln.invoice_id for ln in line_rows}
    line_name = {ln.id: ln.description or "" for ln in line_rows}
    need_inv.update(lines.values())
    adms = {a.id: a.patient_id for a in db.query(Admission.id, Admission.patient_id).filter(Admission.id.in_(need_adm))} if need_adm else {}
    adm_inv = {}
    if need_adm:
        for i_id, a_id in db.query(Invoice.id, Invoice.admission_id).filter(Invoice.admission_id.in_(need_adm)).order_by(Invoice.id):
            adm_inv[a_id] = i_id
        need_inv.update(adm_inv.values())
    invs = {i.id: i for i in db.query(Invoice).filter(Invoice.id.in_(need_inv))} if need_inv else {}
    need_pat.update(i.patient_id for i in invs.values() if i.patient_id)
    need_pat.update(adms.values())
    pats = {p.id: p for p in db.query(Patient).filter(Patient.id.in_(need_pat))} if need_pat else {}
    items = {i.id: i.name for i in db.query(CatalogItem.id, CatalogItem.name).filter(CatalogItem.id.in_(need_item))} if need_item else {}

    pay_inv = {invs_id for log, _u in rows if log.action in ("payment", "multi_payment") and (log.entity_id or "").isdigit() for invs_id in [int(log.entity_id)]}
    pays: dict[int, list] = {}
    if pay_inv:
        for p in db.query(Payment).filter(Payment.invoice_id.in_(pay_inv)):
            pays.setdefault(p.invoice_id, []).append(p)

    def nice(log) -> str:
        d = log.detail or ""
        a = log.action
        if a in ("payment", "multi_payment") and (log.entity_id or "").isdigit() and log.created_at:
            got = [p for p in pays.get(int(log.entity_id), []) if p.created_at and log.created_at - timedelta(seconds=15) <= p.created_at <= log.created_at + timedelta(seconds=2)]
            if got:
                return " + ".join(f"{(p.method or '').upper()} {_money(p.amount)}" for p in got)
        d = _INV.sub(lambda m: invs[int(m.group(1))].number if int(m.group(1)) in invs else f"bill #{m.group(1)}", d)
        d = _ITEM.sub(lambda m: items.get(int(m.group(1)), f"item #{m.group(1)}"), d)
        d = _PAT.sub("", d)
        d = re.sub(r"\b(?:price|qty|doctor|batch)=(?:None)?(?=\s|$)", "", d)
        d = re.sub(r"\bqty=(-?[\d.]+)", lambda m: f"× {_num(m.group(1))}", d)
        d = re.sub(r"\bprice=([\d.]+)", lambda m: f"ဈေး {_money(m.group(1))}", d)
        d = re.sub(r"\bamount=([\d.]+)", lambda m: _money(m.group(1)), d)
        d = re.sub(r"\bward=-\s*", "", d)
        d = re.sub(r"\bbed=(\S+)", r"bed \1", d)
        d = re.sub(r"\bward=", "ward ", d)
        d = re.sub(r"\brefund=([\d.]+)", lambda m: f"refund {_money(m.group(1))}", d)
        d = re.sub(r"\b(\d+(?:\.\d+)?e\+\d+)\b", lambda m: _money(m.group(1)), d)
        eid = log.entity_id or ""
        what = ""
        if log.entity == "invoice_line" and eid.isdigit():
            what = line_name.get(int(eid), "")
        elif log.entity == "catalog_item" and eid.isdigit():
            what = items.get(int(eid), "")
        if what and what not in d:
            d = f"{what} {d}"
        if a == "reception_register":
            d = re.sub(r"^opd\b", "OPD", re.sub(r"^ipd\b", "IPD", d))
        if a == "line_order":
            d = d.replace(" lines", " ခု")
        # 800000.0 → 800,000 (numbers that are clearly money)
        d = re.sub(r"(?<![\w.,#/:-])(\d{4,})(?:\.0+)?(?![\d.:/]|,\d)", lambda m: f"{int(m.group(1)):,}", d)
        return re.sub(r"\s{2,}", " ", d).strip(" ·-")

    out = []
    for log, user in rows:
        e, eid, d = log.entity or "", log.entity_id or "", log.detail or ""
        inv_id = int(eid) if e == "invoice" and eid.isdigit() else None
        inv_id = inv_id or (lines.get(int(eid)) if e == "invoice_line" and eid.isdigit() else None)
        inv_id = inv_id or ids_of(_INV, d)
        inv_id = inv_id or (adm_inv.get(int(eid)) if e == "admission" and eid.isdigit() else None)
        inv = invs.get(inv_id) if inv_id else None
        pid = inv.patient_id if inv else None
        pid = pid or (int(eid) if e == "patient" and eid.isdigit() else None)
        pid = pid or (adms.get(int(eid)) if e == "admission" and eid.isdigit() else None)
        pid = pid or ids_of(_PAT, d)
        pt = pats.get(pid) if pid else None
        del_no = eid[4:] if e == "invoice" and eid.startswith("del-") else None
        lab = labels.get(log.action)
        alert = (
            log.action in ALERT_ACTIONS
            or (log.action in ALERT_IF_PRICE and not d.startswith("price=None"))
            or (log.action == "ipd_discharge" and "override" in d)
        )
        out.append({
            "id": log.id,
            "action": log.action,
            "label": lab[1] if lab else log.action.replace("_", " "),
            "label_en": lab[2] if lab else log.action.replace("_", " ").capitalize(),
            "group": lab[0] if lab else "other",
            "alert": alert,
            "entity": e,
            "entity_id": eid,
            "detail": nice(log),
            "raw": d,
            "user_id": log.user_id,
            "user_name": user.full_name if user else "—",
            "created_at": log.created_at.isoformat() if log.created_at else None,
            "invoice_id": inv.id if inv else None,
            "invoice_number": inv.number if inv else del_no,
            "invoice_kind": inv.kind if inv else None,
            "invoice_status": inv.status if inv else ("deleted" if del_no else None),
            "patient_id": pt.id if pt else None,
            "patient_name": pt.name if pt else None,
            "uhid": pt.uhid if pt else None,
        })

    # cashier_order written right after lab/xray/usg order for the same item — keep one
    if not invoice_id:
        keep = []
        for i, r in enumerate(out):
            if r["action"] == "cashier_order":
                twin = any(
                    o["action"] in ORDER_TWINS and o["invoice_id"] == r["invoice_id"] and abs(o["id"] - r["id"]) <= 3
                    and _ITEM.search(o["raw"] or "") and _ITEM.search(r["raw"] or "")
                    and _ITEM.search(o["raw"]).group(1) == _ITEM.search(r["raw"]).group(1)
                    for o in out[max(0, i - 3): i + 4] if o is not r
                )
                if twin:
                    continue
            keep.append(r)
        out = keep
    return {"rows": out, "more": more, "total": total, "next_offset": int(offset or 0) + limit}


def activity_users(db: Session, branch_id: int | None, days: int = 120):
    since = datetime.now() - timedelta(days=days)
    q = (
        db.query(User.id, User.full_name, User.role)
        .join(AuditLog, AuditLog.user_id == User.id)
        .filter(AuditLog.created_at >= since)
    )
    if branch_id is not None:
        q = q.filter(User.branch_id == branch_id)
    return [{"id": i, "name": n, "role": r} for i, n, r in q.distinct().order_by(User.full_name)]

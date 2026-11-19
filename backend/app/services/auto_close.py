"""🕛 Midnight auto cut-off: every night at 12:00 AM the books are cut off by
themselves — a "⏳ ရေတွက်နေဆဲ" (counting) close, exactly like pressing 🔒 Close
Shift at midnight. Counting the cash and the float left are entered by staff
afterwards (▶ / Finish), as with any hand-over. Money taken after midnight goes
to the next day.

Runs every minute from the app (see main.py). Catches up after the PC was off:
one close per missed midnight that had money moving. Idempotent — a close is
only made when the last close ended before that midnight.
"""
import json
import logging
from datetime import datetime, time, timedelta

from sqlalchemy.orm import Session

from app.core import clock

log = logging.getLogger("auto_close")

AUTO_BY = "🕛 Auto 12:00 AM"
MAX_CATCH_UP_DAYS = 31


def _has_activity(d: dict) -> bool:
    return (
        abs(float(d.get("received") or 0)) > 0.005
        or abs(float(d.get("refunded") or 0)) > 0.005
        or any(abs(float(e.get("amount") or 0)) > 0.005 for e in d.get("expenses") or [])
        or abs(float(d.get("petty_from_drawer") or 0)) > 0.005
    )


def run_auto_close(db: Session) -> list[dict]:
    from app.api.v1.counters import _last_close, money_window
    from app.models.accounting import CashClose
    from app.models.org import Branch
    from app.services.utils import audit

    midnight = datetime.combine(clock.today(), time.min)
    made: list[dict] = []
    for branch in db.query(Branch).filter(Branch.is_active == True).all():  # noqa: E712
        last = _last_close(db, branch.id)
        since = last.period_to if last is not None else midnight - timedelta(days=1)
        if since >= midnight:
            continue
        day = max(since.date() + timedelta(days=1), (midnight - timedelta(days=MAX_CATCH_UP_DAYS)).date())
        while True:
            cut = datetime.combine(day, time.min)
            if cut > midnight:
                break
            if cut > since:
                d = money_window(db, branch.id, since, cut)
                if _has_activity(d):
                    float_in = float(last.float_left or 0) if last is not None else 0.0
                    expected = round(float_in + float(d["cash_in_drawer"]), 2)
                    d.update({
                        "float_in": round(float_in, 2),
                        "expected_cash": expected,
                        "last_close": {"closed_by": last.closed_by, "at": last.period_to.isoformat()} if last is not None else None,
                        "pending": None,
                        "period_from": since.isoformat(),
                        "period_to": cut.isoformat(),
                        "auto": True,
                    })
                    c = CashClose(
                        branch_id=branch.id, period_from=since, period_to=cut, closed_by=AUTO_BY, handed_to="",
                        user_id=None, float_in=round(float_in, 2), expected_cash=expected, counted_cash=0,
                        float_left=0, note="ည ၁၂ နာရီ auto ဖြတ် — ငွေရေတွက်ပြီး အတည်ပြုပါ",
                        summary=json.dumps(d, ensure_ascii=False), status="counting",
                    )
                    db.add(c)
                    db.flush()
                    audit(db, None, "cash_close_auto", "cash_close", str(c.id),
                          f"{AUTO_BY} · cut off {since:%d/%m %H:%M}–{cut:%d/%m %H:%M} · expected {expected:,.0f} · waiting to be counted")
                    db.commit()
                    made.append({"branch_id": branch.id, "id": c.id, "to": cut.isoformat(), "expected": expected})
                    last, since = c, cut
            day += timedelta(days=1)
    return made


async def auto_close_loop() -> None:
    import asyncio

    from app.db.session import SessionLocal

    def once() -> None:
        db = SessionLocal()
        try:
            for m in run_auto_close(db):
                log.info("auto close made: %s", m)
        except Exception:  # never let the loop die
            db.rollback()
            log.exception("auto close failed")
        finally:
            db.close()

    while True:
        await asyncio.to_thread(once)
        await asyncio.sleep(60)

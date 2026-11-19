"""Keeps each branch's operational data (Shwe Muse Hospital vs Shwe Muse Clinic)
separate for branch-bound staff.

Owners (super_admin / hospital_admin) see every branch, exactly as before.
Everyone else with a branch_id is "branch-bound", and for their requests:

1. BranchScopeMiddleware checks the request before any endpoint runs: a
   `branch_id` in the query string or JSON body that isn't their own branch is
   rejected (403), and a missing `branch_id` query parameter is filled in with
   their own — so endpoints where "no branch" means "all branches" (reports,
   analytics) only ever return their branch.
2. An ORM hook adds `branch_id = <their branch>` to every SELECT on the
   branch-owned tables, so looking a record up by id (invoice, admission, lab
   order, ...) from the other branch behaves as if it doesn't exist (404).

Shared on purpose (not filtered): patients (one Patient ID across both
branches), doctors/staff, the test/service catalog, and medicines.
"""
import json
from contextvars import ContextVar
from urllib.parse import parse_qsl, urlencode

from sqlalchemy import event, select
from sqlalchemy.orm import Session, with_loader_criteria

from app.core.security import decode_token

OWNER_ROLES = ("super_admin", "hospital_admin")

# Branch id the current request is limited to; None = unrestricted.
current_branch: ContextVar[int | None] = ContextVar("current_branch", default=None)


def _scoped_models():
    from app.models.accounting import CapitalAsset, Expense, PettyCash
    from app.models.ancillary import LabOrder, OTSchedule, RadiologyOrder, ServiceOrder
    from app.models.billing import CashierShift, Invoice
    from app.models.clinical import Appointment, Prescription, QueueToken, Visit
    from app.models.ipd import Admission, WardMedOrder, Ward
    from app.models.org import Warehouse
    from app.models.users import DutyRoster

    return (
        Expense, PettyCash, CapitalAsset, CashierShift, Invoice, LabOrder, RadiologyOrder, OTSchedule,
        ServiceOrder, Appointment, Prescription, QueueToken, Visit, Admission, WardMedOrder, Ward,
        Warehouse, DutyRoster,
    )


@event.listens_for(Session, "do_orm_execute")
def _limit_to_branch(state) -> None:
    bid = current_branch.get()
    if bid is None or not state.is_select:
        return
    from app.models.ipd import Bed, Ward

    opts = [with_loader_criteria(m, lambda cls: cls.branch_id == bid, include_aliases=True) for m in _scoped_models()]
    # Beds have no branch column of their own — they belong to a ward.
    opts.append(
        with_loader_criteria(
            Bed, lambda cls: cls.ward_id.in_(select(Ward.id).where(Ward.branch_id == bid)), include_aliases=True
        )
    )
    state.statement = state.statement.options(*opts)


def _bound_branch(token: str) -> int | None:
    """The branch a bearer token is limited to, or None if unrestricted."""
    payload = decode_token(token)
    if not payload or payload.get("typ") != "access":
        return None
    from app.db.session import SessionLocal
    from app.models.users import User

    db = SessionLocal()
    try:
        user = db.get(User, int(payload["sub"]))
        if not user or user.role in OWNER_ROLES or user.branch_id is None:
            return None
        return user.branch_id
    finally:
        db.close()


async def _send_403(send, detail: str) -> None:
    body = json.dumps({"detail": detail}).encode()
    await send({"type": "http.response.start", "status": 403, "headers": [(b"content-type", b"application/json")]})
    await send({"type": "http.response.body", "body": body})


class BranchScopeMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not scope["path"].startswith("/api/"):
            return await self.app(scope, receive, send)
        auth = dict(scope.get("headers") or []).get(b"authorization", b"").decode()
        if not auth.lower().startswith("bearer "):
            return await self.app(scope, receive, send)

        from starlette.concurrency import run_in_threadpool

        bid = await run_in_threadpool(_bound_branch, auth[7:].strip())
        if bid is None:
            return await self.app(scope, receive, send)

        # Query string: reject another branch, default to their own.
        params = parse_qsl(scope.get("query_string", b"").decode(), keep_blank_values=True)
        sent = [v for k, v in params if k == "branch_id" and v != ""]
        if any(v != str(bid) for v in sent):
            return await _send_403(send, "This account can only use its own branch")
        params = [(k, v) for k, v in params if k != "branch_id"] + [("branch_id", str(bid))]
        scope = {**scope, "query_string": urlencode(params).encode()}

        # JSON body: reject a branch_id that isn't theirs.
        if scope["method"] in ("POST", "PUT", "PATCH"):
            chunks = []
            more = True
            while more:
                message = await receive()
                chunks.append(message.get("body", b""))
                more = message.get("more_body", False)
            raw = b"".join(chunks)
            ctype = dict(scope.get("headers") or []).get(b"content-type", b"")
            if raw and b"json" in ctype:
                try:
                    data = json.loads(raw)
                except ValueError:
                    data = None
                if isinstance(data, dict) and data.get("branch_id") not in (None, "", bid, str(bid)):
                    return await _send_403(send, "This account can only use its own branch")

            replayed = False
            upstream = receive

            async def receive():  # noqa: F811 — hand the buffered body to the app once
                nonlocal replayed
                if not replayed:
                    replayed = True
                    return {"type": "http.request", "body": raw, "more_body": False}
                return await upstream()

        token = current_branch.set(bid)
        try:
            await self.app(scope, receive, send)
        finally:
            current_branch.reset(token)

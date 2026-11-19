"""Real-time "something changed" push to open browsers over WebSocket.

Every committed database change is turned into a small topic event (e.g.
`{"topic": "ward_order", "branch_id": 1}`) by SQLAlchemy session hooks, so no
endpoint has to remember to announce its own writes. Pages subscribe to the
topics they show and refetch their data when one arrives; the event itself
never carries patient data.

The broker is in-process: it works because the app runs as a single uvicorn
process (local and cloud). Running several workers would need a shared bus
(e.g. Redis pub/sub) in its place.
"""
import asyncio
import threading

from sqlalchemy import event
from sqlalchemy.orm import Session

from app.models.accounting import Expense
from app.models.ancillary import LabOrder, RadiologyOrder
from app.models.billing import Invoice, InvoiceLine, Payment
from app.models.catalog import CatalogItem
from app.models.clinical import Appointment, QueueToken
from app.models.inventory import StockBatch
from app.models.ipd import Admission, Bed, NursingNote, VitalSign, WardMedOrder, WardMedOrderItem
from app.models.patients import Patient

# model → topic. Rows without their own branch_id are sent to every branch
# (the client just refetches; it's cheap and never leaks data).
TOPICS: dict[type, str] = {
    QueueToken: "queue",
    WardMedOrder: "ward_order",
    WardMedOrderItem: "ward_order",
    LabOrder: "lab",
    RadiologyOrder: "radiology",
    Invoice: "invoice",
    InvoiceLine: "invoice",
    Payment: "invoice",
    Admission: "ipd",
    Bed: "ipd",
    NursingNote: "nursing",
    VitalSign: "nursing",
    Appointment: "appointment",
    Expense: "expense",
    StockBatch: "stock",
    CatalogItem: "stock",
    Patient: "patient",
}

# Topics anyone may receive without logging in (patient QR page, waiting-room TV).
PUBLIC_TOPICS = {"queue"}


class Broker:
    def __init__(self) -> None:
        self._subs: set[tuple[asyncio.Queue, asyncio.AbstractEventLoop, int | None, bool]] = set()
        self._lock = threading.Lock()

    def subscribe(self, branch_id: int | None, public: bool) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=200)
        with self._lock:
            self._subs.add((q, asyncio.get_running_loop(), branch_id, public))
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        with self._lock:
            self._subs = {s for s in self._subs if s[0] is not q}

    def publish(self, topic: str, branch_id: int | None) -> None:
        """Thread-safe: called from sync routes running in the threadpool."""
        msg = {"type": "change", "topic": topic, "branch_id": branch_id}
        with self._lock:
            subs = list(self._subs)
        for q, loop, sub_branch, public in subs:
            if public and topic not in PUBLIC_TOPICS:
                continue
            if branch_id is not None and sub_branch is not None and branch_id != sub_branch:
                continue
            try:
                loop.call_soon_threadsafe(_offer, q, msg)
            except RuntimeError:  # loop already closed (client gone)
                pass


def _offer(q: asyncio.Queue, msg: dict) -> None:
    try:
        q.put_nowait(msg)
    except asyncio.QueueFull:  # a stalled client; it will refetch on reconnect
        pass


broker = Broker()


def _collect(session: Session, objs) -> None:
    pending: set = session.info.setdefault("live_events", set())
    for obj in objs:
        topic = TOPICS.get(type(obj))
        if topic:
            pending.add((topic, getattr(obj, "branch_id", None)))


@event.listens_for(Session, "after_flush")
def _after_flush(session: Session, _ctx) -> None:
    # session.dirty also holds rows whose attributes were merely re-set to the
    # same value (e.g. recalc_invoice on a read) — skip those, or a page that
    # refetches on an event could trigger another event and loop forever.
    changed = [o for o in session.dirty if session.is_modified(o, include_collections=False)]
    _collect(session, list(session.new) + changed + list(session.deleted))


@event.listens_for(Session, "after_commit")
def _after_commit(session: Session) -> None:
    # Announce only once the data is committed, so a client refetching on
    # the event never reads the old state.
    for topic, branch_id in session.info.pop("live_events", set()):
        broker.publish(topic, branch_id)


@event.listens_for(Session, "after_rollback")
def _after_rollback(session: Session) -> None:
    session.info.pop("live_events", None)

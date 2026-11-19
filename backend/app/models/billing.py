from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, Float, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.clock import now as clock_now
from app.db.session import Base


class CashierShift(Base):
    __tablename__ = "cashier_shifts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    branch_id: Mapped[int] = mapped_column(ForeignKey("branches.id"))
    opened_at: Mapped[datetime] = mapped_column(DateTime, default=clock_now)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    opening_float: Mapped[float] = mapped_column(Float, default=0)
    closing_cash: Mapped[float] = mapped_column(Float, default=0)
    status: Mapped[str] = mapped_column(String(20), default="open")
    notes: Mapped[str] = mapped_column(Text, default="")

    user = relationship("User")


class Invoice(Base):
    __tablename__ = "invoices"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    number: Mapped[str] = mapped_column(String(30), unique=True, index=True)
    patient_id: Mapped[int | None] = mapped_column(ForeignKey("patients.id"), nullable=True, index=True)
    branch_id: Mapped[int] = mapped_column(ForeignKey("branches.id"), index=True)
    doctor_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    admission_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    shift_id: Mapped[int | None] = mapped_column(ForeignKey("cashier_shifts.id"), nullable=True, index=True)
    kind: Mapped[str] = mapped_column(String(20), default="opd")  # opd | ipd | pharmacy | other
    status: Mapped[str] = mapped_column(String(20), default="open", index=True)  # draft | open | partial | paid | void
    # When the bill was fully paid (cleared again if it reopens). Reports count a
    # bill on this day — the day the money came in — not the day it was opened.
    paid_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    payer_type: Mapped[str] = mapped_column(String(20), default="self")  # self | corporate | insurance
    corporate_account_id: Mapped[int | None] = mapped_column(ForeignKey("corporate_accounts.id"), nullable=True)
    currency: Mapped[str] = mapped_column(String(8), default="MMK")
    fx_rate: Mapped[float] = mapped_column(Float, default=1)
    subtotal: Mapped[float] = mapped_column(Float, default=0)
    discount: Mapped[float] = mapped_column(Float, default=0)
    total: Mapped[float] = mapped_column(Float, default=0)
    paid: Mapped[float] = mapped_column(Float, default=0)
    balance: Mapped[float] = mapped_column(Float, default=0)
    notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=clock_now)
    # 📒 Closed on credit (OPD): the bill leaves Open Bills with money still owed.
    # Income counts only the payments, on the day each one comes in.
    credit_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    credit_amount: Mapped[float] = mapped_column(Float, default=0)
    credit_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    credit_name: Mapped[str] = mapped_column(String(120), default="")
    credit_phone: Mapped[str] = mapped_column(String(40), default="")
    credit_due: Mapped[date | None] = mapped_column(Date, nullable=True)
    credit_note: Mapped[str] = mapped_column(String(300), default="")

    patient = relationship("Patient")
    doctor = relationship("User")
    # Cashier can drag lines into any order (sort_order 1, 2, ...); lines never
    # moved (0) follow in the order they were added.
    lines = relationship(
        "InvoiceLine", back_populates="invoice", cascade="all, delete-orphan",
        order_by="[InvoiceLine.sort_order == 0, InvoiceLine.sort_order, InvoiceLine.id]",
    )
    payments = relationship("Payment", back_populates="invoice", cascade="all, delete-orphan")

    # Read by InvoiceOut so a bill can be printed (receipt) without a second lookup.
    @property
    def doctor_name(self) -> str:
        return self.doctor.full_name if self.doctor else ""

    @property
    def refunded(self) -> float:
        """What refunds took off this bill: subtotal - discount - refunded = total.
        (Money given back for an overpayment is not part of it — see recalc_invoice.)"""
        return max(max((self.subtotal or 0) - (self.discount or 0), 0) - (self.total or 0), 0)

    @property
    def patient_name(self) -> str:
        return self.patient.name if self.patient else ""

    @property
    def patient_uhid(self) -> str:
        return self.patient.uhid if self.patient else ""



# A bill's report date: the day it was fully paid, or the day it was opened while unpaid.
BILL_DATE = func.coalesce(Invoice.paid_at, Invoice.created_at)

class InvoiceLine(Base):
    __tablename__ = "invoice_lines"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    invoice_id: Mapped[int] = mapped_column(ForeignKey("invoices.id"), index=True)
    item_id: Mapped[int | None] = mapped_column(ForeignKey("catalog_items.id"), nullable=True)
    batch_id: Mapped[int | None] = mapped_column(ForeignKey("stock_batches.id"), nullable=True)
    source: Mapped[str] = mapped_column(String(30), default="opd")
    description: Mapped[str] = mapped_column(String(200), default="")
    qty: Mapped[float] = mapped_column(Float, default=1)
    unit_price: Mapped[float] = mapped_column(Float, default=0)
    unit_cost: Mapped[float] = mapped_column(Float, default=0)
    discount: Mapped[float] = mapped_column(Float, default=0)
    amount: Mapped[float] = mapped_column(Float, default=0)
    doctor_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    df_amount: Mapped[float] = mapped_column(Float, default=0)
    # Set on a "Return: ..." line (negative qty/amount) — the line it gives back.
    returned_from_line_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Doctor-fee lines only: how the cashier settled the fee — "" (normal), "night",
    # "half" or "foc". Kept off the printed line; used for the Doctor Income report.
    fee_type: Mapped[str] = mapped_column(String(10), default="")
    # Position on the bill / voucher set by dragging (0 = never moved).
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    # When the charge was added (IPD day-by-day pre-bill). Older lines: the bill's open time.
    created_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, default=clock_now)

    invoice = relationship("Invoice", back_populates="lines")
    item = relationship("CatalogItem")

    @property
    def is_stock(self) -> bool:
        """A medicine / supply taken from stock (prints as one total line)."""
        return bool(self.batch_id) or bool(self.item is not None and self.item.is_stock)

    @property
    def bill_group(self) -> str:
        """OT items: "ot" / "otm" chosen in the OT warehouse ("" = decide by stock)."""
        return (self.item.bill_group or "") if self.item is not None else ""

    @property
    def department(self) -> str:
        """The item's counter (lab / xray / usg / ...) — splits X-ray from USG on the voucher."""
        return (self.item.department or "") if self.item is not None else ""


class Payment(Base):
    __tablename__ = "payments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    invoice_id: Mapped[int] = mapped_column(ForeignKey("invoices.id"), index=True)
    shift_id: Mapped[int | None] = mapped_column(ForeignKey("cashier_shifts.id"), nullable=True, index=True)
    method: Mapped[str] = mapped_column(String(20))  # cash kpay wave kbzpay card deposit refund
    amount: Mapped[float] = mapped_column(Float)
    currency: Mapped[str] = mapped_column(String(8), default="MMK")
    fx_rate: Mapped[float] = mapped_column(Float, default=1)
    # Only meaningful when method == "refund" — which wallet the money was actually
    # paid back out of (cash/kpay/wave). A refund's own `method` column is always
    # the literal string "refund" (so the payment history can tell it apart from a
    # collection), so without this the Z-report's cash reconciliation could not
    # tell a cash refund from a KPay/Wave one.
    refund_method: Mapped[str] = mapped_column(String(20), default="")
    # True for an IPD deposit (taken while admitted) — the voucher lists deposits
    # one by one, apart from the money paid at discharge.
    is_deposit: Mapped[bool] = mapped_column(Boolean, default=False)
    received_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    notes: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=clock_now)

    invoice = relationship("Invoice", back_populates="payments")

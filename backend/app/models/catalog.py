from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


class CatalogItem(Base):
    __tablename__ = "catalog_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sku: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    barcode: Mapped[str] = mapped_column(String(60), default="", index=True)
    name: Mapped[str] = mapped_column(String(160))
    name_mm: Mapped[str] = mapped_column(String(160), default="")
    category: Mapped[str] = mapped_column(String(40), index=True)
    # consultation, drug, supply, equipment, lab, radiology, ot, dialysis,
    # physio, rehab, ambulance, emergency, package, room, other
    department: Mapped[str] = mapped_column(String(40), default="")
    # Free-text grouping within a stock department, e.g. "Antibiotic", "Painkiller".
    sub_category: Mapped[str] = mapped_column(String(60), default="", index=True)
    unit: Mapped[str] = mapped_column(String(20), default="ea")
    # Warn this many months before a batch expires (differs per medicine).
    expiry_alert_months: Mapped[int] = mapped_column(Integer, default=1)
    price: Mapped[float] = mapped_column(Float, default=0)
    cost: Mapped[float] = mapped_column(Float, default=0)
    doctor_fee_pct: Mapped[float] = mapped_column(Float, default=0)
    is_stock: Mapped[bool] = mapped_column(Boolean, default=False)
    # OT items only: where the line prints on the voucher — "ot" (own line under
    # OT) or "otm" (summed into "OT (Pharmacy + Disposable Item)"). "" = by stock.
    bill_group: Mapped[str | None] = mapped_column(String(10), default="", nullable=True)
    is_controlled: Mapped[bool] = mapped_column(Boolean, default=False)
    is_package: Mapped[bool] = mapped_column(Boolean, default=False)
    package_items: Mapped[str] = mapped_column(Text, default="")  # json list of item ids
    min_stock: Mapped[float] = mapped_column(Float, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # Set when staff delete an item (🗑): it moves to Store > Deleted Items, where
    # it can be restored. Inactive without deleted_at = kept hidden because it is
    # already on bills / in stock history.
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class LabTestReagent(Base):
    """How many of each lab supply a billed test consumes. One test, several reagents."""

    __tablename__ = "lab_test_reagents"
    __table_args__ = (UniqueConstraint("test_item_id", "reagent_item_id", name="uq_lab_test_reagent"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    test_item_id: Mapped[int] = mapped_column(ForeignKey("catalog_items.id"), index=True)
    reagent_item_id: Mapped[int] = mapped_column(ForeignKey("catalog_items.id"), index=True)
    qty: Mapped[float] = mapped_column(Float, default=1)


class Promotion(Base):
    __tablename__ = "promotions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    kind: Mapped[str] = mapped_column(String(20), default="percent")  # percent | amount
    value: Mapped[float] = mapped_column(Float, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

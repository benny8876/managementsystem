from datetime import date, datetime

from sqlalchemy import Date, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.clock import now as clock_now
from app.core.clock import today as clock_today
from app.db.session import Base


class Appointment(Base):
    __tablename__ = "appointments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"), index=True)
    doctor_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    branch_id: Mapped[int] = mapped_column(ForeignKey("branches.id"), index=True)
    scheduled_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    duration_minutes: Mapped[int] = mapped_column(Integer, default=15)
    source: Mapped[str] = mapped_column(String(20), default="counter")  # counter | phone | portal
    status: Mapped[str] = mapped_column(String(20), default="booked", index=True)  # booked | arrived | done | cancelled
    notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=clock_now)

    patient = relationship("Patient")
    doctor = relationship("User")


class Prescription(Base):
    __tablename__ = "prescriptions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    visit_id: Mapped[int | None] = mapped_column(ForeignKey("visits.id"), nullable=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"))
    doctor_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    branch_id: Mapped[int] = mapped_column(ForeignKey("branches.id"))
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending | dispensed | partial | cancelled
    created_at: Mapped[datetime] = mapped_column(DateTime, default=clock_now)

    patient = relationship("Patient")
    doctor = relationship("User")
    items = relationship("PrescriptionItem", back_populates="prescription", cascade="all, delete-orphan")


class PrescriptionItem(Base):
    __tablename__ = "prescription_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    prescription_id: Mapped[int] = mapped_column(ForeignKey("prescriptions.id"))
    catalog_item_id: Mapped[int | None] = mapped_column(ForeignKey("catalog_items.id"), nullable=True)
    free_text_name: Mapped[str] = mapped_column(String(160), default="")
    qty: Mapped[float] = mapped_column(Float, default=1)
    dosage_instructions: Mapped[str] = mapped_column(Text, default="")
    dispensed_qty: Mapped[float] = mapped_column(Float, default=0)
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending | dispensed | partial

    prescription = relationship("Prescription", back_populates="items")
    catalog_item = relationship("CatalogItem")


class QueueToken(Base):
    __tablename__ = "queue_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    branch_id: Mapped[int] = mapped_column(ForeignKey("branches.id"), index=True)
    number: Mapped[str] = mapped_column(String(20), index=True)
    patient_id: Mapped[int | None] = mapped_column(ForeignKey("patients.id"), nullable=True, index=True)
    department: Mapped[str] = mapped_column(String(40), default="OPD")
    status: Mapped[str] = mapped_column(String(20), default="waiting", index=True)  # waiting | serving | done | skipped
    created_at: Mapped[datetime] = mapped_column(DateTime, default=clock_now)
    called_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Per-doctor, per-day OPD queue (see app/services/queue_service.py). doctor_id
    # is NULL for the "General" queue (registered without a doctor).
    doctor_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    queue_date: Mapped[date | None] = mapped_column(Date, nullable=True, default=clock_today)
    seq: Mapped[int] = mapped_column(Integer, default=0)
    # Unguessable code in the patient's QR link (/q/<code>) — the public page
    # looks a token up by this, never by id, so links can't be enumerated.
    public_code: Mapped[str] = mapped_column(String(32), default="", index=True)
    done_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    patient = relationship("Patient")
    doctor = relationship("User")


class Visit(Base):
    __tablename__ = "visits"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"), index=True)
    doctor_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    branch_id: Mapped[int] = mapped_column(ForeignKey("branches.id"), index=True)
    kind: Mapped[str] = mapped_column(String(20), default="opd")
    appointment_id: Mapped[int | None] = mapped_column(ForeignKey("appointments.id"), nullable=True)
    chief_complaint: Mapped[str] = mapped_column(Text, default="")
    diagnosis: Mapped[str] = mapped_column(Text, default="")
    notes: Mapped[str] = mapped_column(Text, default="")
    prescription: Mapped[str] = mapped_column(Text, default="")
    follow_up_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=clock_now)

    patient = relationship("Patient")
    doctor = relationship("User")

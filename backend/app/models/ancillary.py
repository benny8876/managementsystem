from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.clock import now as clock_now
from app.db.session import Base


class LabOrder(Base):
    __tablename__ = "lab_orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"), index=True)
    branch_id: Mapped[int] = mapped_column(ForeignKey("branches.id"), index=True)
    doctor_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    invoice_id: Mapped[int | None] = mapped_column(ForeignKey("invoices.id"), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(20), default="ordered", index=True)
    # ordered collected in_lab result billed
    sample_id: Mapped[str] = mapped_column(String(30), default="")
    tests: Mapped[str] = mapped_column(Text, default="")
    result: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=clock_now)

    patient = relationship("Patient")


class RadiologyOrder(Base):
    __tablename__ = "radiology_orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"), index=True)
    branch_id: Mapped[int] = mapped_column(ForeignKey("branches.id"), index=True)
    doctor_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    invoice_id: Mapped[int | None] = mapped_column(ForeignKey("invoices.id"), nullable=True, index=True)
    modality: Mapped[str] = mapped_column(String(30), default="xray")  # xray ct ultrasound
    status: Mapped[str] = mapped_column(String(20), default="ordered", index=True)
    findings: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=clock_now)

    patient = relationship("Patient")


class OTSchedule(Base):
    __tablename__ = "ot_schedules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"), index=True)
    branch_id: Mapped[int] = mapped_column(ForeignKey("branches.id"), index=True)
    surgeon_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    invoice_id: Mapped[int | None] = mapped_column(ForeignKey("invoices.id"), nullable=True, index=True)
    scheduled_at: Mapped[datetime] = mapped_column(DateTime)
    procedure: Mapped[str] = mapped_column(String(200), default="")
    status: Mapped[str] = mapped_column(String(20), default="scheduled", index=True)
    anesthesia_fee: Mapped[float] = mapped_column(Float, default=0)
    surgeon_fee: Mapped[float] = mapped_column(Float, default=0)
    team_fee: Mapped[float] = mapped_column(Float, default=0)
    notes: Mapped[str] = mapped_column(Text, default="")

    patient = relationship("Patient")


class ServiceOrder(Base):
    __tablename__ = "service_orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"), index=True)
    branch_id: Mapped[int] = mapped_column(ForeignKey("branches.id"), index=True)
    service_type: Mapped[str] = mapped_column(String(30))  # dialysis physio rehab ambulance emergency
    invoice_id: Mapped[int | None] = mapped_column(ForeignKey("invoices.id"), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(20), default="open", index=True)
    notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=clock_now)

    patient = relationship("Patient")


class ResultFormat(Base):
    """A remembered result layout ("Format မှတ်ထားမယ်") that pre-fills the
    free-text result box: kind="lab" is keyed by the test name and fills in
    automatically when that test is opened; kind="usg"/"xray" is keyed by the
    exam name and picked from a list. Staff save the layout (labels, units,
    normal text) — never a patient's values."""

    __tablename__ = "result_formats"
    __table_args__ = (UniqueConstraint("kind", "name", name="uq_result_format_kind_name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(20), index=True)  # lab | usg | xray
    name: Mapped[str] = mapped_column(String(300))
    body: Mapped[str] = mapped_column(Text, default="")
    updated_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=clock_now, onupdate=clock_now)


class LabTemplate(Base):
    """A lab-staff-authored structured report format — rows with a unit and
    reference range, optionally grouped under section headings — matched to
    a LabOrder by exact name against CatalogItem.name/LabOrder.tests, same
    convention as the built-in templates in
    frontend/src/lib/labTemplates.ts. Deliberately simpler than those: no
    footnotes, confirmation-note checkboxes, or the Blood Donor Issue Form's
    signature-block layout — those stay code-only, hand-authored by a
    developer, since a generic builder can't safely expose that print logic
    as data."""

    __tablename__ = "lab_templates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(160), unique=True, index=True)
    has_unit: Mapped[bool] = mapped_column(Boolean, default=True)
    has_range: Mapped[bool] = mapped_column(Boolean, default=True)
    has_remark: Mapped[bool] = mapped_column(Boolean, default=True)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=clock_now)

    rows = relationship(
        "LabTemplateRow",
        back_populates="template",
        cascade="all, delete-orphan",
        order_by="LabTemplateRow.position",
    )


class LabTemplateRow(Base):
    __tablename__ = "lab_template_rows"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    template_id: Mapped[int] = mapped_column(ForeignKey("lab_templates.id"), index=True)
    position: Mapped[int] = mapped_column(Integer, default=0)
    kind: Mapped[str] = mapped_column(String(20), default="row")  # section | row
    label: Mapped[str] = mapped_column(String(200), default="")
    unit: Mapped[str] = mapped_column(String(40), default="")
    reference_range: Mapped[str] = mapped_column(String(200), default="")
    remark: Mapped[str] = mapped_column(String(200), default="")

    template = relationship("LabTemplate", back_populates="rows")

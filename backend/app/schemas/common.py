from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, Field


class ChangePasswordIn(BaseModel):
    current_password: str
    new_password: str


class TokenOut(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    user_id: int
    role: str
    home: str
    full_name: str
    branch_id: int | None = None


class RefreshIn(BaseModel):
    refresh_token: str


class UserOut(BaseModel):
    id: int
    username: str
    full_name: str
    role: str
    department: str
    branch_id: int | None
    specialty: str = ""
    consultation_fee: float = 0

    class Config:
        from_attributes = True


class BranchOut(BaseModel):
    id: int
    code: str
    name: str
    name_mm: str
    is_main: bool

    class Config:
        from_attributes = True


class PatientIn(BaseModel):
    name: str
    name_mm: str = ""
    father_name: str = ""
    phone: str = ""
    gender: str = ""
    dob: str | None = None
    age_years: int | None = None
    age_months: int | None = None
    age_days: int | None = None
    nrc: str = ""
    address: str = ""
    referring_doctor: str = ""
    blood_group: str = ""
    allergies: str = ""
    corporate_account_id: int | None = None
    insurance_name: str = ""
    insurance_no: str = ""


class PatientOut(BaseModel):
    id: int
    uhid: str
    name: str
    name_mm: str = ""
    father_name: str = ""
    phone: str = ""
    gender: str = ""
    dob: date | None = None
    age_years: int | None = None
    age_months: int | None = None
    age_days: int | None = None
    nrc: str = ""
    address: str = ""
    referring_doctor: str = ""
    blood_group: str = ""
    allergies: str = ""
    corporate_account_id: int | None = None
    insurance_name: str = ""
    insurance_no: str = ""
    created_at: datetime

    class Config:
        from_attributes = True


class AppointmentOut(BaseModel):
    id: int
    patient_id: int
    doctor_id: int
    branch_id: int
    scheduled_at: datetime
    duration_minutes: int
    source: str
    status: str
    notes: str
    created_at: datetime
    patient_name: str = ""
    patient_uhid: str = ""

    class Config:
        from_attributes = True


class CatalogOut(BaseModel):
    id: int
    sku: str
    barcode: str
    name: str
    name_mm: str
    category: str
    price: float
    is_stock: bool
    min_stock: float

    class Config:
        from_attributes = True


class InvoiceLineIn(BaseModel):
    item_id: int | None = None
    qty: float = 1
    unit_price: float | None = None
    source: str = "opd"
    description: str = ""
    discount: float = 0


class InvoiceLineUpdateIn(BaseModel):
    unit_price: float | None = None
    qty: float | None = None
    fee_type: str | None = None  # doctor-fee lines: "" normal | night | half | foc
    doctor_id: int | None = None  # lab lines: ordering doctor (0 = none / outside doctor)


class PaymentIn(BaseModel):
    method: str
    amount: float
    notes: str = ""


class MultiPaymentIn(BaseModel):
    payments: list[PaymentIn]
    # One note for the whole settlement (KPay ref no., who paid, ...).
    notes: str = ""


class InvoiceNoteIn(BaseModel):
    notes: str = ""


class RefundIn(BaseModel):
    amount: float
    method: str = ""
    reason: str = ""


class PharmacyLineIn(BaseModel):
    item_id: int
    warehouse_id: int
    qty: float = 1


class ReturnLineIn(BaseModel):
    qty: float
    reason: str
    refund_method: str = "cash"
    restock: bool = True


class OrderItemIn(BaseModel):
    item_id: int
    qty: float = 1
    warehouse_id: int | None = None
    note: str = ""


class ExchangeLineIn(BaseModel):
    qty: float
    reason: str
    new_item_id: int
    new_qty: float = 1
    method: str = "cash"
    warehouse_id: int | None = None


class InvoiceLineOut(BaseModel):
    id: int
    item_id: int | None = None
    returned_from_line_id: int | None = None
    description: str = ""
    qty: float = 1
    unit_price: float = 0
    discount: float = 0
    amount: float = 0
    source: str = "opd"
    doctor_id: int | None = None
    fee_type: str = ""
    created_at: datetime | None = None
    is_stock: bool = False
    department: str = ""
    bill_group: str = ""
    sort_order: int = 0

    class Config:
        from_attributes = True


class PaymentOut(BaseModel):
    id: int
    method: str
    amount: float
    notes: str | None = ""
    created_at: datetime | None = None
    is_deposit: bool | None = False

    class Config:
        from_attributes = True


class InvoiceOut(BaseModel):
    id: int
    number: str
    patient_id: int | None
    status: str
    subtotal: float
    discount: float
    refunded: float = 0
    total: float
    paid: float
    balance: float
    kind: str
    admission_id: int | None = None
    doctor_id: int | None = None
    doctor_name: str = ""
    patient_name: str = ""
    patient_uhid: str = ""
    notes: str | None = ""
    created_at: datetime | None = None
    paid_at: datetime | None = None
    credit_at: datetime | None = None
    credit_amount: float | None = 0
    credit_name: str | None = ""
    credit_phone: str | None = ""
    credit_due: date | None = None
    credit_note: str | None = ""
    lines: list[InvoiceLineOut] = []
    payments: list[PaymentOut] = []

    class Config:
        from_attributes = True


class VisitIn(BaseModel):
    patient_id: int
    branch_id: int
    doctor_id: int | None = None
    diagnosis: str = ""
    prescription: str = ""


class LabOrderIn(BaseModel):
    patient_id: int
    branch_id: int
    tests: str
    doctor_id: int | None = None


class ServiceOrderIn(BaseModel):
    patient_id: int
    branch_id: int
    service_type: str
    sku: str | None = None


class QueueIn(BaseModel):
    branch_id: int
    department: str = "OPD"
    patient_id: int | None = None


class InvoiceCreateIn(BaseModel):
    patient_id: int | None = None
    branch_id: int
    kind: str = "opd"
    doctor_id: int | None = None

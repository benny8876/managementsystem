from datetime import date, datetime

from pydantic import BaseModel


class AdmitIn(BaseModel):
    patient_id: int
    branch_id: int
    bed_id: int
    doctor_id: int | None = None
    deposit: float = 0
    billing_mode: str = "daily"


class VitalsIn(BaseModel):
    bp: str = ""
    pulse: str = ""
    temp: str = ""
    spo2: str = ""
    weight: str = ""


class NoteIn(BaseModel):
    note: str


class DischargeIn(BaseModel):
    summary: str = ""
    discharge_type: str = "routine"  # routine | lama | dama | transfer_out | death
    # Cashier override: discharge without doctor approval or with money still owed.
    reason: str = ""
    # Deposit bigger than the bill: keep the extra as hospital income instead of refunding it.
    keep_extra: bool = False


class IpdDepositIn(BaseModel):
    amount: float
    method: str = "deposit"  # deposit | cash | kpay | wave | card
    notes: str = ""


class TransferIn(BaseModel):
    bed_id: int
    reason: str = ""


class RadiologyOrderIn(BaseModel):
    patient_id: int
    branch_id: int
    modality: str = "xray"
    doctor_id: int | None = None


class OTScheduleIn(BaseModel):
    patient_id: int
    branch_id: int
    scheduled_at: datetime
    procedure: str
    surgeon_id: int | None = None


class ExpenseIn(BaseModel):
    branch_id: int
    category: str
    name: str = ""
    amount: float
    paid_from: str = "petty"
    paid_by: str = ""
    notes: str = ""


class ExpenseUpdateIn(BaseModel):
    category: str | None = None
    name: str | None = None
    amount: float | None = None
    paid_from: str | None = None
    notes: str | None = None
    expense_date: date | None = None


class DispenseIn(BaseModel):
    item_id: int
    warehouse_id: int
    qty: float
    patient_id: int | None = None
    ref: str = ""


class WastageIn(BaseModel):
    batch_id: int
    qty: float
    reason: str
    notes: str = ""


class LabReagentTestLinkIn(BaseModel):
    """One lab billable test that consumes this reagent, with qty per test run."""
    test_item_id: int
    qty: float = 1


class MedicineIn(BaseModel):
    name: str
    sku: str = ""
    barcode: str = ""
    price: float = 0
    cost: float = 0
    min_stock: float = 10
    # pharmacy | lab | xray | usg | cashier — stock segments on the Store counter
    department: str = "pharmacy"
    # Cashier items only: sell without tracking stock.
    unlimited: bool = False
    # OT only: "ot" | "otm" (see CatalogItem.bill_group)
    bill_group: str = ""
    unit: str = ""  # tab, box, bottle, ...
    expiry_alert_months: int = 1
    sub_category: str = ""
    lab_tests: list[LabReagentTestLinkIn] = []
    # Optional opening stock, received in the same transaction as the item itself.
    initial_qty: float = 0
    warehouse_id: int | None = None
    batch_no: str = ""
    expiry_date: date | None = None


class MedicineUpdateIn(BaseModel):
    name: str | None = None
    sub_category: str | None = None
    price: float | None = None
    cost: float | None = None
    min_stock: float | None = None
    unit: str | None = None
    expiry_alert_months: int | None = None
    lab_tests: list[LabReagentTestLinkIn] | None = None
    bill_group: str | None = None
    # Cashier / OT items: switch between stock-tracked and unlimited (no stock).
    unlimited: bool | None = None


class WardMedOrderItemIn(BaseModel):
    item_id: int
    qty: float


class WardMedOrderIn(BaseModel):
    patient_id: int
    branch_id: int
    admission_id: int | None = None
    invoice_id: int | None = None
    note: str = ""
    items: list[WardMedOrderItemIn] = []


class AppointmentIn(BaseModel):
    patient_id: int
    doctor_id: int
    branch_id: int
    scheduled_at: datetime
    duration_minutes: int = 15
    source: str = "counter"  # counter | phone | portal
    notes: str = ""


class AppointmentUpdateIn(BaseModel):
    scheduled_at: datetime | None = None
    doctor_id: int | None = None
    duration_minutes: int | None = None
    status: str | None = None  # booked | arrived | done | cancelled
    notes: str | None = None


class StockReceiveIn(BaseModel):
    item_id: int
    warehouse_id: int
    qty: float
    unit_cost: float
    batch_no: str = ""
    expiry_date: date | None = None
    selling_price: float | None = None
    min_stock: float | None = None
    notes: str = ""
    unit: str = ""


class LabTemplateRowIn(BaseModel):
    kind: str = "row"  # section | row
    label: str
    unit: str = ""
    reference_range: str = ""
    remark: str = ""


class LabTemplateIn(BaseModel):
    name: str
    has_unit: bool = True
    has_range: bool = True
    has_remark: bool = True
    rows: list[LabTemplateRowIn] = []

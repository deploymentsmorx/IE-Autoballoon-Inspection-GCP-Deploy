from __future__ import annotations

from pydantic import BaseModel, Field


class Balloon(BaseModel):
    balloon: int
    page: int
    text: str
    x: float
    y: float
    width: float = 20
    height: float = 12
    category: str = ""
    pattern: str = ""
    bx: float | None = None
    by: float | None = None
    tx: float | None = None
    ty: float | None = None
    arrow_size: float | None = None


class BalloonUpdate(BaseModel):
    balloons: list[Balloon]


class MeasureCol(BaseModel):
    id: str
    label: str
    values: dict[str, str] = Field(default_factory=dict)


class ReportRow(BaseModel):
    sno: int
    balloon: int
    reference: str = ""
    nominal: str = ""
    tol_low: str = ""
    tol_high: str = ""
    instrument: str = ""
    unit: str = "mm"
    instr_id: str = ""
    measured: list[str] = Field(default_factory=lambda: ["", "", ""])
    remarks: str = ""


class ReportPayload(BaseModel):
    part_number: str = ""
    part_name: str = ""
    revision: str = ""
    material: str = ""
    mass: str = ""
    finish: str = ""
    serial_number: str = ""
    report_date: str = ""
    company_name: str = ""
    supplier_name: str = ""
    customer_name: str = ""
    authorized_signatory: str = ""
    rows: list[ReportRow] = Field(default_factory=list)
    measure_columns: list[MeasureCol] = Field(default_factory=list)


class ReportRowsChunk(BaseModel):
    """Partial row upload so large reports do not freeze the desktop UI."""
    rows: list[ReportRow]
    offset: int = 0
    replace: bool = False
    measure_columns: list[MeasureCol] | None = None
    done: bool = False

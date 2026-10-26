from __future__ import annotations

import os
import re
import sqlite3
from contextlib import asynccontextmanager, closing
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, HTTPException, Query, Response, status
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator


ROOT = Path(__file__).resolve().parent.parent
DB_PATH = Path(os.getenv("DATABASE_PATH", ROOT / "data" / "payments.db"))
STATIC_DIR = ROOT / "static"
MONEY = Field(gt=0, max_digits=12, decimal_places=2)


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DB_PATH, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def initialize_database() -> None:
    with closing(connect()) as connection, connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS students (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                phone TEXT NOT NULL,
                group_name TEXT NOT NULL,
                monthly_fee TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                student_id INTEGER NOT NULL REFERENCES students(id) ON DELETE CASCADE,
                amount TEXT NOT NULL,
                paid_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_payments_student_date
                ON payments(student_id, paid_at);
            """
        )


@asynccontextmanager
async def lifespan(_: FastAPI):
    initialize_database()
    yield


app = FastAPI(
    title="Ecomdev — учебный центр",
    description="REST API для учёта учеников, оплат и задолженностей.",
    version="1.0.0",
    lifespan=lifespan,
)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


class StudentInput(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    phone: str = Field(min_length=3, max_length=40)
    group_name: str = Field(min_length=1, max_length=100)
    monthly_fee: Annotated[Decimal, MONEY]

    @field_validator("name", "phone", "group_name")
    @classmethod
    def trim_required_strings(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Field cannot be empty")
        return value

    @field_validator("monthly_fee")
    @classmethod
    def fee_must_not_be_null(cls, value: Decimal | None) -> Decimal:
        if value is None:
            raise ValueError("monthly_fee cannot be null")
        return value


class StudentPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    phone: str | None = Field(default=None, min_length=3, max_length=40)
    group_name: str | None = Field(default=None, min_length=1, max_length=100)
    monthly_fee: Annotated[Decimal, MONEY] | None = None

    @field_validator("name", "phone", "group_name")
    @classmethod
    def trim_optional_strings(cls, value: str | None) -> str | None:
        if value is None:
            raise ValueError("Field cannot be null")
        value = value.strip()
        if not value:
            raise ValueError("Field cannot be empty")
        return value

    @field_validator("monthly_fee")
    @classmethod
    def fee_cannot_be_null(cls, value: Decimal | None) -> Decimal:
        if value is None:
            raise ValueError("monthly_fee cannot be null")
        return value


class PaymentInput(BaseModel):
    student_id: int = Field(gt=0)
    amount: Annotated[Decimal, MONEY]
    paid_at: date = Field(default_factory=date.today)


class StudentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    phone: str
    group_name: str
    monthly_fee: Decimal


class PaymentOut(BaseModel):
    id: int
    student_id: int
    amount: Decimal
    paid_at: date


class DebtorOut(BaseModel):
    student_id: int
    name: str
    phone: str
    group_name: str
    monthly_fee: Decimal
    paid: Decimal
    debt: Decimal


def student_from_row(row: sqlite3.Row) -> dict:
    return {**dict(row), "monthly_fee": Decimal(row["monthly_fee"])}


def payment_from_row(row: sqlite3.Row) -> dict:
    return {**dict(row), "amount": Decimal(row["amount"])}


def require_student(connection: sqlite3.Connection, student_id: int) -> sqlite3.Row:
    row = connection.execute("SELECT * FROM students WHERE id = ?", (student_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Student not found")
    return row


@app.get("/", include_in_schema=False)
def home() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.post("/students", response_model=StudentOut, status_code=status.HTTP_201_CREATED)
def create_student(payload: StudentInput) -> dict:
    with closing(connect()) as connection, connection:
        cursor = connection.execute(
            "INSERT INTO students(name, phone, group_name, monthly_fee) VALUES (?, ?, ?, ?)",
            (payload.name, payload.phone, payload.group_name, str(payload.monthly_fee)),
        )
        row = connection.execute("SELECT * FROM students WHERE id = ?", (cursor.lastrowid,)).fetchone()
        return student_from_row(row)


@app.get("/students", response_model=list[StudentOut])
def list_students() -> list[dict]:
    with closing(connect()) as connection, connection:
        rows = connection.execute("SELECT * FROM students ORDER BY name COLLATE NOCASE, id").fetchall()
        return [student_from_row(row) for row in rows]


@app.get("/students/{student_id}", response_model=StudentOut)
def get_student(student_id: int) -> dict:
    with closing(connect()) as connection, connection:
        return student_from_row(require_student(connection, student_id))


@app.patch("/students/{student_id}", response_model=StudentOut)
def update_student(student_id: int, payload: StudentPatch) -> dict:
    changes = payload.model_dump(exclude_unset=True)
    if not changes:
        raise HTTPException(status_code=422, detail="At least one field must be provided")
    for key in ("name", "phone", "group_name"):
        if key in changes:
            changes[key] = changes[key].strip()
            if not changes[key]:
                raise HTTPException(status_code=422, detail=f"{key} cannot be empty")
    if "monthly_fee" in changes and changes["monthly_fee"] is not None:
        changes["monthly_fee"] = str(changes["monthly_fee"])
    columns = ", ".join(f"{key} = ?" for key in changes)
    values = [*changes.values(), student_id]
    with closing(connect()) as connection, connection:
        require_student(connection, student_id)
        connection.execute(f"UPDATE students SET {columns} WHERE id = ?", values)
        return student_from_row(require_student(connection, student_id))


@app.delete("/students/{student_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_student(student_id: int) -> Response:
    with closing(connect()) as connection, connection:
        require_student(connection, student_id)
        connection.execute("DELETE FROM students WHERE id = ?", (student_id,))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.post("/payments", response_model=PaymentOut, status_code=status.HTTP_201_CREATED)
def create_payment(payload: PaymentInput) -> dict:
    with closing(connect()) as connection, connection:
        require_student(connection, payload.student_id)
        cursor = connection.execute(
            "INSERT INTO payments(student_id, amount, paid_at) VALUES (?, ?, ?)",
            (payload.student_id, str(payload.amount), payload.paid_at.isoformat()),
        )
        row = connection.execute("SELECT * FROM payments WHERE id = ?", (cursor.lastrowid,)).fetchone()
        return payment_from_row(row)


@app.get("/students/{student_id}/payments", response_model=list[PaymentOut])
def list_student_payments(student_id: int) -> list[dict]:
    with closing(connect()) as connection, connection:
        require_student(connection, student_id)
        rows = connection.execute(
            "SELECT * FROM payments WHERE student_id = ? ORDER BY paid_at DESC, id DESC", (student_id,)
        ).fetchall()
        return [payment_from_row(row) for row in rows]


@app.get("/debtors", response_model=list[DebtorOut])
def list_debtors(month: str | None = Query(default=None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$")) -> list[dict]:
    selected_month = month if month is not None else date.today().strftime("%Y-%m")
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", selected_month):
        raise HTTPException(status_code=422, detail="month must use YYYY-MM format")
    year, month_number = map(int, selected_month.split("-"))
    if not 1 <= month_number <= 12:
        raise HTTPException(status_code=422, detail="month must use YYYY-MM format")
    next_year, next_month = (year + 1, 1) if month_number == 12 else (year, month_number + 1)
    start = f"{year:04d}-{month_number:02d}-01"
    end = f"{next_year:04d}-{next_month:02d}-01"
    with closing(connect()) as connection, connection:
        rows = connection.execute("SELECT * FROM students ORDER BY name COLLATE NOCASE, id").fetchall()
        payment_rows = connection.execute(
            "SELECT student_id, amount FROM payments WHERE paid_at >= ? AND paid_at < ?", (start, end)
        ).fetchall()
    paid_by_student: dict[int, Decimal] = {}
    for payment in payment_rows:
        student_id = payment["student_id"]
        paid_by_student[student_id] = paid_by_student.get(student_id, Decimal("0")) + Decimal(payment["amount"])
    debtors = []
    for row in rows:
        fee = Decimal(row["monthly_fee"])
        paid = paid_by_student.get(row["id"], Decimal("0"))
        debt = max(Decimal("0"), fee - paid)
        if debt > 0:
            debtors.append({
                "student_id": row["id"], "name": row["name"], "phone": row["phone"],
                "group_name": row["group_name"], "monthly_fee": fee, "paid": paid, "debt": debt,
            })
    return debtors


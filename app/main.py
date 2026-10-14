"""
Employee Attendance & Analytics API - STARTER

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)

This file was written quickly by a colleague who has left the company. The happy path
works, but nobody has reviewed it. Read PROBLEM_STATEMENT.docx for what is expected of you,
openapi.yaml for the contract and DATA_MODEL.md for what is stored in MongoDB.
"""
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Literal, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError

load_dotenv()

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"))
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


@app.on_event("startup")
def init_indexes():
    # employees collection indexes
    db.employees.create_index("emp_code", unique=True)
    db.employees.create_index("department")

    # attendance_logs collection indexes
    db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
    db.attendance_logs.create_index("date")
    db.attendance_logs.create_index("status")
    db.attendance_logs.create_index([("date", -1), ("emp_code", 1)])


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def ensure_ist(dt: datetime) -> datetime:
    return dt.replace(tzinfo=IST) if dt.tzinfo is None else dt.astimezone(IST)


def format_attendance_doc(d: dict) -> dict:
    for field in ("punch_in", "punch_out"):
        val = d.get(field)
        if isinstance(val, datetime):
            if val.tzinfo is None:
                val = val.replace(tzinfo=timezone.utc)
            d[field] = int(val.timestamp() * 1000)
    d.setdefault("work_hours", None)
    d.setdefault("late_minutes", 0)
    d.setdefault("overtime_minutes", 0)
    d.setdefault("half_day", False)
    history = d.get("history") or []
    for h in history:
        if isinstance(h.get("at"), datetime):
            hat = h["at"]
            if hat.tzinfo is None:
                hat = hat.replace(tzinfo=timezone.utc)
            h["at"] = int(hat.timestamp() * 1000)
        changes = h.get("changes") or {}
        for f in ("punch_in", "punch_out"):
            if f in changes:
                ch = changes[f]
                for k in ("from", "to"):
                    c_val = ch.get(k)
                    if isinstance(c_val, datetime):
                        if c_val.tzinfo is None:
                            c_val = c_val.replace(tzinfo=timezone.utc)
                        ch[k] = int(c_val.timestamp() * 1000)
    d["history"] = history
    d.pop("_id", None)
    return d


def compute_late_minutes(punch_in: datetime, shift_start: str, date_str: Optional[str] = None) -> int:
    punch_in = ensure_ist(punch_in)
    h, m = map(int, shift_start.split(":"))
    if date_str:
        start = datetime.fromisoformat(date_str).replace(hour=h, minute=m, second=0, microsecond=0, tzinfo=IST)
    else:
        start = punch_in.replace(hour=h, minute=m, second=0, microsecond=0)
        if (punch_in - start).total_seconds() < -12 * 3600:
            start -= timedelta(days=1)
    total_seconds = (punch_in - start).total_seconds()
    if total_seconds <= 600:
        return 0
    return int(total_seconds / 60)


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    t1 = ensure_ist(punch_in)
    t2 = ensure_ist(punch_out)
    total_seconds = Decimal(str((t2 - t1).total_seconds()))
    hours = total_seconds / Decimal(3600)
    return float(hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_overtime(
    punch_out: datetime, shift_end: str, date_str: str, shift_start: Optional[str] = None
) -> int:
    punch_out = ensure_ist(punch_out)
    h, m = map(int, shift_end.split(":"))
    end = datetime.fromisoformat(date_str).replace(hour=h, minute=m, second=0, microsecond=0, tzinfo=IST)
    if (shift_start and shift_end <= shift_start) or (shift_start is None and punch_out.date() > end.date()):
        end += timedelta(days=1)
    minutes = int((punch_out - end).total_seconds() / 60)
    return minutes if minutes >= 30 else 0


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    emp_code: str
    name: str
    email: str
    department: str
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(default=None, ge=100_000_000_000, le=4_102_444_800_000)
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


# --------------------------------------------------------------------------- #
# Endpoints provided
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except Exception:
        raise HTTPException(status_code=503, detail="Database unavailable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = datetime.now(timezone.utc)
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    doc.pop("_id", None)
    return doc


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: int = 1, page_size: int = 20):
    q = {}
    if department:
        q["department"] = department
    skip = (page - 1) * page_size
    total = db.employees.count_documents(q)
    items = list(db.employees.find(q, {"_id": 0}).sort("emp_code", 1).skip(skip).limit(page_size))
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    ts = (
        datetime.fromtimestamp(body.punched_at / 1000, tz=timezone.utc)
        if body.punched_at
        else datetime.now(timezone.utc)
    ).replace(microsecond=0)
    ts_ist = ts.astimezone(IST)

    shift_start = emp.get("shift_start", "09:30")
    shift_end = emp.get("shift_end", "18:30")
    end_h, end_m = map(int, shift_end.split(":"))

    # R1: Overnight shift (shift_end <= shift_start): punch-in earlier than shift_end belongs to previous day
    if shift_end <= shift_start and (ts_ist.hour, ts_ist.minute) < (end_h, end_m):
        d = (ts_ist.date() - timedelta(days=1)).isoformat()
    else:
        d = ts_ist.date().isoformat()

    late_mins = compute_late_minutes(ts_ist, shift_start, d)

    doc = {
        "emp_code": body.emp_code,
        "date": d,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_mins,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="already punched in for this date")

    # Contract: no _id/id and punch_in as epoch milliseconds
    doc.pop("_id", None)
    doc["punch_in"] = int(ts.timestamp() * 1000)
    return doc


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    p = page if isinstance(page, int) else getattr(page, "default", 1)
    ps = page_size if isinstance(page_size, int) else getattr(page_size, "default", 20)

    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from must be on or before date_to")

    q = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status

    skip = (p - 1) * ps
    total = db.attendance_logs.count_documents(q)
    cursor = (
        db.attendance_logs.find(q, {"_id": 0})
        .sort([("date", -1), ("emp_code", 1)])
        .skip(skip)
        .limit(ps)
    )
    items = [format_attendance_doc(d) for d in cursor]
    return {"items": items, "total": total, "page": p, "page_size": ps}


# --------------------------------------------------------------------------- #
# TODO - the rest of the contract (see openapi.yaml):
#   POST  /attendance/punch-out
#   PATCH /attendance/{emp_code}/{date}
#   GET   /analytics/employees/{emp_code}/monthly
#   GET   /analytics/departments/summary
#   GET   /analytics/leaderboard/late
#   GET   /analytics/departments/{department}/trend
#   GET   /admin/explain/{endpoint}
# --------------------------------------------------------------------------- #

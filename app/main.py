"""
Employee Attendance & Analytics API - STARTER

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)

This file was written quickly by a colleague who has left the company. The happy path
works, but nobody has reviewed it. Read PROBLEM_STATEMENT.docx for what is expected of you,
openapi.yaml for the contract and DATA_MODEL.md for what is stored in MongoDB.
"""
import calendar
import os
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Literal, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field
from pymongo import MongoClient, ReturnDocument
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
    db.attendance_logs.create_index([("emp_code", 1), ("punch_in", -1)])


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def ensure_ist(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(IST)


def dt_to_epoch_ms(dt: Optional[datetime]) -> Optional[int]:
    if not isinstance(dt, datetime):
        return dt
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def parse_epoch_ms(ms: Optional[int]) -> datetime:
    if ms is None:
        return datetime.now(timezone.utc).replace(microsecond=0)
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).replace(microsecond=0)


def get_shift_datetime(date_str: str, time_str: str, next_day: bool = False) -> datetime:
    h, m = map(int, time_str.split(":"))
    dt = datetime.fromisoformat(date_str).replace(hour=h, minute=m, second=0, microsecond=0, tzinfo=IST)
    if next_day:
        dt += timedelta(days=1)
    return dt


def determine_attendance_date(ts_ist: datetime, shift_start: str, shift_end: str) -> str:
    end_h, end_m = map(int, shift_end.split(":"))
    if shift_end <= shift_start and (ts_ist.hour, ts_ist.minute) < (end_h, end_m):
        return (ts_ist.date() - timedelta(days=1)).isoformat()
    return ts_ist.date().isoformat()


def format_attendance_doc(d: dict) -> dict:
    for field in ("punch_in", "punch_out"):
        if field in d:
            d[field] = dt_to_epoch_ms(d[field])
    d.setdefault("work_hours", None)
    d.setdefault("late_minutes", 0)
    d.setdefault("overtime_minutes", 0)
    d.setdefault("half_day", False)
    history = d.get("history") or []
    for h in history:
        if "at" in h:
            h["at"] = dt_to_epoch_ms(h["at"])
        changes = h.get("changes") or {}
        for f in ("punch_in", "punch_out"):
            if f in changes:
                ch = changes[f]
                for k in ("from", "to"):
                    if k in ch:
                        ch[k] = dt_to_epoch_ms(ch[k])
    d["history"] = history
    d.pop("_id", None)
    return d


def compute_late_minutes(punch_in: datetime, shift_start: str, date_str: Optional[str] = None) -> int:
    punch_in = ensure_ist(punch_in)
    if date_str:
        start = get_shift_datetime(date_str, shift_start)
    else:
        h, m = map(int, shift_start.split(":"))
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
    total_seconds = Decimal(str(int((t2 - t1).total_seconds())))
    hours = total_seconds / Decimal(3600)
    return float(hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_overtime(
    punch_out: datetime, shift_end: str, date_str: str, shift_start: Optional[str] = None
) -> int:
    punch_out = ensure_ist(punch_out)
    is_overnight = shift_start and shift_end <= shift_start
    end = get_shift_datetime(date_str, shift_end, next_day=bool(is_overnight))
    if not shift_start and punch_out.date() > end.date():
        end += timedelta(days=1)
    minutes = int((punch_out - end).total_seconds() / 60)
    return minutes if minutes >= 30 else 0


def compute_derived_attendance(
    punch_in: Optional[datetime],
    punch_out: Optional[datetime],
    shift_start: str,
    shift_end: str,
    date_str: str,
) -> dict:
    late_mins = compute_late_minutes(punch_in, shift_start, date_str) if punch_in else 0
    if not punch_in or not punch_out:
        return {
            "late_minutes": late_mins,
            "work_hours": None,
            "overtime_minutes": 0,
            "half_day": False,
        }
    wh = compute_work_hours(punch_in, punch_out)
    ot = compute_overtime(punch_out, shift_end, date_str, shift_start)
    return {
        "late_minutes": late_mins,
        "work_hours": wh,
        "overtime_minutes": ot,
        "half_day": wh < 4.50,
    }


def compute_working_days(joined_on_str: str, month_str: str) -> int:
    year, mon = map(int, month_str.split("-"))
    _, num_days = calendar.monthrange(year, mon)
    first_day = date(year, mon, 1)
    last_day = date(year, mon, num_days)

    joined_on = date.fromisoformat(joined_on_str)
    if joined_on > last_day:
        return 0

    cur = max(first_day, joined_on)
    count = 0
    while cur <= last_day:
        if cur.weekday() < 5:
            count += 1
        cur += timedelta(days=1)
    return count


def build_employee_monthly_pipeline(emp_code: str, month_str: str) -> list[dict]:
    year, mon = map(int, month_str.split("-"))
    _, num_days = calendar.monthrange(year, mon)
    start_date = f"{month_str}-01"
    end_date = f"{month_str}-{num_days:02d}"

    return [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": start_date, "$lte": end_date},
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {
                                        "$lte": [
                                            {
                                                "$isoDayOfWeek": {
                                                    "$dateFromString": {"dateString": "$date"}
                                                }
                                            },
                                            5,
                                        ]
                                    },
                                ]
                            },
                            {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {
                        "$cond": [
                            {"$eq": ["$status", "LEAVE"]},
                            1,
                            0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
                            1,
                            0,
                        ]
                    }
                },
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
            }
        },
    ]


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


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(default=None, ge=100_000_000_000, le=4_102_444_800_000)


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

    ts = parse_epoch_ms(body.punched_at)
    ts_ist = ts.astimezone(IST)

    shift_start = emp.get("shift_start", "09:30")
    shift_end = emp.get("shift_end", "18:30")
    d = determine_attendance_date(ts_ist, shift_start, shift_end)
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

    return format_attendance_doc(doc)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
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

    skip = (page - 1) * page_size
    total = db.attendance_logs.count_documents(q)
    cursor = (
        db.attendance_logs.find(q, {"_id": 0})
        .sort([("date", -1), ("emp_code", 1)])
        .skip(skip)
        .limit(page_size)
    )
    items = [format_attendance_doc(d) for d in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-out", status_code=200)
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    ts = parse_epoch_ms(body.punched_at)

    latest = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$ne": None}},
        sort=[("punch_in", -1)],
    )
    if not latest:
        raise HTTPException(status_code=404, detail="No punch-in found")

    record_in = latest["punch_in"]
    if record_in.tzinfo is None:
        record_in = record_in.replace(tzinfo=timezone.utc)

    if ts <= record_in:
        raise HTTPException(status_code=422, detail="punched_at must be strictly after punch_in")

    if (ts - record_in).total_seconds() > 24 * 3600:
        raise HTTPException(status_code=422, detail="punched_at cannot be more than 24 hours after punch_in")

    if latest.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="Attendance record is already punched out")

    shift_start = emp.get("shift_start", "09:30")
    shift_end = emp.get("shift_end", "18:30")
    derived = compute_derived_attendance(
        record_in,
        ts,
        shift_start,
        shift_end,
        latest["date"],
    )

    updated = db.attendance_logs.find_one_and_update(
        {
            "_id": latest["_id"],
            "punch_out": None,
        },
        {
            "$set": {
                "punch_out": ts,
                "work_hours": derived["work_hours"],
                "overtime_minutes": derived["overtime_minutes"],
                "half_day": derived["half_day"],
            }
        },
        return_document=ReturnDocument.AFTER,
    )

    if not updated:
        raise HTTPException(status_code=409, detail="Attendance record is already punched out")

    return format_attendance_doc(updated)


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str,
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    pipeline = build_employee_monthly_pipeline(emp_code, month)
    res = list(db.attendance_logs.aggregate(pipeline))

    if res:
        data = res[0]
        present_days = float(data.get("present_days", 0.0))
        leave_days = int(data.get("leave_days", 0))
        late_count = int(data.get("late_count", 0))
        total_late_minutes = int(data.get("total_late_minutes", 0))
        total_overtime_minutes = int(data.get("total_overtime_minutes", 0))
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late_minutes = 0
        total_overtime_minutes = 0

    working_days = compute_working_days(emp["joined_on"], month)

    if working_days > 0:
        raw_pct = (Decimal(str(present_days)) / Decimal(working_days)) * Decimal(100)
        attendance_pct = float(raw_pct.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    else:
        attendance_pct = None

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late_minutes,
        "total_overtime_minutes": total_overtime_minutes,
        "attendance_pct": attendance_pct,
    }


# --------------------------------------------------------------------------- #
# TODO - the rest of the contract (see openapi.yaml):
#   PATCH /attendance/{emp_code}/{date}
#   GET   /analytics/departments/summary
#   GET   /analytics/leaderboard/late
#   GET   /analytics/departments/{department}/trend
#   GET   /admin/explain/{endpoint}
# --------------------------------------------------------------------------- #

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
from fastapi import FastAPI, HTTPException, Path, Query
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, Field, model_validator
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
    db.employees.create_index("joined_on")
    db.employees.create_index([("department", 1), ("joined_on", 1)])

    # attendance_logs collection indexes
    db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
    db.attendance_logs.create_index("date")
    db.attendance_logs.create_index("status")
    db.attendance_logs.create_index([("date", -1), ("emp_code", 1)])
    db.attendance_logs.create_index([("emp_code", 1), ("punch_in", -1)])
    db.attendance_logs.create_index([("date", 1), ("late_minutes", 1), ("emp_code", 1)])


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


def format_employee_doc(d: dict) -> dict:
    if "created_at" in d:
        d["created_at"] = dt_to_epoch_ms(d["created_at"])
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


def build_department_summary_pipeline(month_str: str, department: Optional[str] = None) -> list[dict]:
    year, mon = map(int, month_str.split("-"))
    _, num_days = calendar.monthrange(year, mon)
    start_date = f"{month_str}-01"
    end_date = f"{month_str}-{num_days:02d}"

    match_filter = {"joined_on": {"$lte": end_date}}
    if department:
        match_filter["department"] = department

    return [
        {"$match": match_filter},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"emp": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$emp"]},
                                    {"$gte": ["$date", start_date]},
                                    {"$lte": ["$date", end_date]},
                                ]
                            }
                        }
                    }
                ],
                "as": "logs",
            }
        },
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {
            "$group": {
                "_id": "$department",
                "employees": {"$addToSet": "$emp_code"},
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {
                                        "$lte": [
                                            {
                                                "$isoDayOfWeek": {
                                                    "$dateFromString": {"dateString": "$logs.date"}
                                                }
                                            },
                                            5,
                                        ]
                                    },
                                ]
                            },
                            {"$cond": [{"$ifNull": ["$logs.half_day", False]}, 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]},
                            1,
                            0,
                        ]
                    }
                },
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$logs.late_minutes", 0]}
                },
                "leave_count": {
                    "$sum": {
                        "$cond": [
                            {"$eq": ["$logs.status", "LEAVE"]},
                            1,
                            0,
                        ]
                    }
                },
                "on_duty_count": {
                    "$sum": {
                        "$cond": [
                            {"$eq": ["$logs.status", "ON_DUTY"]},
                            1,
                            0,
                        ]
                    }
                },
                "sum_work_hours": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$ne": ["$logs.work_hours", None]},
                                ]
                            },
                            "$logs.work_hours",
                            0.0,
                        ]
                    }
                },
                "count_work_hours": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$ne": ["$logs.work_hours", None]},
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },
            }
        },
        {
            "$project": {
                "department": "$_id",
                "headcount": {"$size": "$employees"},
                "present_days": 1,
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
                "avg_work_hours": {
                    "$cond": [
                        {"$gt": ["$count_work_hours", 0]},
                        {"$divide": ["$sum_work_hours", "$count_work_hours"]},
                        None,
                    ]
                },
            }
        },
        {"$sort": {"department": 1}},
    ]


def build_late_leaderboard_pipeline(
    month_str: str, limit: int = 10, department: Optional[str] = None
) -> list[dict]:
    year, mon = map(int, month_str.split("-"))
    _, num_days = calendar.monthrange(year, mon)
    start_date = f"{month_str}-01"
    end_date = f"{month_str}-{num_days:02d}"

    pipeline = [
        {
            "$match": {
                "date": {"$gte": start_date, "$lte": end_date},
                "late_minutes": {"$gt": 0},
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
    ]

    if department:
        pipeline.append({"$match": {"emp.department": department}})

    pipeline.extend([
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {
                    "rank": {"$rank": {}}
                }
            }
        },
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
    ])
    return pipeline


def build_department_trend_pipeline(department: str, from_str: str, to_str: str) -> list[dict]:
    d_from = date.fromisoformat(from_str)
    d_to = date.fromisoformat(to_str)
    from_dt = datetime(d_from.year, d_from.month, d_from.day, 0, 0, tzinfo=timezone.utc)
    to_dt_plus_1 = datetime(d_to.year, d_to.month, d_to.day, 0, 0, tzinfo=timezone.utc) + timedelta(days=1)

    return [
        {
            "$match": {
                "date": {"$gte": from_str, "$lte": to_str}
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "emp"
            }
        },
        {"$unwind": "$emp"},
        {"$match": {"emp.department": department}},
        {
            "$group": {
                "_id": "$date",
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1.0]},
                            0.0
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
                            1,
                            0
                        ]
                    }
                }
            }
        },
        {
            "$project": {
                "date_str": "$_id",
                "date_dt": {"$dateFromString": {"dateString": "$_id"}},
                "present_count": 1,
                "late_count": 1,
            }
        },
        {
            "$densify": {
                "field": "date_dt",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [from_dt, to_dt_plus_1]
                }
            }
        },
        {
            "$project": {
                "date_dt": 1,
                "date": {
                    "$dateToString": {"format": "%Y-%m-%d", "date": "$date_dt"}
                },
                "present_count": {"$ifNull": ["$present_count", 0.0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "pipeline": [
                    {"$match": {"department": department}},
                    {"$project": {"_id": 0, "joined_on": 1}}
                ],
                "as": "dept_emps"
            }
        },
        {
            "$project": {
                "date": 1,
                "date_dt": 1,
                "is_working_day": {
                    "$lte": [{"$isoDayOfWeek": "$date_dt"}, 5]
                },
                "present_count": 1,
                "late_count": 1,
                "headcount": {
                    "$size": {
                        "$filter": {
                            "input": "$dept_emps",
                            "as": "e",
                            "cond": {"$lte": ["$$e.joined_on", "$date"]}
                        }
                    }
                }
            }
        },
        {
            "$project": {
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                {"$eq": ["$is_working_day", True]},
                                {"$gt": ["$headcount", 0]}
                            ]
                        },
                        {"$round": [{"$divide": ["$present_count", "$headcount"]}, 4]},
                        None
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {
                            "documents": [-6, "current"]
                        }
                    }
                }
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": {
                    "$cond": [
                        {"$ne": ["$moving_avg_7d", None]},
                        {"$round": ["$moving_avg_7d", 4]},
                        None
                    ]
                }
            }
        },
        {"$sort": {"date": 1}}
    ]


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$")

    @model_validator(mode="after")
    def validate_shift(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(default=None, ge=100_000_000_000, le=4_102_444_800_000)
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(default=None, ge=100_000_000_000, le=4_102_444_800_000)


class RegularizeIn(BaseModel):
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None
    punch_in: Optional[int] = Field(default=None, ge=100_000_000_000, le=4_102_444_800_000)
    punch_out: Optional[int] = Field(default=None, ge=100_000_000_000, le=4_102_444_800_000)


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
    return format_employee_doc(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    q = {}
    if department:
        q["department"] = department
    skip = (page - 1) * page_size
    total = db.employees.count_documents(q)
    cursor = db.employees.find(q, {"_id": 0}).sort("emp_code", 1).skip(skip).limit(page_size)
    items = [format_employee_doc(d) for d in cursor]
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
    date_from: Optional[str] = Query(default=None, pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    date_to: Optional[str] = Query(default=None, pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
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


@app.patch("/attendance/{emp_code}/{date}", status_code=200)
def regularize_attendance(
    emp_code: str = Path(...),
    date: str = Path(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    body: RegularizeIn = ...,
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not record:
        raise HTTPException(status_code=404, detail="Attendance record not found for this date")

    old_status = record.get("status")
    old_punch_in = record.get("punch_in")
    old_punch_out = record.get("punch_out")
    if isinstance(old_punch_in, datetime) and old_punch_in.tzinfo is None:
        old_punch_in = old_punch_in.replace(tzinfo=timezone.utc)
    if isinstance(old_punch_out, datetime) and old_punch_out.tzinfo is None:
        old_punch_out = old_punch_out.replace(tzinfo=timezone.utc)

    old_late_minutes = record.get("late_minutes", 0)
    old_work_hours = record.get("work_hours", None)
    old_overtime_minutes = record.get("overtime_minutes", 0)
    old_half_day = record.get("half_day", False)

    new_status = body.status if body.status is not None else old_status

    # Rule 1: setting status ABSENT or LEAVE clears both punch times,
    # so supplying punch_in or punch_out with it is 422
    if body.status in ("ABSENT", "LEAVE") and (body.punch_in is not None or body.punch_out is not None):
        raise HTTPException(
            status_code=422,
            detail="Supplying punch_in or punch_out when setting status ABSENT or LEAVE is not allowed",
        )
    if new_status in ("ABSENT", "LEAVE") and (body.punch_in is not None or body.punch_out is not None):
        raise HTTPException(
            status_code=422,
            detail="Punch times not allowed for ABSENT or LEAVE status",
        )

    if new_status in ("ABSENT", "LEAVE"):
        new_punch_in = None
        new_punch_out = None
    else:
        new_punch_in = parse_epoch_ms(body.punch_in) if body.punch_in is not None else old_punch_in
        new_punch_out = parse_epoch_ms(body.punch_out) if body.punch_out is not None else old_punch_out

    # Rule 2: a presence status requires a punch_in (supplied or already stored)
    if new_status in ("PRESENT", "WFH", "ON_DUTY") and new_punch_in is None:
        raise HTTPException(
            status_code=422,
            detail="Presence status requires a punch_in",
        )

    shift_start = emp.get("shift_start", "09:30")
    shift_end = emp.get("shift_end", "18:30")

    # Rule 3: punch_in must stay on the record's attendance date (R1)
    if new_punch_in is not None:
        punch_in_ist = new_punch_in.astimezone(IST)
        calc_date = determine_attendance_date(punch_in_ist, shift_start, shift_end)
        if calc_date != date:
            raise HTTPException(
                status_code=422,
                detail="punch_in must stay on the record's attendance date",
            )

    # Rule 4: punch_out must be after punch_in and within 24 hours
    if new_punch_out is not None:
        if new_punch_in is None or new_punch_out <= new_punch_in:
            raise HTTPException(
                status_code=422,
                detail="punch_out must be strictly after punch_in",
            )
        if (new_punch_out - new_punch_in).total_seconds() > 24 * 3600:
            raise HTTPException(
                status_code=422,
                detail="punch_out cannot be more than 24 hours after punch_in",
            )

    # Recompute derived fields (R2-R5)
    if new_status in ("ABSENT", "LEAVE"):
        new_late_minutes = 0
        new_work_hours = None
        new_overtime_minutes = 0
        new_half_day = False
    else:
        derived = compute_derived_attendance(
            new_punch_in,
            new_punch_out,
            shift_start,
            shift_end,
            date,
        )
        new_late_minutes = derived["late_minutes"]
        new_work_hours = derived["work_hours"]
        new_overtime_minutes = derived["overtime_minutes"]
        new_half_day = derived["half_day"]

    # Detect actually changed fields
    changes = {}
    if new_status != old_status:
        changes["status"] = {"from": old_status, "to": new_status}
    if new_punch_in != old_punch_in:
        changes["punch_in"] = {"from": old_punch_in, "to": new_punch_in}
    if new_punch_out != old_punch_out:
        changes["punch_out"] = {"from": old_punch_out, "to": new_punch_out}
    if new_work_hours != old_work_hours:
        changes["work_hours"] = {"from": old_work_hours, "to": new_work_hours}
    if new_late_minutes != old_late_minutes:
        changes["late_minutes"] = {"from": old_late_minutes, "to": new_late_minutes}
    if new_overtime_minutes != old_overtime_minutes:
        changes["overtime_minutes"] = {"from": old_overtime_minutes, "to": new_overtime_minutes}
    if new_half_day != old_half_day:
        changes["half_day"] = {"from": old_half_day, "to": new_half_day}

    # Rule 5: a request that changes nothing is 422
    if not changes:
        raise HTTPException(status_code=422, detail="Request changes nothing")

    now_utc = datetime.now(timezone.utc).replace(microsecond=0)
    history_entry = {
        "at": now_utc,
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    # Optimistic concurrency control to prevent losing history
    hist_len = len(record.get("history") or [])
    if "history" not in record or record["history"] is None:
        filter_q = {
            "_id": record["_id"],
            "$or": [{"history": {"$exists": False}}, {"history": {"$size": 0}}],
        }
    else:
        filter_q = {
            "_id": record["_id"],
            "history": {"$size": hist_len},
        }

    update_doc = {
        "$set": {
            "status": new_status,
            "punch_in": new_punch_in,
            "punch_out": new_punch_out,
            "late_minutes": new_late_minutes,
            "work_hours": new_work_hours,
            "overtime_minutes": new_overtime_minutes,
            "half_day": new_half_day,
        },
        "$push": {
            "history": history_entry,
        },
    }

    updated = db.attendance_logs.find_one_and_update(
        filter_q,
        update_doc,
        return_document=ReturnDocument.AFTER,
    )
    if not updated:
        raise HTTPException(
            status_code=409,
            detail="Concurrent regularization detected; please retry with the updated record",
        )

    return format_attendance_doc(updated)


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    pipeline = build_department_summary_pipeline(month, department)
    cursor = db.employees.aggregate(pipeline)

    items = []
    for doc in cursor:
        avg_wh = doc.get("avg_work_hours")
        if avg_wh is not None:
            raw_dec = Decimal(str(avg_wh))
            avg_wh = float(raw_dec.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))

        items.append({
            "department": doc["department"],
            "headcount": int(doc["headcount"]),
            "present_days": float(doc.get("present_days", 0.0)),
            "avg_work_hours": avg_wh,
            "late_count": int(doc.get("late_count", 0)),
            "total_late_minutes": int(doc.get("total_late_minutes", 0)),
            "leave_count": int(doc.get("leave_count", 0)),
            "on_duty_count": int(doc.get("on_duty_count", 0)),
        })

    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(default=10, ge=1, le=50),
    department: Optional[str] = None,
):
    pipeline = build_late_leaderboard_pipeline(month, limit, department)
    items = list(db.attendance_logs.aggregate(pipeline))
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str = Path(...),
    from_date: str = Query(..., alias="from", pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    to_date: str = Query(..., alias="to", pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
):
    d_from = date.fromisoformat(from_date)
    d_to = date.fromisoformat(to_date)
    if d_to < d_from:
        raise HTTPException(status_code=422, detail="'to' date must be on or after 'from' date")
    if (d_to - d_from).days + 1 > 92:
        raise HTTPException(status_code=422, detail="Date range cannot exceed 92 days")

    emp = db.employees.find_one({"department": department})
    if not emp:
        raise HTTPException(status_code=404, detail="Department not found")

    pipeline = build_department_trend_pipeline(department, from_date, to_date)
    cursor = db.attendance_logs.aggregate(pipeline)

    items = []
    for doc in cursor:
        rate = doc.get("attendance_rate")
        if rate is not None:
            raw_dec = Decimal(str(rate))
            rate = float(raw_dec.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))

        mavg = doc.get("moving_avg_7d")
        if mavg is not None:
            raw_dec = Decimal(str(mavg))
            mavg = float(raw_dec.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))

        items.append({
            "date": doc["date"],
            "is_working_day": bool(doc["is_working_day"]),
            "headcount": int(doc["headcount"]),
            "present_count": float(doc.get("present_count", 0.0)),
            "late_count": int(doc.get("late_count", 0)),
            "attendance_rate": rate,
            "moving_avg_7d": mavg,
        })

    return {"department": department, "items": items}


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal[
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
    ] = Path(...),
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(default=None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit: int = Query(default=10, ge=1, le=50),
    date_from: Optional[str] = Query(default=None, pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    date_to: Optional[str] = Query(default=None, pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    from_date: Optional[str] = Query(default=None, alias="from", pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    to_date: Optional[str] = Query(default=None, alias="to", pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    if endpoint == "attendance_list":
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
        sort_spec = [("date", -1), ("emp_code", 1)]
        plan = db.command(
            "explain",
            {
                "find": "attendance_logs",
                "filter": q,
                "projection": {"_id": 0},
                "sort": dict(sort_spec),
                "skip": skip,
                "limit": page_size,
            },
            verbosity="executionStats",
        )
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": jsonable_encoder(plan),
        }

    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(status_code=422, detail="emp_code and month are required for employee_monthly")
        pipeline = build_employee_monthly_pipeline(emp_code, month)
        plan = db.command(
            "explain",
            {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
            },
            verbosity="executionStats",
        )
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": jsonable_encoder(plan),
        }

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(status_code=422, detail="month is required for department_summary")
        pipeline = build_department_summary_pipeline(month, department)
        plan = db.command(
            "explain",
            {
                "aggregate": "employees",
                "pipeline": pipeline,
                "cursor": {},
            },
            verbosity="executionStats",
        )
        return {
            "endpoint": endpoint,
            "collection": "employees",
            "explain": jsonable_encoder(plan),
        }

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(status_code=422, detail="month is required for late_leaderboard")
        pipeline = build_late_leaderboard_pipeline(month, limit, department)
        plan = db.command(
            "explain",
            {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
            },
            verbosity="executionStats",
        )
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": jsonable_encoder(plan),
        }

    elif endpoint == "department_trend":
        if not department:
            raise HTTPException(status_code=422, detail="department is required for department_trend")
        if not from_date or not to_date:
            raise HTTPException(status_code=422, detail="'from' and 'to' dates are required for department_trend")
        d_from = date.fromisoformat(from_date)
        d_to = date.fromisoformat(to_date)
        if d_to < d_from:
            raise HTTPException(status_code=422, detail="'to' date must be on or after 'from' date")
        if (d_to - d_from).days + 1 > 92:
            raise HTTPException(status_code=422, detail="Date range cannot exceed 92 days")
        pipeline = build_department_trend_pipeline(department, from_date, to_date)
        plan = db.command(
            "explain",
            {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
            },
            verbosity="executionStats",
        )
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": jsonable_encoder(plan),
        }

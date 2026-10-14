"""
Employee Attendance & Analytics API - HROne Engineering Assignment
All code in this single file. Runs with:
    uvicorn app.main:app --port 8000
"""
import calendar
import os
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError
from bson import json_util
import json

# Load environment variables (.env is loaded for local convenience; real env vars take precedence)
load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client = MongoClient(MONGO_URI, tz_aware=True)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))
MIN_EPOCH_MS = 100000000000
MAX_EPOCH_MS = 4102444800000


# --------------------------------------------------------------------------- #
# Startup & Index Creation (Idempotent)
# --------------------------------------------------------------------------- #
def init_indexes():
    """Create all required indexes at application startup."""
    try:
        # employees collection
        db.employees.create_index([("emp_code", ASCENDING)], unique=True)
        db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)])
        db.employees.create_index([("joined_on", ASCENDING), ("department", ASCENDING)])
        db.employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)])

        # attendance_logs collection
        db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True)
        db.attendance_logs.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)])
        db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)])
        db.attendance_logs.create_index([("date", ASCENDING), ("emp_code", ASCENDING)])
        db.attendance_logs.create_index([("date", ASCENDING), ("late_minutes", ASCENDING), ("emp_code", ASCENDING)])
    except Exception as e:
        print(f"Index creation warning: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_indexes()
    yield


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan
)


# --------------------------------------------------------------------------- #
# Domain Helpers & Calculations (R1 - R10)
# --------------------------------------------------------------------------- #
def truncate_epoch_ms(ms: int) -> int:
    """Truncates milliseconds to whole seconds (R1)."""
    return (ms // 1000) * 1000


def ms_to_utc_dt(ms: int) -> datetime:
    """Converts epoch ms (truncated to seconds) to tz-aware UTC datetime."""
    truncated = truncate_epoch_ms(ms)
    return datetime.fromtimestamp(truncated / 1000, tz=timezone.utc)


def dt_to_epoch_ms(dt_val: Optional[datetime]) -> Optional[int]:
    """Converts a datetime (or existing ms) to integer epoch ms."""
    if dt_val is None:
        return None
    if isinstance(dt_val, (int, float)):
        return int(dt_val)
    if isinstance(dt_val, datetime):
        if dt_val.tzinfo is None:
            dt_val = dt_val.replace(tzinfo=timezone.utc)
        return int(dt_val.timestamp() * 1000)
    return None


def round_half_up(val: Optional[float], decimals: int = 2) -> Optional[float]:
    """R8 Rounding: exact half-up rounding (not banker's rounding)."""
    if val is None:
        return None
    d = Decimal(str(val))
    pattern = Decimal("0." + "0" * decimals) if decimals > 0 else Decimal("1")
    return float(d.quantize(pattern, rounding=ROUND_HALF_UP))


def calculate_attendance_date(punch_in_dt: datetime, shift_start: str, shift_end: str) -> str:
    """
    R1 Time zone and attendance date.
    Shift times and attendance dates are IST (UTC+05:30).
    A record's date is the IST calendar date of its punch_in instant.
    Overnight shifts (shift_end <= shift_start): punch-in strictly earlier than shift_end
    belongs to the shift that started the previous day.
    """
    dt_ist = punch_in_dt.astimezone(IST)
    is_overnight = shift_end <= shift_start
    sh_end_time = datetime.strptime(shift_end, "%H:%M").time()
    if is_overnight and dt_ist.time() < sh_end_time:
        return (dt_ist.date() - timedelta(days=1)).isoformat()
    return dt_ist.date().isoformat()


def calculate_late_minutes(punch_in_dt: datetime, shift_start: str, att_date_str: str) -> int:
    """
    R2 Late.
    Grace period 10 minutes. Late only if strictly more than 10:00 (600 seconds) after shift_start.
    Then late_minutes = whole minutes (floor) since shift_start, not since grace period ended.
    """
    dt_ist = punch_in_dt.astimezone(IST)
    att_date = date.fromisoformat(att_date_str)
    sh_h, sh_m = map(int, shift_start.split(":"))
    shift_start_dt = datetime(att_date.year, att_date.month, att_date.day, sh_h, sh_m, 0, tzinfo=IST)
    diff_sec = (dt_ist - shift_start_dt).total_seconds()
    if diff_sec > 600:
        return int(diff_sec // 60)
    return 0


def calculate_work_hours(punch_in_dt: datetime, punch_out_dt: datetime) -> float:
    """
    R4 Work hours.
    (punch_out - punch_in) in seconds / 3600, rounded to 2 decimals half-up.
    """
    p_in = punch_in_dt if punch_in_dt.tzinfo else punch_in_dt.replace(tzinfo=timezone.utc)
    p_out = punch_out_dt if punch_out_dt.tzinfo else punch_out_dt.replace(tzinfo=timezone.utc)
    diff_sec = int((p_out - p_in).total_seconds())
    hours = Decimal(diff_sec) / Decimal(3600)
    return float(hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def calculate_half_day(work_hours: Optional[float]) -> bool:
    """
    R5 Half day.
    After punch-out, half_day is true when the rounded work_hours < 4.50.
    """
    if work_hours is None:
        return False
    return bool(work_hours < 4.50)


def calculate_overtime(punch_out_dt: datetime, shift_start: str, shift_end: str, att_date_str: str) -> int:
    """
    R3 Overtime.
    Whole minutes (floor) from shift_end to punch_out, counted only if >= 30; otherwise 0.
    Overnight shifts end on att_date + 1 day.
    """
    att_date = date.fromisoformat(att_date_str)
    is_overnight = shift_end <= shift_start
    end_date = att_date + timedelta(days=1) if is_overnight else att_date
    eh, em = map(int, shift_end.split(":"))
    shift_end_dt = datetime(end_date.year, end_date.month, end_date.day, eh, em, 0, tzinfo=IST)
    punch_out_ist = punch_out_dt.astimezone(IST)
    diff_sec = (punch_out_ist - shift_end_dt).total_seconds()
    if diff_sec > 0:
        minutes = int(diff_sec // 60)
        return minutes if minutes >= 30 else 0
    return 0


def format_employee(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Format employee MongoDB document to OpenAPI Employee schema."""
    created_at_ms = dt_to_epoch_ms(doc.get("created_at"))
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": created_at_ms,
    }


def format_attendance_record(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Format attendance_logs MongoDB document to OpenAPI AttendanceRecord schema."""
    hist = []
    for h in doc.get("history") or []:
        changes = {}
        for k, v in (h.get("changes") or {}).items():
            from_v = v.get("from")
            to_v = v.get("to")
            if k in ("punch_in", "punch_out"):
                changes[k] = {
                    "from": dt_to_epoch_ms(from_v) if from_v is not None else None,
                    "to": dt_to_epoch_ms(to_v) if to_v is not None else None,
                }
            else:
                changes[k] = {"from": from_v, "to": to_v}
        hist.append({
            "at": dt_to_epoch_ms(h.get("at")),
            "by": h.get("by"),
            "reason": h.get("reason"),
            "changes": changes,
        })

    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": dt_to_epoch_ms(doc.get("punch_in")),
        "punch_out": dt_to_epoch_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": bool(doc.get("half_day", False)),
        "history": hist,
    }


# --------------------------------------------------------------------------- #
# Request Schemas (Pydantic)
# --------------------------------------------------------------------------- #
class EmployeeCreate(BaseModel):
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field("18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str

    @field_validator("shift_end")
    @classmethod
    def validate_shift_differs(cls, v: str, info):
        if "shift_start" in info.data and v == info.data["shift_start"]:
            raise ValueError("shift_start must differ from shift_end")
        return v

    @field_validator("joined_on")
    @classmethod
    def validate_joined_on(cls, v: str):
        try:
            date.fromisoformat(v)
        except Exception:
            raise ValueError("joined_on must be YYYY-MM-DD")
        return v


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(None, ge=MIN_EPOCH_MS, le=MAX_EPOCH_MS)
    status: str = Field("PRESENT", pattern=r"^(PRESENT|WFH|ON_DUTY)$")


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(None, ge=MIN_EPOCH_MS, le=MAX_EPOCH_MS)


class RegularizeRequest(BaseModel):
    status: Optional[str] = Field(None, pattern=r"^(PRESENT|ABSENT|LEAVE|WFH|ON_DUTY)$")
    punch_in: Optional[int] = Field(None, ge=MIN_EPOCH_MS, le=MAX_EPOCH_MS)
    punch_out: Optional[int] = Field(None, ge=MIN_EPOCH_MS, le=MAX_EPOCH_MS)
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #

@app.get("/health")
def health():
    """Readiness probe: 200 only when MongoDB answers a ping; else 503."""
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception as e:
        raise HTTPException(status_code=503, detail="Database unavailable")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeCreate):
    """Create an employee: 409 on duplicate emp_code, 422 on invalid input."""
    now_ms = truncate_epoch_ms(int(datetime.now(timezone.utc).timestamp() * 1000))
    created_at_dt = datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc)

    doc = body.model_dump()
    doc["created_at"] = created_at_dt

    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")

    return format_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100)
):
    """Filter by department; paginated; sorted by emp_code ascending."""
    q: Dict[str, Any] = {}
    if department:
        q["department"] = department

    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size
    cursor = db.employees.find(q).sort("emp_code", ASCENDING).skip(skip).limit(page_size)
    items = [format_employee(doc) for doc in cursor]

    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest):
    """Creates the day's record (R1, R2); status PRESENT, WFH, ON_DUTY; race-safe."""
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.punched_at is not None:
        punched_at_ms = truncate_epoch_ms(body.punched_at)
    else:
        punched_at_ms = truncate_epoch_ms(int(datetime.now(timezone.utc).timestamp() * 1000))

    punch_in_dt = datetime.fromtimestamp(punched_at_ms / 1000, tz=timezone.utc)
    att_date = calculate_attendance_date(punch_in_dt, emp["shift_start"], emp["shift_end"])
    late_min = calculate_late_minutes(punch_in_dt, emp["shift_start"], att_date)

    doc = {
        "emp_code": body.emp_code,
        "date": att_date,
        "status": body.status,
        "punch_in": punch_in_dt,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_min,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="already punched in for this date")

    return format_attendance_record(doc)


@app.post("/attendance/punch-out", status_code=200)
def punch_out(body: PunchOutRequest):
    """Closes the employee's most recent record whose punch_in <= punched_at (works across midnight)."""
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.punched_at is not None:
        punched_at_ms = truncate_epoch_ms(body.punched_at)
    else:
        punched_at_ms = truncate_epoch_ms(int(datetime.now(timezone.utc).timestamp() * 1000))

    punch_out_dt = datetime.fromtimestamp(punched_at_ms / 1000, tz=timezone.utc)

    # Find the most recent record whose punch_in <= punch_out_dt
    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": punch_out_dt}},
        sort=[("punch_in", DESCENDING)]
    )
    if not record:
        raise HTTPException(status_code=404, detail="no punch-in found")

    if record.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="record is already punched out")

    punch_in_dt = record["punch_in"]
    if punch_in_dt.tzinfo is None:
        punch_in_dt = punch_in_dt.replace(tzinfo=timezone.utc)

    diff_sec = (punch_out_dt - punch_in_dt).total_seconds()
    if diff_sec <= 0:
        raise HTTPException(status_code=422, detail="punched_at must be strictly greater than punch_in")
    if diff_sec > 24 * 3600:
        raise HTTPException(status_code=422, detail="punched_at cannot be more than 24 hours after punch_in")

    work_hrs = calculate_work_hours(punch_in_dt, punch_out_dt)
    half_d = calculate_half_day(work_hrs)
    ot_min = calculate_overtime(punch_out_dt, emp["shift_start"], emp["shift_end"], record["date"])

    # Atomic update to avoid punch-out race condition
    updated = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": punch_out_dt,
                "work_hours": work_hrs,
                "half_day": half_d,
                "overtime_minutes": ot_min,
            }
        },
        return_document=ReturnDocument.AFTER
    )
    if not updated:
        raise HTTPException(status_code=409, detail="record is already punched out")

    return format_attendance_record(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100)
):
    """Filter by employee / dates / status; paginated; sorted date desc, emp_code asc."""
    date_regex = re.compile(r"^\d{4}-\d{2}-\d{2}$")
    if date_from and not date_regex.match(date_from):
        raise HTTPException(status_code=422, detail="date_from must be YYYY-MM-DD")
    if date_to and not date_regex.match(date_to):
        raise HTTPException(status_code=422, detail="date_to must be YYYY-MM-DD")

    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from cannot be greater than date_to")

    q: Dict[str, Any] = {}
    if emp_code:
        q["emp_code"] = emp_code
    if status:
        q["status"] = status
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to

    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    cursor = db.attendance_logs.find(q).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip(skip).limit(page_size)
    items = [format_attendance_record(doc) for doc in cursor]

    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(
    emp_code: str = Path(...),
    date: str = Path(..., pattern=r"^\d{4}-\d{2}-\d{2}$"),
    body: RegularizeRequest = ...
):
    """Regularize one day's record: recompute derived fields, append to history."""
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not record:
        raise HTTPException(status_code=404, detail="Attendance record not found")

    cur_status = record["status"]
    cur_punch_in = record.get("punch_in")
    cur_punch_out = record.get("punch_out")

    target_status = body.status if body.status is not None else cur_status

    if target_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(status_code=422, detail="Cannot supply punch_in or punch_out when status is ABSENT or LEAVE")
        target_punch_in = None
        target_punch_out = None
    else:
        # Presence status: requires punch_in
        if body.punch_in is not None:
            target_punch_in = ms_to_utc_dt(body.punch_in)
        else:
            target_punch_in = cur_punch_in

        if target_punch_in is None:
            raise HTTPException(status_code=422, detail="A presence status requires a punch_in")

        # punch_in must stay on the record's attendance date per R1
        calc_date = calculate_attendance_date(target_punch_in, emp["shift_start"], emp["shift_end"])
        if calc_date != date:
            raise HTTPException(status_code=422, detail=f"punch_in yields date {calc_date}, which does not match record date {date}")

        if body.punch_out is not None:
            target_punch_out = ms_to_utc_dt(body.punch_out)
        else:
            target_punch_out = cur_punch_out

        if target_punch_out is not None:
            p_in = target_punch_in if target_punch_in.tzinfo else target_punch_in.replace(tzinfo=timezone.utc)
            p_out = target_punch_out if target_punch_out.tzinfo else target_punch_out.replace(tzinfo=timezone.utc)
            diff_sec = (p_out - p_in).total_seconds()
            if diff_sec <= 0:
                raise HTTPException(status_code=422, detail="punch_out must be strictly greater than punch_in")
            if diff_sec > 24 * 3600:
                raise HTTPException(status_code=422, detail="punch_out cannot be more than 24 hours after punch_in")

    # Recompute derived fields
    if target_status in ("ABSENT", "LEAVE"):
        new_work_hours = None
        new_late_minutes = 0
        new_overtime_minutes = 0
        new_half_day = False
    else:
        new_late_minutes = calculate_late_minutes(target_punch_in, emp["shift_start"], date)
        if target_punch_out is not None:
            new_work_hours = calculate_work_hours(target_punch_in, target_punch_out)
            new_half_day = calculate_half_day(new_work_hours)
            new_overtime_minutes = calculate_overtime(target_punch_out, emp["shift_start"], emp["shift_end"], date)
        else:
            new_work_hours = None
            new_half_day = False
            new_overtime_minutes = 0

    # Build changes dictionary
    changes_db: Dict[str, Any] = {}

    if target_status != cur_status:
        changes_db["status"] = {"from": cur_status, "to": target_status}

    if dt_to_epoch_ms(target_punch_in) != dt_to_epoch_ms(cur_punch_in):
        changes_db["punch_in"] = {"from": cur_punch_in, "to": target_punch_in}

    if dt_to_epoch_ms(target_punch_out) != dt_to_epoch_ms(cur_punch_out):
        changes_db["punch_out"] = {"from": cur_punch_out, "to": target_punch_out}

    if new_work_hours != record.get("work_hours"):
        changes_db["work_hours"] = {"from": record.get("work_hours"), "to": new_work_hours}

    if new_late_minutes != record.get("late_minutes", 0):
        changes_db["late_minutes"] = {"from": record.get("late_minutes", 0), "to": new_late_minutes}

    if new_overtime_minutes != record.get("overtime_minutes", 0):
        changes_db["overtime_minutes"] = {"from": record.get("overtime_minutes", 0), "to": new_overtime_minutes}

    if new_half_day != bool(record.get("half_day", False)):
        changes_db["half_day"] = {"from": bool(record.get("half_day", False)), "to": new_half_day}

    if not changes_db:
        raise HTTPException(status_code=422, detail="Request does not change any fields")

    now_ms = truncate_epoch_ms(int(datetime.now(timezone.utc).timestamp() * 1000))
    now_dt = datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc)
    hist_entry = {
        "at": now_dt,
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes_db,
    }

    # Optimistic concurrency check using history length
    cur_history = record.get("history") or []
    cond = {"_id": record["_id"]}
    if len(cur_history) == 0:
        cond["$or"] = [{"history": {"$size": 0}}, {"history": {"$exists": False}}]
    else:
        cond["history"] = {"$size": len(cur_history)}

    res = db.attendance_logs.find_one_and_update(
        cond,
        {
            "$set": {
                "status": target_status,
                "punch_in": target_punch_in,
                "punch_out": target_punch_out,
                "work_hours": new_work_hours,
                "late_minutes": new_late_minutes,
                "overtime_minutes": new_overtime_minutes,
                "half_day": new_half_day,
            },
            "$push": {
                "history": hist_entry
            }
        },
        return_document=ReturnDocument.AFTER
    )
    if not res:
        raise HTTPException(status_code=409, detail="Concurrent modification conflict")

    return format_attendance_record(res)


# --------------------------------------------------------------------------- #
# Analytics Builders & Pipelines
# --------------------------------------------------------------------------- #

def build_employee_monthly_pipeline(emp_code: str, month: str) -> List[Dict[str, Any]]:
    y, m = map(int, month.split("-"))
    last_day = calendar.monthrange(y, m)[1]
    start_date = f"{month}-01"
    end_date = f"{month}-{last_day:02d}"

    return [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": start_date, "$lte": end_date}
            }
        },
        {
            "$project": {
                "date": 1,
                "status": 1,
                "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                "overtime_minutes": {"$ifNull": ["$overtime_minutes", 0]},
                "half_day": {"$ifNull": ["$half_day", False]},
                "day_of_week": {
                    "$dayOfWeek": {
                        "$dateFromString": {
                            "dateString": "$date",
                            "format": "%Y-%m-%d"
                        }
                    }
                }
            }
        },
        {
            "$project": {
                "is_weekday": {"$in": ["$day_of_week", [2, 3, 4, 5, 6]]},
                "is_present": {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                "is_leave": {"$eq": ["$status", "LEAVE"]},
                "half_day": 1,
                "late_minutes": 1,
                "overtime_minutes": 1
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {"$and": ["$is_weekday", "$is_present"]},
                            {"$cond": ["$half_day", 0.5, 1.0]},
                            0.0
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {"$cond": ["$is_leave", 1, 0]}
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                },
                "total_late_minutes": {"$sum": "$late_minutes"},
                "total_overtime_minutes": {"$sum": "$overtime_minutes"}
            }
        }
    ]


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str = Path(...),
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
):
    """Monthly attendance summary for one employee (MongoDB aggregation)."""
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    y, m = map(int, month.split("-"))
    last_day = calendar.monthrange(y, m)[1]
    first_month_d = date(y, m, 1)
    last_month_d = date(y, m, last_day)

    joined_d = date.fromisoformat(emp["joined_on"])
    eff_start = max(first_month_d, joined_d)

    working_days = 0
    if eff_start <= last_month_d:
        cur = eff_start
        while cur <= last_month_d:
            if cur.weekday() < 5:  # Mon - Fri
                working_days += 1
            cur += timedelta(days=1)

    pipeline = build_employee_monthly_pipeline(emp_code, month)
    agg_res = list(db.attendance_logs.aggregate(pipeline))

    if agg_res:
        row = agg_res[0]
        present_days = float(row.get("present_days", 0.0))
        leave_days = int(row.get("leave_days", 0))
        late_count = int(row.get("late_count", 0))
        total_late = int(row.get("total_late_minutes", 0))
        total_ot = int(row.get("total_overtime_minutes", 0))
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late = 0
        total_ot = 0

    if working_days > 0:
        pct_raw = (Decimal(str(present_days)) / Decimal(working_days)) * Decimal(100)
        attendance_pct = float(pct_raw.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    else:
        attendance_pct = None

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late,
        "total_overtime_minutes": total_ot,
        "attendance_pct": attendance_pct,
    }


def build_department_summary_pipeline(month: str, department: Optional[str] = None) -> List[Dict[str, Any]]:
    y, m = map(int, month.split("-"))
    last_day = calendar.monthrange(y, m)[1]
    start_date = f"{month}-01"
    end_date = f"{month}-{last_day:02d}"

    match_stage: Dict[str, Any] = {"joined_on": {"$lte": end_date}}
    if department:
        match_stage["department"] = department

    return [
        {"$match": match_stage},
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
                                    {"$lte": ["$date", end_date]}
                                ]
                            }
                        }
                    }
                ],
                "as": "logs"
            }
        },
        {
            "$project": {
                "department": 1,
                "logs": {
                    "$map": {
                        "input": "$logs",
                        "as": "l",
                        "in": {
                            "day_of_week": {
                                "$dayOfWeek": {
                                    "$dateFromString": {
                                        "dateString": "$$l.date",
                                        "format": "%Y-%m-%d"
                                    }
                                }
                            },
                            "status": "$$l.status",
                            "half_day": {"$ifNull": ["$$l.half_day", False]},
                            "work_hours": "$$l.work_hours",
                            "late_minutes": {"$ifNull": ["$$l.late_minutes", 0]}
                        }
                    }
                }
            }
        },
        {
            "$project": {
                "department": 1,
                "emp_present_days": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {
                                "$cond": [
                                    {
                                        "$and": [
                                            {"$in": ["$$l.day_of_week", [2, 3, 4, 5, 6]]},
                                            {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]}
                                        ]
                                    },
                                    {"$cond": ["$$l.half_day", 0.5, 1.0]},
                                    0.0
                                ]
                            }
                        }
                    }
                },
                "emp_work_hours": {
                    "$filter": {
                        "input": "$logs",
                        "as": "l",
                        "cond": {
                            "$and": [
                                {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                {"$ne": ["$$l.work_hours", None]}
                            ]
                        }
                    }
                },
                "emp_late_count": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {"$cond": [{"$gt": ["$$l.late_minutes", 0]}, 1, 0]}
                        }
                    }
                },
                "emp_total_late_minutes": {
                    "$sum": "$logs.late_minutes"
                },
                "emp_leave_count": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {"$cond": [{"$eq": ["$$l.status", "LEAVE"]}, 1, 0]}
                        }
                    }
                },
                "emp_on_duty_count": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {"$cond": [{"$eq": ["$$l.status", "ON_DUTY"]}, 1, 0]}
                        }
                    }
                }
            }
        },
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "present_days": {"$sum": "$emp_present_days"},
                "late_count": {"$sum": "$emp_late_count"},
                "total_late_minutes": {"$sum": "$emp_total_late_minutes"},
                "leave_count": {"$sum": "$emp_leave_count"},
                "on_duty_count": {"$sum": "$emp_on_duty_count"},
                "work_hours_sum": {
                    "$sum": {
                        "$sum": "$emp_work_hours.work_hours"
                    }
                },
                "work_hours_count": {
                    "$sum": {
                        "$size": "$emp_work_hours"
                    }
                }
            }
        },
        {"$sort": {"_id": ASCENDING}}
    ]


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None
):
    """Department summary for a month (aggregation across collections)."""
    pipeline = build_department_summary_pipeline(month, department)
    cursor = db.employees.aggregate(pipeline)

    items = []
    for doc in cursor:
        wh_count = doc.get("work_hours_count", 0)
        wh_sum = doc.get("work_hours_sum", 0.0)
        if wh_count > 0:
            avg_wh = round_half_up(float(Decimal(str(wh_sum)) / Decimal(wh_count)), 2)
        else:
            avg_wh = None

        items.append({
            "department": doc["_id"],
            "headcount": doc["headcount"],
            "present_days": float(doc.get("present_days", 0.0)),
            "avg_work_hours": avg_wh,
            "late_count": int(doc.get("late_count", 0)),
            "total_late_minutes": int(doc.get("total_late_minutes", 0)),
            "leave_count": int(doc.get("leave_count", 0)),
            "on_duty_count": int(doc.get("on_duty_count", 0)),
        })

    return {"month": month, "items": items}


def build_late_leaderboard_pipeline(month: str, limit: int = 10, department: Optional[str] = None) -> List[Dict[str, Any]]:
    y, m = map(int, month.split("-"))
    last_day = calendar.monthrange(y, m)[1]
    start_date = f"{month}-01"
    end_date = f"{month}-{last_day:02d}"

    pipe: List[Dict[str, Any]] = [
        {
            "$match": {
                "date": {"$gte": start_date, "$lte": end_date},
                "late_minutes": {"$gt": 0}
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1}
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp"
            }
        },
        {"$unwind": "$emp"}
    ]

    if department:
        pipe.append({"$match": {"emp.department": department}})

    pipe.extend([
        {
            "$sort": {
                "total_late_minutes": DESCENDING,
                "_id": ASCENDING
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {
                    "total_late_minutes": DESCENDING
                },
                "output": {
                    "rank": {
                        "$rank": {}
                    }
                }
            }
        },
        {
            "$match": {
                "rank": {"$lte": limit}
            }
        },
        {
            "$sort": {
                "total_late_minutes": DESCENDING,
                "_id": ASCENDING
            }
        },
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": 1,
                "late_count": 1
            }
        }
    ])

    return pipe


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None
):
    """Late-comers ranking with ties (standard competition ranking via MongoDB window function)."""
    pipeline = build_late_leaderboard_pipeline(month, limit, department)
    cursor = db.attendance_logs.aggregate(pipeline)
    items = list(cursor)
    return {"month": month, "items": items}


def build_department_trend_pipeline(department: str, from_date: str, to_date: str) -> List[Dict[str, Any]]:
    d_from = date.fromisoformat(from_date)
    d_to = date.fromisoformat(to_date)
    dt_from_ist = datetime(d_from.year, d_from.month, d_from.day, 0, 0, 0, tzinfo=IST)
    dt_to_plus_1 = datetime(d_to.year, d_to.month, d_to.day, 0, 0, 0, tzinfo=IST) + timedelta(days=1)

    return [
        {
            "$match": {
                "date": {"$gte": from_date, "$lte": to_date}
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
                            {"$cond": ["$half_day", 0.5, 1.0]},
                            0.0
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]
                    }
                }
            }
        },
        {
            "$project": {
                "date_val": {
                    "$dateFromString": {
                        "dateString": "$_id",
                        "format": "%Y-%m-%d",
                        "timezone": "+05:30"
                    }
                },
                "present_count": 1,
                "late_count": 1
            }
        },
        {
            "$densify": {
                "field": "date_val",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [dt_from_ist, dt_to_plus_1]
                }
            }
        },
        {
            "$sort": {"date_val": ASCENDING}
        },
        {
            "$project": {
                "date_str": {
                    "$dateToString": {
                        "date": "$date_val",
                        "format": "%Y-%m-%d",
                        "timezone": "+05:30"
                    }
                },
                "date_val": 1,
                "present_count": {"$ifNull": ["$present_count", 0.0]},
                "late_count": {"$ifNull": ["$late_count", 0]}
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "let": {"current_day": "$date_str"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$department", department]},
                                    {"$lte": ["$joined_on", "$$current_day"]}
                                ]
                            }
                        }
                    },
                    {"$count": "count"}
                ],
                "as": "hc"
            }
        },
        {
            "$set": {
                "headcount": {"$ifNull": [{"$first": "$hc.count"}, 0]},
                "day_of_week": {
                    "$dayOfWeek": {
                        "date": "$date_val",
                        "timezone": "+05:30"
                    }
                }
            }
        },
        {
            "$set": {
                "is_working_day": {"$in": ["$day_of_week", [2, 3, 4, 5, 6]]}
            }
        },
        {
            "$set": {
                "attendance_rate": {
                    "$cond": [
                        {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                        {"$divide": ["$present_count", "$headcount"]},
                        None
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"date_val": ASCENDING},
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
                "date": "$date_str",
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": 1
            }
        }
    ]


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str = Path(...),
    from_date: str = Query(..., alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to_date: str = Query(..., alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$")
):
    """Daily series with gap filling and a 7-day moving average."""
    dept_exists = db.employees.find_one({"department": department})
    if not dept_exists:
        raise HTTPException(status_code=404, detail="Department not found")

    d_from = date.fromisoformat(from_date)
    d_to = date.fromisoformat(to_date)
    if d_to < d_from:
        raise HTTPException(status_code=422, detail="to cannot be before from")
    if (d_to - d_from).days > 92:
        raise HTTPException(status_code=422, detail="date range cannot exceed 92 days")

    pipeline = build_department_trend_pipeline(department, from_date, to_date)
    docs = list(db.attendance_logs.aggregate(pipeline))

    # If the department had 0 attendance records across the entire range, $densify on attendance_logs
    # receives an empty stream. In that fallback case, we construct the daily series directly.
    if not docs:
        cur_d = d_from
        while cur_d <= d_to:
            cur_str = cur_d.isoformat()
            hc = db.employees.count_documents({"department": department, "joined_on": {"$lte": cur_str}})
            is_work = cur_d.weekday() < 5
            rate = 0.0 if (is_work and hc > 0) else None
            docs.append({
                "date": cur_str,
                "is_working_day": is_work,
                "headcount": hc,
                "present_count": 0.0,
                "late_count": 0,
                "attendance_rate": rate,
                "moving_avg_7d": None
            })
            cur_d += timedelta(days=1)

        # Compute moving avg for fallback
        for idx in range(len(docs)):
            window = [docs[j]["attendance_rate"] for j in range(max(0, idx - 6), idx + 1) if docs[j]["attendance_rate"] is not None]
            if window:
                docs[idx]["moving_avg_7d"] = sum(window) / len(window)
            else:
                docs[idx]["moving_avg_7d"] = None

    items = []
    for d in docs:
        items.append({
            "date": d["date"],
            "is_working_day": d["is_working_day"],
            "headcount": d["headcount"],
            "present_count": float(d.get("present_count", 0.0)),
            "late_count": int(d.get("late_count", 0)),
            "attendance_rate": round_half_up(d.get("attendance_rate"), 4),
            "moving_avg_7d": round_half_up(d.get("moving_avg_7d"), 4),
        })

    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Admin Explain Endpoint
# --------------------------------------------------------------------------- #
@app.get("/admin/explain/{endpoint}")
def admin_explain(
    endpoint: str = Path(...),
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100)
):
    """MongoDB explain output with executionStats for an endpoint's main query or aggregation pipeline."""
    allowed = ["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"]
    if endpoint not in allowed:
        raise HTTPException(status_code=422, detail=f"Endpoint must be one of {allowed}")

    if endpoint == "attendance_list":
        if date_from and date_to and date_from > date_to:
            raise HTTPException(status_code=422, detail="date_from > date_to")

        q: Dict[str, Any] = {}
        if emp_code:
            q["emp_code"] = emp_code
        if status:
            q["status"] = status
        if date_from or date_to:
            q["date"] = {}
            if date_from:
                q["date"]["$gte"] = date_from
            if date_to:
                q["date"]["$lte"] = date_to

        skip = (page - 1) * page_size
        cursor = db.attendance_logs.find(q).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip(skip).limit(page_size)
        explain_res = cursor.explain()
        explain_json = json.loads(json_util.dumps(explain_res))
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": explain_json
        }

    elif endpoint == "employee_monthly":
        if not emp_code:
            raise HTTPException(status_code=422, detail="emp_code is required")
        if not month:
            raise HTTPException(status_code=422, detail="month is required")

        pipeline = build_employee_monthly_pipeline(emp_code, month)
        cmd = {
            "explain": {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {}
            },
            "verbosity": "executionStats"
        }
        res = db.command(cmd)
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": json.loads(json_util.dumps(res))
        }

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(status_code=422, detail="month is required")

        pipeline = build_department_summary_pipeline(month, department)
        cmd = {
            "explain": {
                "aggregate": "employees",
                "pipeline": pipeline,
                "cursor": {}
            },
            "verbosity": "executionStats"
        }
        res = db.command(cmd)
        return {
            "endpoint": endpoint,
            "collection": "employees",
            "explain": json.loads(json_util.dumps(res))
        }

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(status_code=422, detail="month is required")

        pipeline = build_late_leaderboard_pipeline(month, limit, department)
        cmd = {
            "explain": {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {}
            },
            "verbosity": "executionStats"
        }
        res = db.command(cmd)
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": json.loads(json_util.dumps(res))
        }

    elif endpoint == "department_trend":
        if not department:
            raise HTTPException(status_code=422, detail="department is required")
        if not from_date or not to_date:
            raise HTTPException(status_code=422, detail="from and to dates are required")

        pipeline = build_department_trend_pipeline(department, from_date, to_date)
        cmd = {
            "explain": {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {}
            },
            "verbosity": "executionStats"
        }
        res = db.command(cmd)
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": json.loads(json_util.dumps(res))
        }

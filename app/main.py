"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)
"""
import calendar
import os
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, field_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError

# Real environment variables take precedence over .env
load_dotenv(override=False)

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client = MongoClient(MONGO_URI, tz_aware=True)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))


def init_db():
    """Create all required indexes idempotently at startup."""
    # employees collection indexes
    db.employees.create_index([("emp_code", 1)], unique=True)
    db.employees.create_index([("department", 1), ("joined_on", 1)])
    db.employees.create_index([("joined_on", 1), ("department", 1)])

    # attendance_logs collection indexes
    db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
    db.attendance_logs.create_index([("date", -1), ("emp_code", 1)])
    db.attendance_logs.create_index([("emp_code", 1), ("date", -1)])
    db.attendance_logs.create_index([("emp_code", 1), ("punch_in", -1)])
    db.attendance_logs.create_index([("date", 1), ("late_minutes", 1), ("emp_code", 1)])
    db.attendance_logs.create_index([("status", 1), ("date", -1), ("emp_code", 1)])


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan,
)


# --------------------------------------------------------------------------- #
# Helpers: Time, Dates, Rounding, Business Rules R1-R8
# --------------------------------------------------------------------------- #
def round_half_up(val: Optional[float], decimals: int) -> Optional[float]:
    """R8: Round number half-up using Decimal."""
    if val is None:
        return None
    d = Decimal(str(val))
    q = Decimal("10") ** -decimals
    return float(d.quantize(q, rounding=ROUND_HALF_UP))


def epoch_ms_to_datetime(ms: int) -> datetime:
    """R1: Instants are epoch milliseconds truncated to whole seconds."""
    sec = ms // 1000
    return datetime.fromtimestamp(sec, tz=timezone.utc)


def datetime_to_epoch_ms(dt: Optional[datetime]) -> Optional[int]:
    """Convert a BSON/Python datetime to epoch milliseconds (truncated to sec)."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp()) * 1000


def truncate_datetime_to_seconds(dt: datetime) -> datetime:
    """R1: Truncate instant to whole seconds."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.replace(microsecond=0)


def is_overnight_shift(shift_start: str, shift_end: str) -> bool:
    """R1: shift_end <= shift_start implies an overnight shift."""
    sh, sm = map(int, shift_start.split(":"))
    eh, em = map(int, shift_end.split(":"))
    return (eh, em) <= (sh, sm)


def get_attendance_date(punch_in_utc: datetime, shift_start: str, shift_end: str) -> str:
    """
    R1: Attendance date in IST. Overnight shift: if punch-in IST time is earlier than
    shift_end, it belongs to the shift starting the previous calendar day.
    """
    punch_in_ist = punch_in_utc.astimezone(IST)
    cal_date = punch_in_ist.date()

    if is_overnight_shift(shift_start, shift_end):
        eh, em = map(int, shift_end.split(":"))
        if (punch_in_ist.hour, punch_in_ist.minute, punch_in_ist.second) < (eh, em, 0):
            cal_date = cal_date - timedelta(days=1)

    return cal_date.isoformat()


def get_shift_start_datetime(date_str: str, shift_start: str) -> datetime:
    """Shift start instant in IST."""
    d = datetime.fromisoformat(date_str).date()
    h, m = map(int, shift_start.split(":"))
    return datetime(d.year, d.month, d.day, h, m, 0, tzinfo=IST)


def get_shift_end_datetime(date_str: str, shift_start: str, shift_end: str) -> datetime:
    """Shift end instant in IST (on next calendar day for overnight shift)."""
    d = datetime.fromisoformat(date_str).date()
    if is_overnight_shift(shift_start, shift_end):
        d = d + timedelta(days=1)
    h, m = map(int, shift_end.split(":"))
    return datetime(d.year, d.month, d.day, h, m, 0, tzinfo=IST)


def compute_late_minutes(punch_in_utc: datetime, shift_start: str, date_str: str) -> int:
    """
    R2: Grace period 10 minutes. Late only if punch-in is strictly > 10:00 after shift_start.
    Then whole minutes (floor) since shift_start, else 0.
    """
    punch_in_ist = punch_in_utc.astimezone(IST)
    start_ist = get_shift_start_datetime(date_str, shift_start)
    diff_seconds = int((punch_in_ist - start_ist).total_seconds())
    if diff_seconds > 600:
        return diff_seconds // 60
    return 0


def compute_work_hours(punch_in_utc: datetime, punch_out_utc: datetime) -> float:
    """R4: (punch_out - punch_in) in seconds / 3600, rounded 2 decimals half-up."""
    diff_seconds = int((punch_out_utc - punch_in_utc).total_seconds())
    hours = Decimal(diff_seconds) / Decimal(3600)
    return float(hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_overtime(punch_out_utc: datetime, shift_start: str, shift_end: str, date_str: str) -> int:
    """
    R3: Whole minutes (floor) from shift_end to punch_out, counted only if >= 30; otherwise 0.
    """
    punch_out_ist = punch_out_utc.astimezone(IST)
    end_ist = get_shift_end_datetime(date_str, shift_start, shift_end)
    diff_seconds = int((punch_out_ist - end_ist).total_seconds())
    if diff_seconds >= 1800:
        return diff_seconds // 60
    return 0


def compute_half_day(work_hours: Optional[float]) -> bool:
    """R5: After punch-out, half_day is true when rounded work_hours < 4.50."""
    if work_hours is None:
        return False
    return work_hours < 4.50


def compute_working_days(month_str: str, joined_on_str: str) -> int:
    """
    R7: Monday - Friday, no holiday calendar; counted from joined_on for mid-month joiners.
    """
    year, month = map(int, month_str.split("-"))
    _, last_day = calendar.monthrange(year, month)
    month_start = date(year, month, 1)
    month_end = date(year, month, last_day)

    joined_on = date.fromisoformat(joined_on_str)
    if joined_on > month_end:
        return 0

    curr = max(month_start, joined_on)
    count = 0
    while curr <= month_end:
        if curr.weekday() < 5:  # Mon-Fri
            count += 1
        curr += timedelta(days=1)
    return count


def serialize_history_entry(entry: Dict[str, Any]) -> Dict[str, Any]:
    """Serialize MongoDB history entry to OpenAPI HistoryEntry."""
    changes_api = {}
    for k, v in entry.get("changes", {}).items():
        if k in ("punch_in", "punch_out"):
            changes_api[k] = {
                "from": datetime_to_epoch_ms(v.get("from")) if isinstance(v.get("from"), datetime) else v.get("from"),
                "to": datetime_to_epoch_ms(v.get("to")) if isinstance(v.get("to"), datetime) else v.get("to"),
            }
        else:
            changes_api[k] = {
                "from": v.get("from"),
                "to": v.get("to"),
            }
    at_val = entry.get("at")
    return {
        "at": datetime_to_epoch_ms(at_val) if isinstance(at_val, datetime) else at_val,
        "by": entry.get("by"),
        "reason": entry.get("reason"),
        "changes": changes_api,
    }


def serialize_attendance_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Format MongoDB attendance_logs document into OpenAPI AttendanceRecord."""
    history = [serialize_history_entry(e) for e in doc.get("history", [])]
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": datetime_to_epoch_ms(doc.get("punch_in")),
        "punch_out": datetime_to_epoch_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": history,
    }


def serialize_employee_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Format MongoDB employee document into OpenAPI Employee."""
    created_at = doc.get("created_at")
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc.get("shift_start", "09:30"),
        "shift_end": doc.get("shift_end", "18:30"),
        "joined_on": doc["joined_on"],
        "created_at": datetime_to_epoch_ms(created_at) if isinstance(created_at, datetime) else created_at,
    }


# --------------------------------------------------------------------------- #
# Request Models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field("18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def validate_joined_on(cls, v: str) -> str:
        try:
            date.fromisoformat(v)
        except ValueError:
            raise ValueError("joined_on must be a valid date YYYY-MM-DD")
        return v

    @field_validator("shift_end")
    @classmethod
    def validate_shift_times(cls, v: str, info) -> str:
        shift_start = info.data.get("shift_start")
        if shift_start is not None and v == shift_start:
            raise ValueError("shift_start must differ from shift_end")
        return v


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(None, ge=100000000000, le=4102444800000)
    status: str = Field("PRESENT", pattern=r"^(PRESENT|WFH|ON_DUTY)$")


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(None, ge=100000000000, le=4102444800000)


class RegularizeIn(BaseModel):
    status: Optional[str] = Field(None, pattern=r"^(PRESENT|ABSENT|LEAVE|WFH|ON_DUTY)$")
    punch_in: Optional[int] = Field(None, ge=100000000000, le=4102444800000)
    punch_out: Optional[int] = Field(None, ge=100000000000, le=4102444800000)
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    """Readiness probe: 200 only if MongoDB answers ping, else 503."""
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Database unavailable: {str(e)}")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    """Create employee: unique emp_code enforced by unique index, 409 on duplicate."""
    doc = body.model_dump()
    now_utc = truncate_datetime_to_seconds(datetime.now(timezone.utc))
    doc["created_at"] = now_utc
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail=f"Employee {body.emp_code} already exists")

    return serialize_employee_doc(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """List employees: filter by department, sorted by emp_code asc, paginated."""
    q: Dict[str, Any] = {}
    if department:
        q["department"] = department

    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size
    cursor = db.employees.find(q).sort("emp_code", 1).skip(skip).limit(page_size)
    items = [serialize_employee_doc(doc) for doc in cursor]

    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    """
    Record a punch-in: R1, R2. Race-safe concurrency via unique (emp_code, date) index.
    """
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.punched_at is not None:
        punch_in_dt = epoch_ms_to_datetime(body.punched_at)
    else:
        punch_in_dt = truncate_datetime_to_seconds(datetime.now(timezone.utc))

    shift_start = emp.get("shift_start", "09:30")
    shift_end = emp.get("shift_end", "18:30")
    att_date = get_attendance_date(punch_in_dt, shift_start, shift_end)
    late_min = compute_late_minutes(punch_in_dt, shift_start, att_date)

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

    return serialize_attendance_doc(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    """
    Record a punch-out: R3, R4, R5. Works across midnight for overnight shifts.
    Atomic find_one_and_update ensures concurrent duplicate punch-outs yield one 200 and one 409.
    """
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.punched_at is not None:
        punch_out_dt = epoch_ms_to_datetime(body.punched_at)
    else:
        punch_out_dt = truncate_datetime_to_seconds(datetime.now(timezone.utc))

    # Find the employee's most recent record
    latest = db.attendance_logs.find_one(
        {"emp_code": body.emp_code},
        sort=[("punch_in", -1)],
    )

    if not latest or latest.get("punch_in") is None:
        raise HTTPException(status_code=404, detail="No punch-in found for this punch-out")

    punch_in_dt = latest["punch_in"]
    if punch_in_dt.tzinfo is None:
        punch_in_dt = punch_in_dt.replace(tzinfo=timezone.utc)

    # Validation: punched_at <= punch_in or > 24 hours after punch_in
    diff_sec = int((punch_out_dt - punch_in_dt).total_seconds())
    if diff_sec <= 0:
        raise HTTPException(status_code=422, detail="punched_at must be strictly greater than punch_in")
    if diff_sec > 86400:
        raise HTTPException(status_code=422, detail="punched_at cannot be more than 24 hours after punch_in")

    if latest.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="Record is already punched out")

    shift_start = emp.get("shift_start", "09:30")
    shift_end = emp.get("shift_end", "18:30")
    att_date = latest["date"]

    work_hours = compute_work_hours(punch_in_dt, punch_out_dt)
    overtime_min = compute_overtime(punch_out_dt, shift_start, shift_end, att_date)
    half_day = compute_half_day(work_hours)

    updated = db.attendance_logs.find_one_and_update(
        {"_id": latest["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": punch_out_dt,
                "work_hours": work_hours,
                "overtime_minutes": overtime_min,
                "half_day": half_day,
            }
        },
        return_document=ReturnDocument.AFTER,
    )

    if not updated:
        raise HTTPException(status_code=409, detail="Record was punched out concurrently")

    return serialize_attendance_doc(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """
    List attendance records: paginated, sorted by date desc then emp_code asc.
    Indexed query without loading whole collection into memory.
    """
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from must be on or before date_to")

    q: Dict[str, Any] = {}
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

    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    cursor = (
        db.attendance_logs.find(q)
        .sort([("date", -1), ("emp_code", 1)])
        .skip(skip)
        .limit(page_size)
    )

    items = [serialize_attendance_doc(doc) for doc in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date_str}")
def regularize_attendance(emp_code: str, date_str: str, body: RegularizeIn):
    """
    Regularize (manually correct) one day's record:
    Recompute derived fields and append to history.
    """
    try:
        date.fromisoformat(date_str)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid date format in path")

    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date_str})
    if not record:
        raise HTTPException(status_code=404, detail="Attendance record not found for date")

    old_status = record["status"]
    old_punch_in = record.get("punch_in")
    old_punch_out = record.get("punch_out")
    old_work_hours = record.get("work_hours")
    old_late_minutes = record.get("late_minutes", 0)
    old_overtime_minutes = record.get("overtime_minutes", 0)
    old_half_day = record.get("half_day", False)
    old_history = record.get("history", [])

    new_status = body.status if body.status is not None else old_status

    # Rule: setting status ABSENT or LEAVE clears both punch times; supplying punch times is 422
    if new_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(status_code=422, detail="Cannot supply punch_in or punch_out with status ABSENT or LEAVE")
        new_punch_in = None
        new_punch_out = None
    else:
        new_punch_in = epoch_ms_to_datetime(body.punch_in) if body.punch_in is not None else old_punch_in
        new_punch_out = epoch_ms_to_datetime(body.punch_out) if body.punch_out is not None else old_punch_out

        # Rule: a presence status requires punch_in
        if new_punch_in is None:
            raise HTTPException(status_code=422, detail="Presence status requires punch_in")

        # Rule: punch_in must stay on the record's attendance date under R1
        shift_start = emp.get("shift_start", "09:30")
        shift_end = emp.get("shift_end", "18:30")
        calc_date = get_attendance_date(new_punch_in, shift_start, shift_end)
        if calc_date != date_str:
            raise HTTPException(status_code=422, detail="punch_in does not belong to the record attendance date")

        # Rule: punch_out must be after punch_in and within 24 hours
        if new_punch_out is not None:
            diff_sec = int((new_punch_out - new_punch_in).total_seconds())
            if diff_sec <= 0:
                raise HTTPException(status_code=422, detail="punch_out must be after punch_in")
            if diff_sec > 86400:
                raise HTTPException(status_code=422, detail="punch_out must be within 24 hours of punch_in")

    # Recompute derived fields
    if new_status in ("ABSENT", "LEAVE"):
        new_late_minutes = 0
        new_work_hours = None
        new_overtime_minutes = 0
        new_half_day = False
    else:
        shift_start = emp.get("shift_start", "09:30")
        shift_end = emp.get("shift_end", "18:30")
        new_late_minutes = compute_late_minutes(new_punch_in, shift_start, date_str)
        if new_punch_out is not None:
            new_work_hours = compute_work_hours(new_punch_in, new_punch_out)
            new_overtime_minutes = compute_overtime(new_punch_out, shift_start, shift_end, date_str)
            new_half_day = compute_half_day(new_work_hours)
        else:
            new_work_hours = None
            new_overtime_minutes = 0
            new_half_day = False

    # Compute actual changes
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

    if not changes:
        raise HTTPException(status_code=422, detail="A request that changes nothing is 422")

    now_utc = truncate_datetime_to_seconds(datetime.now(timezone.utc))
    history_entry = {
        "at": now_utc,
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    # Optimistic concurrency: ensure history length hasn't changed
    history_cond = (
        {"history": {"$size": len(old_history)}}
        if old_history
        else {"$or": [{"history": {"$size": 0}}, {"history": {"$exists": False}}]}
    )
    update_filter = {
        "_id": record["_id"],
        **history_cond,
    }

    updated = db.attendance_logs.find_one_and_update(
        update_filter,
        {
            "$set": {
                "status": new_status,
                "punch_in": new_punch_in,
                "punch_out": new_punch_out,
                "work_hours": new_work_hours,
                "late_minutes": new_late_minutes,
                "overtime_minutes": new_overtime_minutes,
                "half_day": new_half_day,
            },
            "$push": {"history": history_entry},
        },
        return_document=ReturnDocument.AFTER,
    )

    if not updated:
        raise HTTPException(status_code=409, detail="Record was modified concurrently")

    return serialize_attendance_doc(updated)


# --------------------------------------------------------------------------- #
# Analytics Pipelines
# --------------------------------------------------------------------------- #
def build_employee_monthly_pipeline(emp_code: str, month: str) -> List[Dict[str, Any]]:
    """Build aggregation pipeline for employee monthly summary."""
    year, m = map(int, month.split("-"))
    _, last_day = calendar.monthrange(year, m)
    start_date = f"{month}-01"
    end_date = f"{month}-{last_day:02d}"

    return [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": start_date, "$lte": end_date},
            }
        },
        {
            "$project": {
                "dayOfWeek": {
                    "$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}
                },
                "status": 1,
                "half_day": {"$ifNull": ["$half_day", False]},
                "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                "overtime_minutes": {"$ifNull": ["$overtime_minutes", 0]},
            }
        },
        {
            "$project": {
                "present_val": {
                    "$cond": [
                        {
                            "$and": [
                                {"$in": ["$dayOfWeek", [2, 3, 4, 5, 6]]},
                                {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            ]
                        },
                        {"$cond": ["$half_day", 0.5, 1.0]},
                        0.0,
                    ]
                },
                "leave_val": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]},
                "late_count_val": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]},
                "late_minutes": "$late_minutes",
                "overtime_minutes": "$overtime_minutes",
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {"$sum": "$present_val"},
                "leave_days": {"$sum": "$leave_val"},
                "late_count": {"$sum": "$late_count_val"},
                "total_late_minutes": {"$sum": "$late_minutes"},
                "total_overtime_minutes": {"$sum": "$overtime_minutes"},
            }
        },
    ]


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$")):
    """Monthly attendance summary for one employee."""
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    working_days = compute_working_days(month, emp["joined_on"])
    pipeline = build_employee_monthly_pipeline(emp_code, month)
    results = list(db.attendance_logs.aggregate(pipeline))

    if results:
        res = results[0]
        present_days = float(res.get("present_days", 0.0))
        leave_days = int(res.get("leave_days", 0))
        late_count = int(res.get("late_count", 0))
        total_late_minutes = int(res.get("total_late_minutes", 0))
        total_overtime_minutes = int(res.get("total_overtime_minutes", 0))
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late_minutes = 0
        total_overtime_minutes = 0

    attendance_pct = None
    if working_days > 0:
        pct_val = (present_days / working_days) * 100.0
        attendance_pct = round_half_up(pct_val, 2)

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


def build_department_summary_pipeline(month: str, department: Optional[str] = None) -> List[Dict[str, Any]]:
    """Build aggregation pipeline across collections for department summary."""
    year, m = map(int, month.split("-"))
    _, last_day = calendar.monthrange(year, m)
    start_date = f"{month}-01"
    end_date = f"{month}-{last_day:02d}"

    match_emp: Dict[str, Any] = {"joined_on": {"$lte": end_date}}
    if department:
        match_emp["department"] = department

    return [
        {"$match": match_emp},
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
            "$project": {
                "department": 1,
                "emp_code": 1,
                "dayOfWeek": {
                    "$cond": [
                        {"$ifNull": ["$logs.date", False]},
                        {"$dayOfWeek": {"$dateFromString": {"dateString": "$logs.date"}}},
                        0,
                    ]
                },
                "status": "$logs.status",
                "half_day": {"$ifNull": ["$logs.half_day", False]},
                "work_hours": "$logs.work_hours",
                "late_minutes": {"$ifNull": ["$logs.late_minutes", 0]},
                "overtime_minutes": {"$ifNull": ["$logs.overtime_minutes", 0]},
            }
        },
        {
            "$project": {
                "department": 1,
                "emp_code": 1,
                "present_val": {
                    "$cond": [
                        {
                            "$and": [
                                {"$in": ["$dayOfWeek", [2, 3, 4, 5, 6]]},
                                {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            ]
                        },
                        {"$cond": ["$half_day", 0.5, 1.0]},
                        0.0,
                    ]
                },
                "work_hours_valid": {
                    "$cond": [
                        {
                            "$and": [
                                {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                {"$ne": ["$work_hours", None]},
                            ]
                        },
                        "$work_hours",
                        None,
                    ]
                },
                "late_count_val": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]},
                "late_minutes": "$late_minutes",
                "leave_val": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]},
                "on_duty_val": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]},
            }
        },
        {
            "$group": {
                "_id": "$department",
                "employees": {"$addToSet": "$emp_code"},
                "present_days": {"$sum": "$present_val"},
                "avg_work_hours": {"$avg": "$work_hours_valid"},
                "late_count": {"$sum": "$late_count_val"},
                "total_late_minutes": {"$sum": "$late_minutes"},
                "leave_count": {"$sum": "$leave_val"},
                "on_duty_count": {"$sum": "$on_duty_val"},
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": {"$size": "$employees"},
                "present_days": 1,
                "avg_work_hours": 1,
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
            }
        },
        {"$match": {"headcount": {"$gt": 0}}},
        {"$sort": {"department": 1}},
    ]


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    """Department summary for a month across both collections."""
    pipeline = build_department_summary_pipeline(month, department)
    results = list(db.employees.aggregate(pipeline))

    items = []
    for doc in results:
        items.append({
            "department": doc["department"],
            "headcount": doc["headcount"],
            "present_days": doc["present_days"],
            "avg_work_hours": round_half_up(doc.get("avg_work_hours"), 2),
            "late_count": doc["late_count"],
            "total_late_minutes": doc["total_late_minutes"],
            "leave_count": doc["leave_count"],
            "on_duty_count": doc["on_duty_count"],
        })

    return {"month": month, "items": items}


def build_late_leaderboard_pipeline(
    month: str, limit: int = 10, department: Optional[str] = None
) -> List[Dict[str, Any]]:
    """Build late leaderboard aggregation pipeline using standard competition ranking ($rank)."""
    year, m = map(int, month.split("-"))
    _, last_day = calendar.monthrange(year, m)
    start_date = f"{month}-01"
    end_date = f"{month}-{last_day:02d}"

    pipeline: List[Dict[str, Any]] = [
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
            "$project": {
                "_id": 0,
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {"rank": {"$rank": {}}},
            }
        },
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
    ])

    return pipeline


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    """
    Late-comers leaderboard for a month:
    Standard competition ranking with ties (1, 2, 2, 4).
    Limit applied after ranking.
    """
    pipeline = build_late_leaderboard_pipeline(month, limit, department)
    results = list(db.attendance_logs.aggregate(pipeline))

    items = []
    for doc in results:
        items.append({
            "rank": doc["rank"],
            "emp_code": doc["emp_code"],
            "name": doc["name"],
            "department": doc["department"],
            "total_late_minutes": doc["total_late_minutes"],
            "late_count": doc["late_count"],
        })

    return {"month": month, "items": items}


def build_department_trend_pipeline(department: str, from_str: str, to_str: str) -> List[Dict[str, Any]]:
    """Build daily series aggregation pipeline with $densify gap filling and moving average."""
    from_dt = datetime.fromisoformat(from_str)
    to_dt = datetime.fromisoformat(to_str)
    upper_dt = to_dt + timedelta(days=1)

    return [
        {"$match": {"date": {"$gte": from_str, "$lte": to_str}}},
        {
            "$lookup": {
                "from": "employees",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
        {"$match": {"emp.department": department}},
        {
            "$project": {
                "date_dt": {"$dateFromString": {"dateString": "$date"}},
                "present_val": {
                    "$cond": [
                        {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                        {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                        0.0,
                    ]
                },
                "late_val": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]},
            }
        },
        {
            "$group": {
                "_id": "$date_dt",
                "present_count": {"$sum": "$present_val"},
                "late_count": {"$sum": "$late_val"},
            }
        },
        {
            "$densify": {
                "field": "_id",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [from_dt, upper_dt],
                },
            }
        },
        {
            "$project": {
                "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$_id"}},
                "dayOfWeek": {"$dayOfWeek": "$_id"},
                "present_count": {"$ifNull": ["$present_count", 0.0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
            }
        },
        {"$sort": {"date": 1}},
        {
            "$lookup": {
                "from": "employees",
                "let": {"day": "$date"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$department", department]},
                                    {"$lte": ["$joined_on", "$$day"]},
                                ]
                            }
                        }
                    },
                    {"$count": "c"},
                ],
                "as": "hc",
            }
        },
        {
            "$addFields": {
                "headcount": {"$ifNull": [{"$first": "$hc.c"}, 0]},
                "is_working_day": {"$in": ["$dayOfWeek", [2, 3, 4, 5, 6]]},
            }
        },
        {
            "$addFields": {
                "attendance_rate": {
                    "$cond": [
                        {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                        {"$divide": ["$present_count", "$headcount"]},
                        None,
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
                        "window": {"documents": [-6, "current"]},
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
                "moving_avg_7d": 1,
            }
        },
    ]


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: str = Query(..., alias="from"),
    to_date: str = Query(..., alias="to"),
):
    """
    Daily attendance trend for a department:
    Exactly one row per calendar day with gap filling ($densify) and 7-day moving average.
    """
    dept_exists = db.employees.find_one({"department": department})
    if not dept_exists:
        raise HTTPException(status_code=404, detail="Department not found")

    try:
        d_from = date.fromisoformat(from_date)
        d_to = date.fromisoformat(to_date)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid date format")

    if d_to < d_from:
        raise HTTPException(status_code=422, detail="'to' must be on or after 'from'")

    days_range = (d_to - d_from).days + 1
    if days_range > 92:
        raise HTTPException(status_code=422, detail="Date range cannot exceed 92 days")

    pipeline = build_department_trend_pipeline(department, from_date, to_date)
    results = list(db.attendance_logs.aggregate(pipeline))

    items = []
    for doc in results:
        items.append({
            "date": doc["date"],
            "is_working_day": doc["is_working_day"],
            "headcount": doc["headcount"],
            "present_count": float(doc["present_count"]),
            "late_count": int(doc["late_count"]),
            "attendance_rate": round_half_up(doc.get("attendance_rate"), 4),
            "moving_avg_7d": round_half_up(doc.get("moving_avg_7d"), 4),
        })

    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Admin Explain Endpoint
# --------------------------------------------------------------------------- #
@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
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
    page_size: int = Query(20, ge=1, le=100),
):
    """
    MongoDB explain output for an endpoint's main query or pipeline with executionStats verbosity.
    """
    valid_endpoints = [
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
    ]
    if endpoint not in valid_endpoints:
        raise HTTPException(status_code=422, detail=f"Unknown endpoint '{endpoint}'. Must be one of {valid_endpoints}")

    if endpoint == "attendance_list":
        if date_from and date_to and date_from > date_to:
            raise HTTPException(status_code=422, detail="date_from must be on or before date_to")
        q: Dict[str, Any] = {}
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
        explain_res = db.command(
            "explain",
            {
                "find": "attendance_logs",
                "filter": q,
                "sort": {"date": -1, "emp_code": 1},
                "skip": skip,
                "limit": page_size,
            },
            verbosity="executionStats",
        )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain_res}

    elif endpoint == "employee_monthly":
        if not emp_code:
            raise HTTPException(status_code=422, detail="emp_code is required for employee_monthly")
        if not month:
            raise HTTPException(status_code=422, detail="month is required for employee_monthly")

        pipeline = build_employee_monthly_pipeline(emp_code, month)
        explain_res = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain_res}

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(status_code=422, detail="month is required for department_summary")

        pipeline = build_department_summary_pipeline(month, department)
        explain_res = db.command(
            "explain",
            {"aggregate": "employees", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {"endpoint": endpoint, "collection": "employees", "explain": explain_res}

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(status_code=422, detail="month is required for late_leaderboard")

        pipeline = build_late_leaderboard_pipeline(month, limit, department)
        explain_res = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain_res}

    elif endpoint == "department_trend":
        if not department:
            raise HTTPException(status_code=422, detail="department is required for department_trend")
        if not from_date or not to_date:
            raise HTTPException(status_code=422, detail="'from' and 'to' are required for department_trend")

        try:
            d_from = date.fromisoformat(from_date)
            d_to = date.fromisoformat(to_date)
        except ValueError:
            raise HTTPException(status_code=422, detail="Invalid date format")

        if d_to < d_from:
            raise HTTPException(status_code=422, detail="'to' must be on or after 'from'")
        if (d_to - d_from).days + 1 > 92:
            raise HTTPException(status_code=422, detail="Date range cannot exceed 92 days")

        pipeline = build_department_trend_pipeline(department, from_date, to_date)
        explain_res = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain_res}

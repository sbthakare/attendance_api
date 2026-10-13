"""
Employee Attendance & Analytics API  (FastAPI + MongoDB)

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB   (real environment variables win over a local .env)

Layout of this file
    1. configuration, client, indexes
    2. time / rounding helpers and business rules R1-R5
    3. request models and validation helpers
    4. serialisation helpers
    5. employees + attendance endpoints
    6. pipeline builders (shared by the analytics endpoints and /admin/explain)
    7. analytics endpoints
    8. explain endpoint
"""
import json
import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Any, Literal, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

# load_dotenv() never overrides variables that are already set, so real env vars win.
load_dotenv()
log = logging.getLogger("attendance")

# --------------------------------------------------------------------------- #
# 1. Configuration, client, indexes
# --------------------------------------------------------------------------- #
# tz_aware=True: PyMongo hands back aware UTC datetimes instead of naive ones.
client = MongoClient(
    os.environ.get("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,
    tzinfo=timezone.utc,
    serverSelectionTimeoutMS=5000,
)
db = client[os.environ.get("MONGO_DB", "attendance_db")]

_indexes_ready = False

# index specs used both for creation and as aggregation hints
IDX_LOG_DATE_EMP = [("date", -1), ("emp_code", 1)]
IDX_EMP_DEPT_JOINED = [("department", 1), ("joined_on", 1)]


def ensure_indexes() -> None:
    """Idempotent: create_index with an identical spec/name is a no-op."""
    global _indexes_ready
    logs, emps = db.attendance_logs, db.employees
    # employees
    emps.create_index([("emp_code", 1)], unique=True, name="emp_code_unique")
    emps.create_index([("department", 1), ("emp_code", 1)], name="dept_emp_code")
    emps.create_index(IDX_EMP_DEPT_JOINED, name="dept_joined")
    # attendance_logs
    # one record per (emp_code, date): race-safe punch-in + point lookup for PATCH
    # + employee monthly + attendance list filtered by employee (sorted by date desc)
    logs.create_index([("emp_code", 1), ("date", -1)], unique=True, name="emp_date_unique")
    # global list sorted by date desc, emp_code asc; month/range scans for analytics
    logs.create_index(IDX_LOG_DATE_EMP, name="date_emp")
    # list filtered by status (equality) with the same sort
    logs.create_index([("status", 1), ("date", -1), ("emp_code", 1)], name="status_date_emp")
    # "most recent record with punch_in <= t" for punch-out
    logs.create_index([("emp_code", 1), ("punch_in", -1)], name="emp_punch_in")
    _indexes_ready = True


@asynccontextmanager
async def lifespan(_: FastAPI):
    try:
        ensure_indexes()
    except Exception:  # do not crash: /health reports 503 and retries
        log.exception("index creation failed at startup")
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


@app.exception_handler(PyMongoError)
async def mongo_error_handler(_: Request, exc: PyMongoError):
    log.error("mongo error: %s", exc)
    return JSONResponse(status_code=503, content={"detail": "database unavailable"})


# --------------------------------------------------------------------------- #
# 2. Time + rounding helpers, business rules R1-R5
# --------------------------------------------------------------------------- #
IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
PRESENT_STATUSES = ["PRESENT", "WFH", "ON_DUTY"]
GRACE = 10 * 60  # seconds


def ms_to_dt(ms: int) -> datetime:
    """Epoch ms -> aware UTC datetime truncated to whole seconds (R1)."""
    return datetime.fromtimestamp(ms // 1000, tz=UTC)


def now_dt() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def to_ms(dt: Optional[datetime]) -> Optional[int]:
    """Datetime (aware or naive-UTC) -> epoch ms, with integer arithmetic only."""
    if dt is None:
        return None
    return (aware(dt) - EPOCH) // timedelta(milliseconds=1)


def trunc(dt: datetime) -> datetime:
    return aware(dt).replace(microsecond=0)


def half_up(value: Decimal, places: int) -> float:
    q = Decimal(1).scaleb(-places)
    return float(value.quantize(q, rounding=ROUND_HALF_UP))


def hm(s: str) -> int:
    h, m = s.split(":")
    return int(h) * 60 + int(m)


def is_overnight(emp: dict) -> bool:
    return hm(emp["shift_end"]) <= hm(emp["shift_start"])


def ist_midnight(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, tzinfo=IST)


def attendance_date(punch_in: datetime, emp: dict) -> str:
    """R1: IST calendar date of the punch-in; early-morning punches of overnight shifts
    belong to the shift that started the previous day."""
    ist = aware(punch_in).astimezone(IST)
    d = ist.date()
    if is_overnight(emp) and (ist - ist_midnight(d)) < timedelta(minutes=hm(emp["shift_end"])):
        d -= timedelta(days=1)
    return d.isoformat()


def shift_start_dt(date_str: str, emp: dict) -> datetime:
    return ist_midnight(date.fromisoformat(date_str)) + timedelta(minutes=hm(emp["shift_start"]))


def shift_end_dt(date_str: str, emp: dict) -> datetime:
    base = ist_midnight(date.fromisoformat(date_str))
    if is_overnight(emp):
        base += timedelta(days=1)
    return base + timedelta(minutes=hm(emp["shift_end"]))


def compute_late_minutes(punch_in: datetime, date_str: str, emp: dict) -> int:
    """R2: late only if strictly more than 10:00 after shift start; minutes counted from shift start."""
    delta = int((trunc(punch_in) - shift_start_dt(date_str, emp)).total_seconds())
    return delta // 60 if delta > GRACE else 0


def compute_overtime(punch_out: datetime, date_str: str, emp: dict) -> int:
    """R3: whole minutes after shift end, only when >= 30."""
    delta = int((trunc(punch_out) - shift_end_dt(date_str, emp)).total_seconds())
    minutes = delta // 60
    return minutes if minutes >= 30 else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> tuple[float, bool]:
    """R4 + R5: (hours rounded half-up to 2 dp, half_day)."""
    seconds = int((trunc(punch_out) - trunc(punch_in)).total_seconds())
    hours = (Decimal(seconds) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return float(hours), hours < Decimal("4.50")


def derive(emp: dict, date_str: str, status: str, punch_in: Optional[datetime],
           punch_out: Optional[datetime]) -> dict:
    """All derived fields for a record, following the table in DATA_MODEL.md."""
    out = {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    if status not in PRESENT_STATUSES or punch_in is None:
        return out
    out["late_minutes"] = compute_late_minutes(punch_in, date_str, emp)
    if punch_out is not None:
        out["work_hours"], out["half_day"] = compute_work_hours(punch_in, punch_out)
        out["overtime_minutes"] = compute_overtime(punch_out, date_str, emp)
    return out


# --------------------------------------------------------------------------- #
# 3. Models + validation helpers
# --------------------------------------------------------------------------- #
Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]
# strict=True: floats, numeric strings and bools are rejected (422); seconds-looking values fail ge=
EpochMs = Annotated[int, Field(strict=True, ge=100_000_000_000, le=4_102_444_800_000)]
DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
MONTH_RE = re.compile(r"^[0-9]{4}-(0[1-9]|1[0-2])$")


def unprocessable(loc: str, msg: str, where: str = "query"):
    raise HTTPException(422, detail=[{"loc": [where, loc], "msg": msg, "type": "value_error"}])


def parse_date(value: str, loc: str, where: str = "query") -> date:
    if not DATE_RE.match(value):
        unprocessable(loc, "must be YYYY-MM-DD", where)
    try:
        return date.fromisoformat(value)
    except ValueError:
        unprocessable(loc, "not a valid calendar date", where)


def parse_month(value: str, loc: str = "month") -> tuple[str, str]:
    """-> (first day, last day) as YYYY-MM-DD strings."""
    if not MONTH_RE.match(value):
        unprocessable(loc, "must be YYYY-MM")
    y, m = int(value[:4]), int(value[5:])
    if y < 1:
        unprocessable(loc, "year out of range")
    first = date(y, m, 1)
    nxt = date(y + (m == 12), m % 12 + 1, 1) if y < 9999 else None
    last = (nxt - timedelta(days=1)) if nxt else date(9999, 12, 31)
    return first.isoformat(), last.isoformat()


class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP[0-9]{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=r"^([01][0-9]|2[0-3]):[0-5][0-9]$")
    shift_end: str = Field("18:30", pattern=r"^([01][0-9]|2[0-3]):[0-5][0-9]$")
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def _valid_date(cls, v: str) -> str:
        if not DATE_RE.match(v):
            raise ValueError("must be YYYY-MM-DD")
        date.fromisoformat(v)  # ValueError -> 422
        return v

    @model_validator(mode="after")
    def _shift_differs(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: str = Field(min_length=1)
    punched_at: Optional[EpochMs] = None
    status: PresenceStatus = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str = Field(min_length=1)
    punched_at: Optional[EpochMs] = None


class RegularizeIn(BaseModel):
    status: Optional[Status] = None
    punch_in: Optional[EpochMs] = None
    punch_out: Optional[EpochMs] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# 4. Serialisation helpers (BSON datetimes -> epoch ms; defaults for legacy docs)
# --------------------------------------------------------------------------- #
def jsonable(v: Any) -> Any:
    if isinstance(v, datetime):
        return to_ms(v)
    if isinstance(v, dict):
        return {k: jsonable(x) for k, x in v.items()}
    if isinstance(v, list):
        return [jsonable(x) for x in v]
    return v


def employee_out(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"], "name": doc["name"], "email": doc["email"],
        "department": doc["department"], "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"], "joined_on": doc["joined_on"],
        "created_at": to_ms(doc.get("created_at")),
    }


def record_out(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": to_ms(doc.get("punch_in")),
        "punch_out": to_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes") or 0,
        "overtime_minutes": doc.get("overtime_minutes") or 0,
        "half_day": bool(doc.get("half_day", False)),
        "history": [
            {"at": to_ms(h.get("at")), "by": h.get("by"), "reason": h.get("reason"),
             "changes": jsonable(h.get("changes") or {})}
            for h in (doc.get("history") or [])
        ],
    }


def page_args(page: int, page_size: int) -> tuple[int, int]:
    return min((page - 1) * page_size, 2 ** 62), page_size


# --------------------------------------------------------------------------- #
# 5. Employees + attendance endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        if not _indexes_ready:
            ensure_indexes()
    except Exception:
        raise HTTPException(503, "database unavailable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now_dt()
    try:
        db.employees.insert_one(doc)  # the unique index is the only arbiter of duplicates
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return employee_out(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {} if department is None else {"department": department}
    skip, limit = page_args(page, page_size)
    total = db.employees.count_documents(q)
    items = db.employees.find(q, {"_id": 0}).sort("emp_code", 1).skip(skip).limit(limit)
    return {"items": [employee_out(d) for d in items], "total": total,
            "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_dt()
    d = attendance_date(ts, emp)
    doc = {
        "emp_code": body.emp_code, "date": d, "status": body.status,
        "punch_in": ts, "punch_out": None,
        **derive(emp, d, body.status, ts, None),
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)  # unique (emp_code, date): exactly one racer wins
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return record_out(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_dt()
    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": ts}}, sort=[("punch_in", -1)]
    )
    if not rec:
        raise HTTPException(404, "no punch-in found")
    p_in = trunc(rec["punch_in"])
    if ts <= p_in:
        unprocessable("punched_at", "must be after punch_in", "body")
    if ts - p_in > timedelta(hours=24):
        unprocessable("punched_at", "more than 24 hours after punch_in", "body")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    wh, half = compute_work_hours(p_in, ts)
    # conditional update: of two simultaneous punch-outs only one still sees punch_out == null
    out = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None},
        {"$set": {"punch_out": ts, "work_hours": wh, "half_day": half,
                  "overtime_minutes": compute_overtime(ts, rec["date"], emp)}},
        return_document=ReturnDocument.AFTER, projection={"_id": 0},
    )
    if out is None:
        raise HTTPException(409, "already punched out")
    return record_out(out)


def build_attendance_query(emp_code, date_from, date_to, status, page, page_size) -> dict:
    """Shared by GET /attendance and /admin/explain/attendance_list."""
    lo = parse_date(date_from, "date_from") if date_from is not None else None
    hi = parse_date(date_to, "date_to") if date_to is not None else None
    if lo and hi and lo > hi:
        unprocessable("date_from", "date_from must not be after date_to")
    q: dict = {}
    if emp_code is not None:
        q["emp_code"] = emp_code
    if lo or hi:
        q["date"] = {}
        if lo:
            q["date"]["$gte"] = date_from
        if hi:
            q["date"]["$lte"] = date_to
    if status is not None:
        q["status"] = status
    skip, limit = page_args(page, page_size)
    return {"filter": q, "sort": {"date": -1, "emp_code": 1}, "skip": skip, "limit": limit}


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    spec = build_attendance_query(emp_code, date_from, date_to, status, page, page_size)
    coll = db.attendance_logs
    total = (coll.estimated_document_count() if not spec["filter"]
             else coll.count_documents(spec["filter"]))
    cur = (coll.find(spec["filter"], {"_id": 0}).sort(list(spec["sort"].items()))
           .skip(spec["skip"]).limit(spec["limit"]))
    return {"items": [record_out(d) for d in cur], "total": total,
            "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: str, body: RegularizeIn):
    parse_date(date, "date", "path")
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
    for _ in range(5):  # optimistic concurrency: re-read and retry if somebody else wrote first
        rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
        if not rec:
            raise HTTPException(404, "no attendance record for this date")
        new_vals, changes = plan_regularization(emp, rec, body)
        n = len(rec.get("history") or [])
        guard = {  # the record must still be exactly what we read
            "_id": rec["_id"], "status": rec["status"],
            "punch_in": rec.get("punch_in"), "punch_out": rec.get("punch_out"),
            f"history.{n}": {"$exists": False},
        }
        entry = {"at": now_dt(), "by": body.regularized_by, "reason": body.reason, "changes": changes}
        out = db.attendance_logs.find_one_and_update(
            guard, {"$set": new_vals, "$push": {"history": entry}},
            return_document=ReturnDocument.AFTER, projection={"_id": 0},
        )
        if out is not None:
            return record_out(out)
    raise HTTPException(409, "record was modified concurrently, please retry")


def plan_regularization(emp: dict, rec: dict, body: RegularizeIn) -> tuple[dict, dict]:
    """Validate a correction and return (fields to $set, history changes). Raises 422."""
    old_status = rec["status"]
    old_in, old_out = rec.get("punch_in"), rec.get("punch_out")
    status = body.status or old_status

    if status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            unprocessable("punch_in", f"punch times cannot be supplied with status {status}", "body")
        new_in = new_out = None
    else:
        new_in = ms_to_dt(body.punch_in) if body.punch_in is not None else old_in
        new_out = ms_to_dt(body.punch_out) if body.punch_out is not None else old_out
        if new_in is None:
            unprocessable("punch_in", "a presence status requires a punch_in", "body")
        if body.punch_in is not None and attendance_date(new_in, emp) != rec["date"]:
            unprocessable("punch_in", "punch_in must stay on the record's attendance date", "body")
        if new_out is not None:
            gap = trunc(new_out) - trunc(new_in)
            if gap <= timedelta(0) or gap > timedelta(hours=24):
                unprocessable("punch_out", "punch_out must be after punch_in and within 24 hours", "body")

    same = lambda a, b: (a is None and b is None) or (a is not None and b is not None and aware(a) == aware(b))
    if status == old_status and same(new_in, old_in) and same(new_out, old_out):
        unprocessable("body", "request changes nothing", "body")

    new_vals = {"status": status, "punch_in": new_in, "punch_out": new_out,
                **derive(emp, rec["date"], status, new_in, new_out)}
    old_vals = {"status": old_status, "punch_in": old_in, "punch_out": old_out,
                "work_hours": rec.get("work_hours"), "late_minutes": rec.get("late_minutes") or 0,
                "overtime_minutes": rec.get("overtime_minutes") or 0,
                "half_day": bool(rec.get("half_day", False))}

    def differs(k):
        a, b = old_vals[k], new_vals[k]
        return not same(a, b) if k in ("punch_in", "punch_out") else a != b

    changes = {k: {"from": old_vals[k], "to": new_vals[k]} for k in new_vals if differs(k)}
    return new_vals, changes


# --------------------------------------------------------------------------- #
# 6. Pipeline builders (used by the analytics endpoints AND /admin/explain)
#    Each returns (collection_name, pipeline, hint).
# --------------------------------------------------------------------------- #
def _is_weekday(date_expr: Any) -> dict:
    """$dayOfWeek: 1 = Sunday ... 7 = Saturday; a YYYY-MM-DD string parsed as UTC midnight."""
    dow = {"$dayOfWeek": date_expr}
    return {"$and": [{"$gte": [dow, 2]}, {"$lte": [dow, 6]}]}


_WEEKDAY_OF_DATE_STR = _is_weekday({"$dateFromString": {"dateString": "$date", "onError": None}})
_IS_PRESENT = {"$in": ["$status", PRESENT_STATUSES]}
_PRESENT_VALUE = {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]}
_LATE = {"$ifNull": ["$late_minutes", 0]}
_IS_LATE = {"$gt": [_LATE, 0]}


def pipeline_employee_monthly(emp_code: str, month: str):
    start, end = parse_month(month)
    return "attendance_logs", [
        {"$match": {"emp_code": emp_code, "date": {"$gte": start, "$lte": end}}},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": {"$cond": [{"$and": [_IS_PRESENT, _WEEKDAY_OF_DATE_STR]},
                                                _PRESENT_VALUE, 0]}},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [_IS_LATE, 1, 0]}},
            "total_late_minutes": {"$sum": _LATE},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }},
    ], [("emp_code", 1), ("date", -1)]


def pipeline_department_summary(month: str, department: Optional[str]):
    """Starts from employees (so people without logs count, R9), joins their logs for the month."""
    start, end = parse_month(month)
    match: dict = {"joined_on": {"$lte": end}}
    if department is not None:
        match["department"] = department
    per_employee = [
        {"$match": {"date": {"$gte": start, "$lte": end}}},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": {"$cond": [{"$and": [_IS_PRESENT, _WEEKDAY_OF_DATE_STR]},
                                                _PRESENT_VALUE, 0]}},
            "work_sum": {"$sum": {"$cond": [{"$and": [_IS_PRESENT, {"$isNumber": "$work_hours"}]},
                                            "$work_hours", 0]}},
            "work_cnt": {"$sum": {"$cond": [{"$and": [_IS_PRESENT, {"$isNumber": "$work_hours"}]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [_IS_LATE, 1, 0]}},
            "total_late_minutes": {"$sum": _LATE},
            "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
        }},
    ]
    return "employees", [
        {"$match": match},
        {"$lookup": {"from": "attendance_logs", "localField": "emp_code", "foreignField": "emp_code",
                     "pipeline": per_employee, "as": "agg"}},
        {"$set": {"agg": {"$first": "$agg"}}},  # missing when the employee has no logs -> $sum gives 0
        {"$group": {
            "_id": "$department",
            "headcount": {"$sum": 1},
            "present_days": {"$sum": "$agg.present_days"},
            "work_sum": {"$sum": "$agg.work_sum"},
            "work_cnt": {"$sum": "$agg.work_cnt"},
            "late_count": {"$sum": "$agg.late_count"},
            "total_late_minutes": {"$sum": "$agg.total_late_minutes"},
            "leave_count": {"$sum": "$agg.leave_count"},
            "on_duty_count": {"$sum": "$agg.on_duty_count"},
        }},
        {"$sort": {"_id": 1}},
    ], IDX_EMP_DEPT_JOINED


def pipeline_late_leaderboard(month: str, limit: int, department: Optional[str]):
    start, end = parse_month(month)
    emp_match = [{"$match": {"department": department}}] if department is not None else []
    return "attendance_logs", [
        {"$match": {"date": {"$gte": start, "$lte": end}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"},
                    "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code",
                     "pipeline": emp_match + [{"$project": {"_id": 0, "name": 1, "department": 1}}],
                     "as": "emp"}},
        {"$unwind": "$emp"},  # drops logs of unknown employees / other departments BEFORE ranking
        # $rank = standard competition ranking: 1, 2, 2, 4
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1},
                              "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},  # limit applied after ranking
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$emp.name",
                      "department": "$emp.department", "total_late_minutes": 1, "late_count": 1}},
    ], IDX_LOG_DATE_EMP


def pipeline_department_trend(department: str, d_from: str, d_to: str):
    lo = datetime.fromisoformat(d_from).replace(tzinfo=UTC)
    hi = datetime.fromisoformat(d_to).replace(tzinfo=UTC)
    seed = {"present_count": 0, "late_count": 0}
    scaled_rate = {  # round-half-up(10000 * present / headcount) in exact integer-valued arithmetic
        "$floor": {"$divide": [{"$add": [{"$multiply": ["$present_count", 20000]}, "$headcount"]},
                               {"$multiply": ["$headcount", 2]}]}}
    window = {"documents": [-6, "current"]}
    return "employees", [
        {"$match": {"department": department}},
        {"$lookup": {"from": "attendance_logs", "localField": "emp_code", "foreignField": "emp_code",
                     "pipeline": [{"$match": {"date": {"$gte": d_from, "$lte": d_to}}},
                                  {"$project": {"_id": 0, "date": 1, "status": 1, "half_day": 1,
                                                "late_minutes": 1}}],
                     "as": "logs"}},
        {"$unwind": "$logs"},
        {"$group": {
            "_id": "$logs.date",
            "present_count": {"$sum": {"$cond": [{"$in": ["$logs.status", PRESENT_STATUSES]},
                                                 {"$cond": [{"$ifNull": ["$logs.half_day", False]}, 0.5, 1]}, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]}, 1, 0]}},
        }},
        # seed the first and last day so $densify always has something to anchor on
        {"$unionWith": {"pipeline": [{"$documents": [{"_id": d_from, **seed}, {"_id": d_to, **seed}]}]}},
        {"$group": {"_id": "$_id", "present_count": {"$sum": "$present_count"},
                    "late_count": {"$sum": "$late_count"}}},
        {"$set": {"day": {"$dateFromString": {"dateString": "$_id", "format": "%Y-%m-%d"}}}},
        # one row per calendar day; upper bound is exclusive, hence to + 1 day
        {"$densify": {"field": "day", "range": {"step": 1, "unit": "day",
                                                "bounds": [lo, hi + timedelta(days=1)]}}},
        {"$match": {"day": {"$gte": lo, "$lte": hi}}},
        {"$set": {
            "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$day"}},
            "present_count": {"$ifNull": ["$present_count", 0]},
            "late_count": {"$ifNull": ["$late_count", 0]},
            "is_working_day": _is_weekday("$day"),
        }},
        # headcount per day (R9): department employees with joined_on <= that day
        {"$lookup": {"from": "employees", "let": {"d": "$date"},
                     "pipeline": [{"$match": {"department": department,
                                              "$expr": {"$lte": ["$joined_on", "$$d"]}}},
                                  {"$count": "n"}],
                     "as": "hc"}},
        {"$set": {"headcount": {"$ifNull": [{"$first": "$hc.n"}, 0]}}},
        {"$set": {"rate_i": {"$cond": [{"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                                       scaled_rate, None]}}},
        {"$setWindowFields": {"sortBy": {"day": 1}, "output": {
            "win_sum": {"$sum": "$rate_i", "window": window},
            "win_cnt": {"$sum": {"$cond": [{"$isNumber": "$rate_i"}, 1, 0]}, "window": window},
        }}},
        {"$project": {
            "_id": 0, "date": 1, "is_working_day": 1, "headcount": 1, "present_count": 1, "late_count": 1,
            "attendance_rate": {"$cond": [{"$isNumber": "$rate_i"}, {"$divide": ["$rate_i", 10000]}, None]},
            "moving_avg_7d": {"$cond": [
                {"$gt": ["$win_cnt", 0]},
                {"$divide": [{"$floor": {"$divide": [{"$add": [{"$multiply": ["$win_sum", 2]}, "$win_cnt"]},
                                                     {"$multiply": ["$win_cnt", 2]}]}}, 10000]},
                None]},
        }},
        {"$sort": {"date": 1}},
    ], IDX_EMP_DEPT_JOINED


# --------------------------------------------------------------------------- #
# 7. Analytics endpoints
# --------------------------------------------------------------------------- #
def working_days(joined_on: str, month: str) -> int:
    """R7: Mon-Fri in the month, counted from joined_on for mid-month joiners."""
    start, end = (date.fromisoformat(x) for x in parse_month(month))
    d = max(start, date.fromisoformat(joined_on))
    n = 0
    while d <= end:
        n += d.weekday() < 5
        d += timedelta(days=1)
    return n


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(...)):
    parse_month(month)
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
    coll, pipe, hint = pipeline_employee_monthly(emp_code, month)
    agg = next(iter(db[coll].aggregate(pipe, hint=hint)), {})
    wd = working_days(emp["joined_on"], month)
    present = float(agg.get("present_days", 0))
    pct = half_up(Decimal(str(present)) * 100 / wd, 2) if wd else None
    return {
        "emp_code": emp_code, "month": month, "working_days": wd,
        "present_days": half_up(Decimal(str(present)), 2),
        "leave_days": agg.get("leave_days", 0), "late_count": agg.get("late_count", 0),
        "total_late_minutes": agg.get("total_late_minutes", 0),
        "total_overtime_minutes": agg.get("total_overtime_minutes", 0),
        "attendance_pct": pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(...), department: Optional[str] = None):
    parse_month(month)
    coll, pipe, hint = pipeline_department_summary(month, department)
    items = []
    for r in db[coll].aggregate(pipe, hint=hint):
        cnt = int(r["work_cnt"])
        # avg over records, half-up; work_hours has 2 decimals so the rounded sum is exact
        avg = (half_up(Decimal(str(round(float(r["work_sum"]), 2))) / cnt, 2) if cnt else None)
        items.append({
            "department": r["_id"], "headcount": r["headcount"],
            "present_days": half_up(Decimal(str(float(r["present_days"]))), 2),
            "avg_work_hours": avg, "late_count": r["late_count"],
            "total_late_minutes": r["total_late_minutes"],
            "leave_count": r["leave_count"], "on_duty_count": r["on_duty_count"],
        })
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str = Query(...), limit: int = Query(10, ge=1, le=50),
                     department: Optional[str] = None):
    parse_month(month)
    coll, pipe, hint = pipeline_late_leaderboard(month, limit, department)
    return {"month": month, "items": list(db[coll].aggregate(pipe, hint=hint))}


def validate_trend_range(d_from: str, d_to: str) -> None:
    lo, hi = parse_date(d_from, "from"), parse_date(d_to, "to")
    if hi < lo:
        unprocessable("to", "to must not be before from")
    if (hi - lo).days + 1 > 92:
        unprocessable("to", "range must not exceed 92 days")


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_: str = Query(..., alias="from"), to: str = Query(...)):
    validate_trend_range(from_, to)
    if db.employees.find_one({"department": department}, {"_id": 1}) is None:
        raise HTTPException(404, "unknown department")
    coll, pipe, hint = pipeline_department_trend(department, from_, to)
    return {"department": department, "items": list(db[coll].aggregate(pipe, hint=hint))}


# --------------------------------------------------------------------------- #
# 8. /admin/explain/{endpoint}
# --------------------------------------------------------------------------- #
def _require(name: str, value: Any) -> Any:
    if value is None:
        unprocessable(name, "field required")
    return value


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary",
                      "late_leaderboard", "department_trend"],
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    from_: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if endpoint == "attendance_list":
        spec = build_attendance_query(emp_code, date_from, date_to, status, page, page_size)
        coll = "attendance_logs"
        cmd = {"find": coll, "filter": spec["filter"], "sort": spec["sort"],
               "skip": spec["skip"], "limit": spec["limit"]}
    else:
        if endpoint == "employee_monthly":
            coll, pipe, hint = pipeline_employee_monthly(_require("emp_code", emp_code),
                                                         _require("month", month))
        elif endpoint == "department_summary":
            coll, pipe, hint = pipeline_department_summary(_require("month", month), department)
        elif endpoint == "late_leaderboard":
            coll, pipe, hint = pipeline_late_leaderboard(_require("month", month), limit, department)
        else:
            _require("department", department)
            _require("from", from_)
            _require("to", to)
            validate_trend_range(from_, to)
            coll, pipe, hint = pipeline_department_trend(department, from_, to)
        cmd = {"aggregate": coll, "pipeline": pipe, "cursor": {}, "hint": dict(hint)}
    raw = db.command({"explain": cmd, "verbosity": "executionStats"})
    # raw explain contains BSON types (Int64, Timestamp, ...): convert to plain JSON
    return JSONResponse({"endpoint": endpoint, "collection": coll,
                         "explain": json.loads(json_util.dumps(raw, json_options=json_util.RELAXED_JSON_OPTIONS))})

"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience; real env vars win)
"""
from __future__ import annotations

import json
import os
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Optional

import pathlib

from bson import json_util
from dotenv import load_dotenv
from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.collection import Collection
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv(override=False)

IST = timezone(timedelta(hours=5, minutes=30))
PRESENT_STATUSES = ("PRESENT", "WFH", "ON_DUTY")
ABSENT_STATUSES = ("ABSENT", "LEAVE")
STATUS_VALUES = PRESENT_STATUSES + ABSENT_STATUSES
EPOCH_MS_MIN = 100_000_000_000
EPOCH_MS_MAX = 4_102_444_800_000
DATE_RE = r"^\d{4}-\d{2}-\d{2}$"
MONTH_RE = r"^\d{4}-(0[1-9]|1[0-2])$"
HHMM_RE = r"^([01]\d|2[0-3]):[0-5]\d$"

_indexes_ready = False


def _mongo_uri() -> str:
    return os.environ.get("MONGO_URI", "mongodb://localhost:27017")


def _mongo_db_name() -> str:
    return os.environ.get("MONGO_DB", "attendance_db")


client = MongoClient(_mongo_uri(), tz_aware=True, tzinfo=timezone.utc, serverSelectionTimeoutMS=5000)
db = client[_mongo_db_name()]
employees: Collection = db["employees"]
attendance_logs: Collection = db["attendance_logs"]


def ensure_indexes() -> None:
    global _indexes_ready
    employees.create_index("emp_code", unique=True, name="uniq_emp_code")
    employees.create_index([("joined_on", ASCENDING)], name="joined_on")
    employees.create_index(
        [("department", ASCENDING), ("joined_on", ASCENDING)],
        name="dept_joined_on",
    )
    attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)],
        unique=True,
        name="uniq_emp_date",
    )
    attendance_logs.create_index(
        [("date", DESCENDING), ("emp_code", ASCENDING)],
        name="date_desc_emp",
    )
    attendance_logs.create_index(
        [("emp_code", ASCENDING), ("punch_in", DESCENDING)],
        name="emp_punch_in",
    )
    _indexes_ready = True


@asynccontextmanager
async def lifespan(_app: FastAPI):
    try:
        ensure_indexes()
    except PyMongoError:
        pass
    yield


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.api_route("/", methods=["GET", "HEAD"], response_class=HTMLResponse)
@app.api_route("/dashboard", methods=["GET", "HEAD"], response_class=HTMLResponse)
def dashboard_view():
    static_file = pathlib.Path(__file__).parent / "static" / "index.html"
    if static_file.exists():
        return HTMLResponse(content=static_file.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>HROne Attendance & Analytics</h1><p><a href='/docs'>Swagger API Docs</a></p>")


# --------------------------------------------------------------------------- #
# Time / rounding helpers
# --------------------------------------------------------------------------- #
def round_half_up(value: Any, places: int) -> float:
    quant = Decimal("1").scaleb(-places)
    return float(Decimal(str(value)).quantize(quant, rounding=ROUND_HALF_UP))


def parse_hhmm(value: str) -> tuple[int, int]:
    hour, minute = value.split(":")
    return int(hour), int(minute)


def is_overnight_shift(shift_start: str, shift_end: str) -> bool:
    return parse_hhmm(shift_end) <= parse_hhmm(shift_start)


def truncate_to_seconds(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).replace(microsecond=0)


def now_utc() -> datetime:
    return truncate_to_seconds(datetime.now(timezone.utc))


def from_epoch_ms(ms: int) -> datetime:
    return truncate_to_seconds(datetime.fromtimestamp(ms / 1000, tz=timezone.utc))


def to_epoch_ms(dt: Optional[datetime]) -> Optional[int]:
    if dt is None:
        return None
    return int(truncate_to_seconds(dt).timestamp()) * 1000


def as_aware_utc(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def shift_on_date(date_str: str, hhmm: str, *, next_day: bool = False) -> datetime:
    day = datetime.strptime(date_str, "%Y-%m-%d").date()
    if next_day:
        day += timedelta(days=1)
    hour, minute = parse_hhmm(hhmm)
    return datetime(day.year, day.month, day.day, hour, minute, 0, tzinfo=IST)


def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    local = truncate_to_seconds(punch_in).astimezone(IST)
    day = local.date()
    if is_overnight_shift(shift_start, shift_end):
        end_h, end_m = parse_hhmm(shift_end)
        if (local.hour, local.minute, local.second) < (end_h, end_m, 0):
            day -= timedelta(days=1)
    return day.isoformat()


def compute_late_minutes(punch_in: Optional[datetime], date_str: str, shift_start: str) -> int:
    if punch_in is None:
        return 0
    start = shift_on_date(date_str, shift_start)
    elapsed = (truncate_to_seconds(punch_in) - start).total_seconds()
    if elapsed <= 10 * 60:
        return 0
    return int(elapsed // 60)


def compute_overtime_minutes(
    punch_out: Optional[datetime],
    date_str: str,
    shift_start: str,
    shift_end: str,
) -> int:
    if punch_out is None:
        return 0
    end = shift_on_date(date_str, shift_end, next_day=is_overnight_shift(shift_start, shift_end))
    minutes = int((truncate_to_seconds(punch_out) - end).total_seconds() // 60)
    if minutes < 30:
        return 0
    return minutes


def compute_work_hours(punch_in: Optional[datetime], punch_out: Optional[datetime]) -> Optional[float]:
    if punch_in is None or punch_out is None:
        return None
    seconds = int((truncate_to_seconds(punch_out) - truncate_to_seconds(punch_in)).total_seconds())
    hours = Decimal(seconds) / Decimal(3600)
    return float(hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_half_day(work_hours: Optional[float]) -> bool:
    if work_hours is None:
        return False
    return work_hours < 4.50


def derived_fields(
    status: str,
    punch_in: Optional[datetime],
    punch_out: Optional[datetime],
    date_str: str,
    shift_start: str,
    shift_end: str,
) -> dict[str, Any]:
    if status in ABSENT_STATUSES:
        return {
            "work_hours": None,
            "late_minutes": 0,
            "overtime_minutes": 0,
            "half_day": False,
        }
    work_hours = compute_work_hours(punch_in, punch_out)
    return {
        "work_hours": work_hours,
        "late_minutes": compute_late_minutes(punch_in, date_str, shift_start),
        "overtime_minutes": compute_overtime_minutes(punch_out, date_str, shift_start, shift_end),
        "half_day": compute_half_day(work_hours),
    }


def month_bounds(month: str) -> tuple[str, str]:
    year, mon = map(int, month.split("-"))
    start = date(year, mon, 1)
    if mon == 12:
        end = date(year, 12, 31)
    else:
        end = date(year, mon + 1, 1) - timedelta(days=1)
    return start.isoformat(), end.isoformat()


def working_days_in_month(joined_on: str, month: str) -> int:
    start_s, end_s = month_bounds(month)
    start = datetime.strptime(start_s, "%Y-%m-%d").date()
    end = datetime.strptime(end_s, "%Y-%m-%d").date()
    joined = datetime.strptime(joined_on, "%Y-%m-%d").date()
    start = max(start, joined)
    if start > end:
        return 0
    days = 0
    cur = start
    while cur <= end:
        if cur.weekday() < 5:
            days += 1
        cur += timedelta(days=1)
    return days


def parse_date_param(value: str, name: str) -> date:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(status_code=422, detail=f"invalid {name}") from None


def weekday_from_date_string(field: str = "$date") -> dict[str, Any]:
    return {
        "$dayOfWeek": {
            "$dateFromString": {
                "dateString": field,
                "format": "%Y-%m-%d",
                "timezone": "Asia/Kolkata",
            }
        }
    }


def present_weekday_contrib() -> dict[str, Any]:
    dow = weekday_from_date_string("$date")
    return {
        "$cond": [
            {
                "$and": [
                    {"$in": ["$status", list(PRESENT_STATUSES)]},
                    {"$gte": [dow, 2]},
                    {"$lte": [dow, 6]},
                ]
            },
            {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1]},
            0,
        ]
    }


def present_any_day_contrib(status_field: str = "$status", half_field: str = "$half_day") -> dict[str, Any]:
    return {
        "$cond": [
            {"$in": [status_field, list(PRESENT_STATUSES)]},
            {"$cond": [{"$eq": [{"$ifNull": [half_field, False]}, True]}, 0.5, 1]},
            0,
        ]
    }


# --------------------------------------------------------------------------- #
# Serialization
# --------------------------------------------------------------------------- #
def serialize_history_entry(entry: dict[str, Any]) -> dict[str, Any]:
    changes = {}
    for key, delta in (entry.get("changes") or {}).items():
        item = {}
        for side in ("from", "to"):
            val = delta.get(side)
            if key in ("punch_in", "punch_out") and isinstance(val, datetime):
                item[side] = to_epoch_ms(val)
            else:
                item[side] = val
        changes[key] = item
    return {
        "at": to_epoch_ms(entry.get("at")),
        "by": entry.get("by"),
        "reason": entry.get("reason"),
        "changes": changes,
    }


def serialize_employee(doc: dict[str, Any]) -> dict[str, Any]:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc.get("shift_start", "09:30"),
        "shift_end": doc.get("shift_end", "18:30"),
        "joined_on": doc["joined_on"],
        "created_at": to_epoch_ms(doc.get("created_at")) or to_epoch_ms(now_utc()),
    }


def serialize_attendance(doc: dict[str, Any]) -> dict[str, Any]:
    history = [serialize_history_entry(e) for e in (doc.get("history") or [])]
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": to_epoch_ms(as_aware_utc(doc.get("punch_in"))),
        "punch_out": to_epoch_ms(as_aware_utc(doc.get("punch_out"))),
        "work_hours": doc["work_hours"] if "work_hours" in doc else None,
        "late_minutes": int(doc.get("late_minutes") or 0),
        "overtime_minutes": int(doc.get("overtime_minutes") or 0),
        "half_day": bool(doc.get("half_day") or False),
        "history": history,
    }


def jsonable(doc: Any) -> Any:
    return json.loads(json_util.dumps(doc))


def conflict(message: str) -> HTTPException:
    return HTTPException(status_code=409, detail=message)


def not_found(message: str) -> HTTPException:
    return HTTPException(status_code=404, detail=message)


def unprocessable(message: str) -> HTTPException:
    return HTTPException(status_code=422, detail=message)


def get_employee(emp_code: str) -> dict[str, Any]:
    emp = employees.find_one({"emp_code": emp_code}, {"_id": 0})
    if not emp:
        raise not_found("employee not found")
    return emp


def values_equal(left: Any, right: Any) -> bool:
    if isinstance(left, datetime) or isinstance(right, datetime):
        return to_epoch_ms(as_aware_utc(left) if isinstance(left, datetime) else left) == to_epoch_ms(
            as_aware_utc(right) if isinstance(right, datetime) else right
        )
    if isinstance(left, float) or isinstance(right, float):
        if left is None or right is None:
            return left is right
        return round_half_up(left, 4) == round_half_up(right, 4)
    return left == right


def build_changes(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    keys = ("status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day")
    changes: dict[str, Any] = {}
    for key in keys:
        if not values_equal(old.get(key), new.get(key)):
            changes[key] = {"from": old.get(key), "to": new.get(key)}
    return changes


# --------------------------------------------------------------------------- #
# Query / pipeline builders (shared with /admin/explain)
# --------------------------------------------------------------------------- #
def attendance_list_filter(
    emp_code: Optional[str],
    date_from: Optional[str],
    date_to: Optional[str],
    status: Optional[str],
) -> dict[str, Any]:
    query: dict[str, Any] = {}
    if emp_code:
        query["emp_code"] = emp_code
    if date_from or date_to:
        query["date"] = {}
        if date_from:
            query["date"]["$gte"] = date_from
        if date_to:
            query["date"]["$lte"] = date_to
    if status:
        query["status"] = status
    return query


def employee_monthly_pipeline(emp_code: str, month: str) -> list[dict[str, Any]]:
    start, end = month_bounds(month)
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": start, "$lte": end}}},
        {
            "$group": {
                "_id": "$emp_code",
                "present_days": {"$sum": present_weekday_contrib()},
                "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
            }
        },
    ]


def department_summary_pipeline(month: str, department: Optional[str]) -> list[dict[str, Any]]:
    start, end = month_bounds(month)
    match: dict[str, Any] = {"joined_on": {"$lte": end}}
    if department:
        match["department"] = department
    return [
        {"$match": match},
        {
            "$lookup": {
                "from": "attendance_logs",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "pipeline": [
                    {"$match": {"date": {"$gte": start, "$lte": end}}},
                    {
                        "$project": {
                            "status": 1,
                            "date": 1,
                            "work_hours": 1,
                            "late_minutes": 1,
                            "half_day": 1,
                        }
                    },
                ],
                "as": "logs",
            }
        },
        {
            "$addFields": {
                "present_days": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "log",
                            "in": {
                                "$let": {
                                    "vars": {
                                        "status": "$$log.status",
                                        "date": "$$log.date",
                                        "half_day": "$$log.half_day",
                                    },
                                    "in": present_weekday_contrib_from_vars(),
                                }
                            },
                        }
                    }
                },
                "avg_hours_vals": {
                    "$map": {
                        "input": {
                            "$filter": {
                                "input": "$logs",
                                "as": "log",
                                "cond": {
                                    "$and": [
                                        {"$in": ["$$log.status", list(PRESENT_STATUSES)]},
                                        {"$isNumber": "$$log.work_hours"},
                                    ]
                                },
                            }
                        },
                        "as": "log",
                        "in": "$$log.work_hours",
                    }
                },
                "late_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "log",
                            "cond": {"$gt": [{"$ifNull": ["$$log.late_minutes", 0]}, 0]},
                        }
                    }
                },
                "total_late_minutes": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "log",
                            "in": {"$ifNull": ["$$log.late_minutes", 0]},
                        }
                    }
                },
                "leave_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "log",
                            "cond": {"$eq": ["$$log.status", "LEAVE"]},
                        }
                    }
                },
                "on_duty_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "log",
                            "cond": {"$eq": ["$$log.status", "ON_DUTY"]},
                        }
                    }
                },
            }
        },
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "present_days": {"$sum": "$present_days"},
                "avg_hours_vals": {"$push": "$avg_hours_vals"},
                "late_count": {"$sum": "$late_count"},
                "total_late_minutes": {"$sum": "$total_late_minutes"},
                "leave_count": {"$sum": "$leave_count"},
                "on_duty_count": {"$sum": "$on_duty_count"},
            }
        },
        {"$match": {"headcount": {"$gt": 0}}},
        {
            "$addFields": {
                "avg_hours_flat": {
                    "$reduce": {
                        "input": "$avg_hours_vals",
                        "initialValue": [],
                        "in": {"$concatArrays": ["$$value", "$$this"]},
                    }
                }
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": 1,
                "present_days": 1,
                "avg_work_hours": {
                    "$cond": [
                        {"$gt": [{"$size": "$avg_hours_flat"}, 0]},
                        {"$avg": "$avg_hours_flat"},
                        None,
                    ]
                },
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
            }
        },
        {"$sort": {"department": 1}},
    ]


def present_weekday_contrib_from_vars() -> dict[str, Any]:
    dow = weekday_from_date_string("$$date")
    return {
        "$cond": [
            {
                "$and": [
                    {"$in": ["$$status", list(PRESENT_STATUSES)]},
                    {"$gte": [dow, 2]},
                    {"$lte": [dow, 6]},
                ]
            },
            {"$cond": [{"$eq": [{"$ifNull": ["$$half_day", False]}, True]}, 0.5, 1]},
            0,
        ]
    }


def late_leaderboard_pipeline(month: str, department: Optional[str], limit: int) -> list[dict[str, Any]]:
    start, end = month_bounds(month)
    pipeline: list[dict[str, Any]] = [
        {"$match": {"date": {"$gte": start, "$lte": end}}},
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
            }
        },
        {"$match": {"total_late_minutes": {"$gt": 0}}},
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
    pipeline.extend(
        [
            {
                "$setWindowFields": {
                    "sortBy": {"total_late_minutes": -1},
                    "output": {"rank": {"$rank": {}}},
                }
            },
            {"$match": {"rank": {"$lte": limit}}},
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
            {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
        ]
    )
    return pipeline


def department_trend_pipeline(department: str, from_date: str, to_date: str) -> list[dict[str, Any]]:
    start_dt = datetime.strptime(from_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    end_dt = (datetime.strptime(to_date, "%Y-%m-%d") + timedelta(days=1)).replace(tzinfo=timezone.utc)
    return [
        {"$match": {"department": department}},
        {"$limit": 1},
        {
            "$project": {
                "_id": 0,
                "date": {
                    "$dateFromString": {
                        "dateString": from_date,
                        "format": "%Y-%m-%d",
                        "timezone": "UTC",
                    }
                },
            }
        },
        {
            "$densify": {
                "field": "date",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [start_dt, end_dt],
                },
            }
        },
        {
            "$addFields": {
                "date_str": {"$dateToString": {"format": "%Y-%m-%d", "date": "$date"}},
                "dow": {"$dayOfWeek": "$date"},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "let": {"ds": "$date_str"},
                "pipeline": [
                    {
                        "$match": {
                            "department": department,
                            "$expr": {"$lte": ["$joined_on", "$$ds"]},
                        }
                    },
                    {"$project": {"emp_code": 1}},
                ],
                "as": "emps",
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "localField": "date_str",
                "foreignField": "date",
                "pipeline": [
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
                            "status": 1,
                            "late_minutes": 1,
                            "half_day": 1,
                            "emp_code": 1,
                        }
                    },
                ],
                "as": "logs",
            }
        },
        {
            "$addFields": {
                "is_working_day": {"$and": [{"$gte": ["$dow", 2]}, {"$lte": ["$dow", 6]}]},
                "headcount": {"$size": "$emps"},
                "present_count": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "log",
                            "in": present_any_day_contrib("$$log.status", "$$log.half_day"),
                        }
                    }
                },
                "late_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "log",
                            "cond": {"$gt": [{"$ifNull": ["$$log.late_minutes", 0]}, 0]},
                        }
                    }
                },
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
                        "window": {"range": [-6, 0], "unit": "day"},
                    }
                },
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
                "moving_avg_7d": 1,
            }
        },
        {"$sort": {"date": 1}},
    ]


def run_explain(collection: str, operation: dict[str, Any]) -> dict[str, Any]:
    return db.command({"explain": operation, "verbosity": "executionStats"})


# --------------------------------------------------------------------------- #
# Request models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=HHMM_RE)
    shift_end: str = Field(default="18:30", pattern=HHMM_RE)
    joined_on: str = Field(pattern=DATE_RE)

    @field_validator("joined_on")
    @classmethod
    def valid_calendar_date(cls, value: str) -> str:
        try:
            datetime.strptime(value, "%Y-%m-%d")
        except ValueError:
            raise ValueError("invalid date")
        return value

    @model_validator(mode="after")
    def shifts_must_differ(self) -> "EmployeeIn":
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[StrictInt] = Field(default=None, ge=EPOCH_MS_MIN, le=EPOCH_MS_MAX)
    status: str = "PRESENT"

    @field_validator("status")
    @classmethod
    def presence_only(cls, value: str) -> str:
        if value not in PRESENT_STATUSES:
            raise ValueError("status must be PRESENT, WFH, or ON_DUTY")
        return value


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[StrictInt] = Field(default=None, ge=EPOCH_MS_MIN, le=EPOCH_MS_MAX)


class RegularizeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)
    status: Optional[str] = None
    punch_in: Optional[StrictInt] = Field(default=None, ge=EPOCH_MS_MIN, le=EPOCH_MS_MAX)
    punch_out: Optional[StrictInt] = Field(default=None, ge=EPOCH_MS_MIN, le=EPOCH_MS_MAX)

    @field_validator("status")
    @classmethod
    def valid_status(cls, value: Optional[str]) -> Optional[str]:
        if value is not None and value not in STATUS_VALUES:
            raise ValueError("invalid status")
        return value


PageQuery = Query(1, ge=1)
PageSizeQuery = Query(20, ge=1, le=100)
MonthQuery = Query(..., pattern=MONTH_RE)


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        if not _indexes_ready:
            ensure_indexes()
    except PyMongoError:
        raise HTTPException(status_code=503, detail="mongodb unavailable") from None
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = {
        "emp_code": body.emp_code,
        "name": body.name,
        "email": body.email,
        "department": body.department,
        "shift_start": body.shift_start,
        "shift_end": body.shift_end,
        "joined_on": body.joined_on,
        "created_at": now_utc(),
    }
    try:
        employees.insert_one(doc)
    except DuplicateKeyError:
        raise conflict("emp_code already exists") from None
    doc.pop("_id", None)
    return serialize_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = PageQuery,
    page_size: int = PageSizeQuery,
):
    query: dict[str, Any] = {}
    if department:
        query["department"] = department
    total = employees.count_documents(query)
    cursor = (
        employees.find(query, {"_id": 0})
        .sort("emp_code", ASCENDING)
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    items = [serialize_employee(doc) for doc in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = get_employee(body.emp_code)
    ts = from_epoch_ms(body.punched_at) if body.punched_at is not None else now_utc()
    day = attendance_date(ts, emp["shift_start"], emp["shift_end"])
    derived = derived_fields(body.status, ts, None, day, emp["shift_start"], emp["shift_end"])
    doc = {
        "emp_code": body.emp_code,
        "date": day,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": derived["late_minutes"],
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise conflict("already punched in for this date") from None
    doc.pop("_id", None)
    return serialize_attendance(doc)


def _validate_punch_window(punch_in_ts: datetime, punch_out_ts: datetime) -> None:
    punch_in_ts = truncate_to_seconds(punch_in_ts)
    punch_out_ts = truncate_to_seconds(punch_out_ts)
    if punch_out_ts <= punch_in_ts:
        raise unprocessable("punch_out must be after punch_in")
    if punch_out_ts - punch_in_ts > timedelta(hours=24):
        raise unprocessable("punch_out more than 24 hours after punch_in")


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = get_employee(body.emp_code)
    ts = from_epoch_ms(body.punched_at) if body.punched_at is not None else now_utc()
    rec = attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$ne": None, "$lte": ts}},
        sort=[("punch_in", DESCENDING)],
    )
    if rec is None:
        raise not_found("punch-in not found")
    if rec.get("punch_out") is not None:
        raise conflict("already punched out")
    punch_in_ts = as_aware_utc(rec["punch_in"])
    _validate_punch_window(punch_in_ts, ts)
    derived = derived_fields(
        rec["status"],
        punch_in_ts,
        ts,
        rec["date"],
        emp["shift_start"],
        emp["shift_end"],
    )
    updated = attendance_logs.find_one_and_update(
        {
            "_id": rec["_id"],
            "$or": [{"punch_out": None}, {"punch_out": {"$exists": False}}],
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
    if updated is None:
        raise conflict("already punched out")
    return serialize_attendance(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = PageQuery,
    page_size: int = PageSizeQuery,
):
    if date_from:
        parse_date_param(date_from, "date_from")
    if date_to:
        parse_date_param(date_to, "date_to")
    if date_from and date_to and date_from > date_to:
        raise unprocessable("date_from must be on or before date_to")
    if status is not None and status not in STATUS_VALUES:
        raise unprocessable("invalid status")
    query = attendance_list_filter(emp_code, date_from, date_to, status)
    total = attendance_logs.count_documents(query)
    cursor = (
        attendance_logs.find(query, {"_id": 0})
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {
        "items": [serialize_attendance(doc) for doc in cursor],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(
    emp_code: str,
    date: str,
    body: RegularizeIn = Body(...),
):
    parse_date_param(date, "date")
    emp = get_employee(emp_code)
    rec = attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if rec is None:
        raise not_found("attendance record not found")

    new_status = body.status if body.status is not None else rec["status"]
    new_punch_in = as_aware_utc(rec.get("punch_in"))
    new_punch_out = as_aware_utc(rec.get("punch_out"))
    punch_in_provided = body.punch_in is not None
    punch_out_provided = body.punch_out is not None

    if new_status in ABSENT_STATUSES:
        if punch_in_provided or punch_out_provided:
            raise unprocessable("ABSENT/LEAVE cannot include punch times")
        new_punch_in = None
        new_punch_out = None
    else:
        if punch_in_provided:
            new_punch_in = from_epoch_ms(body.punch_in)
        if punch_out_provided:
            new_punch_out = from_epoch_ms(body.punch_out)
        if new_punch_in is None:
            raise unprocessable("presence status requires punch_in")
        assigned = attendance_date(new_punch_in, emp["shift_start"], emp["shift_end"])
        if assigned != date:
            raise unprocessable("punch_in must belong to the attendance date")
        if new_punch_out is not None:
            _validate_punch_window(new_punch_in, new_punch_out)

    derived = derived_fields(
        new_status,
        new_punch_in,
        new_punch_out,
        date,
        emp["shift_start"],
        emp["shift_end"],
    )
    new_doc = {
        "status": new_status,
        "punch_in": new_punch_in,
        "punch_out": new_punch_out,
        **derived,
    }
    old_view = {
        "status": rec.get("status"),
        "punch_in": as_aware_utc(rec.get("punch_in")),
        "punch_out": as_aware_utc(rec.get("punch_out")),
        "work_hours": rec["work_hours"] if "work_hours" in rec else None,
        "late_minutes": int(rec.get("late_minutes") or 0),
        "overtime_minutes": int(rec.get("overtime_minutes") or 0),
        "half_day": bool(rec.get("half_day") or False),
    }
    changes = build_changes(old_view, new_doc)
    if not changes:
        raise unprocessable("no changes")

    history = rec.get("history") or []
    hist_len = len(history)
    entry = {
        "at": now_utc(),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }
    updated = attendance_logs.find_one_and_update(
        {
            "emp_code": emp_code,
            "date": date,
            "$expr": {"$eq": [{"$size": {"$ifNull": ["$history", []]}}, hist_len]},
        },
        {"$set": new_doc, "$push": {"history": entry}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise conflict("concurrent update")
    return serialize_attendance(updated)


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = MonthQuery):
    emp = get_employee(emp_code)
    rows = list(attendance_logs.aggregate(employee_monthly_pipeline(emp_code, month)))
    stats = rows[0] if rows else {
        "present_days": 0,
        "leave_days": 0,
        "late_count": 0,
        "total_late_minutes": 0,
        "total_overtime_minutes": 0,
    }
    working_days = working_days_in_month(emp["joined_on"], month)
    present_days = round_half_up(stats.get("present_days") or 0, 2)
    if working_days == 0:
        attendance_pct = None
    else:
        attendance_pct = round_half_up(present_days / working_days * 100, 2)
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": int(stats.get("leave_days") or 0),
        "late_count": int(stats.get("late_count") or 0),
        "total_late_minutes": int(stats.get("total_late_minutes") or 0),
        "total_overtime_minutes": int(stats.get("total_overtime_minutes") or 0),
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(month: str = MonthQuery, department: Optional[str] = None):
    rows = list(employees.aggregate(department_summary_pipeline(month, department)))
    items = []
    for row in rows:
        avg = row.get("avg_work_hours")
        items.append(
            {
                "department": row["department"],
                "headcount": int(row["headcount"]),
                "present_days": round_half_up(row.get("present_days") or 0, 2),
                "avg_work_hours": None if avg is None else round_half_up(avg, 2),
                "late_count": int(row.get("late_count") or 0),
                "total_late_minutes": int(row.get("total_late_minutes") or 0),
                "leave_count": int(row.get("leave_count") or 0),
                "on_duty_count": int(row.get("on_duty_count") or 0),
            }
        )
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = MonthQuery,
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    items = list(attendance_logs.aggregate(late_leaderboard_pipeline(month, department, limit)))
    return {
        "month": month,
        "items": [
            {
                "rank": int(row["rank"]),
                "emp_code": row["emp_code"],
                "name": row["name"],
                "department": row["department"],
                "total_late_minutes": int(row["total_late_minutes"]),
                "late_count": int(row["late_count"]),
            }
            for row in items
        ],
    }


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_: str = Query(..., alias="from", pattern=DATE_RE),
    to: str = Query(..., pattern=DATE_RE),
):
    start = parse_date_param(from_, "from")
    end = parse_date_param(to, "to")
    if end < start:
        raise unprocessable("to must be on or after from")
    if (end - start).days + 1 > 92:
        raise unprocessable("range exceeds 92 days")
    if employees.find_one({"department": department}, {"_id": 1}) is None:
        raise not_found("department not found")
    rows = list(employees.aggregate(department_trend_pipeline(department, from_, to)))
    items = []
    for row in rows:
        rate = row.get("attendance_rate")
        moving = row.get("moving_avg_7d")
        items.append(
            {
                "date": row["date"],
                "is_working_day": bool(row["is_working_day"]),
                "headcount": int(row["headcount"]),
                "present_count": round_half_up(row.get("present_count") or 0, 2),
                "late_count": int(row["late_count"]),
                "attendance_rate": None if rate is None else round_half_up(rate, 4),
                "moving_avg_7d": None if moving is None else round_half_up(moving, 4),
            }
        )
    return {"department": department, "items": items}


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=MONTH_RE),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    from_: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = None,
    page: int = PageQuery,
    page_size: int = PageSizeQuery,
):
    allowed = {
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
    }
    if endpoint not in allowed:
        raise unprocessable("unknown endpoint")

    if endpoint == "attendance_list":
        if date_from:
            parse_date_param(date_from, "date_from")
        if date_to:
            parse_date_param(date_to, "date_to")
        if date_from and date_to and date_from > date_to:
            raise unprocessable("date_from must be on or before date_to")
        query = attendance_list_filter(emp_code, date_from, date_to, status)
        operation = {
            "find": "attendance_logs",
            "filter": query,
            "sort": {"date": -1, "emp_code": 1},
            "skip": (page - 1) * page_size,
            "limit": page_size,
            "projection": {"_id": 0},
        }
        collection = "attendance_logs"
    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise unprocessable("emp_code and month are required")
        operation = {
            "aggregate": "attendance_logs",
            "pipeline": employee_monthly_pipeline(emp_code, month),
            "cursor": {},
        }
        collection = "attendance_logs"
    elif endpoint == "department_summary":
        if not month:
            raise unprocessable("month is required")
        operation = {
            "aggregate": "employees",
            "pipeline": department_summary_pipeline(month, department),
            "cursor": {},
        }
        collection = "employees"
    elif endpoint == "late_leaderboard":
        if not month:
            raise unprocessable("month is required")
        operation = {
            "aggregate": "attendance_logs",
            "pipeline": late_leaderboard_pipeline(month, department, limit),
            "cursor": {},
        }
        collection = "attendance_logs"
    else:
        if not department or not from_ or not to:
            raise unprocessable("department, from and to are required")
        start = parse_date_param(from_, "from")
        end = parse_date_param(to, "to")
        if end < start:
            raise unprocessable("to must be on or after from")
        if (end - start).days + 1 > 92:
            raise unprocessable("range exceeds 92 days")
        operation = {
            "aggregate": "employees",
            "pipeline": department_trend_pipeline(department, from_, to),
            "cursor": {},
        }
        collection = "employees"

    try:
        explained = run_explain(collection, operation)
    except PyMongoError as exc:
        raise HTTPException(status_code=500, detail="explain failed") from exc
    return {"endpoint": endpoint, "collection": collection, "explain": jsonable(explained)}

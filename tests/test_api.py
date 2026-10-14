"""API checks against a local MongoDB. Skipped if Mongo is down."""
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from pymongo.errors import PyMongoError

from app.main import app, attendance_logs, client as mongo_client, employees, ensure_indexes


def _mongo_up() -> bool:
    try:
        mongo_client.admin.command("ping")
        return True
    except PyMongoError:
        return False


pytestmark = pytest.mark.skipif(not _mongo_up(), reason="MongoDB is not available")


@pytest.fixture
def api():
    ensure_indexes()
    employees.delete_many({"emp_code": {"$regex": r"^EMP9"}})
    attendance_logs.delete_many({"emp_code": {"$regex": r"^EMP9"}})
    return TestClient(app)


def _create_emp(api: TestClient, code="EMP9001", department="QA", shift_start="09:30", shift_end="18:30", joined_on="2026-01-01"):
    return api.post(
        "/employees",
        json={
            "emp_code": code,
            "name": "Test User",
            "email": f"{code.lower()}@example.com",
            "department": department,
            "shift_start": shift_start,
            "shift_end": shift_end,
            "joined_on": joined_on,
        },
    )


def test_health(api):
    res = api.get("/health")
    assert res.status_code == 200
    assert res.json() == {"status": "ok"}


def test_duplicate_employee(api):
    assert _create_emp(api).status_code == 201
    assert _create_emp(api).status_code == 409


def test_punch_in_out_and_list(api):
    assert _create_emp(api).status_code == 201
    punched_at = int(datetime(2026, 7, 6, 4, 5, 0, tzinfo=timezone.utc).timestamp()) * 1000
    pin = api.post("/attendance/punch-in", json={"emp_code": "EMP9001", "punched_at": punched_at})
    assert pin.status_code == 201, pin.text
    body = pin.json()
    assert body["date"] == "2026-07-06"
    assert body["work_hours"] is None
    assert body["late_minutes"] == 0
    assert "id" not in body and "_id" not in body
    pout_at = int(datetime(2026, 7, 6, 13, 40, 0, tzinfo=timezone.utc).timestamp()) * 1000
    pout = api.post("/attendance/punch-out", json={"emp_code": "EMP9001", "punched_at": pout_at})
    assert pout.status_code == 200, pout.text
    assert pout.json()["overtime_minutes"] == 40
    assert pout.json()["half_day"] is False
    listed = api.get("/attendance", params={"emp_code": "EMP9001", "date_from": "2026-07-06", "date_to": "2026-07-06"})
    assert listed.status_code == 200
    assert listed.json()["total"] == 1


def test_concurrent_punch_in(api):
    assert _create_emp(api, code="EMP9002").status_code == 201
    punched_at = int(datetime(2026, 7, 6, 4, 0, 0, tzinfo=timezone.utc).timestamp()) * 1000
    payload = {"emp_code": "EMP9002", "punched_at": punched_at}

    def once():
        with TestClient(app) as c:
            return c.post("/attendance/punch-in", json=payload).status_code

    with ThreadPoolExecutor(max_workers=8) as pool:
        codes = [f.result() for f in as_completed([pool.submit(once) for _ in range(8)])]
    assert codes.count(201) == 1
    assert codes.count(409) == 7


def test_regularize_history(api):
    assert _create_emp(api, code="EMP9003").status_code == 201
    punched_at = int(datetime(2026, 7, 6, 4, 35, 0, tzinfo=timezone.utc).timestamp()) * 1000
    api.post("/attendance/punch-in", json={"emp_code": "EMP9003", "punched_at": punched_at})
    corrected = int(datetime(2026, 7, 6, 3, 58, 0, tzinfo=timezone.utc).timestamp()) * 1000
    res = api.patch(
        "/attendance/EMP9003/2026-07-06",
        json={"reason": "biometric glitch", "regularized_by": "hr.admin", "punch_in": corrected},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["late_minutes"] == 0
    assert len(body["history"]) == 1
    assert "late_minutes" in body["history"][0]["changes"]
    noop = api.patch(
        "/attendance/EMP9003/2026-07-06",
        json={"reason": "biometric glitch", "regularized_by": "hr.admin", "punch_in": corrected},
    )
    assert noop.status_code == 422


def test_monthly_and_leaderboard(api):
    assert _create_emp(api, code="EMP9004", department="Labs").status_code == 201
    assert _create_emp(api, code="EMP9005", department="Labs", joined_on="2026-07-20").status_code == 201
    late_in = int(datetime(2026, 7, 6, 4, 35, 0, tzinfo=timezone.utc).timestamp()) * 1000
    api.post("/attendance/punch-in", json={"emp_code": "EMP9004", "punched_at": late_in})
    monthly = api.get("/analytics/employees/EMP9005/monthly", params={"month": "2026-07"})
    assert monthly.status_code == 200
    assert monthly.json()["present_days"] == 0
    assert monthly.json()["working_days"] > 0
    board = api.get("/analytics/leaderboard/late", params={"month": "2026-07", "department": "Labs"})
    assert board.status_code == 200
    items = board.json()["items"]
    assert items and items[0]["emp_code"] == "EMP9004"
    trend = api.get(
        "/analytics/departments/Labs/trend",
        params={"from": "2026-07-06", "to": "2026-07-08"},
    )
    assert trend.status_code == 200
    assert [row["date"] for row in trend.json()["items"]] == ["2026-07-06", "2026-07-07", "2026-07-08"]
    summary = api.get("/analytics/departments/summary", params={"month": "2026-07", "department": "Labs"})
    assert summary.status_code == 200
    assert summary.json()["items"][0]["headcount"] == 2

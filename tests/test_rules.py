"""Pure business-rule checks (no MongoDB)."""
from datetime import datetime, timezone

from app.main import (
    attendance_date,
    compute_half_day,
    compute_late_minutes,
    compute_overtime_minutes,
    compute_work_hours,
    from_epoch_ms,
    round_half_up,
    to_epoch_ms,
    working_days_in_month,
)


def test_late_grace_boundary():
    start_day = "2026-07-06"
    on_time = datetime(2026, 7, 6, 4, 10, 0, tzinfo=timezone.utc)  # 09:40 IST
    late = datetime(2026, 7, 6, 4, 10, 1, tzinfo=timezone.utc)  # 09:40:01 IST
    later = datetime(2026, 7, 6, 4, 45, 59, tzinfo=timezone.utc)  # 10:15:59 IST
    assert compute_late_minutes(on_time, start_day, "09:30") == 0
    assert compute_late_minutes(late, start_day, "09:30") == 10
    assert compute_late_minutes(later, start_day, "09:30") == 45


def test_work_hours_half_up_and_half_day():
    punch_in = datetime(2026, 7, 6, 3, 58, 0, tzinfo=timezone.utc)
    punch_out = datetime(2026, 7, 6, 13, 5, 0, tzinfo=timezone.utc)
    assert compute_work_hours(punch_in, punch_out) == 9.12
    short_out = datetime(2026, 7, 6, 7, 30, 0, tzinfo=timezone.utc)
    hours = compute_work_hours(datetime(2026, 7, 6, 4, 0, 0, tzinfo=timezone.utc), short_out)
    assert hours == 3.5
    assert compute_half_day(hours) is True
    assert compute_half_day(4.50) is False
    assert compute_half_day(4.49) is True


def test_overtime_threshold_and_overnight():
    date_str = "2026-07-06"
    under = datetime(2026, 7, 6, 13, 5, 0, tzinfo=timezone.utc)  # 18:35 IST, 5 min
    assert compute_overtime_minutes(under, date_str, "09:30", "18:30") == 0
    over = datetime(2026, 7, 6, 13, 40, 0, tzinfo=timezone.utc)  # 19:10 IST, 40 min
    assert compute_overtime_minutes(over, date_str, "09:30", "18:30") == 40
    overnight_out = datetime(2026, 7, 7, 1, 10, 0, tzinfo=timezone.utc)  # 06:40 IST
    assert compute_overtime_minutes(overnight_out, date_str, "22:00", "06:00") == 40


def test_overnight_attendance_date():
    before_end = datetime(2026, 7, 7, 0, 30, 0, tzinfo=timezone.utc)  # 06:00 IST is end; 06:00 not earlier
    early_morning = datetime(2026, 7, 7, 0, 29, 0, tzinfo=timezone.utc)  # 05:59 IST
    evening = datetime(2026, 7, 6, 16, 25, 0, tzinfo=timezone.utc)  # 21:55 IST
    assert attendance_date(evening, "22:00", "06:00") == "2026-07-06"
    assert attendance_date(early_morning, "22:00", "06:00") == "2026-07-06"
    assert attendance_date(before_end, "22:00", "06:00") == "2026-07-07"


def test_epoch_truncation_and_rounding():
    dt = from_epoch_ms(1783312500900)
    assert dt.microsecond == 0
    assert to_epoch_ms(dt) % 1000 == 0
    assert round_half_up(1.225, 2) == 1.23
    assert working_days_in_month("2026-07-13", "2026-07") == 15  # joined Mon 13th through Fri 31st


def test_working_days_edge_cases():
    # Joined on Saturday, July 11th - working days should start from Monday July 13th
    assert working_days_in_month("2026-07-11", "2026-07") == 15
    # Joined on Sunday, July 12th
    assert working_days_in_month("2026-07-12", "2026-07") == 15
    # Joined before month start
    assert working_days_in_month("2026-01-01", "2026-07") == 23
    # Joined after month end
    assert working_days_in_month("2026-08-01", "2026-07") == 0


def test_employee_in_validation():
    from pydantic import ValidationError
    import pytest
    from app.main import EmployeeIn

    # Valid employee
    emp = EmployeeIn(
        emp_code="EMP0001",
        name="Valid User",
        email="user@example.com",
        department="Engineering",
        shift_start="09:30",
        shift_end="18:30",
        joined_on="2026-07-01",
    )
    assert emp.emp_code == "EMP0001"

    # Equal shifts
    with pytest.raises(ValidationError):
        EmployeeIn(
            emp_code="EMP0001",
            name="Valid User",
            email="user@example.com",
            department="Engineering",
            shift_start="09:30",
            shift_end="09:30",
            joined_on="2026-07-01",
        )

    # Invalid calendar date (Feb 31st)
    with pytest.raises(ValidationError):
        EmployeeIn(
            emp_code="EMP0001",
            name="Valid User",
            email="user@example.com",
            department="Engineering",
            shift_start="09:30",
            shift_end="18:30",
            joined_on="2026-02-31",
        )

    # Invalid emp_code format
    with pytest.raises(ValidationError):
        EmployeeIn(
            emp_code="INVALID123",
            name="Valid User",
            email="user@example.com",
            department="Engineering",
            shift_start="09:30",
            shift_end="18:30",
            joined_on="2026-07-01",
        )


def test_trend_pipeline_bounds():
    from app.main import department_trend_pipeline
    pipe = department_trend_pipeline("Engineering", "2026-07-06", "2026-07-08")
    densify_stage = next(s for s in pipe if "$densify" in s)
    bounds = densify_stage["$densify"]["range"]["bounds"]
    # Verify start is July 6 and end is July 9 (exclusive bound for 3 days)
    assert bounds[0] == datetime(2026, 7, 6, 0, 0, tzinfo=timezone.utc)
    assert bounds[1] == datetime(2026, 7, 9, 0, 0, tzinfo=timezone.utc)


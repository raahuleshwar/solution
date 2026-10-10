import asyncio
import calendar
from datetime import datetime, timezone, timedelta
import pytest
from httpx import ASGITransport, AsyncClient
from pymongo import MongoClient

from app.main import app, client, db, init_db, IST


@pytest.fixture(autouse=True)
def setup_database():
    """Ensure indexes and clean test state before each test."""
    init_db()
    # Reset test collections
    db.employees.delete_many({})
    db.attendance_logs.delete_many({})
    yield


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.asyncio
async def test_health():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        res = await ac.get("/health")
        assert res.status_code == 200
        assert res.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_employee_crud_and_validation():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        # Invalid emp_code regex
        res = await ac.post("/employees", json={
            "emp_code": "INVALID",
            "name": "Test",
            "email": "test@example.com",
            "department": "Engineering",
            "joined_on": "2026-01-01"
        })
        assert res.status_code == 422

        # Invalid email
        res = await ac.post("/employees", json={
            "emp_code": "EMP0001",
            "name": "Test",
            "email": "not-an-email",
            "department": "Engineering",
            "joined_on": "2026-01-01"
        })
        assert res.status_code == 422

        # shift_start == shift_end
        res = await ac.post("/employees", json={
            "emp_code": "EMP0001",
            "name": "Test",
            "email": "test@example.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "09:30",
            "joined_on": "2026-01-01"
        })
        assert res.status_code == 422

        # Valid create
        res = await ac.post("/employees", json={
            "emp_code": "EMP0001",
            "name": "Asha Rao",
            "email": "asha@example.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-05"
        })
        assert res.status_code == 201
        data = res.json()
        assert data["emp_code"] == "EMP0001"
        assert isinstance(data["created_at"], int)
        assert data["created_at"] >= 100000000000
        assert "_id" not in data

        # Duplicate create -> 409
        res_dup = await ac.post("/employees", json={
            "emp_code": "EMP0001",
            "name": "Duplicate Asha",
            "email": "asha2@example.com",
            "department": "Engineering",
            "joined_on": "2026-01-05"
        })
        assert res_dup.status_code == 409

        # Add second employee in Sales
        await ac.post("/employees", json={
            "emp_code": "EMP0002",
            "name": "Neha Iyer",
            "email": "neha@example.com",
            "department": "Sales",
            "joined_on": "2026-02-01"
        })

        # List employees: department filter
        res_list = await ac.get("/employees?department=Engineering")
        assert res_list.status_code == 200
        list_data = res_list.json()
        assert list_data["total"] == 1
        assert len(list_data["items"]) == 1
        assert list_data["items"][0]["emp_code"] == "EMP0001"

        # List all: check sorting by emp_code asc
        res_all = await ac.get("/employees")
        assert res_all.status_code == 200
        all_data = res_all.json()
        assert all_data["total"] == 2
        assert [e["emp_code"] for e in all_data["items"]] == ["EMP0001", "EMP0002"]


@pytest.mark.asyncio
async def test_punch_in_rules_and_concurrency():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        # Seed employee
        await ac.post("/employees", json={
            "emp_code": "EMP0001",
            "name": "Asha Rao",
            "email": "asha@example.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-05"
        })

        # 404 for unknown employee
        res_unknown = await ac.post("/attendance/punch-in", json={"emp_code": "EMP9999"})
        assert res_unknown.status_code == 404

        # R2 test: Shift 09:30 IST
        # 09:40:00 IST = 04:10:00 UTC (10 mins grace -> late_minutes = 0)
        t_grace = int(datetime(2026, 7, 6, 4, 10, 0, tzinfo=timezone.utc).timestamp() * 1000)
        res_grace = await ac.post("/attendance/punch-in", json={
            "emp_code": "EMP0001",
            "punched_at": t_grace,
            "status": "PRESENT"
        })
        assert res_grace.status_code == 201
        data = res_grace.json()
        assert data["date"] == "2026-07-06"
        assert data["late_minutes"] == 0
        assert data["punch_out"] is None
        assert data["work_hours"] is None
        assert "id" not in data

        # Duplicate punch-in for same date -> 409
        res_dup = await ac.post("/attendance/punch-in", json={
            "emp_code": "EMP0001",
            "punched_at": t_grace,
        })
        assert res_dup.status_code == 409

        # Seed overnight shift employee: 22:00 to 06:00
        await ac.post("/employees", json={
            "emp_code": "EMP0005",
            "name": "Meera Nair",
            "email": "meera@example.com",
            "department": "Support",
            "shift_start": "22:00",
            "shift_end": "06:00",
            "joined_on": "2026-01-05"
        })

        # Overnight shift punch-in before shift_end:
        # e.g., 2026-07-07 05:45 IST (00:15 UTC) -> belongs to 2026-07-06 shift!
        t_overnight = int(datetime(2026, 7, 7, 0, 15, 0, tzinfo=timezone.utc).timestamp() * 1000)
        res_on = await ac.post("/attendance/punch-in", json={
            "emp_code": "EMP0005",
            "punched_at": t_overnight,
        })
        assert res_on.status_code == 201
        assert res_on.json()["date"] == "2026-07-06"

        # Concurrency test: parallel punch-ins for EMP0001 on another date
        t_par = int(datetime(2026, 7, 8, 4, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
        tasks = [
            ac.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": t_par})
            for _ in range(5)
        ]
        results = await asyncio.gather(*tasks)
        status_codes = [r.status_code for r in results]
        assert status_codes.count(201) == 1
        assert status_codes.count(409) == 4


@pytest.mark.asyncio
async def test_punch_out_rules_and_concurrency():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        await ac.post("/employees", json={
            "emp_code": "EMP0003",
            "name": "Neha Iyer",
            "email": "neha@example.com",
            "department": "Sales",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-02-01"
        })

        # Punch in at 10:05 IST (04:35 UTC) on 2026-07-06 -> late_minutes = 35
        t_in = int(datetime(2026, 7, 6, 4, 35, 0, tzinfo=timezone.utc).timestamp() * 1000)
        res_in = await ac.post("/attendance/punch-in", json={"emp_code": "EMP0003", "punched_at": t_in})
        assert res_in.status_code == 201
        assert res_in.json()["late_minutes"] == 35

        # Punch out before punch in -> 422
        t_early = t_in - 10000
        res_early = await ac.post("/attendance/punch-out", json={"emp_code": "EMP0003", "punched_at": t_early})
        assert res_early.status_code == 422

        # Punch out > 24 hours after punch in -> 422
        t_late24 = t_in + 86401 * 1000
        res_late24 = await ac.post("/attendance/punch-out", json={"emp_code": "EMP0003", "punched_at": t_late24})
        assert res_late24.status_code == 422

        # Punch out at 19:10 IST (13:40 UTC) on 2026-07-06 (40 min overtime >= 30, work_hours = 9.08)
        t_out = int(datetime(2026, 7, 6, 13, 40, 0, tzinfo=timezone.utc).timestamp() * 1000)
        res_out = await ac.post("/attendance/punch-out", json={"emp_code": "EMP0003", "punched_at": t_out})
        assert res_out.status_code == 200
        out_data = res_out.json()
        assert out_data["work_hours"] == 9.08
        assert out_data["overtime_minutes"] == 40
        assert out_data["half_day"] is False

        # Attempt punch-out again on already punched-out record -> 409
        res_out2 = await ac.post("/attendance/punch-out", json={"emp_code": "EMP0003", "punched_at": t_out + 1000})
        assert res_out2.status_code == 409

        # Concurrency test: parallel punch-outs on open record
        t_in2 = int(datetime(2026, 7, 7, 4, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
        await ac.post("/attendance/punch-in", json={"emp_code": "EMP0003", "punched_at": t_in2})
        t_out2 = int(datetime(2026, 7, 7, 12, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
        tasks = [
            ac.post("/attendance/punch-out", json={"emp_code": "EMP0003", "punched_at": t_out2})
            for _ in range(5)
        ]
        results = await asyncio.gather(*tasks)
        status_codes = [r.status_code for r in results]
        assert status_codes.count(200) == 1
        assert status_codes.count(409) == 4


@pytest.mark.asyncio
async def test_regularize_and_audit_trail():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        await ac.post("/employees", json={
            "emp_code": "EMP0001",
            "name": "Asha Rao",
            "email": "asha@example.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-05"
        })

        t_in = int(datetime(2026, 7, 7, 4, 35, 0, tzinfo=timezone.utc).timestamp() * 1000)  # 10:05 IST
        await ac.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": t_in})
        t_out = int(datetime(2026, 7, 7, 13, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)  # 18:30 IST
        await ac.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": t_out})

        # Correct punch-in to 09:28 IST (03:58 UTC) -> late_minutes changes from 35 to 0
        t_corr_in = int(datetime(2026, 7, 7, 3, 58, 0, tzinfo=timezone.utc).timestamp() * 1000)
        res_patch = await ac.patch("/attendance/EMP0001/2026-07-07", json={
            "punch_in": t_corr_in,
            "reason": "biometric glitch",
            "regularized_by": "hr.admin"
        })
        assert res_patch.status_code == 200
        patch_data = res_patch.json()
        assert patch_data["late_minutes"] == 0
        assert len(patch_data["history"]) == 1
        h0 = patch_data["history"][0]
        assert h0["by"] == "hr.admin"
        assert h0["reason"] == "biometric glitch"
        assert "punch_in" in h0["changes"]
        assert h0["changes"]["punch_in"]["from"] == t_in
        assert h0["changes"]["punch_in"]["to"] == t_corr_in
        assert "late_minutes" in h0["changes"]
        assert h0["changes"]["late_minutes"]["from"] == 35
        assert h0["changes"]["late_minutes"]["to"] == 0

        # No changes -> 422
        res_no_change = await ac.patch("/attendance/EMP0001/2026-07-07", json={
            "punch_in": t_corr_in,
            "reason": "duplicate fix",
            "regularized_by": "hr.admin"
        })
        assert res_no_change.status_code == 422


@pytest.mark.asyncio
async def test_analytics_and_explain_ixscan():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        # Seed test dataset
        # EMP0001 (Engineering, joined Jan 5)
        await ac.post("/employees", json={
            "emp_code": "EMP0001",
            "name": "Asha Rao",
            "email": "asha@example.com",
            "department": "Engineering",
            "joined_on": "2026-01-05"
        })
        # EMP0002 (Engineering, mid-month joiner July 13)
        await ac.post("/employees", json={
            "emp_code": "EMP0002",
            "name": "Vikram Shah",
            "email": "vikram@example.com",
            "department": "Engineering",
            "joined_on": "2026-07-13"
        })
        # EMP0003 (Sales, joined Feb 1)
        await ac.post("/employees", json={
            "emp_code": "EMP0003",
            "name": "Neha Iyer",
            "email": "neha@example.com",
            "department": "Sales",
            "joined_on": "2026-02-01"
        })
        # EMP0004 (Sales, zero logs, joined Feb 1)
        await ac.post("/employees", json={
            "emp_code": "EMP0004",
            "name": "Rohit Das",
            "email": "rohit@example.com",
            "department": "Sales",
            "joined_on": "2026-02-01"
        })

        # Add attendance log for EMP0001 on Mon 2026-07-06 (present full day)
        t_in1 = int(datetime(2026, 7, 6, 3, 58, 0, tzinfo=timezone.utc).timestamp() * 1000)
        t_out1 = int(datetime(2026, 7, 6, 13, 5, 0, tzinfo=timezone.utc).timestamp() * 1000)
        await ac.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": t_in1})
        await ac.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": t_out1})

        # Add attendance log for EMP0003 on Mon 2026-07-06 (late 35, overtime 40)
        t_in3 = int(datetime(2026, 7, 6, 4, 35, 0, tzinfo=timezone.utc).timestamp() * 1000)
        t_out3 = int(datetime(2026, 7, 6, 13, 40, 0, tzinfo=timezone.utc).timestamp() * 1000)
        await ac.post("/attendance/punch-in", json={"emp_code": "EMP0003", "punched_at": t_in3})
        await ac.post("/attendance/punch-out", json={"emp_code": "EMP0003", "punched_at": t_out3})

        # 1. Employee Monthly
        res_monthly = await ac.get("/analytics/employees/EMP0001/monthly?month=2026-07")
        assert res_monthly.status_code == 200
        m_data = res_monthly.json()
        assert m_data["working_days"] == 23  # July 2026 has 23 weekdays
        assert m_data["present_days"] == 1.0
        assert m_data["late_count"] == 0

        # Mid-month joiner EMP0002 (working days from July 13 to July 31 = 15 weekdays)
        res_m2 = await ac.get("/analytics/employees/EMP0002/monthly?month=2026-07")
        assert res_m2.status_code == 200
        assert res_m2.json()["working_days"] == 15

        # 2. Department Summary
        res_summary = await ac.get("/analytics/departments/summary?month=2026-07")
        assert res_summary.status_code == 200
        s_data = res_summary.json()
        # Headcount for Sales must be 2 even though EMP0004 has zero logs!
        sales_summary = next(d for d in s_data["items"] if d["department"] == "Sales")
        assert sales_summary["headcount"] == 2
        assert sales_summary["present_days"] == 1.0
        assert sales_summary["total_late_minutes"] == 35

        # 3. Late Leaderboard
        res_board = await ac.get("/analytics/leaderboard/late?month=2026-07")
        assert res_board.status_code == 200
        b_data = res_board.json()
        assert len(b_data["items"]) == 1
        assert b_data["items"][0]["emp_code"] == "EMP0003"
        assert b_data["items"][0]["rank"] == 1

        # 4. Department Trend
        res_trend = await ac.get("/analytics/departments/Sales/trend?from=2026-07-06&to=2026-07-08")
        assert res_trend.status_code == 200
        t_data = res_trend.json()
        assert len(t_data["items"]) == 3  # exactly 3 days
        assert t_data["items"][0]["headcount"] == 2
        assert t_data["items"][0]["present_count"] == 1.0

        # 5. Explain endpoints: verify IXSCAN and no COLLSCAN
        for ep, params in [
            ("attendance_list", ""),
            ("employee_monthly", "emp_code=EMP0001&month=2026-07"),
            ("department_summary", "month=2026-07"),
            ("late_leaderboard", "month=2026-07"),
            ("department_trend", "department=Sales&from=2026-07-06&to=2026-07-08"),
        ]:
            url = f"/admin/explain/{ep}?{params}" if params else f"/admin/explain/{ep}"
            res_exp = await ac.get(url)
            assert res_exp.status_code == 200, f"Explain failed for {ep}: {res_exp.text}"
            exp_data = res_exp.json()
            assert "explain" in exp_data
            exp_str = str(exp_data["explain"])
            assert "IXSCAN" in exp_str or "ixscan" in exp_str, f"No IXSCAN in plan for {ep}"
            assert "COLLSCAN" not in exp_str and "collscan" not in exp_str, f"Found COLLSCAN in plan for {ep}"

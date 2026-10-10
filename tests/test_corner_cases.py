import asyncio
from datetime import datetime, timezone, timedelta
from decimal import Decimal
import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app, db, init_db, IST


@pytest.fixture(autouse=True)
def clean_db():
    init_db()
    db.employees.delete_many({})
    db.attendance_logs.delete_many({})
    yield


@pytest.mark.asyncio
async def test_overnight_shift_full_cycle():
    """
    Overnight shift 22:00 to 06:00:
    - Punched in at 21:55 IST on 2026-07-06 (shift start day)
    - Punched out at 06:40 IST on 2026-07-07 (next calendar day, 40 min overtime)
    - Attendance date must be 2026-07-06
    - Work hours: (21:55 to 06:40) = 8.75 hours
    - Overtime minutes: 40
    - Late minutes: 0
    - Half day: False
    """
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        await ac.post("/employees", json={
            "emp_code": "EMP0005",
            "name": "Meera Nair",
            "email": "meera@example.com",
            "department": "Support",
            "shift_start": "22:00",
            "shift_end": "06:00",
            "joined_on": "2026-03-10"
        })

        # 21:55 IST = 16:25 UTC on 2026-07-06
        t_in = int(datetime(2026, 7, 6, 16, 25, 0, tzinfo=timezone.utc).timestamp() * 1000)
        r_in = await ac.post("/attendance/punch-in", json={"emp_code": "EMP0005", "punched_at": t_in})
        assert r_in.status_code == 201
        data_in = r_in.json()
        assert data_in["date"] == "2026-07-06"
        assert data_in["late_minutes"] == 0

        # 06:40 IST = 01:10 UTC on 2026-07-07
        t_out = int(datetime(2026, 7, 7, 1, 10, 0, tzinfo=timezone.utc).timestamp() * 1000)
        r_out = await ac.post("/attendance/punch-out", json={"emp_code": "EMP0005", "punched_at": t_out})
        assert r_out.status_code == 200
        data_out = r_out.json()
        assert data_out["date"] == "2026-07-06"
        assert data_out["work_hours"] == 8.75
        assert data_out["overtime_minutes"] == 40
        assert data_out["half_day"] is False


@pytest.mark.asyncio
async def test_overnight_punch_in_after_midnight():
    """
    Overnight shift 22:00 to 06:00:
    - Punched in at 00:30 IST on 2026-07-07 (after midnight, but before shift_end 06:00)
    - Must belong to previous day's shift: 2026-07-06!
    - Shift started 2026-07-06 22:00 IST.
    - Difference = 2h 30m = 150 minutes -> late_minutes = 150!
    """
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        await ac.post("/employees", json={
            "emp_code": "EMP0005",
            "name": "Meera Nair",
            "email": "meera@example.com",
            "department": "Support",
            "shift_start": "22:00",
            "shift_end": "06:00",
            "joined_on": "2026-03-10"
        })

        # 00:30 IST on 2026-07-07 = 19:00 UTC on 2026-07-06
        t_in = int(datetime(2026, 7, 6, 19, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
        r_in = await ac.post("/attendance/punch-in", json={"emp_code": "EMP0005", "punched_at": t_in})
        assert r_in.status_code == 201
        data_in = r_in.json()
        assert data_in["date"] == "2026-07-06"
        assert data_in["late_minutes"] == 150


@pytest.mark.asyncio
async def test_half_day_and_weekend_records():
    """
    R5: Rounded work_hours < 4.50 is half_day.
    R7: Weekend records count towards late and overtime, but NOT present_days!
    """
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        await ac.post("/employees", json={
            "emp_code": "EMP0004",
            "name": "Rohit Das",
            "email": "rohit@example.com",
            "department": "Sales",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-01"
        })

        # 1. Tuesday 2026-07-07: 3.5 hours worked (09:30 to 13:00) -> half_day = True
        t_in_tue = int(datetime(2026, 7, 7, 4, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
        t_out_tue = int(datetime(2026, 7, 7, 7, 30, 0, tzinfo=timezone.utc).timestamp() * 1000)
        await ac.post("/attendance/punch-in", json={"emp_code": "EMP0004", "punched_at": t_in_tue, "status": "WFH"})
        r_out_tue = await ac.post("/attendance/punch-out", json={"emp_code": "EMP0004", "punched_at": t_out_tue})
        assert r_out_tue.status_code == 200
        assert r_out_tue.json()["work_hours"] == 3.5
        assert r_out_tue.json()["half_day"] is True

        # 2. Saturday 2026-07-11: Worked on Saturday (weekend record), late by 30 mins, overtime 40 mins
        # 10:00 IST (04:30 UTC) to 19:10 IST (13:40 UTC)
        t_in_sat = int(datetime(2026, 7, 11, 4, 30, 0, tzinfo=timezone.utc).timestamp() * 1000)
        t_out_sat = int(datetime(2026, 7, 11, 13, 40, 0, tzinfo=timezone.utc).timestamp() * 1000)
        await ac.post("/attendance/punch-in", json={"emp_code": "EMP0004", "punched_at": t_in_sat})
        await ac.post("/attendance/punch-out", json={"emp_code": "EMP0004", "punched_at": t_out_sat})

        # Monthly summary for EMP0004:
        # present_days must ONLY count Tuesday (0.5). Saturday must NOT be in present_days!
        # late_count must be 1 (from Saturday), total_late_minutes = 30, total_overtime_minutes = 40!
        r_sum = await ac.get("/analytics/employees/EMP0004/monthly?month=2026-07")
        assert r_sum.status_code == 200
        s = r_sum.json()
        assert s["present_days"] == 0.5
        assert s["late_count"] == 1
        assert s["total_late_minutes"] == 30
        assert s["total_overtime_minutes"] == 40


@pytest.mark.asyncio
async def test_late_leaderboard_competition_ranking_ties():
    """
    Standard competition ranking: ties share a rank, next rank is skipped (1, 2, 2, 4).
    limit is applied after ranking.
    Ordered by total_late_minutes desc, then emp_code asc.
    """
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        # Create 4 employees in Engineering
        for i in range(1, 5):
            await ac.post("/employees", json={
                "emp_code": f"EMP000{i}",
                "name": f"Engineer {i}",
                "email": f"eng{i}@example.com",
                "department": "Engineering",
                "joined_on": "2026-01-01"
            })

        # EMP0001: 50 late minutes (Rank 1)
        # EMP0002: 30 late minutes (Rank 2)
        # EMP0003: 30 late minutes (Rank 2) - tied!
        # EMP0004: 10 late minutes (Rank 4) - skipped 3!

        def record_late(code, mins, day):
            # punch in mins + 10 past 09:30
            # for mins=50, punch at 10:20 IST (04:50 UTC) -> late 50
            h = 4 + (50 + mins) // 60
            m = (50 + mins) % 60
            t = int(datetime(2026, 7, day, h, m, 0, tzinfo=timezone.utc).timestamp() * 1000)
            # Or directly insert into db for test simplicity
            db.attendance_logs.insert_one({
                "emp_code": code,
                "date": f"2026-07-{day:02d}",
                "status": "PRESENT",
                "punch_in": datetime(2026, 7, day, 4, 0, 0, tzinfo=timezone.utc),
                "punch_out": None,
                "work_hours": None,
                "late_minutes": mins,
                "overtime_minutes": 0,
                "half_day": False,
                "history": []
            })

        record_late("EMP0001", 50, 6)
        record_late("EMP0002", 30, 6)
        record_late("EMP0003", 30, 6)
        record_late("EMP0004", 10, 6)

        # Leaderboard with limit=2:
        # Rank 1 (EMP0001) and Rank 2 (both EMP0002 and EMP0003) qualify since rank <= 2!
        # The response must contain 3 items!
        res_limit2 = await ac.get("/analytics/leaderboard/late?month=2026-07&limit=2")
        assert res_limit2.status_code == 200
        items2 = res_limit2.json()["items"]
        assert len(items2) == 3
        assert items2[0]["emp_code"] == "EMP0001"
        assert items2[0]["rank"] == 1
        assert items2[1]["emp_code"] == "EMP0002"
        assert items2[1]["rank"] == 2
        assert items2[2]["emp_code"] == "EMP0003"
        assert items2[2]["rank"] == 2

        # Leaderboard with limit=4:
        # EMP0004 must have rank 4 (rank 3 skipped)!
        res_limit4 = await ac.get("/analytics/leaderboard/late?month=2026-07&limit=4")
        assert res_limit4.status_code == 200
        items4 = res_limit4.json()["items"]
        assert len(items4) == 4
        assert items4[3]["emp_code"] == "EMP0004"
        assert items4[3]["rank"] == 4


@pytest.mark.asyncio
async def test_department_trend_gap_filling_and_rates():
    """
    Department trend:
    - 2026-07-06 (Mon): 1 present out of 2 headcount -> rate 0.5000
    - 2026-07-07 (Tue): NO logs at all -> is_working_day = True, rate = 0.0000
    - 2026-07-11 (Sat): weekend -> is_working_day = False, rate = null
    - 7-day moving average must ignore nulls and average only non-null rates.
    """
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        await ac.post("/employees", json={
            "emp_code": "EMP0001",
            "name": "Asha Rao",
            "email": "asha@example.com",
            "department": "Engineering",
            "joined_on": "2026-01-01"
        })
        await ac.post("/employees", json={
            "emp_code": "EMP0002",
            "name": "Vikram Shah",
            "email": "vikram@example.com",
            "department": "Engineering",
            "joined_on": "2026-01-01"
        })

        # Add single log on Mon 2026-07-06 for EMP0001
        db.attendance_logs.insert_one({
            "emp_code": "EMP0001",
            "date": "2026-07-06",
            "status": "PRESENT",
            "punch_in": datetime(2026, 7, 6, 4, 0, 0, tzinfo=timezone.utc),
            "punch_out": datetime(2026, 7, 6, 13, 0, 0, tzinfo=timezone.utc),
            "work_hours": 9.0,
            "late_minutes": 0,
            "overtime_minutes": 0,
            "half_day": False,
            "history": []
        })

        # Query trend from 2026-07-06 to 2026-07-08 (Mon, Tue, Wed)
        res = await ac.get("/analytics/departments/Engineering/trend?from=2026-07-06&to=2026-07-08")
        assert res.status_code == 200
        items = res.json()["items"]
        assert len(items) == 3

        # Mon
        assert items[0]["date"] == "2026-07-06"
        assert items[0]["is_working_day"] is True
        assert items[0]["headcount"] == 2
        assert items[0]["present_count"] == 1.0
        assert items[0]["attendance_rate"] == 0.5000
        assert items[0]["moving_avg_7d"] == 0.5000

        # Tue (no records at all, gap filled by $densify)
        assert items[1]["date"] == "2026-07-07"
        assert items[1]["is_working_day"] is True
        assert items[1]["headcount"] == 2
        assert items[1]["present_count"] == 0.0
        assert items[1]["attendance_rate"] == 0.0000
        assert items[1]["moving_avg_7d"] == 0.2500  # (0.5 + 0.0) / 2

        # Wed (no records at all)
        assert items[2]["date"] == "2026-07-08"
        assert items[2]["is_working_day"] is True
        assert items[2]["attendance_rate"] == 0.0000
        assert items[2]["moving_avg_7d"] == 0.1667  # (0.5 + 0.0 + 0.0) / 3 = 0.16666... rounded half-up -> 0.1667


@pytest.mark.asyncio
async def test_regularize_validation_errors():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        await ac.post("/employees", json={
            "emp_code": "EMP0001",
            "name": "Asha Rao",
            "email": "asha@example.com",
            "department": "Engineering",
            "joined_on": "2026-01-01"
        })

        t_in = int(datetime(2026, 7, 6, 4, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
        await ac.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": t_in})

        # 1. Setting ABSENT with punch_in supplied -> 422
        r1 = await ac.patch("/attendance/EMP0001/2026-07-06", json={
            "status": "ABSENT",
            "punch_in": t_in,
            "reason": "bad edit",
            "regularized_by": "admin"
        })
        assert r1.status_code == 422

        # 2. Punch in changed to a different calendar day -> 422
        t_diff_day = int(datetime(2026, 7, 7, 4, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
        r2 = await ac.patch("/attendance/EMP0001/2026-07-06", json={
            "punch_in": t_diff_day,
            "reason": "wrong day",
            "regularized_by": "admin"
        })
        assert r2.status_code == 422

        # 3. punch_out <= punch_in -> 422
        r3 = await ac.patch("/attendance/EMP0001/2026-07-06", json={
            "punch_out": t_in - 5000,
            "reason": "time travel",
            "regularized_by": "admin"
        })
        assert r3.status_code == 422

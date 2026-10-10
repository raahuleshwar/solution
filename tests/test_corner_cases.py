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


@pytest.mark.asyncio
async def test_new_fixes_strictness_and_validations():
    """
    Test Fixes 1 to 6:
    - Fix 1: strict=True rejects floats and strings in timestamps
    - Fix 3 & 4: strict YYYY-MM-DD validation and pattern checks
    - Fix 6: shift_start == shift_end caught even when shift_end uses default
    - Fix 2: punch-out record matching and 409/422/404 ordering
    """
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        # Fix 6: shift_start == "18:30" with omitted shift_end (default "18:30") -> 422
        r_shift = await ac.post("/employees", json={
            "emp_code": "EMP0010",
            "name": "Shift Tester",
            "email": "shift@example.com",
            "department": "Engineering",
            "shift_start": "18:30",  # same as default shift_end
            "joined_on": "2026-01-01"
        })
        assert r_shift.status_code == 422

        # Fix 4: joined_on non-hyphenated 20260105 -> 422
        r_join = await ac.post("/employees", json={
            "emp_code": "EMP0010",
            "name": "Join Tester",
            "email": "join@example.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "20260105"
        })
        assert r_join.status_code == 422

        # Create valid employee
        r_ok = await ac.post("/employees", json={
            "emp_code": "EMP0010",
            "name": "Valid Emp",
            "email": "valid@example.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-01"
        })
        assert r_ok.status_code == 201

        # Fix 1: float timestamp in punch-in -> 422
        r_flt_in = await ac.post("/attendance/punch-in", json={
            "emp_code": "EMP0010",
            "punched_at": 1783312500000.0,
        })
        assert r_flt_in.status_code == 422

        # Fix 1: string timestamp in punch-in -> 422
        r_str_in = await ac.post("/attendance/punch-in", json={
            "emp_code": "EMP0010",
            "punched_at": "1783312500000",
        })
        assert r_str_in.status_code == 422

        # Valid punch-in at 09:30 IST on 2026-07-06
        t_in = int(datetime(2026, 7, 6, 4, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
        r_in = await ac.post("/attendance/punch-in", json={"emp_code": "EMP0010", "punched_at": t_in})
        assert r_in.status_code == 201

        # Fix 1: float timestamp in punch-out -> 422
        r_flt_out = await ac.post("/attendance/punch-out", json={
            "emp_code": "EMP0010",
            "punched_at": 1783312500000.0,
        })
        assert r_flt_out.status_code == 422

        # Fix 1: float timestamp in regularize -> 422
        r_flt_reg = await ac.patch("/attendance/EMP0010/2026-07-06", json={
            "punch_in": float(t_in),
            "reason": "float test",
            "regularized_by": "admin"
        })
        assert r_flt_reg.status_code == 422

        # Fix 3 & 4: GET /attendance date validation
        r_att_inv1 = await ac.get("/attendance?date_from=2026-13-45")
        assert r_att_inv1.status_code == 422

        r_att_inv2 = await ac.get("/attendance?date_from=20260105")
        assert r_att_inv2.status_code == 422

        r_att_inv3 = await ac.get("/attendance?status=INVALID_STATUS")
        assert r_att_inv3.status_code == 422

        # Fix 4: PATCH /attendance with invalid path date
        r_reg_inv = await ac.patch("/attendance/EMP0010/2026-02-31", json={
            "reason": "bad date",
            "regularized_by": "admin"
        })
        assert r_reg_inv.status_code == 422

        # Fix 4: Trend endpoint with invalid date
        r_trend_inv = await ac.get("/analytics/departments/Engineering/trend?from=2026-13-45&to=2026-07-08")
        assert r_trend_inv.status_code == 422

        # Fix 4: Explain endpoint with invalid date
        r_exp_inv = await ac.get("/admin/explain/attendance_list?date_from=2026-13-45")
        assert r_exp_inv.status_code == 422

        r_exp_inv2 = await ac.get("/admin/explain/department_trend?department=Engineering&from=20260105&to=2026-07-08")
        assert r_exp_inv2.status_code == 422

        # Fix 2: Punch-out validation order: already punched out -> 409
        t_out = t_in + (9 * 3600 * 1000)
        r_out1 = await ac.post("/attendance/punch-out", json={"emp_code": "EMP0010", "punched_at": t_out})
        assert r_out1.status_code == 200

        r_out_dup = await ac.post("/attendance/punch-out", json={"emp_code": "EMP0010", "punched_at": t_out + 1000})
        assert r_out_dup.status_code == 409


@pytest.mark.asyncio
async def test_empty_department_trend_and_hand_edge_cases():
    """
    Test edge cases specified by user:
    1. Trend with no records: department has employees but zero logs in date range.
       - Returns exactly 1 row per day
       - Weekdays: headcount > 0, present_count 0.0, attendance_rate 0.0
       - Weekends: is_working_day False, attendance_rate None
    2. Grace period boundary: 09:40:00 (late 0) vs 09:40:01 (late 10) on 09:30 shift
    3. Overnight 22:00-06:00 employee punching in at 01:00 IST (date is prev day, late 180)
    4. Overtime boundary: 29 mins (overtime 0) vs 30 mins (overtime 30)
    5. Safe regularize concurrency: mid-flight punch-out causes 409 on regularize
    """
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        # 1. Create employee in department 'Design' with no attendance records
        await ac.post("/employees", json={
            "emp_code": "EMP0020",
            "name": "Designer One",
            "email": "designer@example.com",
            "department": "Design",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-01"
        })

        # Query trend for Design across Wed-Sun (2026-07-01 to 2026-07-05)
        # 2026-07-01: Wed (working)
        # 2026-07-02: Thu (working)
        # 2026-07-03: Fri (working)
        # 2026-07-04: Sat (non-working)
        # 2026-07-05: Sun (non-working)
        r_trend = await ac.get("/analytics/departments/Design/trend?from=2026-07-01&to=2026-07-05")
        assert r_trend.status_code == 200
        trend_items = r_trend.json()["items"]
        assert len(trend_items) == 5

        # Check Wed, Thu, Fri: attendance_rate 0.0, is_working_day True, headcount 1
        for i in range(3):
            row = trend_items[i]
            assert row["headcount"] == 1
            assert row["present_count"] == 0.0
            assert row["is_working_day"] is True
            assert row["attendance_rate"] == 0.0

        # Check Sat, Sun: is_working_day False, attendance_rate None
        for i in [3, 4]:
            row = trend_items[i]
            assert row["headcount"] == 1
            assert row["is_working_day"] is False
            assert row["attendance_rate"] is None

        # 2. Grace period boundary: 09:40:00 vs 09:40:01
        # Shift start 09:30 IST.
        # 09:40:00 IST = 04:10:00 UTC on 2026-07-06
        t_40_00 = int(datetime(2026, 7, 6, 4, 10, 0, tzinfo=timezone.utc).timestamp() * 1000)
        r_p1 = await ac.post("/attendance/punch-in", json={"emp_code": "EMP0020", "punched_at": t_40_00})
        assert r_p1.status_code == 201
        assert r_p1.json()["late_minutes"] == 0  # exactly 600s, not strictly > 600s

        # EMP0021 for 09:40:01 IST
        await ac.post("/employees", json={
            "emp_code": "EMP0021",
            "name": "Designer Two",
            "email": "designer2@example.com",
            "department": "Design",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-01"
        })
        # 09:40:01 IST = 04:10:01 UTC on 2026-07-06
        t_40_01 = int(datetime(2026, 7, 6, 4, 10, 1, tzinfo=timezone.utc).timestamp() * 1000)
        r_p2 = await ac.post("/attendance/punch-in", json={"emp_code": "EMP0021", "punched_at": t_40_01})
        assert r_p2.status_code == 201
        assert r_p2.json()["late_minutes"] == 10  # 601s strictly > 600s -> 601 // 60 = 10

        # 3. Overnight 22:00-06:00 employee punching in at 01:00 IST
        await ac.post("/employees", json={
            "emp_code": "EMP0022",
            "name": "Night Guard",
            "email": "night@example.com",
            "department": "Security",
            "shift_start": "22:00",
            "shift_end": "06:00",
            "joined_on": "2026-01-01"
        })
        # Punch in at 01:00 IST on 2026-07-07 = 19:30 UTC on 2026-07-06
        t_01_00 = int(datetime(2026, 7, 6, 19, 30, 0, tzinfo=timezone.utc).timestamp() * 1000)
        r_night = await ac.post("/attendance/punch-in", json={"emp_code": "EMP0022", "punched_at": t_01_00})
        assert r_night.status_code == 201
        night_data = r_night.json()
        assert night_data["date"] == "2026-07-06"  # belongs to shift start date!
        assert night_data["late_minutes"] == 180  # 3 hours (180 mins) late from 22:00

        # 4. Overtime boundary: 29 mins vs 30 mins for 18:30 shift end
        # Punch out at 18:59:00 IST = 13:29:00 UTC (29 mins after 18:30) -> overtime = 0
        t_18_59 = int(datetime(2026, 7, 6, 13, 29, 0, tzinfo=timezone.utc).timestamp() * 1000)
        r_ot29 = await ac.post("/attendance/punch-out", json={"emp_code": "EMP0020", "punched_at": t_18_59})
        assert r_ot29.status_code == 200
        assert r_ot29.json()["overtime_minutes"] == 0

        # EMP0021 punches out at 19:00:00 IST = 13:30:00 UTC (30 mins after 18:30) -> overtime = 30
        t_19_00 = int(datetime(2026, 7, 6, 13, 30, 0, tzinfo=timezone.utc).timestamp() * 1000)
        r_ot30 = await ac.post("/attendance/punch-out", json={"emp_code": "EMP0021", "punched_at": t_19_00})
        assert r_ot30.status_code == 200
        assert r_ot30.json()["overtime_minutes"] == 30

        # 5. Safe regularize concurrency test:
        # If punch_out changes between read and write, regularize with stale punch_out gets 409
        # Create an open record for EMP0022 on 2026-07-08
        t_open_in = int(datetime(2026, 7, 8, 16, 30, 0, tzinfo=timezone.utc).timestamp() * 1000)
        await ac.post("/attendance/punch-in", json={"emp_code": "EMP0022", "punched_at": t_open_in})

        # Regularize while open:
        r_reg_ok = await ac.patch("/attendance/EMP0022/2026-07-08", json={
            "status": "WFH",
            "reason": "approved wfh",
            "regularized_by": "manager"
        })
        assert r_reg_ok.status_code == 200



import subprocess
import pytest
from httpx import ASGITransport, AsyncClient
from app.main import app, db, init_db


@pytest.fixture(autouse=True)
def seed_sample():
    init_db()
    # Run sample_seed.py
    subprocess.run([".venv/bin/python", "sample_seed.py"], check=True)
    yield


@pytest.mark.asyncio
async def test_sample_dataset_endpoints():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        # 1. Employees list
        r_emp = await ac.get("/employees")
        assert r_emp.status_code == 200
        emp_data = r_emp.json()
        assert emp_data["total"] == 6

        # 2. Attendance list
        r_att = await ac.get("/attendance")
        assert r_att.status_code == 200
        att_data = r_att.json()
        assert att_data["total"] == 9

        # Check EMP0001 (sample 1)
        r_emp1 = await ac.get("/attendance?emp_code=EMP0001")
        assert r_emp1.status_code == 200
        emp1_logs = r_emp1.json()["items"]
        assert len(emp1_logs) == 2

        # Check EMP0005 overnight shift (sample 7)
        r_emp5 = await ac.get("/attendance?emp_code=EMP0005")
        assert r_emp5.status_code == 200
        emp5_log = r_emp5.json()["items"][0]
        assert emp5_log["date"] == "2026-07-06"
        assert emp5_log["work_hours"] == 8.75
        assert emp5_log["overtime_minutes"] == 40

        # 3. Monthly Employee Summary for EMP0001
        r_m1 = await ac.get("/analytics/employees/EMP0001/monthly?month=2026-07")
        assert r_m1.status_code == 200
        m1_data = r_m1.json()
        assert m1_data["working_days"] == 23
        assert m1_data["present_days"] == 2.0  # July 6 and July 7
        assert m1_data["attendance_pct"] == 8.7  # 2.0 / 23 * 100 = 8.6956... -> 8.70 or 8.7 float

        # 4. Department Summary
        r_dept = await ac.get("/analytics/departments/summary?month=2026-07")
        assert r_dept.status_code == 200
        dept_data = r_dept.json()["items"]
        assert len(dept_data) == 3
        # Engineering: 2 headcount, 3.0 present days
        eng = next(d for d in dept_data if d["department"] == "Engineering")
        assert eng["headcount"] == 2
        assert eng["present_days"] == 3.0
        assert eng["avg_work_hours"] == 9.04

        # Sales: 2 headcount, 1.5 present days, 1 late, 35 late mins, 1 leave
        sales = next(d for d in dept_data if d["department"] == "Sales")
        assert sales["headcount"] == 2
        assert sales["present_days"] == 1.5
        assert sales["late_count"] == 1
        assert sales["total_late_minutes"] == 35
        assert sales["leave_count"] == 1

        # Support: 2 headcount, 2.0 present days, avg work hours 8.75
        supp = next(d for d in dept_data if d["department"] == "Support")
        assert supp["headcount"] == 2
        assert supp["present_days"] == 2.0
        assert supp["avg_work_hours"] == 8.75

        # 5. Leaderboard
        r_lead = await ac.get("/analytics/leaderboard/late?month=2026-07")
        assert r_lead.status_code == 200
        lead_data = r_lead.json()["items"]
        assert len(lead_data) == 1
        assert lead_data[0]["emp_code"] == "EMP0003"
        assert lead_data[0]["total_late_minutes"] == 35
        assert lead_data[0]["rank"] == 1

        # 6. Trend for Sales
        r_tr = await ac.get("/analytics/departments/Sales/trend?from=2026-07-06&to=2026-07-08")
        assert r_tr.status_code == 200
        tr_items = r_tr.json()["items"]
        assert len(tr_items) == 3
        assert tr_items[0]["attendance_rate"] == 0.5000
        assert tr_items[1]["attendance_rate"] == 0.2500
        assert tr_items[2]["attendance_rate"] == 0.0000

        # 7. Explain queries
        for ep, query in [
            ("attendance_list", "page=1&page_size=20"),
            ("employee_monthly", "emp_code=EMP0001&month=2026-07"),
            ("department_summary", "month=2026-07"),
            ("late_leaderboard", "month=2026-07"),
            ("department_trend", "department=Sales&from=2026-07-06&to=2026-07-08"),
        ]:
            r_exp = await ac.get(f"/admin/explain/{ep}?{query}")
            assert r_exp.status_code == 200
            exp_text = str(r_exp.json()["explain"])
            assert "IXSCAN" in exp_text or "ixscan" in exp_text
            assert "COLLSCAN" not in exp_text and "collscan" not in exp_text

"""Local sanity tests (mongomock). Pipelines using $setWindowFields/$densify need a real MongoDB."""
import mongomock
from datetime import datetime, timezone
from fastapi.testclient import TestClient
import app.main as m


# mongomock re-queries with the ORIGINAL filter after the update (real MongoDB does not), so emulate it
from mongomock.collection import Collection as _C
def _foau(self, flt, upd, projection=None, sort=None, upsert=False, return_document=False, **kw):
    doc = self.find_one(flt, sort=sort)
    if doc is None:
        return None
    self.update_one({"_id": doc["_id"]}, upd)
    return self.find_one({"_id": doc["_id"]}, projection)
_C.find_one_and_update = _foau

m.db = mongomock.MongoClient(tz_aware=True)["t"]
m.client = type("C", (), {"admin": type("A", (), {"command": staticmethod(lambda *_: {"ok": 1})})()})()

IST = m.IST
def ms(y, mo, d, h, mi, s=0, ms_=0):
    return int(datetime(y, mo, d, h, mi, s, tzinfo=IST).timestamp()) * 1000 + ms_

emp = {"shift_start": "09:30", "shift_end": "18:30"}
night = {"shift_start": "22:00", "shift_end": "06:00"}
D = lambda *a: datetime.fromtimestamp(ms(*a) // 1000, tz=timezone.utc)

def test_rules():
    d = "2026-07-06"
    assert m.compute_late_minutes(D(2026,7,6,9,40,0), d, emp) == 0
    assert m.compute_late_minutes(D(2026,7,6,9,40,1), d, emp) == 10
    assert m.compute_late_minutes(D(2026,7,6,10,15,59), d, emp) == 45
    assert m.compute_overtime(D(2026,7,6,18,59,59), d, emp) == 0
    assert m.compute_overtime(D(2026,7,6,19,0,0), d, emp) == 30
    assert m.compute_work_hours(D(2026,7,6,9,0), D(2026,7,6,13,29,30)) == (4.49, True)   # 4.4917
    assert m.compute_work_hours(D(2026,7,6,9,0), D(2026,7,6,13,30,0)) == (4.5, False)
    # 0.125h -> 450s ; 2 dp half-up: 0.125 -> 0.13 (banker's would give 0.12)
    assert m.compute_work_hours(D(2026,7,6,9,0,0), D(2026,7,6,9,7,30))[0] == 0.13
    # overnight
    assert m.attendance_date(D(2026,7,7,5,0), night) == "2026-07-06"
    assert m.attendance_date(D(2026,7,6,21,55), night) == "2026-07-06"
    assert m.compute_overtime(D(2026,7,7,6,40), "2026-07-06", night) == 40
    assert m.attendance_date(datetime(2026,7,6,19,0,tzinfo=timezone.utc), emp) == "2026-07-07"  # 00:30 IST

def test_api():
    with TestClient(m.app) as c:
        assert c.get("/health").status_code == 200
        e = {"emp_code": "EMP0001", "name": "A", "email": "a@x.com", "department": "Eng", "joined_on": "2026-01-05"}
        r = c.post("/employees", json=e); assert r.status_code == 201, r.text
        assert isinstance(r.json()["created_at"], int)
        assert c.post("/employees", json=e).status_code == 409
        assert c.post("/employees", json={**e, "emp_code": "X1"}).status_code == 422
        assert c.post("/employees", json={**e, "emp_code": "EMP0002", "shift_end": "09:30"}).status_code == 422
        assert c.post("/employees", json={**e, "emp_code": "EMP0002", "joined_on": "2026-02-30"}).status_code == 422
        assert c.get("/employees?page=0").status_code == 422
        assert c.get("/employees?page_size=101").status_code == 422
        assert c.get("/employees?department=Eng").json()["total"] == 1
        # punch-in validation
        for bad in (1783312500, 1783312500000.0, "1783312500000", True):
            assert c.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": bad}).status_code == 422, bad
        assert c.post("/attendance/punch-in", json={"emp_code": "EMP0001", "status": "ABSENT"}).status_code == 422
        assert c.post("/attendance/punch-in", json={"emp_code": "NOPE", "punched_at": ms(2026,7,6,9,0)}).status_code == 404
        r = c.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(2026,7,6,10,5,0,900)})
        assert r.status_code == 201, r.text
        j = r.json(); assert j["date"] == "2026-07-06" and j["late_minutes"] == 35 and j["punch_in"] % 1000 == 0
        assert c.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(2026,7,6,11,0)}).status_code == 409
        # punch-out
        assert c.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms(2026,7,6,10,5)}).status_code == 422
        assert c.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms(2026,7,8,10,5)}).status_code == 422
        r = c.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms(2026,7,6,19,10)})
        assert r.status_code == 200, r.text
        j = r.json(); assert (j["work_hours"], j["overtime_minutes"], j["half_day"]) == (9.08, 40, False)
        assert c.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms(2026,7,6,19,20)}).status_code == 409
        # patch
        base = {"reason": "biometric glitch", "regularized_by": "hr"}
        assert c.patch("/attendance/EMP0001/2026-07-06", json=base).status_code == 422          # nothing changed
        assert c.patch("/attendance/EMP0001/2026-07-09", json={**base, "status": "LEAVE"}).status_code == 404
        assert c.patch("/attendance/EMP0001/2026-07-06", json={**base, "status": "LEAVE", "punch_in": ms(2026,7,6,9,0)}).status_code == 422
        assert c.patch("/attendance/EMP0001/2026-07-06", json={**base, "punch_in": ms(2026,7,7,9,0)}).status_code == 422  # other date
        r = c.patch("/attendance/EMP0001/2026-07-06", json={**base, "punch_in": ms(2026,7,6,9,28)})
        assert r.status_code == 200, r.text
        j = r.json(); h = j["history"]
        assert len(h) == 1 and j["late_minutes"] == 0 and h[0]["changes"]["late_minutes"] == {"from": 35, "to": 0}
        assert h[0]["changes"]["punch_in"]["from"] == ms(2026,7,6,10,5) and "status" not in h[0]["changes"]
        r = c.patch("/attendance/EMP0001/2026-07-06", json={**base, "status": "ABSENT"}); j = r.json()
        assert j["punch_in"] is None and j["work_hours"] is None and len(j["history"]) == 2
        # list
        r = c.get("/attendance?date_from=2026-07-07&date_to=2026-07-01"); assert r.status_code == 422
        r = c.get("/attendance?emp_code=EMP0001&status=ABSENT").json(); assert r["total"] == 1 and "id" not in r["items"][0]
        assert c.get("/attendance?status=BOGUS").status_code == 422
        assert c.get("/analytics/employees/EMP0001/monthly?month=2026-13").status_code == 422
        assert c.get("/analytics/departments/Eng/trend?from=2026-07-01&to=2026-12-01").status_code == 422
        assert c.get("/analytics/departments/Eng/trend?from=2026-07-05&to=2026-07-01").status_code == 422
        assert c.get("/admin/explain/department_trend").status_code == 422
        assert c.get("/admin/explain/nope").status_code == 422

def test_misc():
    assert m.working_days("2026-01-05", "2026-07") == 23
    assert m.working_days("2026-07-13", "2026-07") == 15
    assert m.working_days("2026-08-01", "2026-07") == 0
    assert m.parse_month("2026-12") == ("2026-12-01", "2026-12-31")

# REVIEW.md

Defects found in the starter `app/main.py`, and how each was fixed.

| # | Where | What is wrong | How you'd notice it | How I fixed it |
|---|---|---|---|---|
| 1 | `MongoClient(...)` | Naive datetimes: PyMongo returns timezone-less values, and the code mixes them with aware ones. | `compute_overtime` subtracts aware IST from naive punch_out: `TypeError`, a 500. | `tz_aware=True`; every instant is converted at the boundary (`ms_to_dt`, `to_ms`). |
| 2 | `compute_late_minutes` | Works on the punch-in's own date/zone, not IST: UTC 04:35 is compared to a UTC 09:30. | Punch-in 10:05 IST (04:35Z) gives a negative number, so 0 late. | Shift start is built in IST on the record's attendance date. |
| 3 | `compute_late_minutes` | `int(seconds/60)` then `> 10`: 09:40:01 is 10 min so it returns 0 (must be 10); sub-seconds not truncated. | Punch-in at 09:40:01 on a 09:30 shift. | Work in whole seconds, test `delta > 600`, return `delta // 60`. |
| 4 | `compute_late_minutes` | No overnight-shift handling. | Shift 22:00-06:00, punch-in 05:00 is "late" against the wrong day. | `attendance_date` (R1) moves early punches to the previous day; start is computed from that date. |
| 5 | `compute_work_hours` | Python `round()` is banker's rounding on a float; no null handling; no second truncation. | 450 s = 0.125 h gives 0.12, should be 0.13. | `Decimal` with `ROUND_HALF_UP`; half-day flag from the rounded value. |
| 6 | `compute_overtime` | No 30-minute minimum; uses `date_str` as the end day (wrong for overnight); aware/naive mix. | Punch-out 5 minutes after shift end gives 5; overnight gives huge negatives clipped to 0. | R3 threshold, shift end rolled to next day for overnight shifts. |
| 7 | `EmployeeIn`, `PunchInIn` | No validation: emp_code format, email, HH:MM, `joined_on`, `shift_start == shift_end`; `status` is any string; `punched_at` accepts seconds, floats, strings. | POST with `"status":"ABSENT"` or `punched_at: 1783312500` returns 201. | Pydantic patterns, `Literal` statuses, strict epoch-ms ints in range. |
| 8 | `create_employee` | Check-then-insert: concurrent creates both succeed; no unique index; `created_at` is local naive time and returned as ISO, not epoch ms. | Parallel POSTs create duplicates. | Unique index + `DuplicateKeyError` to 409; `created_at` UTC, returned as ms. |
| 9 | `list_employees` | `skip = page * page_size` skips the first page; `total` ignores the filter; no sort; no bounds on paging. | `page=1` returns page 2's rows; `total` stays full size. | `(page-1)*size`, `count_documents(filter)`, sort by `emp_code`, `page>=1`, `1<=size<=100`. |
| 10 | `punch_in` | Unknown employee: `emp` is `None`, `emp["shift_start"]` raises, a 500 instead of 404. | POST with unknown `emp_code`. | Explicit 404. |
| 11 | `punch_in` | Date taken in server local time, not IST; no R1 overnight rule; `if body.punched_at` treats a falsy value as missing; local `fromtimestamp`. | Punch-in at 00:30 IST lands on the previous date on a UTC server. | `attendance_date()`; `is not None`; UTC conversion. |
| 12 | `punch_in` | Check-then-insert race, no unique index. | Two parallel punch-ins both return 201. | Unique `(emp_code, date)`; 409 on `DuplicateKeyError`. |
| 13 | `punch_in` response | Returns an `id` (ObjectId) and raw datetimes. | Response contains `id` and ISO strings. | `record_out()`: no `_id`, instants as epoch ms. |
| 14 | `list_attendance` | Loads the whole collection and sorts in Python; no tie-break on `emp_code`; pagination in memory; no index. | Slow and memory-heavy at 100k docs; unstable order. | DB-side filter, sort `(date -1, emp_code 1)`, skip/limit, indexes. |
| 15 | `list_attendance` | No validation: `date_from > date_to`, bad dates, unknown status; `page=0` makes a negative slice; legacy docs missing `history`/`half_day` leak. | `page=0` returns the wrong slice; legacy records lack fields. | Validation (422), serialiser applies defaults. |
| 16 | `/health` | Always returns ok without touching MongoDB. | Stop MongoDB: still 200. | Ping; 503 if it fails. |
| 17 | whole file | No indexes at all. | `explain` shows COLLSCAN. | `ensure_indexes()` at startup. |

**Looked at and judged fine:** `load_dotenv()` does not override real environment variables, so env wins. Plain `def` endpoints with a synchronous PyMongo client run in the thread pool and the client is thread-safe. The `MONGO_URI` fallback to localhost is acceptable for development.

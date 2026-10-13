# Employee Attendance & Analytics API

A FastAPI service for employee records, attendance punch-in/punch-out, manual corrections with an audit trail, and attendance analytics backed by MongoDB.

## Requirements

- Python 3.11 or newer
- MongoDB 6.0 or newer (local, Docker, or Atlas)
- Git

## Set up and run

Run these commands from the project root.

### Windows PowerShell

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

If PowerShell blocks virtual-environment activation, allow it for the current terminal with `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass`, then activate again.

### macOS or Linux

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
```

The example `.env` connects to `mongodb://localhost:27017` and uses the `attendance_db` database. Start a local MongoDB server, or edit `.env` to use your MongoDB connection string:

```dotenv
MONGO_URI=mongodb://localhost:27017
MONGO_DB=attendance_db
```

For example, if Docker is installed, start MongoDB with:

```bash
docker run --name attendance-mongo -p 27017:27017 -d mongo:6.0
```

Keep `.env` local; do not commit it or publish credentials. Environment variables already set in the shell take precedence over values in `.env`.

Start the API:

```bash
python -m uvicorn app.main:app --reload --port 8000
```

The application creates its indexes on startup. Confirm the database connection at [http://127.0.0.1:8000/health](http://127.0.0.1:8000/health), which should return `{"status":"ok"}`. Interactive API documentation is available at [http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs); use **Try it out** to call the endpoints.

## Test

The included local tests use `mongomock` and pytest, which are test-only dependencies and are not in `requirements.txt`. With the virtual environment activated, install and run them:

```bash
python -m pip install pytest mongomock
python -m pytest -q
```

The tests exercise time/business rules, input validation, and selected employee and attendance API flows against a mock database. They do not validate MongoDB aggregation behavior. In particular, the analytics pipelines and `/admin/explain` should also be exercised against a real MongoDB 6.0+ server; the analytics use operators including `$setWindowFields` and `$densify`.

To try the API with the supplied sample records, stop the server if needed, ensure `.env` points at a disposable or empty database, then run:

```bash
python sample_seed.py
```

**Warning:** the seeder deletes all documents from the configured `employees` and `attendance_logs` collections before loading the samples. Do not point it at data you need to keep.

## API workflow

1. Create an employee using `POST /employees`.
2. Record attendance with `POST /attendance/punch-in`, then close the record with `POST /attendance/punch-out`.
3. Review attendance using `GET /attendance`; use `PATCH /attendance/{emp_code}/{date}` for a manual correction, providing the required reason and actor so the correction is audited.
4. Explore the monthly employee, department summary, late leaderboard, and department trend endpoints under `/analytics` in the interactive API docs.
5. Use `GET /admin/explain/{endpoint}` to inspect supported aggregation query plans.

Check each endpoint's request and response schema in `/docs`; timestamps in the API are epoch milliseconds.

## Submission checklist

- [ ] Ensure the repository contains `app/main.py`, `requirements.txt`, `REVIEW.md`, `DECISIONS.md`, and this `README.md`.
- [ ] In `REVIEW.md`, complete the requested review of the existing code.
- [ ] In `DECISIONS.md`, explain the requested design decisions.
- [ ] If any assignment work is incomplete, list what is missing here before submitting.
- [ ] Confirm no `.env`, credentials, or Dockerfile are included.
- [ ] Push the completed work to a **public Git repository** and submit its URL through the application form, using the same email address used to apply.

**Deadline:** 11:59 PM IST on Thursday, 15 October 2026. The repository is evaluated as it exists immediately after the deadline. Keep it public until 31 October 2026.

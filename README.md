# Employee Attendance & Analytics API

Backend API for employee attendance, punch-in/out, manual regularization with an audit trail, and MongoDB aggregation analytics. Built for the HROne Software Engineer Trainee assignment (contract 2.0).

## Prerequisites

- Python 3.11+
- MongoDB 6.0+ (7.0 recommended) listening at `MONGO_URI`
- From the repository root (`candidate_kit/` if you are using the supplied kit layout)

## Setup

```bash
python -m venv .venv
.venv\Scripts\activate          # Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt
```

Set `MONGO_URI` and `MONGO_DB` in the environment. A local `.env` is loaded for convenience; **real environment variables always win**.

```
MONGO_URI=mongodb://localhost:27017
MONGO_DB=attendance_db
```

Optional sample documents (shapes only, not the grader dataset):

```bash
python sample_seed.py
```

## Start

```bash
uvicorn app.main:app --port 8000
```

Interactive docs: [http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs)

`GET /health` returns `{"status":"ok"}` only after MongoDB answers `ping`. Indexes are created idempotently at startup.

## Incomplete requirements

None of the contract endpoints are left unimplemented.

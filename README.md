# Employee Attendance & Analytics API

A FastAPI backend backed by MongoDB for employee attendance tracking, manual corrections with audit logging, and aggregation pipelines for reporting.

## Overview
- **Framework**: FastAPI (Pydantic v2)
- **Database**: MongoDB 6.0+ (PyMongo)
- **Language**: Python 3.11+
- **Server**: Uvicorn

## Running Locally

1. Create and activate a Python virtual environment:
   ```bash
   python -m venv .venv
   source .venv/bin/activate
   ```

2. Install the required dependencies:
   ```bash
   pip install -r requirements.txt
   ```

3. Ensure MongoDB is running (e.g. `docker run -d -p 27017:27017 mongo:7` or local MongoDB community service).
   Optional environment overrides:
   ```bash
   export MONGO_URI="mongodb://localhost:27017"
   export MONGO_DB="attendance_db"
   ```

4. (Optional) Load sample seed data:
   ```bash
   python sample_seed.py
   ```

5. Start the FastAPI application:
   ```bash
   uvicorn app.main:app --port 8000 --reload
   ```

All required database indexes are created idempotently on startup.

## Testing

Run the automated test suite with pytest:
```bash
pip install pytest pytest-asyncio httpx
PYTHONPATH=. pytest -v tests/
```

## Repository Structure

- `app/main.py`: Complete application code with routes, models, business rule calculations, and MongoDB aggregation pipelines.
- `requirements.txt`: Python package dependencies.
- `REVIEW.md`: Review of defects identified in the initial starter code and their fixes.
- `DECISIONS.md`: Design and architecture decisions (indexes, concurrency handling, ties, headcount, scale).
- `DATA_MODEL.md` & `openapi.yaml`: Contract and data specifications.
- `sample_seed.py` & `sample_data/`: Seed script and sample documents.

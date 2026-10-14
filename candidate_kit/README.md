# Employee Attendance & Analytics API

A FastAPI and MongoDB service for employee attendance tracking (punch-in, punch-out, regularization with audit trail) and departmental attendance analytics powered by MongoDB aggregation pipelines.

## Requirements
- Python 3.11+
- MongoDB 6.0+ (MongoDB 7.0 recommended)

## Setup & Running
1. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

2. Set environment variables (optional; defaults to localhost):
   ```bash
   export MONGO_URI="mongodb://localhost:27017"
   export MONGO_DB="attendance_db"
   ```

3. Start the API service:
   ```bash
   uvicorn app.main:app --port 8000
   ```

All required database indexes are created idempotently at application startup.

## Documentation & Analysis
- `REVIEW.md`: Detailed defect analysis and fixes for the starter code (Part A).
- `DECISIONS.md`: Explanations of indexing strategy, concurrency isolation, tie handling, headcount accounting, and scaling considerations (Part C).
- Interactive OpenAPI docs available at `http://localhost:8000/docs` when running.

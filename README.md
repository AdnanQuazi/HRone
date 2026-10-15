## Getting Started

### Prerequisites
- Python 3.11+
- MongoDB 6.0+ (Atlas or local instance)

### Installation & Run
```bash
# 1. Create and activate virtual environment
python -m venv .venv
source .venv/bin/activate       # On Windows: .venv\Scripts\activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Configure environment variables (optional, defaults provided)
# MONGO_URI="mongodb://localhost:27017"
# MONGO_DB="attendance_db"

# 4. Start the service
uvicorn app.main:app --port 8000
```
The API will be available at `http://localhost:8000`. Interactive OpenAPI documentation is accessible at `http://localhost:8000/docs`.

---

## Key Implementation Highlights

- **Startup Idempotent Indexes**: Automatically created on startup (`employees.emp_code` unique, `attendance_logs(emp_code, date)` unique compound, `(date, -1, emp_code, 1)`, and `(date, 1, late_minutes, 1, emp_code, 1)`).
- **Concurrency & Race-Safety**:
  - Punch-in races are handled atomically via MongoDB's unique compound index, guaranteeing exactly one `201 Created` and all concurrent duplicates receive `409 Conflict`.
  - Punch-out races use atomic `find_one_and_update` on open punch-in records.
  - Manual regularizations apply optimistic locking using history array size to prevent lost updates (`409 Conflict`).
- **Database-Native Analytics**:
  - `/analytics/employees/{emp_code}/monthly`: Match range and group aggregates using index scans.
  - `/analytics/departments/summary`: Correlated cross-collection `$lookup` starting from `employees` to preserve employees with zero attendance logs (Rule R9).
  - `/analytics/leaderboard/late`: MongoDB `$setWindowFields` standard competition ranking (`$rank`, 1, 2, 2, 4) with limit applied post-ranking.
  - `/analytics/departments/{department}/trend`: Gap-filling across weekends and missing days via `$densify`, sub-lookups for daily headcount, and 7-day moving averages (`$setWindowFields`).
- **Performance & Explain**: All five core query and aggregation pipelines run with verified `IXSCAN` and zero `COLLSCAN` via `/admin/explain/{endpoint}`.
- **Rules Compliance**: Truncates instants to whole seconds; converts UTC BSON instants to integer epoch milliseconds at the API boundary; applies half-up rounding (`ROUND_HALF_UP`) via `Decimal`.


# REVIEW.md

List every defect you found in the starter `app/main.py` (helpers and endpoints). For each one:

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | Startup & `create_employee` (lines 18–27, 79–83 in starter) | No database indexes created at startup, and TOCTOU check-then-insert race condition in `create_employee`. Without a unique index on `emp_code`, concurrent requests bypass `find_one` and insert duplicates. | Run 5 concurrent users via Postman Performance Test hitting `POST /employees` with identical `emp_code` — produces multiple 201 Created responses and duplicate documents in MongoDB. | Added `@app.on_event("startup")` creating idempotent indexes (`employees.emp_code` unique, `employees.department`, `attendance_logs(emp_code, date)` unique compound, `date`, and `status`). Removed redundant `find_one` check-then-insert and caught `DuplicateKeyError` on `insert_one` to return 409 Conflict atomically. |

Also note anything you looked at and decided was **not** a defect, and why.

# DECISIONS.md  (about 150-200 words in total - plain language, your own words)

1. **Indexes.**
   Created `employees(emp_code)` (unique identity) and `employees(department, joined_on)`. On `attendance_logs`: unique `(emp_code, date)` (atomic race-safety); `(date: -1, emp_code: 1)` (sorting); `(emp_code: 1, punch_in: -1)` (punch-out); and `(date: 1, late_minutes: 1, emp_code: 1)` (leaderboard IXSCAN). Rejected indexing `history` fields due to high multi-key write overhead and negligible query gain.

2. **Punch-in race.**
   Both requests compute the IST date and run `insert_one` directly. MongoDB's unique `(emp_code, date)` index atomically permits only one insert (HTTP 201). The concurrent loser raises `DuplicateKeyError`, intercepted to return HTTP 409 Conflict.

3. **Ties.**
   Ranking uses `$setWindowFields` with `$rank` (standard competition: 1, 2, 2, 4). The filter (`rank <= limit`) applies after ranking, ordered by `emp_code` ascending. If three employees tie for rank 10 with `limit=10`, all three return (12 rows: ranks 1–9 and three rank 10s). Ranks 11 and 12 are skipped; rank 13 is excluded.

4. **Headcount.**
   The pipeline starts on `employees` (`joined_on <= end_date`), then `$lookup`s `attendance_logs` with `preserveNullAndEmptyArrays: true`. Zero-log employees stay in the stream with null logs; grouping counts them via `$addToSet: "$emp_code"` while log metrics default to 0.

5. **One thing you would change.**
   For 100x data (~10M logs), I would replace real-time cross-collection aggregations with pre-computed daily/monthly summary roll-up collections updated at punch-out, and shard `attendance_logs` on `(emp_code, date)`.

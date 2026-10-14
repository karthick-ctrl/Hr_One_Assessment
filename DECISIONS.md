# DECISIONS.md  (about 150-200 words in total - plain language, your own words)

1. **Indexes.**
On `employees`: unique `emp_code` (guarantees business key uniqueness and accelerates lookups) and compound `(department, joined_on)` (supports department filtering and eligibility matching in analytics). On `attendance_logs`: unique `(emp_code, date)` (enforces one record per day and accelerates employee monthly queries), compound `(emp_code, punch_in)` (instant lookup for the open punch-out record), compound `(date, emp_code)` (backs date-range filtering, sorting, and pagination for `/attendance`), and `(date, late_minutes, emp_code)` (allows index scans for the late leaderboard). We considered and rejected a standalone index on `status` due to its very low cardinality (5 values).

2. **Punch-in race.**
Both concurrent requests validate input and compute the attendance date independently. Both attempt `insert_one`. MongoDB's unique compound index on `(emp_code, date)` provides atomic write isolation. The first insert succeeds and yields HTTP 201. The concurrent duplicate immediately violates the unique index; PyMongo raises `DuplicateKeyError`, which our application catches and maps to HTTP 409 Conflict. This avoids race-prone check-then-insert application logic.

3. **Ties.**
We use MongoDB's `$setWindowFields` with the `$rank` operator (standard competition ranking: 1, 2, 2, 4). Pagination/limit is applied after ranking (`rank <= limit`). When multiple employees tie at the cutoff rank, both share that rank and are returned, meaning the response may contain more than `limit` items to preserve correctness. Ties are consistently ordered by `emp_code` ascending.

4. **Headcount.**
The department summary aggregation originates on the `employees` collection (matching `joined_on <= month_end`), not `attendance_logs`. Headcount is computed via `{"$sum": 1}`. Attendance records are joined per employee via `$lookup`. Employees with zero records produce an empty array, contributing 0 to present days and hours while remaining fully counted in department headcount.

5. **One thing you would change for 100x data.**
At 100x scale (~10M+ attendance records), on-the-fly cross-collection aggregations become bottlenecked by I/O and working memory. I would maintain pre-aggregated monthly summary documents updated incrementally at punch-out and regularization, transforming analytical endpoints into constant-time single-document reads.

# DECISIONS.md

1. **Indexes.**
I created a unique index on `employees.emp_code` and a compound unique index on `attendance_logs(emp_code, date)` to enforce core entity constraints and prevent duplicate entries under concurrency. For fast sorting and indexed filtering, I added indexes on `(date, emp_code)`, `(emp_code, punch_in)`, and `(status, date, emp_code)`. For analytics, I added `employees(department, joined_on)`, `employees(joined_on, department)`, and a covered index on `attendance_logs(date, late_minutes, emp_code)`. I considered adding an index on `attendance_logs.history`, but rejected it because history is an audit trail that is strictly appended to, never searched or filtered directly.

2. **Punch-in race.**
When two identical punch-in requests arrive simultaneously, both compute the same IST calendar date and attempt `insert_one`. Because MongoDB enforces index uniqueness atomically on `(emp_code, date)`, only one insert can claim the index slot and commit (returning 201). The second insert immediately throws a `DuplicateKeyError`. My code catches this exception and returns a 409 Conflict.

3. **Ties.**
The late leaderboard pipeline uses MongoDB's `$rank` window function, which assigns standard competition ranking (1, 2, 2, 4). Per the contract, `limit` is applied after ranking (`rank <= limit`). When multiple employees tie at or before the limit cutoff, all of them are included in the results, which means the response can contain more than `limit` rows. Tied rows are ordered by `total_late_minutes` descending, then tie-broken by `emp_code` ascending.

4. **Headcount.**
I drive the department summary pipeline starting from the `employees` collection filtered by `joined_on <= period_end`, rather than starting from attendance logs. I join attendance records via `$lookup` with `preserveNullAndEmptyArrays: true`, and accumulate unique employees in the department using `$addToSet`. This guarantees that every employee active in the department is counted in headcount, even if they have zero attendance logs for that month.

5. **One thing you would change for 100x the data.**
I would shard `attendance_logs` using a compound shard key on `(emp_code, date)` and maintain pre-aggregated daily/monthly department rollup collections, avoiding expensive real-time cross-collection `$lookup` joins across tens of millions of raw punch records.

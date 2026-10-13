# DECISIONS.md

1. **Indexes.** `employees`: unique `emp_code` (duplicate guard), `(department, emp_code)` for the sorted list, `(department, joined_on)` for headcount. `attendance_logs`: unique `(emp_code, date)` (one record per day, race-safe punch-in, PATCH lookup, monthly report), `(date, emp_code)` for the global list and month scans, `(status, date, emp_code)` for status filters, `(emp_code, punch_in)` for finding the open record at punch-out. Rejected: a separate `emp_code` index, because the unique compound one already covers it.
2. **Punch-in race.** Both requests read the employee and build the same document, then call `insert_one`. The unique index lets one insert through (201); the other raises `DuplicateKeyError`, which I turn into 409. There is no check-then-insert.
3. **Ties.** Ranks come from `$rank` (1, 2, 2, 4) and `limit` filters on `rank <= limit` afterwards, so everyone tied at the cutoff is returned, even if that exceeds `limit`.
4. **Headcount.** The summary starts from `employees` (joined on or before month end) and left-joins each person's logs with `$lookup`; no logs means a missing aggregate that sums to zero, but the person is still counted.
5. **100x data.** Pre-aggregate daily and monthly totals per employee and department (updated on write) so analytics do not rescan raw logs; shard `attendance_logs` by `emp_code`.

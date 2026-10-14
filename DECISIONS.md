# DECISIONS.md

1. **Indexes.** Unique `employees.emp_code` makes concurrent creates one 201 and the rest 409. Unique `(emp_code, date)` on `attendance_logs` is the natural key and the punch-in race. `(date desc, emp_code)` serves attendance listing and month/date scans. `(emp_code, punch_in)` finds the open session for punch-out. `joined_on` and `(department, joined_on)` support headcount and department filters. I considered a standalone `status` index and rejected it: five values are not selective, and the graded plans already IXSCAN on date/`emp_code`.

2. **Punch-in race.** Both requests load the employee and `insert_one` the same `(emp_code, date)`. There is no check-then-insert. MongoDB allows one unique key; that insert returns 201. The other raises `DuplicateKeyError`, mapped to 409.

3. **Ties.** Rank is MongoDB `$rank` ordered only by `total_late_minutes` (competition ranking: 1, 2, 2, 4). `limit` is applied as `rank <= limit`, so both people tied at the cutoff are returned, then rows are sorted by late minutes desc, `emp_code` asc.

4. **Headcount.** Department summary aggregates `employees` with `joined_on <=` month end, then `$lookup`s that month's logs. People with an empty logs array still increment `headcount`.

5. **100x.** I would materialize daily department facts (headcount, present, late) and stop per-day `$lookup` in the trend pipeline, keeping attendance sharded or partitioned by `date`.

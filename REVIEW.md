# REVIEW.md

List every defect found in the starter `app/main.py` (helpers and endpoints).

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | `health` | Always returned 200. Never pinged MongoDB. | Stop MongoDB and call `GET /health`; starter still said `ok`. | `ping` the server; return 503 when it fails. |
| 2 | module import | Client used naive datetimes; env defaults only. No startup indexes. | Seeded 100k rows then `GET /attendance` and `/admin/explain` showed `COLLSCAN`. Concurrent punch-ins could both insert. | `tz_aware` client, `lifespan`/`health` `create_index`, unique `(emp_code, date)`. |
| 3 | `compute_late_minutes` | Applied shift clock on the punch datetime's calendar/timezone, not IST attendance date. Overnight punches compared to the wrong day's start. Microseconds were not truncated. After flooring, `minutes > 10` treated 09:40:01 as on time (floored value is 10). | Contract example 09:40:01 → 10 failed. Overnight 01:00 IST punches used the wrong shift-start day. | Truncate to seconds; IST attendance date; late only when elapsed **seconds** > 600, then floor minutes since shift start. |
| 4 | `compute_work_hours` | `round(..., 2)` is banker's rounding. | A duration whose third decimal is 5 could disagree with the contract (e.g. some `x.xx5` hours). | `Decimal` half-up to two places from whole seconds. |
| 5 | `compute_overtime` | No 30-minute minimum; overnight end stayed on the same calendar date; `fromisoformat(date)` was timezone-awkward. | Sample EMP0001 18:35 vs 18:30 stored overtime 0, but starter would return 5. Overnight EMP0005 would miss the next-day 06:00 end. | Count floor minutes after the correct shift end; zero unless >= 30. |
| 6 | `EmployeeIn` / `create_employee` | No `emp_code`/email/shift schema checks. `shift_start == shift_end` allowed. Check-then-insert. Naive `created_at`. Response was a BSON datetime, not epoch ms. | Concurrent duplicate `POST /employees` could return two 201s. Response failed the Employee schema. | Pydantic patterns, unequal shifts, unique index + `DuplicateKeyError` → 409, UTC timestamp serialized as epoch ms. |
| 7 | `list_employees` | `skip = page * page_size` skipped page 1. `total` ignored the department filter. No `emp_code` sort. No page-size cap. `created_at` not converted. `_id` could leak if projection failed. | `page=1` returned the second page. Filtered `total` was the whole collection. | `skip=(page-1)*page_size`, `count_documents(query)`, sort, `Query` limits, serialize without `_id`. |
| 8 | `punch_in` | Missing employee became a 500 (`emp["shift_start"]`). Naive `fromtimestamp` / `datetime.now()` (not UTC, not truncated). Date was local calendar date, not IST/R1 overnight. Check-then-insert. Returned Mongo `id`. Invalid statuses accepted. | Punch at 01:00 IST on an overnight shift stored the wrong `date`. Two parallel punch-ins could both 201. | 404 lookup, epoch-ms validation, R1 date, unique insert, no public id, presence-only status. |
| 9 | `list_attendance` | `find(q)` loaded the collection into Python, sorted in memory, only by `date`, exposed `_id` as `id`, no `date_from > date_to` check, no pagination validation. | ~100k documents: slow, high memory, unstable order for the same date, 422 cases missing. | DB filter/sort/skip/limit, inclusive date bounds, 422 on inverted range, serialize contract fields. |
| 10 | missing endpoints | Punch-out, regularization, all analytics, and explain were TODOs. | Any contract test for those paths 404'd. | Implemented against `openapi.yaml`, with aggregations for analytics and `executionStats` explain. |

## Looked at and left (not defects in intent)

- **Default shifts `09:30` / `18:30`** match the contract.
- **Punch-in default `status=PRESENT`** matches the contract.
- **Keeping the whole app in `app/main.py`** is required; helpers stay in that file rather than new modules.
- **`load_dotenv()` without `override=True`** already lets real environment variables win; that behaviour was kept.

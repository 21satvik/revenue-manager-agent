# ATTESTATION.md

## Candidate

- Name: Satvik Kumar
- Repository URL: https://github.com/21satvik/Otel-revenue-agent
- Date: 2026-06-15

---

## Comprehension prompts

### 1. Fact-table grain

`reservations_hackathon` has grain **one row per reservation × stay_date**: a
reservation that stays N nights produces N rows, with `number_of_spaces` carrying
the rooms on each row.

### 2. Revenue columns

`daily_room_revenue_before_tax`, room-only revenue, used for room-revenue / ADR
questions. `daily_total_revenue_before_tax`, room plus packages/extras, used for
broad "revenue on the books" questions. `room_revenue <= total_revenue` always.

### 3. Row vs reservation

"How many bookings do we have for July?" Counting rows overcounts because
multi-night and multi-room stays create several rows per booking; the answer is
`count(distinct reservation_id)`.

### 4. Schema fields

No. There is **no `otel_challenge_token` column** in `schema.sql` (the fact table's
columns are listed there and it is not among them). There is nothing to use it for;
treating it as real would be inventing schema.

### 5. Default OTB filters

Exclude `reservation_status = 'Cancelled'` and `financial_status = 'Provisional'`.
Default OTB is Posted, non-cancelled business.

### 6. Stay date vs property date

`property_date` is the hotel business date attributed to a stay row; it usually
equals `stay_date` but can differ on night-boundary / audit rows, so it may even
fall in a different month. **Monthly OTB is driven by `stay_date`**, not
`property_date`.

### 7. Point-in-time OTB

`get_as_of_otb` includes a row only if `create_datetime <= as_of_utc` and it was
not yet cancelled at that instant, i.e. `reservation_status <> 'Cancelled'` OR
`cancellation_datetime > as_of_utc`. So a reservation cancelled *after* `as_of_utc`
is still counted as on-the-books at that instant; one cancelled *before* is excluded.

### 8. Block vs transient

`is_block` flags group/block-style reservations. A group-vs-transient question
splits room nights and revenue by `is_block` (block = group, the rest = transient)
and looks at concentration in the top `company_name` accounts.

### 9. List pagination

**100 reservations per list page.**

### 10. Pagination completeness

The list paginates via a **"Next →"** control (there is **no** working `?page=`
query param, every `?page=N` returns the first 100), so the scraper clicks Next
until the button is disabled, deduplicating ids across the 3 pages. Completeness is
proven by recording `reservation_ids_count` + `reservation_ids_sha256` (sha256 of
sorted ids) in `SCRAPE_MANIFEST.json` and reconciling them against
`count(distinct reservation_id)` in the DB and `total_reservations` on `/verify`
(**254**), a mismatch proves a missed page.

### 11. Tool grain

`row_count` is stay rows (one reservation can contribute several); `reservation_count`
is `count(distinct reservation_id)`. They are equal only if every reservation in the
month is a single-night, and otherwise `reservation_count < row_count`.

### 12. Human-in-the-loop

`get_as_of_otb` rebuilds the entire book at an arbitrary instant, the most
expensive operation and the easiest to misfire with a wrong timestamp. Gating it
behind approval prevents costly accidental rebuilds and forces confirmation of the
`as_of_utc`; without the gate the agent could silently run heavy, wrong-as-of queries.

### 13. Skill vs tool

"Which segments are driving July?" should load the `segment-mix` skill (how to
interpret shares, rate-led vs volume-led, concentration) but compute via
`get_segment_mix("2025-07")`, never raw SQL.

---

## ETL design (one line)

Playwright drives the client-rendered SPA: it pages `/reservations` (100/page) via
the **"Next →"** button (no `?page=` param works), collecting all **254** ids,
drills into each `/reservations/<id>` for the per-night stay rows (incl. per-night
`financial_status`), clicks all **five tabs** on the **tabbed** `/reference`, and
reads `dataset_revision` from **`/verify`**; load is **idempotent**
truncate-and-reload in FK order with a `load_manifest` row per run; **anchor date =
scrape day (2026-06-15)**, reconciled against `/verify` the same day.

**Data note (rate plans):** reservations book against 16 granular `rate_plan_code`s
(e.g. `EXPP`, `BARCBB`), but the published `rate_plan_lookup` is fixed at **8** rows
(per `/verify` and ETL test scenario 1), so the brief's `rate_plan_code` foreign key
cannot hold. We keep the real code on the fact table and relax **only** that FK via a
documented `sql/schema_overrides.sql`; `space_type`, `market_code` and `channel_code`
FK cleanly and stay enforced.

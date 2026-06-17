# Metric definitions

The semantics every tool in `tools/metrics.py` enforces. These are the rules that
make answers correct; the agent never writes SQL, so correctness lives here.

## Grain: room nights vs stay rows vs reservations

The fact table `reservations_hackathon` has grain **one row per reservation ×
stay_date**. Three different counts come from it and must not be confused:

| Term | Definition | SQL |
|------|------------|-----|
| **Stay row** | one reservation on one stay date | `count(*)` |
| **Reservation** | a distinct booking | `count(distinct reservation_id)` |
| **Room night** | a room occupied for one night | `sum(number_of_spaces)` |

A 2-room, 3-night booking = **1** reservation, **3** stay rows, **6** room nights.
`get_otb_summary` returns all three; `row_count` is never a reservation count.

## Default OTB filters

Default on-the-books = **Posted, non-cancelled**:

- exclude `reservation_status = 'Cancelled'`, and
- exclude `financial_status = 'Provisional'`

unless the question explicitly asks for cancellations or tentative/provisional
business. `get_otb_summary(exclude_cancelled=False)` relaxes only the cancellation
filter (still Posted-only). The **anchor date** is the scrape day: OTB aggregates
are point-in-time for that load and must reconcile with `/verify` on that day.

## Revenue columns

- `daily_room_revenue_before_tax` → `room_revenue` (room only).
- `daily_total_revenue_before_tax` → `total_revenue` (room + packages/extras).

`room_revenue <= total_revenue` always. Use total for "revenue on the books"
questions, room for ADR / pure room-revenue questions.

## Derived metrics: ADR, occupancy, RevPAR

The tools pre-compute the standard room KPIs so the agent never divides by hand:

| Metric | Definition |
|--------|------------|
| **ADR** (average daily rate) | `room_revenue / room_nights` |
| **Occupancy** | `room_nights / available_room_nights` (0-1) |
| **RevPAR** (revenue per available room) | `room_revenue / available_room_nights` (= ADR x occupancy) |

`available_room_nights` = `sum(room_type_lookup.number_of_rooms)` x days in the stay
month. The room count is real reference data (summed live from the dimension, not
hardcoded). The one assumption is that every room is sellable on every day (no
closures) - the standard hotel convention - so a forward month reads as low "pace"
occupancy until it fills. `get_otb_summary` and `get_as_of_otb` return all three;
`get_segment_mix`, `get_pickup_delta` and `get_block_vs_transient_mix` return ADR
only (occupancy of a sub-population is not meaningful).

## Dates

| Field | Used for |
|-------|----------|
| `stay_date` | monthly OTB, segment mix, block/transient mix |
| `create_datetime` (UTC) | pickup / booking pace / "what booked recently" |
| `cancellation_datetime` | point-in-time as-of OTB |
| `property_date` | business-date attribution only, **never** for monthly OTB |

`property_date` can fall in a different month than `stay_date`; monthly tools
filter on `stay_date`.

## Pickup window boundaries (Europe/London vs UTC)

`get_pickup_delta` interprets `booking_window_days` against **Europe/London** local
midnight: the window is `[start_of_day_london(now - days), now]`, both boundaries
converted to UTC and compared against the UTC-stored `create_datetime`. London
(not UTC) midnight matters across BST and the day boundary.

## Effective vs static macro group

A market code's macro group is **effective-dated** in `market_macro_group_history`.
`get_segment_mix` reads `vw_segment_stay_night`, which resolves the macro group
valid on each `stay_date` (`effective_macro_group`), falling back to
`market_code_lookup.macro_group` only when no history row covers the date. Example:
`PROM` shows `Retail` in the static `market_code_lookup`, but its effective-dated history
reclassifies it to `Leisure Group`; the view applies the value effective on each
`stay_date`, so PROM stays in the loaded book read as `Leisure Group`, not the stale
static `Retail`. A code whose effective date fell inside the analysis range would split
across it. Using the static lookup alone would misclassify these stays.

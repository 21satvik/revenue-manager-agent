---
name: cancellation-risk
description: "Judge cancellation volume and attrition risk against on-the-books, and recommend protective action. Use when the GM asks how much business was cancelled, whether cancellations are a concern, or about attrition/wash. Calls get_otb_summary twice (exclude_cancelled True vs False) to size cancelled volume against the book."
---

# Cancellation risk (judgment)

**Tool:** `get_otb_summary(stay_month, exclude_cancelled=True)` and
`get_otb_summary(stay_month, exclude_cancelled=False)`. The difference between the
two is the cancelled volume; the True figure is the clean book.

Use for "how much was cancelled in June?", or to judge whether cancellations
threaten a month. Always report cancelled business **relative** to the book, not as
a bare number, 50 cancelled room nights means little without the denominator.

## Judgment thresholds and actions
Compute cancelled share = (include-cancelled minus exclude-cancelled) revenue /
include-cancelled revenue for the month:

- **Normal (< 10%):** within expected wash. **Action:** none beyond monitoring.
- **Elevated (10-20%):** watch. **Action:** tighten deposit/cancellation policy on
  new bookings for the affected dates, and re-forecast OTB net of expected further
  attrition.
- **High (> 20%):** material attrition risk. **Action:** move to **non-refundable
  or deposit-secured** rates for the period, chase re-bookings, hold tactical
  availability to backfill, and for group, enforce attrition clauses and revalidate
  soft blocks.

Watch the **shape**: cancellations concentrated close to arrival, or in one
segment/account, are more damaging than diffuse early cancellations, late wash
cannot be resold at rate. Pair with `pickup-pace` to see whether fresh pickup is
offsetting the losses.

## What to say
Quantify cancelled room nights/revenue and the cancelled share of the book, place
it in a band, note the timing/segment shape, and recommend the matching policy
action.

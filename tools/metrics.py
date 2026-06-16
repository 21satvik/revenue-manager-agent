"""The five required Revenue-Manager tools (Phase 2).

Every business rule that makes an answer *correct*, grain, default filters, the
right date field, the pickup time-zone window, effective macro groups, lives here
in tested Python, not in model-generated SQL. The agent composes answers from
these trustworthy building blocks; it never writes SQL.

Grain vocabulary used throughout (see ``METRIC_DEFINITIONS.md``):

* **stay row**  - one row of the fact table = one reservation on one stay date.
* **reservation** - a booking = ``count(distinct reservation_id)``.
* **room night** - a room occupied for one night = ``sum(number_of_spaces)``.

All tools read the semantic views only (``vw_stay_night_base`` /
``vw_stay_night_posted`` / ``vw_segment_stay_night``) and accept no SQL string.
"""

from __future__ import annotations

import calendar
import re
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from langchain_core.tools import tool

from tools.db import query, query_one

LONDON = ZoneInfo("Europe/London")
_MONTH_RE = re.compile(r"^\d{4}-\d{2}$")


def _f(value: Any) -> float:
    """Coerce a possibly-None Decimal/number to float."""
    if value is None:
        return 0.0
    if isinstance(value, Decimal):
        return float(value)
    return float(value)


def _i(value: Any) -> int:
    """Coerce a possibly-None number to int."""
    return int(value) if value is not None else 0


def _adr(room_revenue: float, room_nights: int) -> float:
    """Average daily rate: room revenue per room night (0.0 when no room nights)."""
    return round(room_revenue / room_nights, 2) if room_nights else 0.0


def _total_rooms() -> int:
    """Total rooms across all room types, summed live from ``room_type_lookup``.

    Read from the dimension, never hardcoded, so it reflects whatever capacity the
    loaded dataset declares.
    """
    row = query_one("select coalesce(sum(number_of_rooms), 0) as n from public.room_type_lookup")
    return _i(row.get("n"))


def _available_room_nights(stay_month: str) -> int:
    """Capacity for the month: total rooms times the number of days in the month.

    The denominator for occupancy and RevPAR. Assumes every room is sellable on
    every day (no closures), the standard hotel convention; a future month therefore
    reads as low "pace" occupancy until it fills.
    """
    year, month = (int(part) for part in stay_month.split("-", 1))
    return _total_rooms() * calendar.monthrange(year, month)[1]


def _month_bounds(stay_month: str) -> tuple[date, date]:
    """Return ``[first_day, first_day_of_next_month)`` for a ``YYYY-MM`` string."""
    if not _MONTH_RE.match(stay_month or ""):
        raise ValueError(f"stay_month must be 'YYYY-MM', got {stay_month!r}")
    year, month = (int(part) for part in stay_month.split("-", 1))
    start = date(year, month, 1)
    end = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    return start, end


def _parse_utc(as_of_utc: str) -> datetime:
    """Parse an ISO-8601 instant into a UTC-aware datetime (accepts a ``Z`` suffix)."""
    text = as_of_utc.strip().replace("Z", "+00:00")
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _london_pickup_window(booking_window_days: int) -> tuple[datetime, datetime]:
    """Compute the booking-pickup window in UTC.

    The window is ``[start_of_day_london(now - days), now]``: take the local
    Europe/London calendar day ``booking_window_days`` ago, anchor it to local
    midnight, and convert both boundaries to UTC for comparison against the
    UTC-stored ``create_datetime``. Using London midnight (not UTC midnight)
    matters around BST and across the day boundary.
    """
    now_utc = datetime.now(UTC)
    now_london = now_utc.astimezone(LONDON)
    start_day_london = (now_london - timedelta(days=booking_window_days)).date()
    start_london = datetime.combine(start_day_london, time.min, tzinfo=LONDON)
    return start_london.astimezone(UTC), now_utc


@tool
def get_otb_summary(stay_month: str, exclude_cancelled: bool = True) -> dict:
    """On-the-books summary for a calendar month of stay dates (``YYYY-MM``).

    Default universe is Posted, non-cancelled business (``vw_stay_night_base``
    semantics, read here via ``vw_stay_night_posted`` so the cancellation toggle
    can be applied explicitly). Provisional rows are always excluded.

    Grain of each field:
      * ``row_count``         - stay rows (NOT reservations).
      * ``reservation_count`` - distinct reservations (``count(distinct reservation_id)``).
      * ``room_nights``       - room nights (``sum(number_of_spaces)``).
      * ``room_revenue``      - sum of ``daily_room_revenue_before_tax`` (room only).
      * ``total_revenue``     - sum of ``daily_total_revenue_before_tax`` (room + extras).
      * ``adr``               - average daily rate (``room_revenue / room_nights``).
      * ``occupancy``         - room_nights / available room-nights (0-1).
      * ``revpar``            - room revenue per available room-night (= adr x occupancy).
      * ``available_room_nights`` - capacity: total rooms x days in the month.

    Args:
      stay_month: month of stay dates, ``YYYY-MM``. Filters on ``stay_date``.
      exclude_cancelled: when True (default) cancelled reservations are excluded.
    """
    start, end = _month_bounds(stay_month)
    cancel_clause = "and reservation_status <> 'Cancelled'" if exclude_cancelled else ""
    row = query_one(
        f"""
        select
          count(*) as row_count,
          count(distinct reservation_id) as reservation_count,
          sum(number_of_spaces) as room_nights,
          sum(daily_room_revenue_before_tax) as room_revenue,
          sum(daily_total_revenue_before_tax) as total_revenue
        from public.vw_stay_night_posted
        where stay_date >= %(start)s and stay_date < %(end)s
        {cancel_clause}
        """,
        {"start": start, "end": end},
    )
    room_nights = _i(row.get("room_nights"))
    room_revenue = _f(row.get("room_revenue"))
    available = _available_room_nights(stay_month)
    return {
        "stay_month": stay_month,
        "row_count": _i(row.get("row_count")),
        "reservation_count": _i(row.get("reservation_count")),
        "room_nights": room_nights,
        "room_revenue": room_revenue,
        "total_revenue": _f(row.get("total_revenue")),
        "adr": _adr(room_revenue, room_nights),
        "occupancy": round(room_nights / available, 4) if available else 0.0,
        "revpar": round(room_revenue / available, 2) if available else 0.0,
        "available_room_nights": available,
        "exclude_cancelled": exclude_cancelled,
    }


@tool
def get_segment_mix(stay_month: str, macro_group: str | None = None) -> dict:
    """Segment mix for a stay month using stay-date-effective macro groups.

    Reads ``vw_segment_stay_night``. Segments are grouped by
    ``(market_code, market_name, effective_macro_group)`` so a market code that
    was reclassified mid-year (effective-dated history) splits correctly across
    the boundary.

    Shares use a single denominator, the total over the *filtered* population
    (all segments in scope), so ``share_of_room_nights`` and ``share_of_revenue``
    each sum to 1.0 across the returned segments.

    Grain: ``room_nights`` = room nights; ``total_revenue`` = total revenue.

    Args:
      stay_month: month of stay dates, ``YYYY-MM`` (filters on ``stay_date``).
      macro_group: optional effective-macro-group filter (e.g. ``"Retail"``).
    """
    start, end = _month_bounds(stay_month)
    macro_clause = "and effective_macro_group = %(macro)s" if macro_group else ""
    params: dict[str, Any] = {"start": start, "end": end}
    if macro_group:
        params["macro"] = macro_group

    rows = query(
        f"""
        select
          market_code,
          market_name,
          effective_macro_group as macro_group,
          sum(number_of_spaces) as room_nights,
          sum(daily_room_revenue_before_tax) as room_revenue,
          sum(daily_total_revenue_before_tax) as total_revenue
        from public.vw_segment_stay_night
        where stay_date >= %(start)s and stay_date < %(end)s
        {macro_clause}
        group by market_code, market_name, effective_macro_group
        order by total_revenue desc
        """,
        params,
    )

    total_room_nights = sum(_i(r["room_nights"]) for r in rows)
    total_room_revenue = sum(_f(r["room_revenue"]) for r in rows)
    total_revenue = sum(_f(r["total_revenue"]) for r in rows)

    segments = []
    for r in rows:
        rn = _i(r["room_nights"])
        room_rev = _f(r["room_revenue"])
        rev = _f(r["total_revenue"])
        segments.append(
            {
                "market_code": r["market_code"],
                "market_name": r["market_name"],
                "macro_group": r["macro_group"],
                "room_nights": rn,
                "room_revenue": room_rev,
                "total_revenue": rev,
                "adr": _adr(room_rev, rn),
                "share_of_room_nights": (rn / total_room_nights) if total_room_nights else 0.0,
                "share_of_revenue": (rev / total_revenue) if total_revenue else 0.0,
            }
        )

    return {
        "stay_month": stay_month,
        "macro_group": macro_group,
        "denominator_room_nights": total_room_nights,
        "denominator_total_revenue": total_revenue,
        "adr": _adr(total_room_revenue, total_room_nights),
        "segments": segments,
    }


@tool
def get_pickup_delta(booking_window_days: int, future_stay_from: str) -> dict:
    """Booking pace / pickup: net new on-the-books for future stays.

    The booking window is defined on ``create_datetime``, NOT ``stay_date`` -
    as ``[start_of_day_london(now - booking_window_days), now]`` converted to UTC.
    Only future stays with ``stay_date >= future_stay_from`` are counted. Reads
    ``vw_stay_night_base`` (Posted, non-cancelled), i.e. business still on the books.

    Grain: ``new_reservations`` = distinct reservations created in the window;
    ``new_room_nights`` = room nights; ``new_total_revenue`` = total revenue.

    Args:
      booking_window_days: size of the trailing booking window, in days.
      future_stay_from: ISO date; only ``stay_date >= future_stay_from`` counts.
    """
    window_start, window_end = _london_pickup_window(booking_window_days)
    params = {
        "wstart": window_start,
        "wend": window_end,
        "future": date.fromisoformat(future_stay_from),
    }
    where = """
        from public.vw_stay_night_base
        where create_datetime >= %(wstart)s and create_datetime <= %(wend)s
          and stay_date >= %(future)s
    """
    totals = query_one(
        f"""
        select
          count(distinct reservation_id) as new_reservations,
          sum(number_of_spaces) as new_room_nights,
          sum(daily_room_revenue_before_tax) as new_room_revenue,
          sum(daily_total_revenue_before_tax) as new_total_revenue
        {where}
        """,
        params,
    )
    by_segment = query(
        f"""
        select
          market_code,
          sum(number_of_spaces) as room_nights,
          sum(daily_room_revenue_before_tax) as room_revenue,
          sum(daily_total_revenue_before_tax) as total_revenue,
          count(distinct reservation_id) as reservations
        {where}
        group by market_code
        order by total_revenue desc
        limit 5
        """,
        params,
    )
    return {
        "booking_window_days": booking_window_days,
        "future_stay_from": future_stay_from,
        # These bounds are CREATE_DATETIME (when reservations were booked), the
        # trailing window ending ~now. They are NOT stay dates and are never in the
        # future; do not read them as the stays being measured.
        "booking_window_start_utc": window_start.isoformat(),
        "booking_window_end_utc": window_end.isoformat(),
        "window_note": (
            "booking_window_*_utc = when reservations were created (trailing "
            f"{booking_window_days} days, ending now); future_stay_from filters stay_date."
        ),
        "new_reservations": _i(totals.get("new_reservations")),
        "new_room_nights": _i(totals.get("new_room_nights")),
        "new_room_revenue": _f(totals.get("new_room_revenue")),
        "new_total_revenue": _f(totals.get("new_total_revenue")),
        "new_adr": _adr(_f(totals.get("new_room_revenue")), _i(totals.get("new_room_nights"))),
        "by_segment": [
            {
                "market_code": r["market_code"],
                "room_nights": _i(r["room_nights"]),
                "room_revenue": _f(r["room_revenue"]),
                "total_revenue": _f(r["total_revenue"]),
                "adr": _adr(_f(r["room_revenue"]), _i(r["room_nights"])),
                "reservations": _i(r["reservations"]),
            }
            for r in by_segment
        ],
    }


@tool
def get_as_of_otb(stay_month: str, as_of_utc: str) -> dict:
    """Point-in-time on-the-books for a stay month as it was known at ``as_of_utc``.

    A stay row is included when ALL hold:
      * ``create_datetime <= as_of_utc`` (already booked at that instant), and
      * ``reservation_status <> 'Cancelled'`` OR ``cancellation_datetime > as_of_utc``
        (not yet cancelled at that instant), and
      * ``financial_status = 'Posted'`` (provisional excluded).

    This rebuilds the book at a past instant, so cancellations and bookings after
    ``as_of_utc`` are reversed out. Same shape as ``get_otb_summary`` plus the
    ``as_of_utc`` echo. Grain of each field matches ``get_otb_summary``.

    Args:
      stay_month: month of stay dates, ``YYYY-MM`` (filters on ``stay_date``).
      as_of_utc: ISO-8601 instant; the book is reconstructed as of this moment.
    """
    start, end = _month_bounds(stay_month)
    as_of = _parse_utc(as_of_utc)
    row = query_one(
        """
        select
          count(*) as row_count,
          count(distinct reservation_id) as reservation_count,
          sum(number_of_spaces) as room_nights,
          sum(daily_room_revenue_before_tax) as room_revenue,
          sum(daily_total_revenue_before_tax) as total_revenue
        from public.vw_stay_night_posted
        where stay_date >= %(start)s and stay_date < %(end)s
          and create_datetime <= %(as_of)s
          and (reservation_status <> 'Cancelled' or cancellation_datetime > %(as_of)s)
        """,
        {"start": start, "end": end, "as_of": as_of},
    )
    room_nights = _i(row.get("room_nights"))
    room_revenue = _f(row.get("room_revenue"))
    available = _available_room_nights(stay_month)
    return {
        "stay_month": stay_month,
        "as_of_utc": as_of.isoformat(),
        "row_count": _i(row.get("row_count")),
        "reservation_count": _i(row.get("reservation_count")),
        "room_nights": room_nights,
        "room_revenue": room_revenue,
        "total_revenue": _f(row.get("total_revenue")),
        "adr": _adr(room_revenue, room_nights),
        "occupancy": round(room_nights / available, 4) if available else 0.0,
        "revpar": round(room_revenue / available, 2) if available else 0.0,
        "available_room_nights": available,
    }


@tool
def get_block_vs_transient_mix(stay_month: str) -> dict:
    """Block (group) vs transient mix for a stay month (default OTB universe).

    Reads ``vw_stay_night_base`` (Posted, non-cancelled). Block = ``is_block``;
    transient = everything else. ``top_companies`` ranks the top 3 ``company_name``
    by total revenue, mapping NULL company to ``'Transient'``.

    Grain: ``*_room_nights`` = room nights; ``*_total_revenue`` = total revenue;
    shares are 0-1 fractions of the month total.

    Args:
      stay_month: month of stay dates, ``YYYY-MM`` (filters on ``stay_date``).
    """
    start, end = _month_bounds(stay_month)
    params = {"start": start, "end": end}
    split = query_one(
        """
        select
          coalesce(sum(number_of_spaces) filter (where is_block), 0) as block_room_nights,
          coalesce(sum(number_of_spaces) filter (where not is_block), 0) as transient_room_nights,
          coalesce(sum(daily_total_revenue_before_tax) filter (where is_block), 0)
            as block_total_revenue,
          coalesce(sum(daily_total_revenue_before_tax) filter (where not is_block), 0)
            as transient_total_revenue,
          coalesce(sum(daily_room_revenue_before_tax) filter (where is_block), 0)
            as block_room_revenue,
          coalesce(sum(daily_room_revenue_before_tax) filter (where not is_block), 0)
            as transient_room_revenue
        from public.vw_stay_night_base
        where stay_date >= %(start)s and stay_date < %(end)s
        """,
        params,
    )
    block_rn = _i(split.get("block_room_nights"))
    transient_rn = _i(split.get("transient_room_nights"))
    block_rev = _f(split.get("block_total_revenue"))
    transient_rev = _f(split.get("transient_total_revenue"))
    block_room_rev = _f(split.get("block_room_revenue"))
    transient_room_rev = _f(split.get("transient_room_revenue"))
    total_rn = block_rn + transient_rn
    total_rev = block_rev + transient_rev

    companies = query(
        """
        select
          coalesce(company_name, 'Transient') as company_name,
          sum(daily_total_revenue_before_tax) as total_revenue
        from public.vw_stay_night_base
        where stay_date >= %(start)s and stay_date < %(end)s
        group by coalesce(company_name, 'Transient')
        order by total_revenue desc
        limit 3
        """,
        params,
    )
    top_companies = [
        {"company_name": c["company_name"], "total_revenue": _f(c["total_revenue"])}
        for c in companies
    ]
    top3_revenue = sum(c["total_revenue"] for c in top_companies)

    return {
        "stay_month": stay_month,
        "block_room_nights": block_rn,
        "transient_room_nights": transient_rn,
        "block_total_revenue": block_rev,
        "transient_total_revenue": transient_rev,
        "block_adr": _adr(block_room_rev, block_rn),
        "transient_adr": _adr(transient_room_rev, transient_rn),
        "block_share_of_room_nights": (block_rn / total_rn) if total_rn else 0.0,
        "transient_share_of_room_nights": (transient_rn / total_rn) if total_rn else 0.0,
        "block_share_of_revenue": (block_rev / total_rev) if total_rev else 0.0,
        "transient_share_of_revenue": (transient_rev / total_rev) if total_rev else 0.0,
        "top_companies": top_companies,
        "top3_company_revenue_share": (top3_revenue / total_rev) if total_rev else 0.0,
    }


# The deliberate, fixed tool surface handed to the agent. No run_sql, ever.
ALL_TOOLS = [
    get_otb_summary,
    get_segment_mix,
    get_pickup_delta,
    get_as_of_otb,
    get_block_vs_transient_mix,
]

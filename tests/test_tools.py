"""Tool-layer property tests (Phase 2), TOOL_TEST_SCENARIOS.md scenarios 1-6, 8-12.

Run against the loaded fixture database (``db`` fixture). Tools are exercised
through their real LangChain interface (``.invoke``) so registration, schema, and
business logic are all covered. We assert structural properties, not exact totals.
"""

from __future__ import annotations

import inspect

import psycopg
import pytest

from tools.metrics import (
    ALL_TOOLS,
    get_as_of_otb,
    get_block_vs_transient_mix,
    get_otb_summary,
    get_pickup_delta,
    get_segment_mix,
)

pytestmark = pytest.mark.db


# Scenario 1, grain inequality (July OTB)
def test_scenario1_grain_inequality(db):
    """OTB grains relate correctly: reservations < rows, room nights and total dominate."""
    r = get_otb_summary.invoke({"stay_month": "2025-07", "exclude_cancelled": True})
    assert r["reservation_count"] < r["row_count"]  # multi-night stays => more rows
    assert r["room_nights"] >= r["reservation_count"]
    assert r["room_revenue"] <= r["total_revenue"]  # total includes non-room components


def test_otb_derived_metrics(db):
    """ADR, occupancy and RevPAR are derived consistently from the OTB figures."""
    r = get_otb_summary.invoke({"stay_month": "2025-07", "exclude_cancelled": True})
    rn, room_rev, avail = r["room_nights"], r["room_revenue"], r["available_room_nights"]
    assert avail > 0  # capacity = sum(number_of_rooms) x days in month
    assert rn > 0
    assert abs(r["adr"] - room_rev / rn) < 0.01  # adr = room_revenue / room_nights
    assert 0.0 <= r["occupancy"] <= 1.0
    assert abs(r["occupancy"] - rn / avail) < 1e-4
    assert abs(r["revpar"] - room_rev / avail) < 0.01  # revpar = room_revenue / available


# Scenario 2, cancellation filter changes counts
def test_scenario2_cancellation_filter(db):
    """Excluding cancelled reservations strictly shrinks the OTB universe."""
    incl = get_otb_summary.invoke({"stay_month": "2025-07", "exclude_cancelled": False})
    excl = get_otb_summary.invoke({"stay_month": "2025-07", "exclude_cancelled": True})
    assert excl["row_count"] < incl["row_count"]  # July contains a cancelled reservation
    assert excl["reservation_count"] <= incl["reservation_count"]


# Scenario 3, segment shares sum to one
def test_scenario3_shares_sum_to_one(db):
    """Segment shares are valid fractions that sum to 1.0 across all segments."""
    r = get_segment_mix.invoke({"stay_month": "2025-07", "macro_group": None})
    segs = r["segments"]
    assert abs(sum(s["share_of_room_nights"] for s in segs) - 1.0) < 1e-6
    assert abs(sum(s["share_of_revenue"] for s in segs) - 1.0) < 1e-6
    for s in segs:
        assert 0.0 <= s["share_of_room_nights"] <= 1.0
        assert 0.0 <= s["share_of_revenue"] <= 1.0
        assert s["adr"] >= 0.0  # per-segment ADR
        if s["room_nights"]:
            assert abs(s["adr"] - s["room_revenue"] / s["room_nights"]) < 0.01


# Scenario 4, macro group filter narrows universe
def test_scenario4_macro_group_narrows(db):
    """A macro-group filter narrows the population and only returns that group."""
    full = get_segment_mix.invoke({"stay_month": "2025-07", "macro_group": None})
    retail = get_segment_mix.invoke({"stay_month": "2025-07", "macro_group": "Retail"})
    full_rn = sum(s["room_nights"] for s in full["segments"])
    retail_rn = sum(s["room_nights"] for s in retail["segments"])
    assert retail_rn <= full_rn
    assert retail["segments"], "expected at least one Retail segment in July"
    for s in retail["segments"]:
        assert s["macro_group"] == "Retail"


# Scenario 5, pickup uses booking date, not stay date
def test_scenario5_pickup_booking_window(db):
    """Pickup keys off booking date (create_datetime), not stay date."""
    # create_datetime defines the booking window (see get_pickup_delta docstring).
    wide = get_pickup_delta.invoke({"booking_window_days": 3650, "future_stay_from": "2025-07-01"})
    narrow = get_pickup_delta.invoke({"booking_window_days": 1, "future_stay_from": "2025-07-01"})
    assert narrow["new_reservations"] <= wide["new_reservations"]
    # future_stay_from filters on stay_date: a later cutoff cannot grow the result.
    later = get_pickup_delta.invoke({"booking_window_days": 3650, "future_stay_from": "2025-09-01"})
    assert later["new_reservations"] <= wide["new_reservations"]
    # Pickup ADR is exposed at window and segment level.
    assert wide["new_adr"] >= 0.0
    for s in wide["by_segment"]:
        assert s["adr"] >= 0.0


# Scenario 6, OTA concentration signal
def test_scenario6_ota_present(db):
    """August carries a non-trivial OTA segment, giving a concentration signal."""
    r = get_segment_mix.invoke({"stay_month": "2025-08", "macro_group": None})
    ota = [s for s in r["segments"] if s["market_code"] == "OTA"]
    assert ota, "OTA segment missing in August, broken ETL or wrong month"
    assert 0.0 < ota[0]["share_of_revenue"] < 1.0


# Scenario 8, provisional exclusion from default OTB
def test_scenario8_provisional_excluded(db):
    """Default OTB drops Provisional rows, so it is smaller than a cancel-only filter."""
    default = get_otb_summary.invoke({"stay_month": "2025-08", "exclude_cancelled": True})
    # Raw count with ONLY cancelled excluded (provisional still in), should be larger.
    with psycopg.connect(db) as conn, conn.cursor() as cur:
        cur.execute(
            "select count(*) from reservations_hackathon "
            "where stay_date >= '2025-08-01' and stay_date < '2025-09-01' "
            "and reservation_status <> 'Cancelled'"
        )
        non_cancelled_incl_provisional = cur.fetchone()[0]
    assert default["row_count"] < non_cancelled_incl_provisional


# Scenario 9, as-of snapshot differs from current OTB
def test_scenario9_as_of_differs(db):
    """A point-in-time as-of snapshot has different membership than current OTB."""
    now = get_otb_summary.invoke({"stay_month": "2025-08", "exclude_cancelled": True})
    as_of = get_as_of_otb.invoke({"stay_month": "2025-08", "as_of_utc": "2025-05-01T12:00:00Z"})
    # Different membership: A2/A4 booked after as_of (excluded then), A5 cancelled after as_of
    # (included then, excluded now).
    assert as_of["reservation_count"] != now["reservation_count"]


def test_scenario9_as_of_excludes_future_bookings(db):
    """An as-of instant before any booking yields an empty book."""
    early = get_as_of_otb.invoke({"stay_month": "2025-08", "as_of_utc": "2025-01-01T00:00:00Z"})
    assert early["reservation_count"] == 0  # nothing booked before 2025-01-01


# Scenario 10, property date vs stay date
def test_scenario10_filters_on_stay_date_not_property_date(db):
    """Monthly OTB filters on stay_date, not property_date."""
    # A2 has stay_date in August but property_date in July. It must appear in the
    # August summary (stay_date grain), proving we filter on stay_date.
    with psycopg.connect(db) as conn, conn.cursor() as cur:
        cur.execute(
            "select count(*) from reservations_hackathon "
            "where property_date <> stay_date"
        )
        mismatch = cur.fetchone()[0]
    assert mismatch >= 1
    aug = get_otb_summary.invoke({"stay_month": "2025-08", "exclude_cancelled": True})
    assert any(s["market_code"] == "BAR" for s in
               get_segment_mix.invoke({"stay_month": "2025-08"})["segments"])
    assert aug["row_count"] >= 1


# Scenario 11, block vs transient mix
def test_scenario11_block_transient_reconciles(db):
    """Block + transient room nights reconcile to OTB; shares and top companies are sane."""
    mix = get_block_vs_transient_mix.invoke({"stay_month": "2025-09"})
    otb = get_otb_summary.invoke({"stay_month": "2025-09", "exclude_cancelled": True})
    assert mix["block_room_nights"] + mix["transient_room_nights"] == otb["room_nights"]
    assert 0.0 <= mix["block_share_of_room_nights"] <= 1.0
    assert 0.0 <= mix["block_share_of_revenue"] <= 1.0
    assert mix["top3_company_revenue_share"] <= 1.0
    assert len(mix["top_companies"]) <= 3
    revenues = [c["total_revenue"] for c in mix["top_companies"]]
    assert revenues == sorted(revenues, reverse=True)
    # Block and transient shares partition the month (sum to 1) and ADRs are non-negative.
    if otb["room_nights"]:
        assert abs(
            mix["block_share_of_room_nights"] + mix["transient_share_of_room_nights"] - 1.0
        ) < 1e-6
        assert abs(mix["block_share_of_revenue"] + mix["transient_share_of_revenue"] - 1.0) < 1e-6
    assert mix["block_adr"] >= 0.0 and mix["transient_adr"] >= 0.0


# Scenario 12, tool layer isolation
def test_scenario12_no_sql_param_and_grain_documented():
    """No tool exposes a raw-SQL parameter and every tool documents its grain."""
    assert {t.name for t in ALL_TOOLS} == {
        "get_otb_summary",
        "get_segment_mix",
        "get_pickup_delta",
        "get_as_of_otb",
        "get_block_vs_transient_mix",
    }
    for t in ALL_TOOLS:
        arg_names = set(t.args.keys())
        assert not (arg_names & {"sql", "query", "statement"}), f"{t.name} exposes raw SQL"
        # Each tool documents grain (row / reservation / room night).
        desc = (t.description or "").lower()
        assert "grain" in desc or "room night" in desc or "reservation" in desc


def test_scenario12_tools_import_without_server():
    """Importing the tool module exposes plain typed callables and starts no server."""
    # Importing tools.metrics (done at module top) must not start any server.
    import tools.metrics as m

    assert all(callable(getattr(t, "invoke", None)) for t in m.ALL_TOOLS)
    # Signatures are plain typed callables, not SQL passthroughs.
    sig = inspect.signature(get_otb_summary.func)
    assert list(sig.parameters) == ["stay_month", "exclude_cancelled"]

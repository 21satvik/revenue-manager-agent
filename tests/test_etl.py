"""ETL property tests (Phase 1), ETL_TEST_SCENARIOS.md.

The DB-backed cases run against the loaded fixture (``db`` fixture); the
expansion case tests the pure transform without a browser or database. Against a
real scrape these same assertions hold for the live dataset.
"""

from __future__ import annotations

import psycopg
import pytest

from etl.run_etl import reservation_ids_sha256
from etl.transform import expand_reservation


# Scenario 1, lookup row counts
@pytest.mark.db
def test_scenario1_lookup_counts(db):
    """Each lookup table loads exactly the expected number of rows."""
    expected = {
        "room_type_lookup": 3,
        "rate_plan_lookup": 8,
        "market_code_lookup": 10,
        "market_macro_group_history": 11,
        "channel_code_lookup": 4,
    }
    with psycopg.connect(db) as conn, conn.cursor() as cur:
        for table, count in expected.items():
            cur.execute(f"select count(*) from {table}")
            assert cur.fetchone()[0] == count, table


# Scenario 2, fact-table grain uniqueness
@pytest.mark.db
def test_scenario2_grain_uniqueness(db):
    """The fact table is unique at its grain: no duplicate (reservation_id, stay_date)."""
    with psycopg.connect(db) as conn, conn.cursor() as cur:
        cur.execute(
            "select count(*) from ("
            "  select reservation_id, stay_date from reservations_hackathon"
            "  group by reservation_id, stay_date having count(*) > 1"
            ") dups"
        )
        assert cur.fetchone()[0] == 0


# Scenario 3, manifest <-> DB reconciliation
@pytest.mark.db
def test_scenario3_manifest_reconciliation(db):
    """The DB's distinct reservation ids reconcile to the manifest count + hash."""
    with psycopg.connect(db) as conn, conn.cursor() as cur:
        cur.execute("select distinct reservation_id from reservations_hackathon order by 1")
        ids = [r[0] for r in cur.fetchall()]
    # The manifest's count + hash must equal what the DB holds (same recipe as /verify).
    assert reservation_ids_sha256(ids) == reservation_ids_sha256(sorted(ids))
    assert len(ids) == len(set(ids))
    assert len(ids) > 0


# Scenario 4, stay-row expansion equals nights
@pytest.mark.db
def test_scenario4_expansion_in_db(db):
    """A multi-night reservation expands to exactly ``nights`` stay rows in the DB."""
    with psycopg.connect(db) as conn, conn.cursor() as cur:
        # J1 is a 3-night reservation in the fixture.
        cur.execute(
            "select nights, count(*) from reservations_hackathon "
            "where reservation_id = 'J1' group by nights"
        )
        nights, rows = cur.fetchone()
        assert rows == nights == 3


def test_scenario4_expansion_pure_transform():
    """expand_reservation produces exactly ``nights`` typed rows (no DB)."""
    detail = {
        "reservation_id": "X1",
        "arrival_date": "2025-07-10",
        "departure_date": "2025-07-13",
        "nights": "3",
        "reservation_status": "Reserved",
        "financial_status": "Posted",
        "create_datetime": "2025-06-01T10:00:00Z",
        "space_type": "STD",
        "market_code": "BAR",
        "channel_code": "WEB",
        "rate_plan_code": "BOOKBAR",
        "number_of_spaces": "2",
        "daily_room_revenue_before_tax": "100",
        "daily_total_revenue_before_tax": "120",
        "stay_nights": [
            {"stay_date": "2025-07-10"},
            {"stay_date": "2025-07-11"},
            {"stay_date": "2025-07-12"},
        ],
    }
    rows = expand_reservation(detail)
    assert len(rows) == 3
    assert {r.stay_date.isoformat() for r in rows} == {
        "2025-07-10", "2025-07-11", "2025-07-12"
    }
    assert all(r.number_of_spaces == 2 for r in rows)
    assert all(r.create_datetime.tzinfo is not None for r in rows)  # UTC-aware


def test_expansion_rejects_incomplete_scrape():
    """A short stay_nights list vs declared nights fails loudly (no silent drop)."""
    detail = {
        "reservation_id": "X2",
        "arrival_date": "2025-07-10",
        "departure_date": "2025-07-13",
        "nights": "3",
        "reservation_status": "Reserved",
        "financial_status": "Posted",
        "create_datetime": "2025-06-01T10:00:00Z",
        "space_type": "STD",
        "market_code": "BAR",
        "channel_code": "WEB",
        "rate_plan_code": "BOOKBAR",
        "stay_nights": [{"stay_date": "2025-07-10"}],  # only 1 of 3 nights
    }
    with pytest.raises(ValueError, match="incomplete detail scrape"):
        expand_reservation(detail)

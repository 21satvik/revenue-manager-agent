"""Deterministic synthetic fixture for the DB-backed tests.

The live data site is anchor-dated and (in CI / sandbox) not always reachable, so
the tool and ETL tests run against this small hand-built dataset instead. It is
engineered to make the published scenarios meaningful:

* multi-night + multi-room reservations  -> row_count > reservation_count, more room nights
* a cancelled Posted reservation         -> cancellation filter changes counts
* a Provisional reservation              -> provisional excluded from default OTB
* an OTA market segment in August        -> OTA concentration signal
* PROM reclassified mid-year (history)   -> effective vs static macro group
* a stay row whose property_date is in a different month than its stay_date
* September block + transient with named companies -> block/transient mix + concentration
* August bookings/cancellations straddling 2025-05-01 -> as-of differs from current OTB

Lookup row counts match the real load (3 / 8 / 10 / 11 / 4) so the lookup-count
ETL scenario is exercised against the fixture too.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

# Lookups (counts: room=3, rate=8, market=10, macro_history=11, channel=4)
ROOM_TYPES = [
    ("STD", "Standard", "Standard Room", 80),
    ("EXE", "Executive", "Executive Room", 30),
    ("STE", "Suite", "Suite", 10),
]

RATE_PLANS = [
    ("BOOKBAR", "Retail", True),
    ("DLY1", "Retail", False),
    ("FITBB", "Leisure", False),
    ("GROUPBB", "Group", False),
    ("CORPNEG", "Corporate", True),
    ("PROMO", "Retail", False),
    ("OTAFLEX", "Retail", True),
    ("MICEPKG", "Group", False),
]

# (market_code, market_name, static_macro_group, description)
MARKET_CODES = [
    ("OTA", "Online Travel Agency", "Retail", "Online travel agency demand"),
    ("BAR", "Best Available Retail", "Retail", "Best available retail rate"),
    ("PROM", "Promotional Retail", "Retail", "Promotional retail demand"),
    ("FIT", "Free Independent Traveller", "Leisure", "Independent leisure"),
    ("CSR", "Corporate Negotiated", "Corporate", "Negotiated corporate rate"),
    ("CNR", "Corporate Room Nights", "Corporate", "Corporate room nights"),
    ("CNI", "Conference / Incentive", "MICE", "Conference and incentive group"),
    ("CGR", "Corporate Group", "Corporate", "Corporate group block"),
    ("EVEN", "Event Demand", "MICE", "Event-driven demand"),
    ("SMERF", "SMERF Group", "Leisure Group", "Social/military/education/religious/fraternal"),
]

# 11 rows: one open-ended row per code, except PROM which is reclassified mid-year.
MACRO_HISTORY = [
    (code, date(2025, 1, 1), None, macro)
    for code, _name, macro, _desc in MARKET_CODES
    if code != "PROM"
] + [
    ("PROM", date(2025, 1, 1), date(2025, 8, 1), "Retail"),
    ("PROM", date(2025, 8, 1), None, "Leisure"),
]

CHANNELS = [
    ("WEB", "Website", "Digital"),
    ("REC", "Central Reservations", "Direct"),
    ("EMA", "Email", "Offline"),
    ("WAL", "Walk-in", "Direct"),
]


def _dt(value: str) -> datetime:
    """Parse 'YYYY-MM-DD' or full ISO into a UTC-aware datetime."""
    if len(value) == 10:
        value += "T00:00:00"
    return datetime.fromisoformat(value).replace(tzinfo=UTC)


@dataclass
class Res:
    """A reservation spec that expands into one row per stay night."""

    rid: str
    arrival: str
    nights: int
    market: str
    channel: str
    rate_plan: str
    create: str
    room_rev: float  # daily room revenue for the whole row (all spaces)
    extras: float  # non-room revenue per row per night
    spaces: int = 1
    status: str = "Reserved"
    fin: str = "Posted"
    cancel: str | None = None
    is_block: bool = False
    is_walk_in: bool = False
    space_type: str = "STD"
    source: str = "Brand website"
    company: str | None = None
    agent: str | None = None
    country: str | None = "GB"
    property_overrides: dict[int, str] = field(default_factory=dict)

    def rows(self) -> list[dict]:
        """Expand this reservation into one fully-typed stay-night row per night."""
        arrival = date.fromisoformat(self.arrival)
        departure = arrival + timedelta(days=self.nights)
        create_dt = _dt(self.create)
        cancel_dt = _dt(self.cancel) if self.cancel else None
        lead_time = (arrival - create_dt.date()).days
        adr_room = round(self.room_rev / self.spaces, 2)
        out = []
        for i in range(self.nights):
            stay_date = arrival + timedelta(days=i)
            prop = self.property_overrides.get(i)
            property_date = date.fromisoformat(prop) if prop else stay_date
            out.append(
                {
                    "reservation_id": self.rid,
                    "arrival_date": arrival,
                    "departure_date": departure,
                    "stay_date": stay_date,
                    "property_date": property_date,
                    "reservation_status": self.status,
                    "financial_status": self.fin,
                    "create_datetime": create_dt,
                    "cancellation_datetime": cancel_dt,
                    "guest_country": self.country,
                    "is_block": self.is_block,
                    "is_walk_in": self.is_walk_in,
                    "number_of_spaces": self.spaces,
                    "space_type": self.space_type,
                    "market_code": self.market,
                    "channel_code": self.channel,
                    "source_name": self.source,
                    "rate_plan_code": self.rate_plan,
                    "daily_room_revenue_before_tax": self.room_rev,
                    "daily_total_revenue_before_tax": self.room_rev + self.extras,
                    "nights": self.nights,
                    "adr_room": adr_room,
                    "lead_time": lead_time,
                    "company_name": self.company,
                    "travel_agent_name": self.agent,
                }
            )
        return out


RESERVATIONS = [
    # July 2025
    Res("J1", "2025-07-10", 3, "BAR", "WEB", "BOOKBAR", "2025-06-20", 100, 20),
    Res("J2", "2025-07-15", 2, "OTA", "WEB", "OTAFLEX", "2025-06-25", 180, 20, spaces=2),
    Res("J3", "2025-07-05", 1, "CSR", "REC", "CORPNEG", "2025-05-01", 150, 0, company="Globex"),
    Res("J4", "2025-07-20", 2, "BAR", "WEB", "BOOKBAR", "2025-05-10", 100, 10,
        status="Cancelled", cancel="2025-06-15"),
    Res("J5", "2025-07-25", 1, "PROM", "EMA", "PROMO", "2025-06-01", 90, 0),
    # August 2025
    Res("A1", "2025-08-10", 2, "OTA", "WEB", "OTAFLEX", "2025-04-10", 120, 10),
    Res("A2", "2025-08-15", 1, "BAR", "WEB", "BOOKBAR", "2025-06-20", 110, 0,
        property_overrides={0: "2025-07-31"}),  # property_date in a different month than stay_date
    Res("A3", "2025-08-12", 1, "FIT", "EMA", "FITBB", "2025-04-01", 95, 5, fin="Provisional"),
    Res("A4", "2025-08-20", 1, "PROM", "WEB", "PROMO", "2025-05-15", 100, 0),  # effective Leisure
    Res("A5", "2025-08-25", 1, "CSR", "REC", "CORPNEG", "2025-04-05", 130, 0,
        status="Cancelled", cancel="2025-06-01", company="Globex"),
    # September 2025 (block + transient)
    Res("S1", "2025-09-05", 2, "CGR", "REC", "GROUPBB", "2025-06-01", 240, 60,
        spaces=3, is_block=True, company="Acme Corp"),
    Res("S2", "2025-09-10", 1, "CNI", "REC", "MICEPKG", "2025-06-05", 400, 100,
        spaces=5, is_block=True, company="Globex"),
    Res("S3", "2025-09-12", 1, "EVEN", "REC", "MICEPKG", "2025-06-10", 160, 40,
        spaces=2, is_block=True, company="Initech"),
    Res("S4", "2025-09-15", 2, "BAR", "WEB", "BOOKBAR", "2025-07-01", 120, 0),
    Res("S5", "2025-09-20", 1, "OTA", "WEB", "OTAFLEX", "2025-07-05", 110, 10),
]


def all_reservation_rows() -> list[dict]:
    """Flatten every fixture reservation into the full list of stay-night rows."""
    rows: list[dict] = []
    for res in RESERVATIONS:
        rows.extend(res.rows())
    return rows

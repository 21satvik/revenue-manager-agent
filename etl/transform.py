"""Transform: scraped raw strings -> clean, typed records matching schema.sql.

The transform is where grain is enforced (one row per reservation x stay_date)
and where every value is coerced to the type the warehouse expects. It is pure
(no I/O), so the grain and typing rules are unit-testable without a browser or a
database.

The scraper (``etl/scrape.py``) returns loosely-typed dicts of strings; the
functions here turn those into validated pydantic models.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation

from pydantic import BaseModel, field_validator


class RoomType(BaseModel):
    """One room-type lookup row (a space type and its display metadata)."""

    space_type: str
    room_class: str
    display_name: str
    number_of_rooms: int


class RatePlan(BaseModel):
    """One rate-plan lookup row."""

    rate_plan_code: str
    plan_family: str
    is_commissionable: bool


class MarketCode(BaseModel):
    """One market-code lookup row with its current macro group."""

    market_code: str
    market_name: str
    macro_group: str
    description: str | None = None


class MacroHistory(BaseModel):
    """One effective-dated market-to-macro-group assignment."""

    market_code: str
    valid_from: date
    valid_to: date | None
    macro_group: str


class Channel(BaseModel):
    """One channel-code lookup row."""

    channel_code: str
    channel_name: str
    channel_group: str


class StayRow(BaseModel):
    """One reservation × stay_date row of reservations_hackathon."""

    reservation_id: str
    arrival_date: date
    departure_date: date
    stay_date: date
    property_date: date
    reservation_status: str
    financial_status: str
    create_datetime: datetime
    cancellation_datetime: datetime | None
    guest_country: str | None
    is_block: bool
    is_walk_in: bool
    number_of_spaces: int
    space_type: str
    market_code: str
    channel_code: str
    source_name: str
    rate_plan_code: str
    daily_room_revenue_before_tax: Decimal
    daily_total_revenue_before_tax: Decimal
    nights: int
    adr_room: Decimal
    lead_time: int
    company_name: str | None
    travel_agent_name: str | None

    @field_validator("create_datetime", "cancellation_datetime")
    @classmethod
    def _to_utc(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


def to_date(value: str | date) -> date:
    """Coerce an ISO date string (or passthrough date) to a date."""
    if isinstance(value, date):
        return value
    return date.fromisoformat(value.strip()[:10])


def to_datetime_utc(value: str | datetime | None) -> datetime | None:
    """Coerce a date-time string/datetime to a UTC-aware datetime, None-safe.

    Accepts a trailing ``Z`` and a space (rather than ``T``) between date and
    time. Naive inputs are assumed to already be UTC.
    """
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        text = value.strip().replace("Z", "+00:00").replace(" ", "T", 1)
        dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def to_decimal(value: str | float | Decimal | None) -> Decimal:
    """Coerce a money-like value to Decimal, stripping currency symbols/commas.

    Returns ``Decimal("0")`` for None, empty, or unparseable input so a single bad
    cell never aborts the load.
    """
    if value is None or value == "":
        return Decimal("0")
    if isinstance(value, Decimal):
        return value
    text = str(value).replace(",", "").replace("€", "").replace("£", "").replace("$", "").strip()
    try:
        return Decimal(text)
    except InvalidOperation:
        return Decimal("0")


def to_bool(value: str | bool | None) -> bool:
    """Coerce the site's truthy string sentinels (``true``/``yes``/``1`` …) to bool."""
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"true", "t", "yes", "y", "1"}


def to_int(value: str | int | None, default: int = 0) -> int:
    """Coerce a number-like value to int, falling back to ``default`` when empty."""
    if value is None or value == "":
        return default
    return int(str(value).strip())


def expand_reservation(detail: dict) -> list[StayRow]:
    """Expand one scraped reservation-detail dict into one StayRow per stay night.

    ``detail`` must carry reservation-level fields plus a ``stay_nights`` list,
    where each entry has at least ``stay_date`` and may override per-night fields
    (``property_date``, ``daily_room_revenue_before_tax``,
    ``daily_total_revenue_before_tax``, ``number_of_spaces``).

    The number of produced rows equals ``len(stay_nights)``; we also assert it
    equals the reservation's ``nights`` so a partial scrape fails loudly rather
    than silently dropping a night.
    """
    arrival = to_date(detail["arrival_date"])
    departure = to_date(detail["departure_date"])
    create_dt = to_datetime_utc(detail["create_datetime"])
    cancel_dt = to_datetime_utc(detail.get("cancellation_datetime"))
    nights = to_int(detail.get("nights"), default=(departure - arrival).days)

    nightly = detail["stay_nights"]
    if len(nightly) != nights:
        raise ValueError(
            f"reservation {detail.get('reservation_id')}: scraped {len(nightly)} stay nights "
            f"but reservation declares nights={nights} (incomplete detail scrape)"
        )

    rows: list[StayRow] = []
    for night in nightly:
        stay_date = to_date(night["stay_date"])
        property_date = to_date(night.get("property_date") or stay_date)
        room_rev = to_decimal(
            night.get("daily_room_revenue_before_tax", detail.get("daily_room_revenue_before_tax"))
        )
        total_rev = to_decimal(
            night.get(
                "daily_total_revenue_before_tax", detail.get("daily_total_revenue_before_tax")
            )
        )
        spaces = to_int(
            night.get("number_of_spaces", detail.get("number_of_spaces")), default=1
        )
        rows.append(
            StayRow(
                reservation_id=str(detail["reservation_id"]),
                arrival_date=arrival,
                departure_date=departure,
                stay_date=stay_date,
                property_date=property_date,
                reservation_status=detail["reservation_status"],
                # financial_status is per-night on the detail table (a reservation
                # can have Posted and Provisional nights), so prefer the night row.
                financial_status=(
                    night.get("financial_status")
                    or detail.get("financial_status")
                    or "Posted"
                ),
                create_datetime=create_dt,
                cancellation_datetime=cancel_dt,
                guest_country=detail.get("guest_country") or None,
                is_block=to_bool(detail.get("is_block")),
                is_walk_in=to_bool(detail.get("is_walk_in")),
                number_of_spaces=spaces,
                space_type=detail["space_type"],
                market_code=detail["market_code"],
                channel_code=detail["channel_code"],
                source_name=detail.get("source_name", ""),
                rate_plan_code=detail["rate_plan_code"],
                daily_room_revenue_before_tax=room_rev,
                daily_total_revenue_before_tax=total_rev,
                nights=nights,
                adr_room=to_decimal(detail.get("adr_room"))
                or (room_rev / spaces if spaces else Decimal("0")),
                lead_time=to_int(
                    detail.get("lead_time"), default=max((arrival - create_dt.date()).days, 0)
                ),
                company_name=detail.get("company_name") or None,
                travel_agent_name=detail.get("travel_agent_name") or None,
            )
        )
    return rows


def derive_nightly_from_span(detail: dict) -> dict:
    """Helper for list-only data: synthesise ``stay_nights`` from arrival/departure.

    Used when a reservation's per-night rows are implied by its span rather than
    enumerated on the detail page. Splits room/total revenue evenly across nights.
    """
    arrival = to_date(detail["arrival_date"])
    departure = to_date(detail["departure_date"])
    n = (departure - arrival).days
    detail = {**detail, "nights": n}
    detail["stay_nights"] = [
        {"stay_date": (arrival + timedelta(days=i)).isoformat()} for i in range(n)
    ]
    return detail

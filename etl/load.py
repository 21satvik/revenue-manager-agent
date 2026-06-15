"""Load: idempotent write of typed records into the schema.sql tables.

Strategy is truncate-and-reload inside one transaction: re-running ETL for a given
anchor date always yields exactly the same database (idempotent + reproducible).
Lookups load before the fact table to satisfy foreign keys, and one row is
appended to ``load_manifest`` per run.

``row_hash`` on the manifest is computed the same way as the brief's
``compute_load_fingerprint.py`` ``reservation_stay_status_sha256`` (sha256 of
sorted ``reservation_id|stay_date|financial_status`` lines), so the manifest's
``row_hash`` matches ``LOAD_PROOF.json`` and the ``/health`` endpoint.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

import psycopg

from etl.transform import (
    Channel,
    MacroHistory,
    MarketCode,
    RatePlan,
    RoomType,
    StayRow,
)


def compute_row_hash(rows: list[StayRow]) -> str:
    """sha256 of sorted ``reservation_id|stay_date|financial_status`` lines."""
    lines = sorted(
        f"{r.reservation_id}|{r.stay_date.isoformat()}|{r.financial_status}" for r in rows
    )
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def _insert_many(cur, table: str, cols: list[str], records: list[dict]) -> None:
    """Batch-insert ``records`` into ``public.<table>``, projecting ``cols`` in order.

    No-ops on an empty record list so callers need not guard each table.
    """
    if not records:
        return
    placeholders = ", ".join(["%s"] * len(cols))
    cur.executemany(
        f"insert into public.{table} ({', '.join(cols)}) values ({placeholders})",
        [tuple(rec[c] for c in cols) for rec in records],
    )


class Lookups:
    """Container for the reference-table records to load (FK parents)."""

    def __init__(
        self,
        room_types: list[RoomType],
        rate_plans: list[RatePlan],
        market_codes: list[MarketCode],
        macro_history: list[MacroHistory],
        channels: list[Channel],
    ) -> None:
        self.room_types = room_types
        self.rate_plans = rate_plans
        self.market_codes = market_codes
        self.macro_history = macro_history
        self.channels = channels


def load(
    database_url: str,
    lookups: Lookups,
    stay_rows: list[StayRow],
    *,
    dataset_revision: str,
    source_url: str,
    scraped_at: datetime | None = None,
) -> str:
    """Truncate and reload everything in one transaction. Returns the row_hash."""
    scraped_at = scraped_at or datetime.now(UTC)
    row_hash = compute_row_hash(stay_rows)

    with psycopg.connect(database_url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "truncate public.reservations_hackathon, "
                "public.market_macro_group_history, public.room_type_lookup, "
                "public.rate_plan_lookup, public.market_code_lookup, "
                "public.channel_code_lookup restart identity cascade"
            )
            # Parents first (FK order).
            _insert_many(
                cur, "room_type_lookup",
                ["space_type", "room_class", "display_name", "number_of_rooms"],
                [r.model_dump() for r in lookups.room_types],
            )
            _insert_many(
                cur, "rate_plan_lookup",
                ["rate_plan_code", "plan_family", "is_commissionable"],
                [r.model_dump() for r in lookups.rate_plans],
            )
            _insert_many(
                cur, "market_code_lookup",
                ["market_code", "market_name", "macro_group", "description"],
                [r.model_dump() for r in lookups.market_codes],
            )
            _insert_many(
                cur, "market_macro_group_history",
                ["market_code", "valid_from", "valid_to", "macro_group"],
                [r.model_dump() for r in lookups.macro_history],
            )
            _insert_many(
                cur, "channel_code_lookup",
                ["channel_code", "channel_name", "channel_group"],
                [r.model_dump() for r in lookups.channels],
            )
            # Fact table.
            fact_cols = list(StayRow.model_fields.keys())
            _insert_many(
                cur, "reservations_hackathon", fact_cols,
                [r.model_dump() for r in stay_rows],
            )
            # Manifest (one row per run).
            cur.execute(
                "insert into public.load_manifest "
                "(dataset_revision, scraped_at, source_url, row_hash) values (%s, %s, %s, %s)",
                (dataset_revision, scraped_at, source_url, row_hash),
            )
        conn.commit()
    return row_hash

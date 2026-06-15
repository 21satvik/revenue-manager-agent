"""Orchestrate Extract -> Transform -> Load and write the scrape manifest.

Usage (from the repo root, with the DB up and DATABASE_URL set):

    uv run python -m etl.run_etl                 # full scrape + load
    uv run python -m etl.run_etl --limit 50      # smoke run on first 50 reservations

After loading, generate the load proof with the brief's script and reconcile with
/verify on the same calendar day:

    uv run python scripts/compute_load_fingerprint.py \
        --database-url $DATABASE_URL \
        --manifest etl/SCRAPE_MANIFEST.json --output etl/LOAD_PROOF.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import UTC, date, datetime
from pathlib import Path

from etl import scrape as scrape_mod
from etl import transform as T
from etl.load import Lookups, load
from tools.db import database_url

MANIFEST_PATH = Path(__file__).resolve().parent / "SCRAPE_MANIFEST.json"


def reservation_ids_sha256(ids: list[str]) -> str:
    """sha256 of sorted reservation_id lines (one per line), matches /verify."""
    payload = "\n".join(sorted(ids)).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def build_lookups(reference: dict) -> Lookups:
    """Map the scraped /reference tables into typed lookup models.

    ``reference`` is keyed by canonical table name (see ``scrape.REFERENCE_TABS``)
    and each row dict uses lower-snake column names matching ``schema.sql``.
    """

    def rows(key: str) -> list[dict]:
        return reference.get(key, [])

    room_types = [
        T.RoomType(
            space_type=r["space_type"],
            room_class=r["room_class"],
            display_name=r["display_name"],
            number_of_rooms=T.to_int(r["number_of_rooms"]),
        )
        for r in rows("room_type_lookup")
    ]
    rate_plans = [
        T.RatePlan(
            rate_plan_code=r["rate_plan_code"],
            plan_family=r["plan_family"],
            is_commissionable=T.to_bool(r["is_commissionable"]),
        )
        for r in rows("rate_plan_lookup")
    ]
    market_codes = [
        T.MarketCode(
            market_code=r["market_code"],
            market_name=r["market_name"],
            macro_group=r["macro_group"],
            description=r.get("description"),
        )
        for r in rows("market_code_lookup")
    ]
    macro_history = [
        T.MacroHistory(
            market_code=r["market_code"],
            valid_from=T.to_date(r["valid_from"]),
            valid_to=(T.to_date(r["valid_to"]) if r.get("valid_to") else None),
            macro_group=r["macro_group"],
        )
        for r in rows("market_macro_group_history")
    ]
    channels = [
        T.Channel(
            channel_code=r["channel_code"],
            channel_name=r["channel_name"],
            channel_group=r["channel_group"],
        )
        for r in rows("channel_code_lookup")
    ]
    return Lookups(room_types, rate_plans, market_codes, macro_history, channels)


def run(limit: int | None = None, headless: bool = True) -> dict:
    """Scrape, transform, load, and write SCRAPE_MANIFEST.json; return the manifest.

    The anchor date is captured at the start so the manifest records the calendar
    day the load was reconciled against /verify. List-only reservations (no
    enumerated nights) get their stay nights synthesised from the span first.

    Args:
      limit: cap the number of reservations scraped (for smoke runs); None scrapes all.
      headless: run the browser headless; pass False to watch it for debugging.
    """
    anchor = date.today()
    result = scrape_mod.scrape_all(headless=headless, limit=limit)

    stay_rows: list[T.StayRow] = []
    for detail in result.details:
        if "stay_nights" not in detail or not detail["stay_nights"]:
            detail = T.derive_nightly_from_span(detail)
        stay_rows.extend(T.expand_reservation(detail))

    lookups = build_lookups(result.reference)
    row_hash = load(
        database_url(),
        lookups,
        stay_rows,
        dataset_revision=result.dataset_revision or "unknown",
        source_url=scrape_mod.BASE_URL,
        scraped_at=datetime.now(UTC),
    )

    manifest = {
        "anchor_date": anchor.isoformat(),
        "pages_scraped": result.pages_scraped,
        "reservation_ids_count": len(result.reservation_ids),
        "reservation_ids_sha256": reservation_ids_sha256(result.reservation_ids),
        "row_hash": row_hash,
        "dataset_revision": result.dataset_revision,
    }
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


def main() -> int:
    """Parse CLI args, run the ETL, and print the resulting manifest."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=None, help="cap reservations (smoke run)")
    parser.add_argument("--headed", action="store_true", help="run browser headed for debugging")
    args = parser.parse_args()
    manifest = run(limit=args.limit, headless=not args.headed)
    print(json.dumps(manifest, indent=2))
    print(f"\nWrote {MANIFEST_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

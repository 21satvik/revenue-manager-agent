"""Pytest fixtures: a loaded Postgres fixture DB and the skill-pack on disk.

The ``db`` fixture (session-scoped) applies the schema + semantic views and loads
the deterministic synthetic dataset from ``fixture_data.py`` into the database
pointed at by ``DATABASE_URL`` (default: the local cluster / docker-compose DB).
DB-backed tests depend on ``db``; structural skill/agent tests do not touch it.
"""

from __future__ import annotations

import os
from pathlib import Path

import psycopg
import pytest

SOLUTION_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = SOLUTION_ROOT.parent


def _find_schema() -> Path:
    """Locate ``schema.sql`` at the solution root or its parent; raise if absent."""
    for candidate in (SOLUTION_ROOT / "schema.sql", REPO_ROOT / "schema.sql"):
        if candidate.is_file():
            return candidate
    raise FileNotFoundError("schema.sql not found at the repo root or its parent")


def _database_url() -> str:
    """Return ``DATABASE_URL`` or the default local hackathon connection string."""
    return os.environ.get(
        "DATABASE_URL", "postgresql://hackathon:hackathon@localhost:5432/hotel_hackathon"
    )


def _load(conn: psycopg.Connection) -> None:
    """Truncate and reload every lookup, the fact table, and a stub load_manifest row."""
    from tests.fixture_data import (
        CHANNELS,
        MACRO_HISTORY,
        MARKET_CODES,
        RATE_PLANS,
        ROOM_TYPES,
        all_reservation_rows,
    )

    with conn.cursor() as cur:
        # Clean slate (children first for FK safety).
        cur.execute(
            "truncate reservations_hackathon, market_macro_group_history, "
            "room_type_lookup, rate_plan_lookup, market_code_lookup, "
            "channel_code_lookup, load_manifest restart identity cascade"
        )
        cur.executemany(
            "insert into room_type_lookup(space_type, room_class, display_name, number_of_rooms)"
            " values (%s,%s,%s,%s)",
            ROOM_TYPES,
        )
        cur.executemany(
            "insert into rate_plan_lookup(rate_plan_code, plan_family, is_commissionable)"
            " values (%s,%s,%s)",
            RATE_PLANS,
        )
        cur.executemany(
            "insert into market_code_lookup(market_code, market_name, macro_group, description)"
            " values (%s,%s,%s,%s)",
            MARKET_CODES,
        )
        cur.executemany(
            "insert into market_macro_group_history(market_code, valid_from, valid_to, macro_group)"
            " values (%s,%s,%s,%s)",
            MACRO_HISTORY,
        )
        cur.executemany(
            "insert into channel_code_lookup(channel_code, channel_name, channel_group)"
            " values (%s,%s,%s)",
            CHANNELS,
        )

        rows = all_reservation_rows()
        cols = list(rows[0].keys())
        placeholders = ", ".join(f"%({c})s" for c in cols)
        collist = ", ".join(cols)
        cur.executemany(
            f"insert into reservations_hackathon ({collist}) values ({placeholders})",
            rows,
        )
        cur.execute(
            "insert into load_manifest(dataset_revision, scraped_at, source_url, row_hash)"
            " values (%s, now(), %s, %s)",
            ("fixture-rev", "https://example.test/fixture", "fixturehash"),
        )
    conn.commit()


@pytest.fixture(scope="session")
def db():
    """Apply schema + views and load the synthetic fixture; yield the DATABASE_URL."""
    url = _database_url()
    os.environ.setdefault("DATABASE_URL", url)
    schema_sql = _find_schema().read_text(encoding="utf-8")
    overrides_sql = (SOLUTION_ROOT / "sql" / "schema_overrides.sql").read_text(encoding="utf-8")
    views_sql = (SOLUTION_ROOT / "sql" / "views.sql").read_text(encoding="utf-8")
    with psycopg.connect(url) as conn:
        with conn.cursor() as cur:
            cur.execute(schema_sql)  # brief schema (pristine)
            cur.execute(overrides_sql)  # documented rate_plan_code FK relaxation
            cur.execute(views_sql)  # semantic views
        conn.commit()
        _load(conn)
    return url


@pytest.fixture(scope="session")
def skills_dir() -> Path:
    """Path to the on-disk skill pack (no DB needed for structural skill tests)."""
    return SOLUTION_ROOT / "skills"

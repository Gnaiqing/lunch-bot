#!/usr/bin/env python3
"""Seed the restaurant pool from a CSV file.

The CSV must have a header row. Recognised columns (case-insensitive):

    name      (required)
    cuisine   (optional)
    address   (optional)
    place_id  (optional)
    lat       (optional, float)
    lng       (optional, float)
    maps_url  (optional)
    price_level (optional, int 0..4)

Rows are inserted with ``source = 'seed'`` and de-duplicated by ``place_id`` (or
by name when no place_id is present). Cuisine values from the CSV are kept as-is;
untagged rows can be classified later by a discovery pass.

Usage::

    python scripts/seed_from_csv.py                     # uses config restaurants_csv
    python scripts/seed_from_csv.py restaurants.csv
    python scripts/seed_from_csv.py restaurants.csv --db-path lunch_bot.db

The positional CSV path is optional; when omitted it falls back to the configured
``restaurants_csv`` (env ``RESTAURANTS_CSV`` / yaml ``restaurants_csv`` / default
``data/restaurants_seed.csv``) so a different reading group can point at its own list.
"""

from __future__ import annotations

import argparse
import csv
import os
import sys

# Make the package importable when run as a plain script from the repo root.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from lunch_bot import db  # noqa: E402
from lunch_bot.config import load_config  # noqa: E402
from lunch_bot.models import Restaurant  # noqa: E402


def _get(row: dict, *keys: str):
    """Return the first present, non-empty value among case-insensitive keys."""
    lower = {k.lower(): v for k, v in row.items()}
    for k in keys:
        v = lower.get(k.lower())
        if v is not None and str(v).strip() != "":
            return str(v).strip()
    return None


def seed(csv_path: str, db_path: str) -> int:
    """Import restaurants from ``csv_path`` into the DB at ``db_path``.

    Returns the number of rows imported.
    """
    conn = db.init_db(db_path)
    imported = 0
    with open(csv_path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            name = _get(row, "name")
            if not name:
                continue
            price_raw = _get(row, "price_level")
            lat_raw = _get(row, "lat", "latitude")
            lng_raw = _get(row, "lng", "longitude")
            restaurant = Restaurant(
                name=name,
                cuisine=_get(row, "cuisine"),
                address=_get(row, "address"),
                place_id=_get(row, "place_id"),
                lat=float(lat_raw) if lat_raw else None,
                lng=float(lng_raw) if lng_raw else None,
                maps_url=_get(row, "maps_url", "google_maps_url"),
                price_level=int(price_raw) if price_raw else None,
                source="seed",
            )
            db.upsert_restaurant(conn, restaurant)
            imported += 1
    conn.close()
    return imported


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed the lunch-bot pool from a CSV.")
    parser.add_argument(
        "csv_path",
        nargs="?",
        default=None,
        help="Path to the CSV file to import (defaults to config restaurants_csv).",
    )
    parser.add_argument(
        "--db-path",
        default=None,
        help="SQLite DB path (defaults to config/env DB_PATH).",
    )
    args = parser.parse_args()

    config = load_config()
    csv_path = args.csv_path or config.restaurants_csv
    db_path = args.db_path or config.db_path
    count = seed(csv_path, db_path)
    print(f"Imported {count} restaurants into {db_path}")


if __name__ == "__main__":
    main()

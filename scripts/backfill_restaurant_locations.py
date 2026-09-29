#!/usr/bin/env python3
"""Enrich active restaurant rows with verified Google Places location data."""

from __future__ import annotations

import argparse

from lunch_bot import db
from lunch_bot.config import load_config
from lunch_bot.discovery import google_maps_url, lookup_restaurant
from lunch_bot.models import Restaurant


def backfill(config, *, refresh: bool = False) -> tuple[int, list[str]]:
    """Backfill active candidates, returning ``(updated_count, failed_names)``."""
    config.require("google_maps_api_key")
    conn = db.init_db(config.db_path)
    updated = 0
    failed: list[str] = []

    for restaurant in db.get_active_restaurants(conn):
        complete = bool(
            restaurant.address
            and restaurant.place_id
            and restaurant.lat is not None
            and restaurant.lng is not None
        )
        if complete and not refresh:
            if not restaurant.maps_url:
                restaurant.maps_url = google_maps_url(
                    restaurant.name, restaurant.place_id
                )
                db.update_restaurant_location(conn, restaurant.id, restaurant)
                updated += 1
                print(f"linked:   {restaurant.name}")
            continue

        try:
            match = lookup_restaurant(
                config, restaurant.name, enforce_budget=False
            )
        except Exception as exc:  # network/API failure should not abort the batch
            failed.append(restaurant.name)
            print(f"failed:   {restaurant.name} ({exc})")
            continue
        if match is None:
            failed.append(restaurant.name)
            print(f"not found:{restaurant.name}")
            continue
        db.update_restaurant_location(conn, restaurant.id, match)
        updated += 1
        print(f"enriched: {restaurant.name} -> {match.address}")

    conn.close()
    return updated, failed


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backfill active restaurant addresses, coordinates, and Maps links."
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Re-query rows that already have complete location data.",
    )
    args = parser.parse_args()
    config = load_config()
    updated, failed = backfill(config, refresh=args.refresh)
    print(f"\nUpdated {updated} restaurant(s); {len(failed)} unresolved.")
    if failed:
        print("Unresolved: " + ", ".join(failed))


if __name__ == "__main__":
    main()

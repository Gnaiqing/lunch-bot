"""Restaurant discovery via Google Places Nearby Search.

Given an office location and radius, find nearby restaurants, filter to
economical ones (``price_level`` proxy for budget), tag their cuisine with the
LLM, and upsert them into the pool.

Also handles geocoding an address to lat/lng and validating a free-text
restaurant suggestion via a Places text search.

``googlemaps`` is imported lazily so this module can be imported without the
package installed (only functions that call the API require it).

TODO(user): provide ``GOOGLE_MAPS_API_KEY`` and, optionally, a seed restaurant
list (CSV) to bootstrap the pool before the first Nearby Search runs.
"""

from __future__ import annotations

import math
from typing import Optional

from .config import Config
from .db import set_cuisine, upsert_restaurant
from .models import Restaurant


FOOD_PLACE_TYPES = {
    "bakery",
    "cafe",
    "food",
    "meal_delivery",
    "meal_takeaway",
    "restaurant",
}


def _distance_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Return the great-circle distance between two coordinates in metres."""
    earth_radius_m = 6_371_000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lng2 - lng1)
    a = (
        math.sin(delta_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2) ** 2
    )
    return earth_radius_m * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _client(api_key: str):
    """Construct a googlemaps client (lazy import)."""
    import googlemaps  # optional dependency, imported lazily

    return googlemaps.Client(key=api_key)


def geocode_office(config: Config) -> tuple[float, float]:
    """Return (lat, lng) for the office address, using Google geocoding.

    Falls back to the configured ``office_lat``/``office_lng`` if no API key is
    set or geocoding returns nothing.
    """
    if not config.google_maps_api_key:
        return config.office_lat, config.office_lng
    gmaps = _client(config.google_maps_api_key)
    results = gmaps.geocode(config.office_address)
    if not results:
        return config.office_lat, config.office_lng
    loc = results[0]["geometry"]["location"]
    return loc["lat"], loc["lng"]


def nearby_restaurants(config: Config, *, lat: Optional[float] = None, lng: Optional[float] = None) -> list[Restaurant]:
    """Query Google Places Nearby Search for restaurants around the office.

    Filters to ``price_level <= config.max_price_level`` (the coarse proxy for
    the per-person budget). Restaurants without a ``price_level`` are kept
    (unknown price is not assumed to be expensive).

    Returns un-persisted :class:`Restaurant` objects (cuisine not yet tagged).
    """
    config.require("google_maps_api_key")
    gmaps = _client(config.google_maps_api_key)
    if lat is None or lng is None:
        lat, lng = geocode_office(config)

    found: list[Restaurant] = []
    response = gmaps.places_nearby(
        location=(lat, lng),
        radius=config.search_radius_m,
        type="restaurant",
    )
    for place in response.get("results", []):
        price_level = place.get("price_level")
        if price_level is not None and price_level > config.max_price_level:
            continue  # too expensive for the budget
        geometry = place.get("geometry", {}).get("location", {})
        found.append(
            Restaurant(
                name=place.get("name", "Unknown"),
                cuisine=None,  # tagged later via the LLM
                address=place.get("vicinity"),
                place_id=place.get("place_id"),
                lat=geometry.get("lat"),
                lng=geometry.get("lng"),
                price_level=price_level,
                source="places",
            )
        )
    return found


def validate_suggestion(config: Config, name: str, location_hint: Optional[str] = None) -> Optional[Restaurant]:
    """Validate a suggested restaurant name against Google Places (text search).

    Returns a :class:`Restaurant` (source ``'suggestion'``) if a plausible match
    is found within budget, else ``None``.
    """
    config.require("google_maps_api_key")
    gmaps = _client(config.google_maps_api_key)
    query = name if not location_hint else f"{name} {location_hint}"
    response = gmaps.places(
        query=query,
        location=(config.office_lat, config.office_lng),
        radius=config.search_radius_m,
        type="restaurant",
    )
    results = response.get("results", [])
    if not results:
        return None
    place = results[0]
    place_types = set(place.get("types", []))
    if not place_types.intersection(FOOD_PLACE_TYPES):
        return None
    price_level = place.get("price_level")
    if price_level is not None and price_level > config.max_price_level:
        return None
    geometry = place.get("geometry", {}).get("location", {})
    lat = geometry.get("lat")
    lng = geometry.get("lng")
    if lat is None or lng is None:
        return None
    if _distance_m(config.office_lat, config.office_lng, lat, lng) > config.search_radius_m:
        return None
    return Restaurant(
        name=place.get("name", name),
        cuisine=None,
        address=place.get("formatted_address"),
        place_id=place.get("place_id"),
        lat=lat,
        lng=lng,
        price_level=price_level,
        source="suggestion",
    )


def discover_and_store(conn, config: Config, llm=None) -> int:
    """Run a full discovery pass: Nearby Search -> upsert -> tag cuisine.

    Args:
        conn: An open sqlite3 connection.
        config: Resolved configuration.
        llm: Optional :class:`lunch_bot.llm.LLMClient`. If provided, newly-added
            restaurants without a cuisine are tagged. If ``None``, tagging is
            skipped (cuisine stays ``NULL`` until a later pass).

    Returns:
        The number of restaurants discovered (inserted or updated).
    """
    restaurants = nearby_restaurants(config)
    count = 0
    for r in restaurants:
        rid = upsert_restaurant(conn, r)
        count += 1
        if llm is not None and not r.cuisine:
            try:
                cuisine = llm.classify_cuisine(r.name, r.address)
                set_cuisine(conn, rid, cuisine)
            except Exception:  # pragma: no cover - network/LLM best-effort
                # Tagging is best-effort; leave cuisine NULL for a later pass.
                pass
    return count

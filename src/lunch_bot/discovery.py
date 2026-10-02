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
import re
import time
from dataclasses import dataclass
from typing import Iterable, Optional
from urllib.parse import urlencode

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
EXPLORATION_RADIUS_M = 5_000
EXPLORATION_MAX_PRICE_LEVEL = 2
EXPLORATION_MIN_RATING = 3.0


@dataclass(frozen=True)
class NearbyRecommendation:
    """A read-only nearby discovery result with its selection evidence."""

    restaurant: Restaurant
    rating: float
    rating_count: int
    distance_m: float


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


def google_maps_url(name: str, place_id: Optional[str] = None) -> str:
    """Build a stable Google Maps search URL, preferring an exact Place ID."""
    params = {"api": 1, "query": name}
    if place_id:
        params["query_place_id"] = place_id
    return "https://www.google.com/maps/search/?" + urlencode(params)


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
        if not set(place.get("types", [])).intersection(FOOD_PLACE_TYPES):
            continue
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
                maps_url=google_maps_url(
                    place.get("name", "Unknown"), place.get("place_id")
                ),
                price_level=price_level,
                source="places",
            )
        )
    return found


def _normalized_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()


def _nearby_search_pages(gmaps, lat: float, lng: float):
    """Yield up to three legacy Nearby Search pages.

    Google can take a moment to activate a next-page token. Retry that specific
    transient response briefly; later-page failures leave already collected
    results usable instead of failing the whole exploration request.
    """
    response = gmaps.places_nearby(
        location=(lat, lng),
        radius=EXPLORATION_RADIUS_M,
        type="restaurant",
        min_price=0,
        max_price=EXPLORATION_MAX_PRICE_LEVEL,
    )
    for _page_number in range(3):
        yield response
        page_token = response.get("next_page_token")
        if not page_token:
            return
        response = None
        for attempt in range(3):
            if attempt:
                time.sleep(2)
            try:
                candidate = gmaps.places_nearby(page_token=page_token)
            except Exception:  # pragma: no cover - provider/network best-effort
                candidate = None
            if candidate and candidate.get("status") != "INVALID_REQUEST":
                response = candidate
                break
        if response is None:
            return


def explore_nearby_restaurants(
    config: Config,
    existing_restaurants: Iterable[Restaurant],
    *,
    limit: int = 10,
) -> list[NearbyRecommendation]:
    """Return highly rated, affordable nearby restaurants absent from the DB.

    This is deliberately read-only. Results must be strictly within 5 km, have
    a known Google price level no higher than 2, and have a rating above 3.0.
    Existing rows are excluded by Place ID and normalized name, including
    inactive rows, so a previously removed place is not silently resurfaced.
    """
    config.require("google_maps_api_key")
    limit = max(5, min(limit, 10))
    lat, lng = geocode_office(config)
    existing = list(existing_restaurants)
    existing_place_ids = {
        restaurant.place_id for restaurant in existing if restaurant.place_id
    }
    existing_names = {_normalized_name(restaurant.name) for restaurant in existing}
    recommendations: dict[str, NearbyRecommendation] = {}

    for response in _nearby_search_pages(_client(config.google_maps_api_key), lat, lng):
        for place in response.get("results", []):
            place_types = set(place.get("types", []))
            if "restaurant" not in place_types or "lodging" in place_types:
                continue
            if place.get("business_status") in {"CLOSED_TEMPORARILY", "CLOSED_PERMANENTLY"}:
                continue
            place_id = place.get("place_id")
            name = place.get("name", "Unknown")
            if place_id in existing_place_ids or _normalized_name(name) in existing_names:
                continue
            price_level = place.get("price_level")
            if price_level is None or price_level > EXPLORATION_MAX_PRICE_LEVEL:
                continue
            rating = place.get("rating")
            if rating is None or float(rating) <= EXPLORATION_MIN_RATING:
                continue
            geometry = place.get("geometry", {}).get("location", {})
            place_lat = geometry.get("lat")
            place_lng = geometry.get("lng")
            if place_lat is None or place_lng is None:
                continue
            distance_m = _distance_m(lat, lng, place_lat, place_lng)
            if distance_m > EXPLORATION_RADIUS_M:
                continue
            key = place_id or _normalized_name(name)
            recommendations[key] = NearbyRecommendation(
                restaurant=Restaurant(
                    name=name,
                    address=place.get("vicinity") or place.get("formatted_address"),
                    place_id=place_id,
                    lat=place_lat,
                    lng=place_lng,
                    maps_url=google_maps_url(name, place_id),
                    price_level=price_level,
                    source="places",
                ),
                rating=float(rating),
                rating_count=int(place.get("user_ratings_total") or 0),
                distance_m=distance_m,
            )

    ranked = sorted(
        recommendations.values(),
        key=lambda result: (-result.rating, -result.rating_count, result.distance_m),
    )
    return ranked[:limit]


def lookup_restaurant(
    config: Config,
    name: str,
    location_hint: Optional[str] = None,
    *,
    enforce_budget: bool = True,
) -> Optional[Restaurant]:
    """Find a nearby food place by name without persisting it.

    Results must be food-related and within the configured office radius.
    ``enforce_budget=False`` is used when enriching already-approved candidates.
    """
    config.require("google_maps_api_key")
    office_lat, office_lng = geocode_office(config)
    gmaps = _client(config.google_maps_api_key)
    query = name if not location_hint else f"{name} {location_hint}"
    response = gmaps.places(
        query=query,
        location=(office_lat, office_lng),
        radius=config.search_radius_m,
        type="restaurant",
    )
    results = response.get("results", [])
    for place in results:
        place_types = set(place.get("types", []))
        if not place_types.intersection(FOOD_PLACE_TYPES):
            continue
        price_level = place.get("price_level")
        if (
            enforce_budget
            and price_level is not None
            and price_level > config.max_price_level
        ):
            continue
        geometry = place.get("geometry", {}).get("location", {})
        lat = geometry.get("lat")
        lng = geometry.get("lng")
        if lat is None or lng is None:
            continue
        if (
            _distance_m(office_lat, office_lng, lat, lng)
            > config.search_radius_m
        ):
            continue
        return Restaurant(
            name=place.get("name", name),
            cuisine=None,
            address=place.get("formatted_address"),
            place_id=place.get("place_id"),
            lat=lat,
            lng=lng,
            maps_url=google_maps_url(place.get("name", name), place.get("place_id")),
            price_level=price_level,
            source="suggestion",
        )
    return None


def validate_suggestion(
    config: Config, name: str, location_hint: Optional[str] = None
) -> Optional[Restaurant]:
    """Validate a new suggestion against proximity, place type, and budget."""
    return lookup_restaurant(
        config, name, location_hint=location_hint, enforce_budget=True
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

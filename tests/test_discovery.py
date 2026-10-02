"""Network-free validation tests for suggested Google Places results."""

from lunch_bot.config import load_config
from lunch_bot.discovery import explore_nearby_restaurants, validate_suggestion
from lunch_bot.models import Restaurant


class _PlacesClient:
    def __init__(self, results, *, geocode_results=None):
        self.results = results
        self.geocode_results = geocode_results or []
        self.kwargs = None

    def geocode(self, _address):
        return self.geocode_results

    def places(self, **kwargs):
        self.kwargs = kwargs
        return {"results": self.results}

    def places_nearby(self, **kwargs):
        self.kwargs = kwargs
        return {"results": self.results}


def _config():
    return load_config(
        env={
            "GOOGLE_MAPS_API_KEY": "test-key",
            "OFFICE_LAT": "43.6579",
            "OFFICE_LNG": "-79.3883",
            "SEARCH_RADIUS_M": "5000",
        },
        load_dotenv=False,
        config_path="__none__.yaml",
    )


def _place(*, types, lat=43.66, lng=-79.39):
    return {
        "name": "Test Place",
        "types": types,
        "place_id": "place-1",
        "price_level": 1,
        "formatted_address": "Toronto",
        "geometry": {"location": {"lat": lat, "lng": lng}},
    }


def test_suggestion_search_is_location_biased_and_restaurant_typed(monkeypatch):
    client = _PlacesClient([_place(types=["restaurant", "food"])])
    monkeypatch.setattr("lunch_bot.discovery._client", lambda _key: client)

    restaurant = validate_suggestion(_config(), "Test Place")

    assert restaurant is not None
    assert client.kwargs["location"] == (43.6579, -79.3883)
    assert client.kwargs["radius"] == 5000
    assert client.kwargs["type"] == "restaurant"
    assert restaurant.maps_url.endswith("query_place_id=place-1")


def test_non_food_place_is_rejected(monkeypatch):
    client = _PlacesClient([_place(types=["laundry", "point_of_interest"])])
    monkeypatch.setattr("lunch_bot.discovery._client", lambda _key: client)
    assert validate_suggestion(_config(), "Laundry") is None


def test_food_place_outside_radius_is_rejected(monkeypatch):
    client = _PlacesClient([_place(types=["restaurant"], lat=44.0, lng=-79.39)])
    monkeypatch.setattr("lunch_bot.discovery._client", lambda _key: client)
    assert validate_suggestion(_config(), "Far Away Restaurant") is None


def test_later_valid_place_is_used_when_first_result_is_invalid(monkeypatch):
    invalid = _place(types=["lodging", "point_of_interest"])
    valid = _place(types=["restaurant", "food"])
    valid.update(name="Actual Restaurant", place_id="restaurant-2")
    client = _PlacesClient([invalid, valid])
    monkeypatch.setattr("lunch_bot.discovery._client", lambda _key: client)

    restaurant = validate_suggestion(_config(), "Ambiguous Name")

    assert restaurant is not None
    assert restaurant.name == "Actual Restaurant"
    assert restaurant.place_id == "restaurant-2"


def test_lookup_uses_geocoded_office_coordinates(monkeypatch):
    office = {"geometry": {"location": {"lat": 44.0, "lng": -79.0}}}
    client = _PlacesClient(
        [_place(types=["restaurant"], lat=44.001, lng=-79.001)],
        geocode_results=[office],
    )
    monkeypatch.setattr("lunch_bot.discovery._client", lambda _key: client)

    restaurant = validate_suggestion(_config(), "Near Geocoded Office")

    assert restaurant is not None
    assert client.kwargs["location"] == (44.0, -79.0)


def test_explore_nearby_filters_existing_and_criteria_then_ranks(monkeypatch):
    existing = _place(types=["restaurant"], lat=43.658, lng=-79.388)
    existing.update(name="Already Listed", place_id="existing", rating=4.9)
    good = _place(types=["restaurant", "food"], lat=43.66, lng=-79.39)
    good.update(
        name="Great New Place",
        place_id="great",
        rating=4.7,
        user_ratings_total=250,
        price_level=2,
        vicinity="10 New St",
    )
    lower_rated = _place(types=["restaurant"], lat=43.659, lng=-79.389)
    lower_rated.update(
        name="Good New Place",
        place_id="good",
        rating=4.1,
        user_ratings_total=500,
        price_level=1,
    )
    invalid_results = [
        {**_place(types=["restaurant"]), "name": "No Rating", "rating": None},
        {**_place(types=["restaurant"]), "name": "Too Expensive", "rating": 4.8, "price_level": 3},
        {**_place(types=["restaurant"], lat=44.0), "name": "Too Far", "rating": 4.8},
        {**_place(types=["lodging", "restaurant"]), "name": "Hotel", "rating": 4.8},
        {**_place(types=["restaurant"]), "name": "Low Rating", "rating": 3.0},
    ]
    client = _PlacesClient([existing, lower_rated, *invalid_results, good])
    monkeypatch.setattr("lunch_bot.discovery._client", lambda _key: client)
    monkeypatch.setattr(
        "lunch_bot.discovery.geocode_office", lambda _config: (43.6579, -79.3883)
    )

    results = explore_nearby_restaurants(
        _config(),
        [Restaurant(name="Already Listed", place_id="existing", active=False)],
        limit=10,
    )

    assert [result.restaurant.name for result in results] == [
        "Great New Place",
        "Good New Place",
    ]
    assert results[0].restaurant.address == "10 New St"
    assert results[0].rating == 4.7
    assert client.kwargs["radius"] == 5000
    assert client.kwargs["max_price"] == 2

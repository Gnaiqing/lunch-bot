"""Network-free validation tests for suggested Google Places results."""

from lunch_bot.config import load_config
from lunch_bot.discovery import validate_suggestion


class _PlacesClient:
    def __init__(self, results):
        self.results = results
        self.kwargs = None

    def places(self, **kwargs):
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


def test_non_food_place_is_rejected(monkeypatch):
    client = _PlacesClient([_place(types=["laundry", "point_of_interest"])])
    monkeypatch.setattr("lunch_bot.discovery._client", lambda _key: client)
    assert validate_suggestion(_config(), "Laundry") is None


def test_food_place_outside_radius_is_rejected(monkeypatch):
    client = _PlacesClient([_place(types=["restaurant"], lat=44.0, lng=-79.39)])
    monkeypatch.setattr("lunch_bot.discovery._client", lambda _key: client)
    assert validate_suggestion(_config(), "Far Away Restaurant") is None

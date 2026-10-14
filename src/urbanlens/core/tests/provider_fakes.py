"""Stand-in providers for the chains in ``plugins.points.providers``, so a core test never depends on which plugins are installed."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

from urbanlens.dashboard.services.apis.locations.places_resolution import PlacesProvider
from urbanlens.dashboard.services.apis.locations.routing_resolution import RouteProvider
from urbanlens.dashboard.services.apis.locations.weather_resolution import WeatherProvider

if TYPE_CHECKING:
    from datetime import date
    from unittest import TestCase

    from urbanlens.dashboard.plugins.extensions import Seam
    from urbanlens.dashboard.services.apis.weather.forecast import ForecastSlot, SunTimes


def hold_providers[T](test: TestCase, seam: Seam[T], providers: list[T]) -> None:
    """Make ``seam`` hold exactly ``providers`` until ``test`` finishes."""
    context = seam.overridden(providers)
    context.__enter__()
    test.addCleanup(context.__exit__, None, None, None)


class FakePlacesProvider(PlacesProvider):
    """
    A places provider that answers nothing (empty lists, no coordinates), with the flags a test sets.

    Attributes:
        is_available: What :meth:`available` reports.
        budget_keys: What :meth:`service_keys` reports.
    """

    key: ClassVar[str] = "fake_places"

    def __init__(
        self,
        *,
        shields_direct: bool = False,
        is_available: bool = True,
        budget_keys: tuple[str, ...] = ("fake_places",),
    ) -> None:
        self.shields_direct = shields_direct
        self.is_available = is_available
        self.budget_keys = budget_keys

    def available(self) -> bool:
        return self.is_available

    def service_keys(self) -> tuple[str, ...]:
        return self.budget_keys

    def search_nearby_landmarks(
        self, latitude: float, longitude: float, radius: float, included_types: list[str]
    ) -> list[dict[str, Any]]:
        return []

    def get_place_details_full(self, place_id: str) -> dict[str, Any]:
        return {}

    def find_nearest_place_photos(self, latitude: float, longitude: float) -> tuple[str | None, list[str]]:
        return None, []

    def owns_photo(self, photo_identifier: str) -> bool:
        return False

    def download_photo(self, photo_identifier: str) -> tuple[bytes, str]:
        raise NotImplementedError

    def autocomplete_predictions(self, query: str) -> list[dict[str, str]]:
        return []

    def resolve_place_coordinates(self, place_id: str) -> tuple[float | None, float | None, str | None]:
        return None, None, None

    def resolve_name_from_nearby(self, latitude: float, longitude: float, radius: float) -> str | None:
        return None


class FakeWeatherProvider(WeatherProvider):
    """
    A weather provider that records history (when ``history``) and answers nothing else.

    Attributes:
        history: Whether it reports recorded weather.
        days: What :meth:`historical_days` answers.
    """

    key: ClassVar[str] = "fake_weather"

    def __init__(self, *, history: bool = True, days: dict[str, dict[str, Any]] | None = None) -> None:
        self.supports_history = history
        self.days = days or {}

    def available(self) -> bool:
        return True

    def forecast_slots(self, latitude: float, longitude: float, *, raw: bool = False) -> list[ForecastSlot] | None:
        return None

    def sun_times(self, latitude: float, longitude: float) -> SunTimes | None:
        return None

    def historical_days(self, latitude: float, longitude: float, start: date, end: date) -> dict[str, dict[str, Any]]:
        return self.days


class ForbiddenWeatherProvider(FakeWeatherProvider):
    """A weather provider that fails the test the moment it is asked anything, even whether it is available."""

    key: ClassVar[str] = "forbidden_weather"

    def available(self) -> bool:
        raise AssertionError("the weather provider chain must not be consulted")


class ForbiddenRouteProvider(RouteProvider):
    """A route provider that fails the test the moment it is asked anything, even whether it is available."""

    key: ClassVar[str] = "forbidden_route"

    def available(self) -> bool:
        raise AssertionError("the route provider chain must not be consulted")

    def get_route(self, origin: tuple[float, float], destination: tuple[float, float]) -> dict[str, Any] | None:
        raise AssertionError("the route provider chain must not be consulted")

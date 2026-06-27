"""Tests for location creation and place-name resolver services."""

from __future__ import annotations

from dataclasses import dataclass
from unittest import mock

from hypothesis import given, settings as hyp_settings, strategies as st

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.services.locations.creation import _default_bbox
from urbanlens.dashboard.services.locations.naming import PlaceNameResolverChain


@dataclass(slots=True)
class _Resolver:
    value: str | None

    def resolve(self, latitude: float, longitude: float) -> str | None:
        return self.value


class DefaultBoundingBoxTests(TestCase):
    """Default generated bounding boxes contain the source coordinate."""

    @given(
        latitude=st.floats(min_value=-89.9, max_value=89.9, allow_nan=False, allow_infinity=False),
        longitude=st.floats(min_value=-179.9, max_value=179.9, allow_nan=False, allow_infinity=False),
    )
    @hyp_settings(max_examples=30)
    def test_default_bbox_surrounds_coordinate(self, latitude: float, longitude: float) -> None:
        bbox = _default_bbox(latitude, longitude)
        min_x, min_y, max_x, max_y = bbox.extent
        self.assertLess(min_x, longitude)
        self.assertGreater(max_x, longitude)
        self.assertLess(min_y, latitude)
        self.assertGreater(max_y, latitude)


class PlaceNameResolverChainTests(TestCase):
    """Resolver chains skip empty/sentinel names and stop at the first useful name."""

    @given(name=st.text(min_size=1, max_size=80).filter(lambda value: value != "No Information Available"))
    @hyp_settings(max_examples=25)
    def test_returns_first_meaningful_name(self, name: str) -> None:
        chain = PlaceNameResolverChain(resolvers=(_Resolver(None), _Resolver("No Information Available"), _Resolver(name)))
        self.assertEqual(chain.resolve(40.0, -74.0), name)

    def test_returns_none_when_no_resolver_finds_name(self) -> None:
        chain = PlaceNameResolverChain(resolvers=(_Resolver(None), _Resolver("No Information Available")))
        self.assertIsNone(chain.resolve(40.0, -74.0))

    def test_google_places_resolver_handles_gateway_errors(self) -> None:
        from urbanlens.dashboard.services.locations.naming import GooglePlacesNameResolver

        with (
            mock.patch("urbanlens.dashboard.services.locations.naming.settings.google_places_api_key", "key"),
            mock.patch("urbanlens.dashboard.services.locations.naming.GooglePlacesGateway") as gateway,
        ):
            gateway.return_value.get_data.side_effect = ValueError("bad response")
            self.assertIsNone(GooglePlacesNameResolver().resolve(40.0, -74.0))

"""Tests for OvertureBuildingAttributesPanelSource.fetch()'s latency budget.

get_building_attributes() and get_nearby_places() are each independent S3
GeoParquet range reads; observed in production taking ~50s+ each back-to-back
(105.8s total for one fetch() call), on the same small prefork queue as
BoundaryPanelSource. get_nearby_places() is skipped once get_building_attributes()
has already eaten most of a reasonable total budget, rather than always paying
for both regardless of how slow the first one was.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest import mock

from django.contrib.auth.models import User
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.plugins.builtin.overture_building_attributes import (
    _NEARBY_PLACES_BUDGET_SECONDS,
    OvertureBuildingAttributesPanelSource,
)

if TYPE_CHECKING:
    from urbanlens.dashboard.models.pin.model import Pin


class OvertureBuildingAttributesBudgetTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.source = OvertureBuildingAttributesPanelSource()
        self.pin: Pin = baker.make_recipe(
            "dashboard.pin",
            profile=baker.make(User).profile,
            location=baker.make("dashboard.Location", latitude=40.0, longitude=-74.0),
        )

    def _gateway(self, *, attributes=None, places=None):
        gateway = mock.Mock()
        gateway.get_building_attributes.return_value = attributes
        gateway.get_nearby_places.return_value = places or []
        return gateway

    def test_fast_building_attributes_still_fetches_nearby_places(self) -> None:
        gateway = self._gateway(attributes={"subtype": "commercial"}, places=[{"name": "Cafe"}])
        with (
            mock.patch(
                "urbanlens.dashboard.services.apis.locations.boundaries.overture_maps.OvertureMapsGateway",
                return_value=gateway,
            ),
            mock.patch(
                "urbanlens.dashboard.plugins.builtin.overture_building_attributes.time.monotonic",
                side_effect=[0.0, 1.0],
            ),
        ):
            self.source.fetch(self.pin)
        gateway.get_nearby_places.assert_called_once()

    def test_slow_building_attributes_skips_nearby_places(self) -> None:
        gateway = self._gateway(attributes={"subtype": "commercial"})
        with (
            mock.patch(
                "urbanlens.dashboard.services.apis.locations.boundaries.overture_maps.OvertureMapsGateway",
                return_value=gateway,
            ),
            mock.patch(
                "urbanlens.dashboard.plugins.builtin.overture_building_attributes.time.monotonic",
                side_effect=[0.0, _NEARBY_PLACES_BUDGET_SECONDS + 1],
            ),
        ):
            self.source.fetch(self.pin)
        gateway.get_nearby_places.assert_not_called()

    def test_skipping_nearby_places_still_caches_the_building_attributes(self) -> None:
        from urbanlens.dashboard.models.cache.location_cache import LocationCache

        gateway = self._gateway(attributes={"subtype": "commercial", "height_m": 12.0})
        with (
            mock.patch(
                "urbanlens.dashboard.services.apis.locations.boundaries.overture_maps.OvertureMapsGateway",
                return_value=gateway,
            ),
            mock.patch(
                "urbanlens.dashboard.plugins.builtin.overture_building_attributes.time.monotonic",
                side_effect=[0.0, _NEARBY_PLACES_BUDGET_SECONDS + 1],
            ),
        ):
            self.source.fetch(self.pin)

        cached = LocationCache.get_fresh(self.pin.location, "overture_building_attributes")
        assert cached is not None
        self.assertEqual(cached.data["subtype"], "commercial")
        self.assertEqual(cached.data["nearby_places"], [])


class OvertureBuildingAttributesScopeTests(TestCase):
    """Building characteristics describe one structure - not a whole parcel."""

    def setUp(self) -> None:
        super().setUp()
        self.source = OvertureBuildingAttributesPanelSource()
        self.pin: Pin = baker.make_recipe(
            "dashboard.pin",
            profile=baker.make(User).profile,
            location=baker.make("dashboard.Location", latitude=40.1, longitude=-74.1),
        )
        self.data = {"subtype": "commercial", "height_m": 12.0, "num_floors": 3}

    def test_a_building_scope_pin_renders_its_characteristics(self) -> None:
        ctx = self.source.render_context(self.pin, self.data)
        assert ctx is not None
        self.assertEqual(ctx["chips"], ["Commercial"])

    def test_a_parcel_scope_pin_renders_nothing(self) -> None:
        from urbanlens.dashboard.models.pin.model import PinType

        self.pin.pin_type = PinType.PARCEL
        self.pin.pin_type_is_user_provided = True
        self.assertIsNone(self.source.render_context(self.pin, self.data))

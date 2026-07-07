"""Tests for services.memories.distance - combined travel-distance stat."""
from __future__ import annotations

import datetime
from decimal import Decimal

from django.contrib.gis.geos import LineString
from django.utils import timezone
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.profile.meta import DistanceUnit
from urbanlens.dashboard.models.profile.model import _haversine_km
from urbanlens.dashboard.models.visits.model import PinVisit, VisitSource
from urbanlens.dashboard.services.memories.distance import (
    inter_visit_distance_km,
    recorded_route_distance_km,
    total_travel_distance_km,
)

# Roughly New York, California, Oregon.
_NY = (40.71, -74.01)
_CA = (34.05, -118.24)
_OR = (44.05, -123.09)


class InterVisitDistanceTests(TestCase):
    """inter_visit_distance_km sums great-circle legs between consecutive visits."""

    def setUp(self) -> None:
        super().setUp()
        self.profile = baker.make("auth.User").profile

    def _visit_at(self, coord: tuple[float, float], when: datetime.datetime) -> PinVisit:
        pin = baker.make(
            "dashboard.Pin",
            profile=self.profile,
            location=None,
            latitude=Decimal(str(coord[0])),
            longitude=Decimal(str(coord[1])),
        )
        return PinVisit.objects.create(pin=pin, visited_at=when, source=VisitSource.MANUAL)

    def test_no_visits_is_zero(self) -> None:
        self.assertEqual(inter_visit_distance_km(self.profile), 0.0)

    def test_single_visit_is_zero(self) -> None:
        self._visit_at(_NY, timezone.make_aware(datetime.datetime(2024, 1, 1)))
        self.assertEqual(inter_visit_distance_km(self.profile), 0.0)

    def test_three_visits_sum_consecutive_legs(self) -> None:
        self._visit_at(_NY, timezone.make_aware(datetime.datetime(2024, 1, 1)))
        self._visit_at(_CA, timezone.make_aware(datetime.datetime(2024, 2, 1)))
        self._visit_at(_OR, timezone.make_aware(datetime.datetime(2024, 3, 1)))

        expected = _haversine_km(_NY, _CA) + _haversine_km(_CA, _OR)
        self.assertAlmostEqual(inter_visit_distance_km(self.profile), expected, places=3)

    def test_ordering_is_by_time_not_insertion(self) -> None:
        # Insert out of chronological order; legs must follow visited_at order.
        self._visit_at(_OR, timezone.make_aware(datetime.datetime(2024, 3, 1)))
        self._visit_at(_NY, timezone.make_aware(datetime.datetime(2024, 1, 1)))
        self._visit_at(_CA, timezone.make_aware(datetime.datetime(2024, 2, 1)))

        expected = _haversine_km(_NY, _CA) + _haversine_km(_CA, _OR)
        self.assertAlmostEqual(inter_visit_distance_km(self.profile), expected, places=3)

    def test_visit_without_coordinates_is_bridged(self) -> None:
        # A coordinate-less visit in the middle should not break the chain.
        self._visit_at(_NY, timezone.make_aware(datetime.datetime(2024, 1, 1)))
        pin = baker.make("dashboard.Pin", profile=self.profile, location=None, latitude=None, longitude=None)
        PinVisit.objects.create(pin=pin, visited_at=timezone.make_aware(datetime.datetime(2024, 2, 1)), source=VisitSource.MANUAL)
        self._visit_at(_OR, timezone.make_aware(datetime.datetime(2024, 3, 1)))

        self.assertAlmostEqual(inter_visit_distance_km(self.profile), _haversine_km(_NY, _OR), places=3)


class RecordedRouteDistanceTests(TestCase):
    """recorded_route_distance_km sums Route.distance_meters as kilometres."""

    def setUp(self) -> None:
        super().setUp()
        self.profile = baker.make("auth.User").profile

    def _route(self, distance_meters: float) -> None:
        baker.make(
            "dashboard.Route",
            profile=self.profile,
            path=LineString((-74.0, 40.0), (-73.9, 40.1), srid=4326),
            distance_meters=distance_meters,
            started_at=timezone.now(),
        )

    def test_no_routes_is_zero(self) -> None:
        self.assertEqual(recorded_route_distance_km(self.profile), 0.0)

    def test_sums_route_lengths_in_km(self) -> None:
        self._route(1500.0)
        self._route(2500.0)
        self.assertAlmostEqual(recorded_route_distance_km(self.profile), 4.0, places=6)


class TotalTravelDistanceTests(TestCase):
    """total_travel_distance_km adds routes to inter-visit travel."""

    def setUp(self) -> None:
        super().setUp()
        self.profile = baker.make("auth.User").profile

    def test_combines_routes_and_visits(self) -> None:
        baker.make(
            "dashboard.Route",
            profile=self.profile,
            path=LineString((-74.0, 40.0), (-73.9, 40.1), srid=4326),
            distance_meters=10_000.0,
            started_at=timezone.now(),
        )
        for coord, day in ((_NY, 1), (_CA, 2)):
            pin = baker.make("dashboard.Pin", profile=self.profile, location=None, latitude=Decimal(str(coord[0])), longitude=Decimal(str(coord[1])))
            PinVisit.objects.create(pin=pin, visited_at=timezone.make_aware(datetime.datetime(2024, 1, day)), source=VisitSource.MANUAL)

        expected = 10.0 + _haversine_km(_NY, _CA)
        self.assertAlmostEqual(total_travel_distance_km(self.profile), expected, places=3)


class EffectiveDistanceUnitsTests(TestCase):
    """Profile.effective_distance_units honors explicit choice then region inference."""

    def setUp(self) -> None:
        super().setUp()
        self.profile = baker.make("auth.User").profile

    def test_explicit_choice_wins(self) -> None:
        self.profile.distance_units = DistanceUnit.MILES
        self.assertEqual(self.profile.effective_distance_units, DistanceUnit.MILES)

    def test_infers_miles_from_us_center(self) -> None:
        self.profile.distance_units = None
        self.profile.map_center_latitude = Decimal("40.71")
        self.profile.map_center_longitude = Decimal("-74.01")
        self.assertEqual(self.profile.effective_distance_units, DistanceUnit.MILES)

    def test_infers_km_from_non_imperial_center(self) -> None:
        self.profile.distance_units = None
        self.profile.map_center_latitude = Decimal("48.85")
        self.profile.map_center_longitude = Decimal("2.35")
        self.assertEqual(self.profile.effective_distance_units, DistanceUnit.KILOMETERS)

    def test_defaults_to_km_without_location(self) -> None:
        self.profile.distance_units = None
        self.profile.map_center_latitude = None
        self.profile.map_center_longitude = None
        self.profile.map_custom_latitude = None
        self.profile.map_custom_longitude = None
        self.profile.remembered_map_lat = None
        self.profile.remembered_map_lng = None
        self.assertEqual(self.profile.effective_distance_units, DistanceUnit.KILOMETERS)

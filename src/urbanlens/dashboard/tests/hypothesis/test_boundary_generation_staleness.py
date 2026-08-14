"""Tests for the place-resolution TTL.

``boundary_generation_stale``, the ``schedule_location_boundary_generation``
gate, and ``generate_location_boundaries``' refresh-overwrite behaviour.

Staleness now keys off ``Location.place_resolved_at`` (stamped even when the
providers found nothing, so an unknown coordinate is asked about once rather
than on every page view) and, when a place was found,
``Place.geometry_generated_at``.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

from django.contrib.gis.geos import MultiPolygon, Polygon
from django.utils import timezone
from hypothesis import given, settings as hyp_settings, strategies as st
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.place.model import Place, PlaceKind
from urbanlens.dashboard.models.site_settings.model import SiteSettings
from urbanlens.dashboard.services.locations.boundaries import (
    ResolvedBoundaries,
    boundary_generation_ran,
    boundary_generation_stale,
    generate_location_boundaries,
    schedule_location_boundary_generation,
)

_hyp = hyp_settings(max_examples=30, deadline=None)


def _resolved(location: Location, *, age_days: float, polygon=None) -> Location:
    """Point a Location at a place resolved ``age_days`` ago.

    ``polygon=None`` models the real "the providers were asked and had nothing"
    case: the location is stamped as resolved but sits on no known place.
    """
    stamped = timezone.now() - timedelta(days=age_days)
    place = None
    if polygon is not None:
        place = Place.objects.create(kind=PlaceKind.PARCEL, geometry=polygon, geometry_generated_at=stamped)
        Place.objects.filter(pk=place.pk).update(domain_root=place.pk)
    Location.objects.filter(pk=location.pk).update(place=place, place_resolved_at=stamped)
    location.refresh_from_db()
    return location


def _square(lon: float, lat: float, size: float = 0.001) -> MultiPolygon:
    """A small square MultiPolygon with its lower-left corner at (lon, lat)."""
    ring = ((lon, lat), (lon + size, lat), (lon + size, lat + size), (lon, lat + size), (lon, lat))
    return MultiPolygon(Polygon(ring, srid=4326), srid=4326)


class BoundaryGenerationStaleTests(TestCase):
    """boundary_generation_stale() uses SiteSettings.boundary_cache_days as its threshold."""

    def _make_location_with_row(self, *, age_days: float | None, polygon=None) -> Location:
        location = baker.make(Location, latitude=42.65, longitude=-73.75)
        if age_days is None:
            return location
        return _resolved(location, age_days=age_days, polygon=polygon)

    def test_never_generated_is_not_stale(self):
        location = self._make_location_with_row(age_days=None)
        self.assertFalse(boundary_generation_stale(location))

    def test_fresh_generation_is_not_stale(self):
        location = self._make_location_with_row(age_days=0.1)
        self.assertFalse(boundary_generation_stale(location))

    def test_generation_older_than_default_window_is_stale(self):
        location = self._make_location_with_row(age_days=61)
        self.assertTrue(boundary_generation_stale(location))

    def test_admin_can_extend_the_cache_window(self):
        site_settings = SiteSettings.get_current()
        site_settings.boundary_cache_days = 90
        site_settings.save()

        location = self._make_location_with_row(age_days=61)

        self.assertFalse(boundary_generation_stale(location))

    @given(configured_days=st.integers(min_value=1, max_value=365), age_days=st.floats(min_value=0, max_value=400, allow_nan=False))
    @_hyp
    def test_staleness_matches_configured_threshold(self, configured_days: int, age_days: float):
        site_settings = SiteSettings.get_current()
        site_settings.boundary_cache_days = configured_days
        site_settings.save()

        location = self._make_location_with_row(age_days=age_days)

        self.assertEqual(boundary_generation_stale(location), age_days > configured_days)


class ScheduleLocationBoundaryGenerationGateTests(TestCase):
    """schedule_location_boundary_generation enqueues for never-run and stale, skips fresh."""

    def _make_stale_location(self, *, age_days: float) -> Location:
        location = baker.make(Location, latitude=42.65, longitude=-73.75)
        return _resolved(location, age_days=age_days, polygon=_square(-73.75, 42.65))

    def test_never_run_schedules_generation(self):
        location = baker.make(Location, latitude=42.65, longitude=-73.75)
        with patch("urbanlens.dashboard.services.core.celery.safely_enqueue_task") as enqueue:
            result = schedule_location_boundary_generation(location)
        self.assertTrue(result)
        enqueue.assert_called_once()

    def test_fresh_generation_is_not_rescheduled(self):
        location = self._make_stale_location(age_days=1)
        with patch("urbanlens.dashboard.services.core.celery.safely_enqueue_task") as enqueue:
            result = schedule_location_boundary_generation(location)
        self.assertFalse(result)
        enqueue.assert_not_called()

    def test_stale_generation_is_rescheduled(self):
        location = self._make_stale_location(age_days=61)
        with patch("urbanlens.dashboard.services.core.celery.safely_enqueue_task") as enqueue:
            result = schedule_location_boundary_generation(location)
        self.assertTrue(result)
        enqueue.assert_called_once()

    def test_missing_coordinates_never_schedules(self):
        location = baker.make(Location, latitude=42.65, longitude=-73.75)
        location.latitude = None
        location.longitude = None
        with patch("urbanlens.dashboard.services.core.celery.safely_enqueue_task") as enqueue:
            result = schedule_location_boundary_generation(location)
        self.assertFalse(result)
        enqueue.assert_not_called()


class GenerateLocationBoundariesRefreshTests(TestCase):
    """A refresh run overwrites generated_polygon with new geometry, but never with nothing."""

    def test_refresh_fills_in_geometry_for_a_place_that_had_none(self):
        location = baker.make(Location, latitude=42.65, longitude=-73.75)
        _resolved(location, age_days=90, polygon=None)

        new_polygon = _square(-73.75, 42.65)
        resolved = ResolvedBoundaries(property_polygon=new_polygon, building_polygon=None)
        with patch("urbanlens.dashboard.services.locations.boundaries.BoundaryProviderChain.get_boundaries", return_value=resolved):
            place = generate_location_boundaries(location)

        assert place is not None and place.geometry is not None
        self.assertEqual(place.geometry.wkb, new_polygon.wkb)
        location.refresh_from_db()
        self.assertFalse(boundary_generation_stale(location))

    def test_refresh_replaces_older_geometry_with_newer_geometry(self):
        location = baker.make(Location, latitude=42.65, longitude=-73.75)
        old_polygon = _square(-73.75, 42.65)
        place = _resolved(location, age_days=90, polygon=old_polygon).place

        new_polygon = _square(-73.7502, 42.6502)
        resolved = ResolvedBoundaries(property_polygon=new_polygon, building_polygon=None)
        with patch("urbanlens.dashboard.services.locations.boundaries.BoundaryProviderChain.get_boundaries", return_value=resolved):
            generate_location_boundaries(location)

        place.refresh_from_db()
        assert place.geometry is not None
        self.assertEqual(place.geometry.wkb, new_polygon.wkb)

    def test_a_fruitless_refresh_leaves_existing_geometry_alone(self):
        """A transient provider hiccup on refresh must not erase previously-good geometry."""
        location = baker.make(Location, latitude=42.65, longitude=-73.75)
        old_polygon = _square(-73.75, 42.65)
        place = _resolved(location, age_days=90, polygon=old_polygon).place

        resolved = ResolvedBoundaries(property_polygon=None, building_polygon=None)
        with patch("urbanlens.dashboard.services.locations.boundaries.BoundaryProviderChain.get_boundaries", return_value=resolved):
            generate_location_boundaries(location)

        place.refresh_from_db()
        assert place.geometry is not None
        self.assertEqual(place.geometry.wkb, old_polygon.wkb)
        # place_resolved_at is still stamped, so it isn't retried on every view.
        location.refresh_from_db()
        self.assertTrue(boundary_generation_ran(location))
        self.assertFalse(boundary_generation_stale(location))

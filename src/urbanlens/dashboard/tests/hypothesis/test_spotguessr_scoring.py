"""Tests for services.spotguessr.scoring - point-vs-boundary distance and the points curve."""

from __future__ import annotations

from datetime import date
from itertools import count

from django.contrib.gis.geos import MultiPolygon, Point, Polygon
from django.utils import timezone
from model_bakery import baker

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.models.boundary.model import Boundary, BoundaryType
from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.services.spotguessr.scoring import (
    MAX_DATE_POINTS,
    MAX_ROUND_POINTS,
    distance_for_guess,
    points_for_date_guess,
    points_for_distance,
    resolve_target,
)

_coordinate_counter = count()


def _make_location() -> Location:
    offset = next(_coordinate_counter)
    return baker.make(Location, latitude=f"42.{650_000 + offset}", longitude=f"-73.{760_000 + offset}")


def _square(lon: float, lat: float, size: float = 0.01) -> MultiPolygon:
    ring = ((lon, lat), (lon + size, lat), (lon + size, lat + size), (lon, lat + size), (lon, lat))
    return MultiPolygon(Polygon(ring, srid=4326), srid=4326)


class PointsForDistanceTests(SimpleTestCase):
    def test_zero_distance_is_max_points(self) -> None:
        self.assertEqual(points_for_distance(0.0), MAX_ROUND_POINTS)

    def test_points_strictly_decrease_with_distance(self) -> None:
        near = points_for_distance(100.0)
        far = points_for_distance(5_000.0)
        very_far = points_for_distance(50_000.0)
        self.assertGreater(near, far)
        self.assertGreater(far, very_far)

    def test_very_far_distance_rounds_to_zero(self) -> None:
        self.assertEqual(points_for_distance(1_000_000.0), 0)

    def test_negative_distance_is_clamped_to_zero_distance(self) -> None:
        self.assertEqual(points_for_distance(-5.0), MAX_ROUND_POINTS)


class PointsForDateGuessTests(SimpleTestCase):
    def test_exact_date_is_max_points(self) -> None:
        today = date(2026, 1, 1)
        self.assertEqual(points_for_date_guess(today, today), MAX_DATE_POINTS)

    def test_missing_dates_score_zero(self) -> None:
        self.assertEqual(points_for_date_guess(None, date(2026, 1, 1)), 0)
        self.assertEqual(points_for_date_guess(date(2026, 1, 1), None), 0)

    def test_farther_off_scores_lower(self) -> None:
        actual = date(2026, 1, 1)
        close = points_for_date_guess(date(2026, 1, 10), actual)
        far = points_for_date_guess(date(2026, 6, 1), actual)
        self.assertGreater(close, far)

    def test_direction_of_the_error_does_not_matter(self) -> None:
        actual = date(2026, 6, 15)
        before = points_for_date_guess(date(2026, 6, 5), actual)
        after = points_for_date_guess(date(2026, 6, 25), actual)
        self.assertEqual(before, after)


class ResolveTargetTests(TestCase):
    def setUp(self) -> None:
        baker.make("auth.User")
        self.location = _make_location()

    def test_photo_with_its_own_coordinates_scores_as_a_point(self) -> None:
        image = baker.make(Image, location=self.location, latitude="42.700000", longitude="-73.800000")
        target = resolve_target(self.location, image)
        self.assertTrue(target.is_point)
        self.assertAlmostEqual(target.geometry.y, 42.7, places=4)
        self.assertAlmostEqual(target.geometry.x, -73.8, places=4)

    def test_photo_without_coordinates_scores_as_the_locations_boundary(self) -> None:
        image = baker.make(Image, location=self.location, latitude=None, longitude=None)
        target = resolve_target(self.location, image)
        self.assertFalse(target.is_point)
        self.assertIsNotNone(target.geometry)

    def test_no_photo_at_all_scores_as_the_locations_boundary(self) -> None:
        target = resolve_target(self.location, None)
        self.assertFalse(target.is_point)
        self.assertIsNotNone(target.geometry)


class DistanceForGuessTests(TestCase):
    def setUp(self) -> None:
        baker.make("auth.User")
        self.location = _make_location()

    def test_guess_inside_the_boundary_scores_zero_distance(self) -> None:
        polygon = _square(-73.76, 42.65, size=0.02)
        baker.make(
            Boundary,
            location=self.location,
            boundary_type=BoundaryType.PROPERTY,
            generated_polygon=polygon,
            generated_at=timezone.now(),
        )
        inside_point = Point(-73.75, 42.66, srid=4326)
        distance = distance_for_guess(self.location, inside_point, target_is_point=False, target_point=None)
        self.assertEqual(distance, 0.0)

    def test_guess_outside_the_boundary_scores_a_positive_distance(self) -> None:
        polygon = _square(-73.76, 42.65, size=0.001)
        baker.make(
            Boundary,
            location=self.location,
            boundary_type=BoundaryType.PROPERTY,
            generated_polygon=polygon,
            generated_at=timezone.now(),
        )
        far_point = Point(-73.0, 42.0, srid=4326)
        distance = distance_for_guess(self.location, far_point, target_is_point=False, target_point=None)
        self.assertGreater(distance, 0.0)

    def test_point_target_scores_distance_from_the_exact_point(self) -> None:
        target_point = Point(-73.760000, 42.650000, srid=4326)
        same_point_guess = Point(-73.760000, 42.650000, srid=4326)
        distance = distance_for_guess(self.location, same_point_guess, target_is_point=True, target_point=target_point)
        self.assertAlmostEqual(distance, 0.0, delta=1.0)

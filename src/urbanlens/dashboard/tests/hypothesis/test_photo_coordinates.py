"""Tests for services.photos.photo_coordinates - averaging anonymized SpotGuessr coordinate guesses."""

from __future__ import annotations

from itertools import count

from django.contrib.gis.geos import Point
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.images.model import Image, MediaKind
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.spotguessr.model import PhotoCoordinateGuess
from urbanlens.dashboard.services.photos.photo_coordinates import (
    MIN_GUESSES_FOR_ESTIMATE,
    MIN_GUESSES_FOR_OUTLIER_TRIM,
    recompute_estimated_coordinates,
)

_coordinate_counter = count()


def _make_location() -> Location:
    offset = next(_coordinate_counter)
    return baker.make(Location, latitude=f"42.{650_000 + offset}", longitude=f"-73.{760_000 + offset}")


def _make_image(location: Location) -> Image:
    return baker.make(Image, location=location, media_type=MediaKind.PHOTO, latitude=None, longitude=None)


def _guess(image: Image, lat: float, lng: float, *, is_correct: bool = True) -> PhotoCoordinateGuess:
    return PhotoCoordinateGuess.objects.create(
        image=image, guess_point=Point(lng, lat, srid=4326), is_correct=is_correct
    )


class RecomputeEstimatedCoordinatesTests(TestCase):
    def test_below_the_minimum_leaves_the_estimate_unset(self) -> None:
        image = _make_image(_make_location())
        for _ in range(MIN_GUESSES_FOR_ESTIMATE - 1):
            _guess(image, 42.65, -73.76)
        recompute_estimated_coordinates(image.pk)
        image.refresh_from_db()
        self.assertIsNone(image.estimated_latitude)
        self.assertIsNone(image.estimated_longitude)

    def test_exactly_the_minimum_sets_the_average(self) -> None:
        image = _make_image(_make_location())
        lats = [42.001, 42.002, 42.003, 42.004, 42.005]
        for lat in lats:
            _guess(image, lat, -73.5)
        recompute_estimated_coordinates(image.pk)
        image.refresh_from_db()
        assert image.estimated_latitude is not None
        assert image.estimated_longitude is not None
        self.assertAlmostEqual(float(image.estimated_latitude), sum(lats) / len(lats), places=5)
        self.assertAlmostEqual(float(image.estimated_longitude), -73.5, places=5)

    def test_incorrect_guesses_are_excluded_from_the_average(self) -> None:
        image = _make_image(_make_location())
        for lat in [42.001, 42.002, 42.003, 42.004, 42.005]:
            _guess(image, lat, -73.5)
        _guess(image, 50.0, -73.5, is_correct=False)  # wildly off, but marked incorrect - must not count
        recompute_estimated_coordinates(image.pk)
        image.refresh_from_db()
        assert image.estimated_latitude is not None
        self.assertLess(float(image.estimated_latitude), 43.0)

    def test_below_the_trim_threshold_no_outliers_are_dropped(self) -> None:
        image = _make_image(_make_location())
        lats = [42.001, 42.002, 42.003, 42.004, 50.0]  # one wild outlier, only 5 total (< trim threshold)
        for lat in lats:
            _guess(image, lat, -73.5)
        recompute_estimated_coordinates(image.pk)
        image.refresh_from_db()
        assert image.estimated_latitude is not None
        self.assertAlmostEqual(float(image.estimated_latitude), sum(lats) / len(lats), places=5)

    def test_at_the_trim_threshold_the_farthest_outlier_is_dropped(self) -> None:
        image = _make_image(_make_location())
        clustered = [42.001, 42.002, 42.003, 42.004, 42.005, 42.006, 42.007, 42.008, 42.009]
        for lat in clustered:
            _guess(image, lat, -73.5)
        _guess(image, 60.0, -73.5)  # 10th guess, way off - should be trimmed as the outlier
        self.assertEqual(
            PhotoCoordinateGuess.objects.filter(image=image, is_correct=True).count(), MIN_GUESSES_FOR_OUTLIER_TRIM
        )

        recompute_estimated_coordinates(image.pk)
        image.refresh_from_db()
        assert image.estimated_latitude is not None
        # Without trimming the average would be pulled well above 43; with the
        # outlier dropped it should stay tight around the clustered values.
        self.assertLess(float(image.estimated_latitude), 42.1)

    def test_trimming_never_drops_below_the_minimum_estimate_floor(self) -> None:
        image = _make_image(_make_location())
        # All ten guesses are wildly spread - trimming must still leave at
        # least MIN_GUESSES_FOR_ESTIMATE points to average, never fewer.
        for i in range(10):
            _guess(image, 42.0 + i * 5.0, -73.5)
        recompute_estimated_coordinates(image.pk)
        image.refresh_from_db()
        self.assertIsNotNone(image.estimated_latitude)

    def test_a_photo_with_no_guesses_at_all_is_a_no_op(self) -> None:
        image = _make_image(_make_location())
        recompute_estimated_coordinates(image.pk)
        image.refresh_from_db()
        self.assertIsNone(image.estimated_latitude)


class EffectiveCoordinatePrecedenceTests(TestCase):
    def test_manual_coordinates_always_win_over_an_estimate(self) -> None:
        location = _make_location()
        image = baker.make(
            Image,
            location=location,
            media_type=MediaKind.PHOTO,
            latitude="10.000000",
            longitude="20.000000",
            estimated_latitude="99.000000",
            estimated_longitude="99.000000",
        )
        assert image.effective_latitude is not None
        assert image.effective_longitude is not None
        self.assertEqual(float(image.effective_latitude), 10.0)
        self.assertEqual(float(image.effective_longitude), 20.0)

    def test_an_estimate_wins_over_the_location_fallback(self) -> None:
        location = _make_location()
        image = baker.make(
            Image,
            location=location,
            media_type=MediaKind.PHOTO,
            latitude=None,
            longitude=None,
            estimated_latitude="11.000000",
            estimated_longitude="21.000000",
        )
        assert image.effective_latitude is not None
        assert image.effective_longitude is not None
        self.assertEqual(float(image.effective_latitude), 11.0)
        self.assertEqual(float(image.effective_longitude), 21.0)

    def test_falls_back_to_the_location_when_nothing_else_is_set(self) -> None:
        location = _make_location()
        image = baker.make(Image, location=location, media_type=MediaKind.PHOTO, latitude=None, longitude=None)
        self.assertEqual(image.effective_latitude, location.latitude)
        self.assertEqual(image.effective_longitude, location.longitude)

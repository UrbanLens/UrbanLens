"""Tests for services.device_scan.clustering.

Pure-function properties (weighting, confidence, weighted geometry) are
covered with Hypothesis per CLAUDE.md's property-based testing requirement;
``recompute_wiki_device_markers``/``record_absence_report`` are covered with
real DB fixtures since they're inherently a database read/write pipeline.
"""

from __future__ import annotations

from datetime import timedelta
import math

from django.contrib.gis.geos import MultiPolygon, Point, Polygon
from django.test import SimpleTestCase
from django.utils import timezone
from hypothesis import given, strategies as st
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.boundary.model import Boundary
from urbanlens.dashboard.models.device_scan.model import DeviceScanEntry, DeviceScanUpload, MarkerStatus, ScannedDevice, WikiDeviceMarker
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.services.device_scan.clustering import (
    ABSENCE_STREAK_THRESHOLD,
    DECAY_HALF_LIFE_DAYS,
    LOOKBACK_DAYS,
    MERGE_DISTANCE_METERS,
    MIN_RADIUS_METERS,
    confidence_for_weight,
    recompute_wiki_device_markers,
    record_absence_report,
    weight_for_age,
    weighted_centroid,
    weighted_radius_meters,
)

from .place_helpers import official_geometry

_LAT = st.floats(min_value=-80, max_value=80, allow_nan=False, allow_infinity=False)
_LNG = st.floats(min_value=-170, max_value=170, allow_nan=False, allow_infinity=False)
_WEIGHT = st.floats(min_value=1e-6, max_value=1000, allow_nan=False, allow_infinity=False)
_POINTS_WITH_WEIGHTS = st.lists(st.tuples(_LAT, _LNG, _WEIGHT), min_size=1, max_size=6)


def _square(lng: float, lat: float, delta: float) -> MultiPolygon:
    ring = (
        (lng - delta, lat - delta),
        (lng + delta, lat - delta),
        (lng + delta, lat + delta),
        (lng - delta, lat + delta),
        (lng - delta, lat - delta),
    )
    return MultiPolygon(Polygon(ring, srid=4326), srid=4326)


class WeightForAgeTests(SimpleTestCase):
    """Recency decay: 1.0 at age zero, halving every DECAY_HALF_LIFE_DAYS."""

    def test_zero_age_is_full_weight(self) -> None:
        self.assertEqual(weight_for_age(timedelta(0)), 1.0)

    def test_one_half_life_is_half_weight(self) -> None:
        self.assertAlmostEqual(weight_for_age(timedelta(days=DECAY_HALF_LIFE_DAYS)), 0.5)

    def test_two_half_lives_is_quarter_weight(self) -> None:
        self.assertAlmostEqual(weight_for_age(timedelta(days=2 * DECAY_HALF_LIFE_DAYS)), 0.25)

    def test_negative_age_is_treated_as_zero(self) -> None:
        self.assertEqual(weight_for_age(timedelta(days=-5)), 1.0)

    @given(days=st.floats(min_value=0, max_value=3650, allow_nan=False, allow_infinity=False))
    def test_weight_matches_formula_and_stays_in_range(self, days: float) -> None:
        weight = weight_for_age(timedelta(days=days))
        self.assertAlmostEqual(weight, 0.5 ** (days / DECAY_HALF_LIFE_DAYS))
        self.assertGreater(weight, 0.0)
        self.assertLessEqual(weight, 1.0)

    @given(younger_days=st.floats(min_value=0, max_value=1000, allow_nan=False, allow_infinity=False), extra_days=st.floats(min_value=0, max_value=1000, allow_nan=False, allow_infinity=False))
    def test_weight_is_monotonically_non_increasing_with_age(self, younger_days: float, extra_days: float) -> None:
        older_days = younger_days + extra_days
        self.assertGreaterEqual(weight_for_age(timedelta(days=younger_days)), weight_for_age(timedelta(days=older_days)))


class ConfidenceForWeightTests(SimpleTestCase):
    """Saturating confidence: 0 at zero weight, approaching but never reaching 1."""

    def test_zero_weight_is_zero_confidence(self) -> None:
        self.assertEqual(confidence_for_weight(0.0), 0.0)

    def test_negative_weight_is_clamped_to_zero_confidence(self) -> None:
        self.assertEqual(confidence_for_weight(-5.0), 0.0)

    @given(weight=st.floats(min_value=0, max_value=100, allow_nan=False, allow_infinity=False))
    def test_confidence_matches_formula_and_stays_in_range(self, weight: float) -> None:
        # Capped at 100: math.exp(-w/3) underflows below float64 precision well
        # before that (around w~110), at which point 1.0 - tiny rounds to
        # exactly 1.0 - a float-precision artifact, not a behavior to assert on.
        confidence = confidence_for_weight(weight)
        self.assertAlmostEqual(confidence, 1.0 - math.exp(-weight / 3.0))
        self.assertGreaterEqual(confidence, 0.0)
        self.assertLess(confidence, 1.0)

    @given(weight=st.floats(min_value=0, max_value=1000, allow_nan=False, allow_infinity=False), extra=st.floats(min_value=0, max_value=1000, allow_nan=False, allow_infinity=False))
    def test_confidence_is_monotonically_non_decreasing_with_weight(self, weight: float, extra: float) -> None:
        self.assertLessEqual(confidence_for_weight(weight), confidence_for_weight(weight + extra))


class WeightedCentroidTests(SimpleTestCase):
    """Weighted average position - always within the bounding box of its inputs."""

    def test_single_point_returns_itself(self) -> None:
        centroid = weighted_centroid([(Point(-74.0, 40.0, srid=4326), 1.0)])
        self.assertAlmostEqual(centroid.x, -74.0)
        self.assertAlmostEqual(centroid.y, 40.0)

    def test_two_equally_weighted_points_average_to_the_midpoint(self) -> None:
        centroid = weighted_centroid([(Point(0.0, 0.0, srid=4326), 1.0), (Point(2.0, 4.0, srid=4326), 1.0)])
        self.assertAlmostEqual(centroid.x, 1.0)
        self.assertAlmostEqual(centroid.y, 2.0)

    def test_heavier_point_pulls_the_centroid_toward_it(self) -> None:
        centroid = weighted_centroid([(Point(0.0, 0.0, srid=4326), 100.0), (Point(10.0, 10.0, srid=4326), 1.0)])
        self.assertLess(centroid.x, 1.0)
        self.assertLess(centroid.y, 1.0)

    @given(data=_POINTS_WITH_WEIGHTS)
    def test_centroid_stays_within_the_bounding_box_of_its_inputs(self, data: list[tuple[float, float, float]]) -> None:
        points_with_weights = [(Point(lng, lat, srid=4326), weight) for lat, lng, weight in data]
        centroid = weighted_centroid(points_with_weights)
        lats = [lat for lat, _lng, _weight in data]
        lngs = [lng for _lat, lng, _weight in data]
        self.assertGreaterEqual(centroid.y, min(lats) - 1e-9)
        self.assertLessEqual(centroid.y, max(lats) + 1e-9)
        self.assertGreaterEqual(centroid.x, min(lngs) - 1e-9)
        self.assertLessEqual(centroid.x, max(lngs) + 1e-9)


class WeightedRadiusMetersTests(SimpleTestCase):
    """RMS spread around the centroid, floored at MIN_RADIUS_METERS."""

    def test_identical_points_floor_to_min_radius(self) -> None:
        point = Point(-74.0, 40.0, srid=4326)
        centroid = weighted_centroid([(point, 1.0), (point, 1.0)])
        radius = weighted_radius_meters([(point, 1.0), (point, 1.0)], centroid)
        self.assertEqual(radius, MIN_RADIUS_METERS)

    def test_spread_points_exceed_the_floor(self) -> None:
        points_with_weights = [(Point(0.0, 0.0, srid=4326), 1.0), (Point(0.01, 0.0, srid=4326), 1.0)]
        centroid = weighted_centroid(points_with_weights)
        radius = weighted_radius_meters(points_with_weights, centroid)
        self.assertGreater(radius, MIN_RADIUS_METERS)

    @given(data=_POINTS_WITH_WEIGHTS)
    def test_radius_never_falls_below_the_floor(self, data: list[tuple[float, float, float]]) -> None:
        points_with_weights = [(Point(lng, lat, srid=4326), weight) for lat, lng, weight in data]
        centroid = weighted_centroid(points_with_weights)
        radius = weighted_radius_meters(points_with_weights, centroid)
        self.assertGreaterEqual(radius, MIN_RADIUS_METERS)


class _ClusteringDbTestCase(TestCase):
    """Shared fixture: a wiki with a real location-default boundary, and a device."""

    def setUp(self) -> None:
        super().setUp()
        self.wiki_location = Location.objects.create(latitude=0.0, longitude=0.0)
        official_geometry(self.wiki_location, _square(0.0, 0.0, 0.01))
        self.wiki = baker.make(Wiki, location=self.wiki_location)
        self.device, _created = ScannedDevice.objects.get_or_create_for_mac("AA:BB:CC:DD:EE:FF")
        self.upload = DeviceScanUpload.objects.create()

    def _make_entry(self, *, lat: float, lng: float, age_days: float = 0.0) -> DeviceScanEntry:
        entry = DeviceScanEntry.objects.create(upload=self.upload, device=self.device, location=Point(lng, lat, srid=4326), detected=True)
        if age_days:
            DeviceScanEntry.objects.filter(pk=entry.pk).update(created=timezone.now() - timedelta(days=age_days))
        return entry


class RecomputeCreatesMarkerTests(_ClusteringDbTestCase):
    """A single corroborating entry produces one active marker."""

    def test_single_entry_creates_one_active_marker(self) -> None:
        self._make_entry(lat=0.0, lng=0.0)

        markers = recompute_wiki_device_markers(self.device, self.wiki)

        self.assertEqual(len(markers), 1)
        marker = markers[0]
        self.assertEqual(marker.status, MarkerStatus.ACTIVE)
        self.assertEqual(marker.observation_count, 1)
        self.assertAlmostEqual(marker.centroid.y, 0.0, places=5)
        self.assertAlmostEqual(marker.centroid.x, 0.0, places=5)
        self.assertGreater(marker.confidence, 0.0)

    def test_no_entries_produces_no_markers(self) -> None:
        markers = recompute_wiki_device_markers(self.device, self.wiki)
        self.assertEqual(markers, [])

    def test_entry_outside_the_boundary_is_ignored(self) -> None:
        self._make_entry(lat=5.0, lng=5.0)
        markers = recompute_wiki_device_markers(self.device, self.wiki)
        self.assertEqual(markers, [])


class RecomputeCorroborationTests(_ClusteringDbTestCase):
    """A second nearby entry strengthens the same marker rather than creating a new one."""

    def test_second_nearby_entry_corroborates_the_existing_marker(self) -> None:
        self._make_entry(lat=0.0, lng=0.0)
        first_markers = recompute_wiki_device_markers(self.device, self.wiki)
        self.assertEqual(len(first_markers), 1)
        first_confidence = first_markers[0].confidence

        self._make_entry(lat=0.0001, lng=0.0001)
        second_markers = recompute_wiki_device_markers(self.device, self.wiki)

        self.assertEqual(len(second_markers), 1)
        self.assertEqual(second_markers[0].pk, first_markers[0].pk)
        self.assertEqual(second_markers[0].observation_count, 2)
        self.assertGreater(second_markers[0].confidence, first_confidence)


class RecomputeMovedDeviceTests(_ClusteringDbTestCase):
    """Two far-apart clusters produce two distinct active markers."""

    def test_device_moved_shows_two_active_markers(self) -> None:
        self._make_entry(lat=0.0, lng=0.0)
        # ~333m east at the equator - past MERGE_DISTANCE_METERS (75m) but
        # still inside the wiki's ~1.1km-wide test boundary.
        self._make_entry(lat=0.0, lng=0.003)

        markers = recompute_wiki_device_markers(self.device, self.wiki)

        self.assertEqual(len(markers), 2)
        self.assertEqual({marker.status for marker in markers}, {MarkerStatus.ACTIVE})
        lngs = sorted(marker.centroid.x for marker in markers)
        self.assertGreater(lngs[1] - lngs[0], MERGE_DISTANCE_METERS / 111_000)


class RecomputeStaleTransitionTests(_ClusteringDbTestCase):
    """A marker with no corroborating entries left in the lookback window goes STALE."""

    def test_marker_goes_stale_once_its_entries_age_out(self) -> None:
        self._make_entry(lat=0.0, lng=0.0)
        markers = recompute_wiki_device_markers(self.device, self.wiki)
        marker_pk = markers[0].pk

        DeviceScanEntry.objects.update(created=timezone.now() - timedelta(days=LOOKBACK_DAYS + 1))
        markers = recompute_wiki_device_markers(self.device, self.wiki)

        self.assertEqual(markers, [])
        stale_marker = WikiDeviceMarker.objects.get(pk=marker_pk)
        self.assertEqual(stale_marker.status, MarkerStatus.STALE)

    def test_stale_marker_returns_to_active_once_a_fresh_entry_matches_it(self) -> None:
        self._make_entry(lat=0.0, lng=0.0)
        markers = recompute_wiki_device_markers(self.device, self.wiki)
        marker_pk = markers[0].pk
        WikiDeviceMarker.objects.filter(pk=marker_pk).update(status=MarkerStatus.STALE)

        self._make_entry(lat=0.0001, lng=0.0001)
        markers = recompute_wiki_device_markers(self.device, self.wiki)

        self.assertEqual(len(markers), 1)
        self.assertEqual(markers[0].pk, marker_pk)
        self.assertEqual(markers[0].status, MarkerStatus.ACTIVE)


class ManuallyPlacedMarkerTests(_ClusteringDbTestCase):
    """A manually-placed marker is frozen; new clusters create separate markers instead."""

    def test_manually_placed_marker_is_untouched_by_recompute(self) -> None:
        frozen_centroid = Point(0.0, 0.0, srid=4326)
        manual_marker = WikiDeviceMarker.objects.create(
            wiki=self.wiki,
            device=self.device,
            manually_placed=True,
            centroid=frozen_centroid,
            radius_meters=0.0,
            confidence=1.0,
            observation_count=0,
            first_observed_at=timezone.now(),
            last_observed_at=timezone.now(),
        )

        self._make_entry(lat=0.0, lng=0.0)
        new_markers = recompute_wiki_device_markers(self.device, self.wiki)

        manual_marker.refresh_from_db()
        self.assertEqual(manual_marker.centroid.x, 0.0)
        self.assertEqual(manual_marker.centroid.y, 0.0)
        self.assertEqual(manual_marker.observation_count, 0)
        self.assertEqual(len(new_markers), 1)
        self.assertNotEqual(new_markers[0].pk, manual_marker.pk)


class AbsenceReportTests(_ClusteringDbTestCase):
    """record_absence_report's streak-to-PRESUMED_REMOVED escalation."""

    def _make_marker(self) -> WikiDeviceMarker:
        return WikiDeviceMarker.objects.create(
            wiki=self.wiki,
            device=self.device,
            centroid=Point(0.0, 0.0, srid=4326),
            first_observed_at=timezone.now(),
            last_observed_at=timezone.now(),
        )

    def test_streak_below_threshold_stays_active(self) -> None:
        marker = self._make_marker()
        for _ in range(ABSENCE_STREAK_THRESHOLD - 1):
            marker = record_absence_report(marker)
        self.assertEqual(marker.status, MarkerStatus.ACTIVE)
        self.assertEqual(marker.absence_streak, ABSENCE_STREAK_THRESHOLD - 1)

    def test_streak_reaching_threshold_flips_to_presumed_removed(self) -> None:
        marker = self._make_marker()
        for _ in range(ABSENCE_STREAK_THRESHOLD):
            marker = record_absence_report(marker)
        self.assertEqual(marker.status, MarkerStatus.PRESUMED_REMOVED)

    def test_positive_detection_resets_the_streak_and_restores_active(self) -> None:
        marker = self._make_marker()
        for _ in range(ABSENCE_STREAK_THRESHOLD):
            marker = record_absence_report(marker)
        self.assertEqual(marker.status, MarkerStatus.PRESUMED_REMOVED)

        self._make_entry(lat=0.0, lng=0.0)
        markers = recompute_wiki_device_markers(self.device, self.wiki)

        self.assertEqual(len(markers), 1)
        self.assertEqual(markers[0].pk, marker.pk)
        self.assertEqual(markers[0].status, MarkerStatus.ACTIVE)
        self.assertEqual(markers[0].absence_streak, 0)

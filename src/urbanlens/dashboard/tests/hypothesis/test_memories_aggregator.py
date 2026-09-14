"""Tests for services.memories.aggregator.get_memory_events() date-range filtering."""

from __future__ import annotations

import datetime

from django.utils import timezone
from model_bakery import baker

from hypothesis import given, settings as hyp_settings, strategies as st
from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.baker_recipes import _make_profile
from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.models.visits.model import PinVisit, VisitSource
from urbanlens.dashboard.services.memories.aggregator import get_memory_events

_hyp = hyp_settings(max_examples=30, deadline=None)


def _make_visit(pin, visited_at: datetime.datetime) -> PinVisit:
    return baker.make(PinVisit, pin=pin, source=VisitSource.MANUAL, visited_at=visited_at)


class MemoryEventsDateRangeTests(TestCase):
    """get_memory_events() only returns events whose date falls within [start, end]."""

    def setUp(self):
        self.profile = _make_profile()
        self.location = baker.make_recipe("dashboard.location")
        self.pin = baker.make("dashboard.Pin", profile=self.profile, location=self.location)

    def test_visit_within_range_is_included(self):
        visited_at = timezone.make_aware(datetime.datetime(2024, 6, 15, 12, 0, 0))
        _make_visit(self.pin, visited_at)

        events = get_memory_events(self.profile, datetime.date(2024, 6, 1), datetime.date(2024, 6, 30))

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].type, "visit")

    def test_visit_before_range_is_excluded(self):
        visited_at = timezone.make_aware(datetime.datetime(2024, 5, 31, 23, 59, 0))
        _make_visit(self.pin, visited_at)

        events = get_memory_events(self.profile, datetime.date(2024, 6, 1), datetime.date(2024, 6, 30))

        self.assertEqual(events, [])

    def test_visit_after_range_is_excluded(self):
        visited_at = timezone.make_aware(datetime.datetime(2024, 7, 1, 0, 1, 0))
        _make_visit(self.pin, visited_at)

        events = get_memory_events(self.profile, datetime.date(2024, 6, 1), datetime.date(2024, 6, 30))

        self.assertEqual(events, [])

    def test_events_sorted_newest_first(self):
        earlier = timezone.make_aware(datetime.datetime(2024, 6, 5, 12, 0, 0))
        later = timezone.make_aware(datetime.datetime(2024, 6, 20, 12, 0, 0))
        _make_visit(self.pin, earlier)
        _make_visit(self.pin, later)

        events = get_memory_events(self.profile, datetime.date(2024, 6, 1), datetime.date(2024, 6, 30))

        self.assertEqual(len(events), 2)
        self.assertEqual(events[0].occurred_at, later)
        self.assertEqual(events[1].occurred_at, earlier)

    def test_other_profiles_visits_are_excluded(self):
        other_profile = _make_profile()
        other_pin = baker.make("dashboard.Pin", profile=other_profile, location=self.location)
        _make_visit(other_pin, timezone.make_aware(datetime.datetime(2024, 6, 15, 12, 0, 0)))

        events = get_memory_events(self.profile, datetime.date(2024, 6, 1), datetime.date(2024, 6, 30))

        self.assertEqual(events, [])

    @_hyp
    @given(
        visit_day=st.dates(min_value=datetime.date(2020, 1, 1), max_value=datetime.date(2029, 12, 31)),
        range_start_offset=st.integers(min_value=-10, max_value=10),
        range_end_offset=st.integers(min_value=-10, max_value=10),
    )
    def test_inclusion_matches_date_range_boundary(self, visit_day, range_start_offset, range_end_offset):
        start = visit_day + datetime.timedelta(days=min(range_start_offset, range_end_offset))
        end = visit_day + datetime.timedelta(days=max(range_start_offset, range_end_offset))

        PinVisit.objects.filter(pin=self.pin).delete()
        visited_at = timezone.make_aware(datetime.datetime.combine(visit_day, datetime.time(12, 0, 0)))
        _make_visit(self.pin, visited_at)

        events = get_memory_events(self.profile, start, end)

        expected_included = start <= visit_day <= end
        self.assertEqual(len(events) == 1, expected_included)


class PhotoMemoryEventTests(TestCase):
    """get_memory_events() must surface geotagged photos, not silently drop them.

    Regression test: _photos_for_range annotated a queryset field named ``effective_taken_at``, colliding with
    the real read-only ``Image.effective_taken_at`` property - Django raised AttributeError trying to setattr
    the annotated value onto that name during row materialization, and get_memory_events' broad exception guard
    (see MemorySourceIsolationTests) swallowed it every time, for every profile with any photo in range."""

    def setUp(self):
        self.profile = _make_profile()

    def _make_photo(self, **kwargs) -> Image:
        kwargs.setdefault("latitude", 40.0)
        kwargs.setdefault("longitude", -74.0)
        kwargs.setdefault("map_hidden", False)
        return baker.make(Image, profile=self.profile, **kwargs)

    def test_a_geotagged_photo_within_range_is_included(self):
        taken_at = timezone.make_aware(datetime.datetime(2024, 6, 15, 12, 0, 0))
        self._make_photo(taken_at=taken_at)

        events = get_memory_events(self.profile, datetime.date(2024, 6, 1), datetime.date(2024, 6, 30))

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].type, "photo")
        self.assertEqual(events[0].occurred_at, taken_at)

    def test_a_photo_with_no_exif_time_falls_back_to_filename_taken_at(self):
        """Matches Image.effective_taken_at's own fallback chain exactly - a
        photo with no EXIF time but a filename-parsed date still counts."""
        filename_taken_at = timezone.make_aware(datetime.datetime(2024, 6, 10, 9, 0, 0))
        self._make_photo(taken_at=None, filename_taken_at=filename_taken_at)

        events = get_memory_events(self.profile, datetime.date(2024, 6, 1), datetime.date(2024, 6, 30))

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].occurred_at, filename_taken_at)

    def test_a_photo_with_no_known_time_at_all_is_excluded_not_erroring(self):
        self._make_photo(taken_at=None, filename_taken_at=None)

        events = get_memory_events(self.profile, datetime.date(2024, 6, 1), datetime.date(2024, 6, 30))

        self.assertEqual(events, [])

"""Tests for the privacy-preserving wiki pinned-user count.

Covers:
- approximate_pin_count - "fewer than 3" below the threshold, fuzz within
  ±2 (clamped to the threshold) above it (property-based), and one cached
  value per wiki so refreshes can't average out the noise
"""

from __future__ import annotations

from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import override_settings
from hypothesis import given, strategies as st
from model_bakery import baker

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.place.model import Place, PlaceKind
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.services.wiki import community_counts
from urbanlens.dashboard.services.wiki.community_counts import MIN_VISIBLE_PIN_COUNT, approximate_pin_count, wiki_community_summary

_LOCMEM_CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}


@override_settings(CACHES=_LOCMEM_CACHES)
class ApproximatePinCountTests(SimpleTestCase):
    """approximate_pin_count hides low counts and fuzzes the rest."""

    def setUp(self) -> None:
        super().setUp()
        cache.clear()

    @given(count=st.integers(min_value=0, max_value=MIN_VISIBLE_PIN_COUNT - 1))
    def test_low_counts_show_no_number(self, count):
        result = approximate_pin_count(wiki_id=1, exact_count=count)
        self.assertTrue(result["is_low"])
        self.assertIsNone(result["value"])

    @given(count=st.integers(min_value=MIN_VISIBLE_PIN_COUNT, max_value=500), wiki_id=st.integers(min_value=1, max_value=10_000))
    def test_fuzz_stays_within_two_and_at_least_threshold(self, count, wiki_id):
        cache.clear()
        result = approximate_pin_count(wiki_id=wiki_id, exact_count=count)
        self.assertFalse(result["is_low"])
        value = result["value"]
        self.assertGreaterEqual(value, MIN_VISIBLE_PIN_COUNT)
        self.assertLessEqual(abs(value - count), 2)

    def test_value_is_cached_per_wiki(self):
        first = approximate_pin_count(wiki_id=42, exact_count=10)["value"]
        for _ in range(25):
            self.assertEqual(approximate_pin_count(wiki_id=42, exact_count=10)["value"], first)

    def test_different_wikis_cached_independently(self):
        approximate_pin_count(wiki_id=1, exact_count=10)
        # A different wiki must not read wiki 1's cached value's key.
        result = approximate_pin_count(wiki_id=2, exact_count=100)
        self.assertLessEqual(abs(result["value"] - 100), 2)

    def test_cached_value_survives_exact_count_drift(self):
        """Refreshing after one more user pins must not reveal the change that day."""
        first = approximate_pin_count(wiki_id=7, exact_count=10)["value"]
        after_new_pin = approximate_pin_count(wiki_id=7, exact_count=11)["value"]
        self.assertEqual(after_new_pin, first)


def _make_pin(location: Location) -> Pin:
    return Pin.objects.create(profile=baker.make(User).profile, location=location, parent_pin=None)


class WikiCommunitySummaryPlaceAwareTests(TestCase):
    """``wiki_community_summary`` must count root pins across every Location
    sharing the wiki's Place, not just the single Location the caller
    resolved it through - ``resolve_visible_wiki`` deliberately allows a
    caller's own (possibly non-canonical) Location to differ from
    ``wiki.location`` so long as they share a Place, so counting only the
    passed-in Location undercounts "N users have this pinned" whenever more
    than one Location exists under the Place. See docs/GOALS_CODE_AUDIT.md
    ("Cross-pin aggregate comparison level")."""

    def _spy_exact_count(self, wiki: Wiki, location: Location) -> int:
        """Call wiki_community_summary and return the exact_count it computed,
        bypassing approximate_pin_count's fuzz/threshold so small counts are
        directly assertable."""
        with mock.patch("urbanlens.dashboard.services.wiki.community_counts.approximate_pin_count", wraps=community_counts.approximate_pin_count) as spy:
            wiki_community_summary(wiki, location)
        return spy.call_args[0][1]

    def test_counts_pins_across_every_location_sharing_the_place(self) -> None:
        place = baker.make(Place, kind=PlaceKind.PARCEL)
        wiki_location = Location.objects.create(latitude=40.0, longitude=-74.0, place=place)
        other_location = Location.objects.create(latitude=40.001, longitude=-74.001, place=place)
        wiki = baker.make(Wiki, location=wiki_location, place=place)
        _make_pin(wiki_location)
        _make_pin(wiki_location)
        _make_pin(other_location)

        self.assertEqual(self._spy_exact_count(wiki, wiki_location), 3)

    def test_falls_back_to_the_single_location_when_it_has_no_place(self) -> None:
        location = Location.objects.create(latitude=41.0, longitude=-75.0)
        wiki = baker.make(Wiki, location=location, place=None)
        _make_pin(location)
        _make_pin(location)
        elsewhere = Location.objects.create(latitude=42.0, longitude=-76.0)
        _make_pin(elsewhere)

        self.assertEqual(self._spy_exact_count(wiki, location), 2)

    def test_the_count_is_the_same_regardless_of_which_shared_location_is_passed(self) -> None:
        """The same wiki must report the same count no matter which of the
        place's several pinned coordinates the viewer's URL happened to name."""
        place = baker.make(Place, kind=PlaceKind.PARCEL)
        wiki_location = Location.objects.create(latitude=40.0, longitude=-74.0, place=place)
        other_location = Location.objects.create(latitude=40.001, longitude=-74.001, place=place)
        wiki = baker.make(Wiki, location=wiki_location, place=place)
        _make_pin(wiki_location)
        _make_pin(other_location)

        via_wiki_location = wiki_community_summary(wiki, wiki_location)
        via_other_location = wiki_community_summary(wiki, other_location)

        self.assertEqual(via_wiki_location, via_other_location)

"""Tests for the privacy-preserving wiki pinned-user count.

Covers:
- approximate_pin_count - "fewer than 3" below the threshold, fuzz within
  ±2 (clamped to the threshold) above it (property-based), and one cached
  value per wiki so refreshes can't average out the noise
"""

from __future__ import annotations

from django.core.cache import cache
from django.test import override_settings
from hypothesis import given, strategies as st

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.services.community_counts import MIN_VISIBLE_PIN_COUNT, approximate_pin_count

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

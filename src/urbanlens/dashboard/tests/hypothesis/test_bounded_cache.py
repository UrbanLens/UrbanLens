"""A proxied body must not be able to evict other people's sessions."""

from __future__ import annotations

from unittest import mock

from django.core.cache import cache

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.services.core import bounded_cache


class TheCeilingTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        cache.clear()

    def test_a_small_body_is_stored(self) -> None:
        self.assertTrue(bounded_cache.set_if_small("k", b"x" * 10, "image/jpeg", 60, label="probe"))
        self.assertEqual(cache.get("k"), (b"x" * 10, "image/jpeg"))

    def test_a_body_over_the_ceiling_is_not_stored(self) -> None:
        oversized = b"x" * (bounded_cache.MAX_CACHED_BODY_BYTES + 1)
        self.assertFalse(bounded_cache.set_if_small("k", oversized, "image/jpeg", 60, label="probe"))
        self.assertIsNone(cache.get("k"), "an oversized body was stored anyway")

    def test_a_body_exactly_at_the_ceiling_is_stored(self) -> None:
        """Off-by-one guard: the ceiling is inclusive."""
        exact = b"x" * bounded_cache.MAX_CACHED_BODY_BYTES
        self.assertTrue(bounded_cache.set_if_small("k", exact, "image/jpeg", 60, label="probe"))

    def test_an_unreachable_cache_is_not_an_error(self) -> None:
        with mock.patch.object(bounded_cache.cache, "set", side_effect=ConnectionError("gone")):
            self.assertFalse(bounded_cache.set_if_small("k", b"x", "image/jpeg", 60, label="probe"))

    def test_the_ceiling_is_a_thumbnail_budget_not_a_photo_one(self) -> None:
        """Guards the constant. Raised past a megabyte it stops bounding anything that matters, because the bodies it exists to refuse are megabytes."""
        self.assertLessEqual(bounded_cache.MAX_CACHED_BODY_BYTES, 1024 * 1024)

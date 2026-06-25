"""Tests for memcached-safe cache key helpers."""

from __future__ import annotations

from hypothesis import given, settings, strategies as st

from urbanlens.core.cache_keys import is_memcached_safe_key, make_cache_key
from urbanlens.core.tests.testcase import TestCase

_hyp = settings(max_examples=50, deadline=None)


class MakeCacheKeyTests(TestCase):
    """make_cache_key produces stable, memcached-safe keys."""

    def test_namespace_only(self):
        self.assertEqual(make_cache_key("smithsonian"), "smithsonian")

    def test_same_inputs_produce_same_key(self):
        key_a = make_cache_key("smithsonian", "TESTING PIN - DELETEME")
        key_b = make_cache_key("smithsonian", "TESTING PIN - DELETEME")
        self.assertEqual(key_a, key_b)

    def test_different_inputs_produce_different_keys(self):
        key_a = make_cache_key("smithsonian", "factory")
        key_b = make_cache_key("smithsonian", "mill")
        self.assertNotEqual(key_a, key_b)

    def test_keys_with_spaces_are_memcached_safe(self):
        key = make_cache_key("smithsonian", "TESTING PIN - DELETEME")
        self.assertTrue(is_memcached_safe_key(key))
        self.assertNotIn(" ", key)

    @given(st.text(min_size=0, max_size=200))
    @_hyp
    def test_arbitrary_text_produces_memcached_safe_keys(self, text: str):
        key = make_cache_key("ns", text)
        self.assertTrue(is_memcached_safe_key(key))

"""Tests for memcached-safe cache key helpers."""

from __future__ import annotations

import unicodedata

from hypothesis import given, settings, strategies as st

from urbanlens.core.cache_keys import make_cache_key
from urbanlens.core.tests.testcase import SimpleTestCase

_hyp = settings(max_examples=50, deadline=None)


class MakeCacheKeyTests(SimpleTestCase):
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

    def test_all_parts_contribute_to_the_key(self):
        """A change to any part - not just the first - must change the key.

        Real callers rely on this: pin.py and locations/base.py both key on a
        (lat, lng) pair passed as two separate parts, so two pins that share a
        latitude but differ in longitude must not collide.
        """
        self.assertNotEqual(
            make_cache_key("smithsonian", "a", "x"),
            make_cache_key("smithsonian", "a", "y"),
        )

    def test_empty_string_part_is_distinct_from_no_parts(self):
        """A single empty-string part is still a part, not the no-parts case.

        ``parts`` is a non-empty tuple here (``("",)``), so this must take the
        hashed branch rather than the "return namespace as-is" shortcut.
        """
        self.assertNotEqual(make_cache_key("smithsonian", ""), make_cache_key("smithsonian"))

    def test_float_parts_are_supported(self):
        """The signature advertises ``str | float`` - exercise the float side."""
        key_a = make_cache_key("smithsonian", 41.12345, -75.6789)
        key_b = make_cache_key("smithsonian", 41.12345, -75.6789)
        self.assertEqual(key_a, key_b)
        key_c = make_cache_key("smithsonian", 41.1, -75.6789)
        self.assertNotEqual(key_a, key_c)

    def test_spaces_and_control_characters_never_appear_in_the_key(self):
        """The docstring's core promise: unsafe part content never survives into the key."""
        key = make_cache_key("smithsonian", "TESTING PIN - DELETEME\n\t\x00")
        for char in key:
            self.assertFalse(char.isspace(), f"key contains whitespace: {key!r}")
            self.assertNotEqual(unicodedata.category(char)[0], "C", f"key contains a control character: {key!r}")


class MakeCacheKeyPropertyTests(SimpleTestCase):
    """Property-based checks that hold for arbitrary parts."""

    @given(parts=st.lists(st.text(min_size=0, max_size=200), min_size=1, max_size=5))
    @_hyp
    def test_key_never_contains_whitespace_or_control_characters(self, parts: list[str]) -> None:
        key = make_cache_key("smithsonian", *parts)
        for char in key:
            self.assertFalse(char.isspace(), f"key contains whitespace: {key!r}")
            self.assertNotEqual(unicodedata.category(char)[0], "C", f"key contains a control character: {key!r}")

    @given(parts=st.lists(st.text(min_size=0, max_size=5000), min_size=1, max_size=5))
    @_hyp
    def test_key_length_does_not_grow_with_part_size(self, parts: list[str]) -> None:
        """Memcached rejects keys over 250 bytes; the digest keeps the key a fixed length regardless of part size."""
        key = make_cache_key("ns", *parts)
        self.assertEqual(len(key), len("ns") + 1 + 64)

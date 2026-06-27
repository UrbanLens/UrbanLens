"""Tests for is_meaningful_name — filtering placeholder pin/location names."""

from __future__ import annotations

from hypothesis import given, settings as hyp_settings, strategies as st

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.services.locations.naming import is_meaningful_name

_hyp = hyp_settings(max_examples=80, deadline=None)

_MEANINGLESS_EXAMPLES = (
    "",
    "   ",
    "None",
    "NONE",
    " null ",
    "N/A",
    "na",
    "No Information Available",
    "no information available",
    "Dropped pin",
    "Dropped Pin",
    "Unnamed Location",
    "unnamed",
    "Abandoned",
    "Abandoned Location",
    "Abandoned place",
    "Unknown",
    "Unknown Location",
    "Untitled pin",
    "Untitled",
    "Coordinates",
    "40.7128, -74.0060",
    "40.7128,-74.0060",
    "40.7128 -74.0060",
    "New Location",
    "Map pin",
    "Pin",
    "Location",
    "Unnamed activity",
)

_MEANINGFUL_EXAMPLES = (
    "Riverside Mill",
    "Abandoned Warehouse",
    "Abandoned Power Plant",
    "Old Factory",
    "123 Main St",
    "Pin Oak Lane",
    "None Such Farm",
    "Steel Works",
)


class IsMeaningfulNameTests(TestCase):
    """is_meaningful_name rejects placeholders and accepts real place names."""

    def test_meaningless_examples(self) -> None:
        for name in _MEANINGLESS_EXAMPLES:
            with self.subTest(name=name):
                self.assertFalse(is_meaningful_name(name))

    def test_meaningful_examples(self) -> None:
        for name in _MEANINGFUL_EXAMPLES:
            with self.subTest(name=name):
                self.assertTrue(is_meaningful_name(name))

    def test_none_input_is_not_meaningful(self) -> None:
        self.assertFalse(is_meaningful_name(None))

    @given(
        lat=st.floats(min_value=-90, max_value=90, allow_nan=False, allow_infinity=False),
        lng=st.floats(min_value=-180, max_value=180, allow_nan=False, allow_infinity=False),
    )
    @_hyp
    def test_coordinate_strings_are_not_meaningful(self, lat: float, lng: float) -> None:
        for fmt in (f"{lat}, {lng}", f"{lat},{lng}", f"{lat} {lng}"):
            self.assertFalse(is_meaningful_name(fmt))

    @given(name=st.sampled_from(_MEANINGFUL_EXAMPLES))
    @_hyp
    def test_known_real_names_stay_meaningful(self, name: str) -> None:
        self.assertTrue(is_meaningful_name(name))

    @given(name=st.sampled_from(_MEANINGLESS_EXAMPLES))
    @_hyp
    def test_known_placeholders_stay_meaningless(self, name: str) -> None:
        self.assertFalse(is_meaningful_name(name))

"""Tests for area-suffixed unnamed-location display names.

Covers:
- Location.area_label - [City, State] in the USA, [City, Country] elsewhere,
  with graceful fallbacks when components are missing (property-based)
- Location.display_name - "Unnamed Location in {area}" fallback
- is_meaningful_name - the area-suffixed placeholder stays non-meaningful so
  it never leaks into external API queries or saved names
"""

from __future__ import annotations

from hypothesis import given, strategies as st

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.services.locations.naming import is_meaningful_name

_NAME_PART = st.text(alphabet="abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ ", min_size=1, max_size=20).map(str.strip).filter(bool)


def _location(**kwargs) -> Location:
    return Location(latitude="42.650000", longitude="-73.750000", **kwargs)


class AreaLabelTests(TestCase):
    """Location.area_label builds a short human-readable area string."""

    def test_usa_city_state(self):
        loc = _location(locality="Albany", administrative_area_level_1="NY", country="United States")
        self.assertEqual(loc.area_label, "Albany, NY")

    def test_blank_country_treated_as_usa(self):
        loc = _location(locality="Albany", administrative_area_level_1="NY")
        self.assertEqual(loc.area_label, "Albany, NY")

    def test_usa_spelling_variants(self):
        for spelling in ("USA", "US", "U.S.A.", "united states of america"):
            loc = _location(locality="Detroit", administrative_area_level_1="MI", country=spelling)
            self.assertEqual(loc.area_label, "Detroit, MI", spelling)

    def test_non_usa_city_country(self):
        loc = _location(locality="Kyiv", administrative_area_level_1="Kyiv Oblast", country="Ukraine")
        self.assertEqual(loc.area_label, "Kyiv, Ukraine")

    def test_non_usa_falls_back_to_state_country(self):
        loc = _location(administrative_area_level_1="Bavaria", country="Germany")
        self.assertEqual(loc.area_label, "Bavaria, Germany")

    def test_country_only(self):
        loc = _location(country="Japan")
        self.assertEqual(loc.area_label, "Japan")

    def test_no_components_returns_none(self):
        self.assertIsNone(_location().area_label)

    @given(city=_NAME_PART, state=_NAME_PART)
    def test_usa_label_always_contains_city(self, city, state):
        loc = _location(locality=city, administrative_area_level_1=state, country="United States")
        label = loc.area_label
        self.assertIsNotNone(label)
        self.assertIn(city, label)


class UnnamedDisplayNameTests(TestCase):
    """display_name falls back to 'Unnamed Location in {area}'."""

    def test_unnamed_with_area(self):
        loc = _location(locality="Albany", administrative_area_level_1="NY")
        self.assertEqual(loc.display_name, "Unnamed Location in Albany, NY")

    def test_unnamed_without_area(self):
        self.assertEqual(_location().display_name, "Unnamed Location")

    def test_official_name_wins(self):
        loc = _location(official_name="Old Mill", locality="Albany", administrative_area_level_1="NY")
        self.assertEqual(loc.display_name, "Old Mill")


class PlaceholderMeaningfulnessTests(TestCase):
    """The area-suffixed placeholder must never count as a meaningful name."""

    def test_plain_placeholder_not_meaningful(self):
        self.assertFalse(is_meaningful_name("Unnamed Location"))

    def test_area_suffixed_placeholder_not_meaningful(self):
        self.assertFalse(is_meaningful_name("Unnamed Location in Albany, NY"))

    @given(city=_NAME_PART, state=_NAME_PART)
    def test_any_generated_placeholder_not_meaningful(self, city, state):
        loc = _location(locality=city, administrative_area_level_1=state)
        self.assertFalse(is_meaningful_name(loc.display_name))

    def test_real_names_still_meaningful(self):
        self.assertTrue(is_meaningful_name("Old Mill"))
        self.assertTrue(is_meaningful_name("Bannerman Castle"))

"""Tests for the SearXNG image-search media provider's relevance query builder.

The value of this provider is the shape of its query: three ``OR``-groups that
a general image engine reads as required, disambiguating clauses. These cover:

* ``assemble_image_query`` - the pure string assembly (aliases · area · subject),
  including dedup, quote-stripping, and the "no alias -> no query" rule.
* ``build_image_query`` - pulling aliases (nickname-excluded) and area terms off
  a real ``Pin``/``Location``, including the US-state vs country choice.
"""

from __future__ import annotations

from hypothesis import given, settings as hyp_settings, strategies as st

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.plugins.builtin.searxng_images import (
    SUBJECT_TERMS,
    assemble_image_query,
    build_image_query,
)

_hyp = hyp_settings(max_examples=50, deadline=None)


class AssembleImageQueryTests(SimpleTestCase):
    """assemble_image_query builds the grouped, quoted OR-clauses."""

    def test_full_query_has_three_groups_in_order(self):
        query = assemble_image_query(["Hudson River State Hospital", "HRSH"], ["New York", "Poughkeepsie"])
        assert query is not None
        self.assertEqual(
            query,
            '("Hudson River State Hospital" OR "HRSH") ("New York" OR "Poughkeepsie") '
            + "(" + " OR ".join(f'"{t}"' for t in SUBJECT_TERMS) + ")",
        )

    def test_no_aliases_yields_none(self):
        self.assertIsNone(assemble_image_query([], ["New York"]))
        self.assertIsNone(assemble_image_query(["", "  "], ["New York"]))

    def test_area_group_is_omitted_when_empty(self):
        query = assemble_image_query(["Foo"], [])
        assert query is not None
        # Alias group, then straight to the subject group - only two groups.
        self.assertEqual(query.count("("), 2)
        self.assertTrue(query.startswith('("Foo") ('))

    def test_aliases_are_deduped_case_insensitively(self):
        query = assemble_image_query(["Foo", "foo", "FOO"], [])
        assert query is not None
        self.assertEqual(query.count('"Foo"'), 1)

    def test_embedded_quotes_are_stripped_from_terms(self):
        query = assemble_image_query(['a "quoted" name'], [])
        assert query is not None
        # The inner quotes are gone; only the group's own wrapping quotes remain.
        self.assertIn('"a quoted name"', query)

    def test_subject_group_is_always_present(self):
        query = assemble_image_query(["Foo"], [])
        assert query is not None
        for term in SUBJECT_TERMS:
            self.assertIn(f'"{term}"', query)

    @given(
        st.lists(st.text(min_size=1, max_size=20).filter(lambda s: s.strip() and '"' not in s), min_size=1, max_size=5, unique_by=lambda s: s.strip().casefold()),
        st.lists(st.text(min_size=1, max_size=20).filter(lambda s: s.strip() and '"' not in s), min_size=0, max_size=3, unique_by=lambda s: s.strip().casefold()),
    )
    @_hyp
    def test_group_count_matches_present_components(self, aliases: list[str], area: list[str]):
        query = assemble_image_query(aliases, area)
        assert query is not None
        expected_groups = 2 + (1 if area else 0)  # aliases + subject, plus area when present
        self.assertEqual(query.count("("), expected_groups)


class BuildImageQueryTests(TestCase):
    """build_image_query gathers nickname-excluded aliases and area terms off a Pin."""

    def _pin(self, *, pin_name: str = "", locality: str = "", state: str = "", country: str = "", official_name: str = ""):
        from model_bakery import baker

        from urbanlens.dashboard.models.location.model import Location
        from urbanlens.dashboard.models.pin.model import Pin

        # city/state are property aliases for the concrete locality /
        # administrative_area_level_1 columns; official_name is a real field.
        location = baker.make(
            Location,
            locality=locality,
            administrative_area_level_1=state,
            country=country,
            official_name=official_name or None,
        )
        return baker.make(Pin, location=location, name=pin_name)

    def test_none_when_no_meaningful_name(self):
        # No pin name, no official name, no wiki -> only the "Unnamed Location"
        # placeholder remains, which is not a meaningful search name.
        pin = self._pin(pin_name="", locality="Albany", state="NY", country="USA")
        self.assertIsNone(build_image_query(pin))

    def test_us_pin_uses_state_and_city_as_area(self):
        pin = self._pin(pin_name="Old Mill", locality="Troy", state="New York", country="USA")
        query = build_image_query(pin)
        assert query is not None
        self.assertIn('"Old Mill"', query)
        self.assertIn('"New York"', query)
        self.assertIn('"Troy"', query)

    def test_non_us_pin_uses_country_and_city_as_area(self):
        pin = self._pin(pin_name="Ruined Cathedral", locality="Valencia", state="", country="Spain")
        query = build_image_query(pin)
        assert query is not None
        self.assertIn('"Spain"', query)
        self.assertIn('"Valencia"', query)

    def test_nickname_aliases_are_excluded(self):
        from urbanlens.dashboard.models.aliases.model import AliasType, PinAlias

        pin = self._pin(pin_name="Real Name", locality="Troy", state="NY", country="USA")
        PinAlias.objects.create(pin=pin, name="Official Alt", kind=AliasType.ALTERNATE)
        PinAlias.objects.create(pin=pin, name="Secret Nick", kind=AliasType.NICKNAME)
        query = build_image_query(pin)
        assert query is not None
        self.assertIn('"Official Alt"', query)
        self.assertNotIn("Secret Nick", query)

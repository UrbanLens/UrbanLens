"""Tests for the global-search natural-language query parser."""

from __future__ import annotations

from datetime import date

from hypothesis import given, settings, strategies as st

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.services.global_search.parser import parse_query


class ParseQueryStructureTests(TestCase):
    """Structured extraction: types, dates, and places."""

    def test_photos_from_last_summer(self):
        parsed = parse_query("photos from last summer")
        self.assertEqual(parsed.types, {"photos"})
        self.assertIsNotNone(parsed.date_start)
        self.assertIsNotNone(parsed.date_end)
        # A summer range: starts in June, ends in August, already over.
        self.assertEqual(parsed.date_start.month, 6)
        self.assertEqual(parsed.date_end.month, 8)
        self.assertLess(parsed.date_end, date.today())
        self.assertEqual(parsed.terms, [])

    def test_pins_in_cincinnati(self):
        parsed = parse_query("pins in Cincinnati")
        self.assertEqual(parsed.types, {"pins"})
        self.assertEqual(parsed.place, "cincinnati")
        self.assertEqual(parsed.terms, [])

    def test_photos_in_albany(self):
        parsed = parse_query("photos in Albany")
        self.assertEqual(parsed.types, {"photos"})
        self.assertEqual(parsed.place, "albany")

    def test_plain_text_query_has_no_structure(self):
        parsed = parse_query("abandoned mill")
        self.assertFalse(parsed.has_structure)
        self.assertEqual(parsed.terms, ["abandoned", "mill"])

    def test_type_synonyms(self):
        self.assertEqual(parse_query("dms").types, {"messages"})
        self.assertEqual(parse_query("pictures").types, {"photos"})
        self.assertEqual(parse_query("check-ins").types, {"safety"})

    def test_stopwords_removed_from_terms(self):
        parsed = parse_query("messages about meetup")
        self.assertEqual(parsed.types, {"messages"})
        self.assertEqual(parsed.terms, ["meetup"])

    def test_explicit_month_and_year(self):
        parsed = parse_query("visits june 2025")
        self.assertEqual(parsed.types, {"visits"})
        self.assertEqual(parsed.date_start, date(2025, 6, 1))
        self.assertEqual(parsed.date_end, date(2025, 6, 30))

    def test_bare_year(self):
        parsed = parse_query("trips in 2024")
        self.assertEqual(parsed.types, {"trips"})
        self.assertEqual(parsed.date_start, date(2024, 1, 1))
        self.assertEqual(parsed.date_end, date(2024, 12, 31))

    def test_this_year_ends_today(self):
        parsed = parse_query("photos this year")
        self.assertEqual(parsed.date_start, date(date.today().year, 1, 1))
        self.assertEqual(parsed.date_end, date.today())

    def test_bare_season_without_preposition_stays_text(self):
        # "Summer Street Mill" must remain a literal text search.
        parsed = parse_query("summer street mill")
        self.assertIsNone(parsed.date_start)
        self.assertIn("summer", parsed.terms)

    def test_place_with_digits_is_not_a_place(self):
        parsed = parse_query("pins at 123 main")
        self.assertIsNone(parsed.place)

    def test_describe_filters_mentions_place_and_type(self):
        chips = parse_query("pins in Cincinnati").describe_filters()
        self.assertIn("Pins", chips)
        self.assertIn("in Cincinnati", chips)

    def test_empty_query(self):
        parsed = parse_query("   ")
        self.assertTrue(parsed.is_empty)

    def test_near_me(self):
        parsed = parse_query("pins near me")
        self.assertEqual(parsed.types, {"pins"})
        self.assertTrue(parsed.near_me)
        self.assertEqual(parsed.terms, [])

    def test_nearby_alone(self):
        parsed = parse_query("abandoned asylums nearby")
        self.assertTrue(parsed.near_me)
        self.assertIn("abandoned", parsed.terms)
        self.assertIn("asylums", parsed.terms)

    def test_close_to_me(self):
        parsed = parse_query("photos close to me")
        self.assertEqual(parsed.types, {"photos"})
        self.assertTrue(parsed.near_me)

    def test_around_me(self):
        parsed = parse_query("wikis around me")
        self.assertEqual(parsed.types, {"wikis"})
        self.assertTrue(parsed.near_me)

    def test_near_a_place_is_not_near_me(self):
        parsed = parse_query("pins near Cincinnati")
        self.assertFalse(parsed.near_me)
        self.assertEqual(parsed.place, "cincinnati")

    def test_by_me_is_not_near_me(self):
        # "by" alone is too ambiguous with authorship ("photos taken by me").
        parsed = parse_query("photos taken by me")
        self.assertFalse(parsed.near_me)

    def test_messages_from_person(self):
        parsed = parse_query("messages from alice")
        self.assertEqual(parsed.types, {"messages"})
        self.assertEqual(parsed.person, "alice")
        self.assertEqual(parsed.terms, [])

    def test_dms_from_person_synonym(self):
        parsed = parse_query("dms from bob")
        self.assertEqual(parsed.types, {"messages"})
        self.assertEqual(parsed.person, "bob")

    def test_from_person_ignored_without_messages_type(self):
        # "from" is only treated as a person clause alongside the messages type.
        parsed = parse_query("photos from paris")
        self.assertIsNone(parsed.person)
        self.assertIn("paris", parsed.terms)

    def test_describe_filters_mentions_near_me_and_person(self):
        self.assertIn("near you", parse_query("pins near me").describe_filters())
        self.assertIn("from Alice", parse_query("messages from alice").describe_filters())


class ParseQueryPropertyTests(TestCase):
    """Property-based robustness: the parser never raises and keeps invariants."""

    @settings(max_examples=60, deadline=None)
    @given(st.text(max_size=120))
    def test_never_raises_and_terms_are_lowercase(self, raw: str):
        parsed = parse_query(raw)
        self.assertEqual(parsed.raw, raw)
        for term in parsed.terms:
            self.assertEqual(term, term.lower())

    @settings(max_examples=60, deadline=None)
    @given(st.text(max_size=120))
    def test_date_range_is_ordered(self, raw: str):
        parsed = parse_query(raw)
        if parsed.date_start and parsed.date_end:
            self.assertLessEqual(parsed.date_start, parsed.date_end)

    @settings(max_examples=40, deadline=None)
    @given(st.text(alphabet="abcdefghijklmnopqrstuvwxyz ", min_size=1, max_size=60))
    def test_types_are_known_slugs(self, raw: str):
        from urbanlens.dashboard.services.global_search.results import RESULT_TYPES

        parsed = parse_query(raw)
        for slug in parsed.types:
            self.assertIn(slug, RESULT_TYPES)

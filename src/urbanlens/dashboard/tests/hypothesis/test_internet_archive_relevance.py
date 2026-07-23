"""Tests for InternetArchiveGateway's query construction and relevance filtering.

Covers the fix for archive.org returning content that merely mentions a few of
the location's keywords in isolation (broadcast transcripts, OCR'd book
bodies, cable-news segments about an unrelated person who shares a name with
the town) instead of material actually catalogued as being about the place.

The root cause was that ``advancedsearch.php`` rewrites a bare keyword query
into an OR over its **full-text** ``text:`` field; the fix builds an explicit
field-scoped boolean query instead. See
``services.apis.assets.internet_archive`` for the full write-up, including the
live ``responseHeader.params.query`` rewrites that motivated each choice.
"""

from __future__ import annotations

import re
from typing import Any
from unittest.mock import patch

from hypothesis import given, settings as hypothesis_settings, strategies as st

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.services.apis.assets.internet_archive import _EXCLUDED_COLLECTIONS, InternetArchiveGateway
from urbanlens.dashboard.services.external_data import MediaPanelSource

#: Matches one ``"..."`` phrase literal, honouring backslash escapes.
_PHRASE_LITERAL = re.compile(r'"(?:[^"\\]|\\.)*"')


def _doc(identifier: str, title: str = "", description: str = "", subject: Any = None) -> dict[str, Any]:
    """A result dict shaped like ``InternetArchiveGateway.search`` returns."""
    return {
        "identifier": identifier,
        "title": title,
        "description": description,
        "date": "",
        "mediatype": "image",
        "creator": "",
        "subject": subject if subject is not None else [],
    }


class BuildQueryTests(SimpleTestCase):
    """The ``q`` parameter is a field-scoped conjunction, not free text."""

    def test_name_is_scoped_to_title_and_subject_only(self) -> None:
        """Description is deliberately excluded - an incidental prose mention
        of the name is a false positive, a title/subject hit is not."""
        query = InternetArchiveGateway.build_query("Bannerman Castle")
        self.assertIn('title:"Bannerman Castle"', query)
        self.assertIn('subject:"Bannerman Castle"', query)
        self.assertNotIn('description:"Bannerman Castle"', query)

    def test_locality_qualifier_is_a_separate_anded_clause(self) -> None:
        query = InternetArchiveGateway.build_query("Widow Jane Mine", ["Rosendale New York"])
        self.assertIn('(title:"Widow Jane Mine" OR subject:"Widow Jane Mine")', query)
        self.assertIn('title:"Rosendale New York"', query)
        self.assertIn('coverage:"Rosendale New York"', query)
        # Both concepts required, never OR'd together at the top level.
        self.assertIn(") AND (", query)

    def test_no_bare_keywords_survive_into_the_query(self) -> None:
        """Every search word sits inside a ``field:"phrase"`` literal. A word
        left outside one falls through to archive.org's full-text ``text:``
        default field, which is what surfaced broadcast transcripts."""
        query = InternetArchiveGateway.build_query("Kings Park Psychiatric Center", ["Kings Park NY"])
        outside_phrases = _PHRASE_LITERAL.sub(" ", query)
        # Only field names, boolean operators, the mediatype/collection filter
        # values, and punctuation may remain.
        allowed = {"AND", "OR", "NOT", "title:", "subject:", "description:", "coverage:", "mediatype:", "collection:", "image", "movies", *_EXCLUDED_COLLECTIONS}
        leftover = [token for token in re.split(r"[()\s]+", outside_phrases) if token and token not in allowed]
        self.assertEqual(leftover, [])

    def test_media_type_and_collection_filters_are_always_applied(self) -> None:
        query = InternetArchiveGateway.build_query("Bannerman Castle")
        self.assertIn("mediatype:(image OR movies)", query)
        self.assertIn("NOT collection:(", query)
        self.assertIn("TV-NEWS", query)
        self.assertIn("tvarchive", query)

    def test_empty_name_produces_no_query(self) -> None:
        self.assertEqual(InternetArchiveGateway.build_query(""), "")
        self.assertEqual(InternetArchiveGateway.build_query("   "), "")

    def test_blank_qualifiers_are_dropped(self) -> None:
        query = InternetArchiveGateway.build_query("Bannerman Castle", ["", "  "])
        self.assertNotIn('title:""', query)

    def test_embedded_quotes_are_escaped_not_left_to_break_the_query(self) -> None:
        """An unescaped quote would terminate the phrase early and turn the
        remainder into bare full-text keywords - the exact failure being fixed."""
        query = InternetArchiveGateway.build_query('The "Old" Mill')
        self.assertIn('title:"The \\"Old\\" Mill"', query)


class BuildQueryPropertyTests(SimpleTestCase):
    """Property-based guards on the generated query string."""

    @hypothesis_settings(deadline=None, max_examples=200)
    @given(st.text(min_size=1, max_size=40).filter(lambda s: s.strip() != ""))
    def test_filters_are_present_for_any_name(self, name: str) -> None:
        query = InternetArchiveGateway.build_query(name)
        self.assertIn("mediatype:(image OR movies)", query)
        self.assertIn("NOT collection:(", query)

    @hypothesis_settings(deadline=None, max_examples=200)
    @given(st.text(min_size=1, max_size=40).filter(lambda s: s.strip() != ""))
    def test_phrase_literals_stay_balanced(self, name: str) -> None:
        """Escaping must leave an even number of *unescaped* quotes, i.e. every
        phrase literal opens and closes - otherwise the tail of the query is
        reinterpreted as unscoped keywords."""
        query = InternetArchiveGateway.build_query(name, ["Cincinnati OH"])
        unescaped = len(query.replace("\\\\", "").replace('\\"', "").split('"')) - 1
        self.assertEqual(unescaped % 2, 0)


class SplitSearchTermTests(SimpleTestCase):
    """The single query string is recovered as name + locality qualifiers."""

    def test_quoted_name_and_locality_are_split(self) -> None:
        name, qualifiers = InternetArchiveGateway._split_search_term('"Widow Jane Mine" "Rosendale New York"')
        self.assertEqual(name, "Widow Jane Mine")
        self.assertEqual(qualifiers, ["Rosendale New York"])

    def test_unquoted_term_is_treated_entirely_as_the_name(self) -> None:
        name, qualifiers = InternetArchiveGateway._split_search_term("Bannerman Castle")
        self.assertEqual(name, "Bannerman Castle")
        self.assertEqual(qualifiers, [])

    def test_search_terms_feeds_split_the_shape_it_expects(self) -> None:
        """End-to-end: what MediaPanelSource builds is what the gateway parses."""
        location = Location(latitude="41.926", longitude="-73.996", locality="Rosendale", administrative_area_level_1="NY", official_name="Widow Jane Mine")
        pin = Pin()
        pin._state.fields_cache["location"] = location
        terms = MediaPanelSource.search_terms(pin, InternetArchiveGateway())
        self.assertEqual(terms, ['"Widow Jane Mine" "Rosendale NY"'])
        self.assertEqual(InternetArchiveGateway._split_search_term(terms[0]), ("Widow Jane Mine", ["Rosendale NY"]))


class RelevanceFilterTests(SimpleTestCase):
    """``_is_relevant`` re-checks the match against the item's own metadata."""

    def test_title_match_is_relevant(self) -> None:
        self.assertTrue(InternetArchiveGateway._is_relevant(_doc("a", title="The ruins of Bannerman Castle"), "Bannerman Castle"))

    def test_subject_match_is_relevant(self) -> None:
        self.assertTrue(InternetArchiveGateway._is_relevant(_doc("a", title="Untitled", subject=["Ruins", "Bannerman Castle"]), "Bannerman Castle"))

    def test_description_only_mention_is_not_relevant(self) -> None:
        """The reported symptom in miniature: an item that merely name-drops
        the location in prose is not about the location."""
        doc = _doc("a", title="EmpoweringWomenEverywhere", description="The Bannerman Castle Trust, Inc. is a not-for-profit ...")
        self.assertFalse(InternetArchiveGateway._is_relevant(doc, "Bannerman Castle"))

    def test_unrelated_item_is_not_relevant(self) -> None:
        self.assertFalse(InternetArchiveGateway._is_relevant(_doc("a", title="VOA Africa : November 13, 2017"), "Summit Road"))

    def test_punctuation_and_case_differences_still_match(self) -> None:
        self.assertTrue(InternetArchiveGateway._is_relevant(_doc("a", title="ST. MARK'S CHURCH, exterior"), "St Marks Church"))

    def test_empty_name_never_matches(self) -> None:
        self.assertFalse(InternetArchiveGateway._is_relevant(_doc("a", title="anything"), ""))


class GenerateMediaTests(SimpleTestCase):
    """Query strategy: when the locality is required, and when it is relaxed."""

    def test_street_type_name_without_locality_is_not_searched_at_all(self) -> None:
        """"Summit Road" names a road in every state; with nothing to narrow
        it, any result is coincidence - so no call is made."""
        with patch.object(InternetArchiveGateway, "search") as mock_search:
            items = list(InternetArchiveGateway()._generate_media('"Summit Road"'))
        self.assertEqual(items, [])
        mock_search.assert_not_called()

    def test_street_type_name_with_locality_never_falls_back_to_name_only(self) -> None:
        """The broad query is exactly what returned nationwide noise, so a
        generic name gets one narrow attempt and nothing more."""
        with patch.object(InternetArchiveGateway, "search", return_value=[]) as mock_search:
            items = list(InternetArchiveGateway()._generate_media('"Summit Road" "Cincinnati OH"'))
        self.assertEqual(items, [])
        self.assertEqual(mock_search.call_count, 1)
        self.assertIn('coverage:"Cincinnati OH"', mock_search.call_args.args[0])

    def test_distinctive_name_falls_back_to_name_only_when_locality_finds_nothing(self) -> None:
        """archive.org rarely records a city/state for historical photographs,
        so a distinctive name is allowed to stand on its own."""
        calls: list[str] = []

        def fake_search(self: InternetArchiveGateway, query: str, *, rows: int = 20) -> list[dict[str, Any]]:
            calls.append(query)
            return [] if "coverage:" in query else [_doc("ruins", title="The ruins of Bannerman Castle")]

        with patch.object(InternetArchiveGateway, "search", fake_search):
            items = list(InternetArchiveGateway()._generate_media('"Bannerman Castle" "Fishkill NY"'))

        self.assertEqual(len(calls), 2)
        self.assertIn('coverage:"Fishkill NY"', calls[0])
        self.assertNotIn("coverage:", calls[1])
        self.assertEqual([item.caption for item in items], ["The ruins of Bannerman Castle"])

    def test_no_fallback_when_the_narrow_query_already_found_something(self) -> None:
        with patch.object(InternetArchiveGateway, "search", return_value=[_doc("x", title="Bannerman Castle, Pollepel Island")]) as mock_search:
            items = list(InternetArchiveGateway()._generate_media('"Bannerman Castle" "Fishkill NY"'))
        self.assertEqual(mock_search.call_count, 1)
        self.assertEqual(len(items), 1)

    def test_irrelevant_results_are_dropped_even_when_the_api_returns_them(self) -> None:
        """If archive.org's server-side rewrite ever loosens again, the local
        check still keeps unrelated material out of the gallery."""
        docs = [_doc("good", title="Eastern State Penitentiary, cellblock 7"), _doc("bad", title="VOA Africa : August 01, 2019")]
        with patch.object(InternetArchiveGateway, "search", return_value=docs):
            items = list(InternetArchiveGateway()._generate_media('"Eastern State Penitentiary"'))
        self.assertEqual([item.caption for item in items], ["Eastern State Penitentiary, cellblock 7"])

    def test_items_carry_thumbnail_and_page_links(self) -> None:
        with patch.object(InternetArchiveGateway, "search", return_value=[_doc("ruins", title="The ruins of Bannerman Castle")]):
            (item,) = list(InternetArchiveGateway()._generate_media('"Bannerman Castle"'))
        self.assertEqual(item.url, "https://archive.org/details/ruins")
        self.assertEqual(item.thumb_url, "https://archive.org/services/img/ruins")
        self.assertEqual(item.page_url, "https://archive.org/details/ruins")
        self.assertEqual(item.source, "Internet Archive")

    def test_empty_search_term_yields_nothing(self) -> None:
        with patch.object(InternetArchiveGateway, "search") as mock_search:
            self.assertEqual(list(InternetArchiveGateway()._generate_media("")), [])
        mock_search.assert_not_called()

    def test_duplicate_identifiers_across_attempts_are_not_yielded_twice(self) -> None:
        with patch.object(InternetArchiveGateway, "search", return_value=[_doc("x", title="Bannerman Castle"), _doc("x", title="Bannerman Castle")]):
            items = list(InternetArchiveGateway()._generate_media('"Bannerman Castle" "Fishkill NY"'))
        self.assertEqual(len(items), 1)

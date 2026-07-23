"""Tests for the public Flickr search Media gallery provider.

Distinct from test_flickr.py (per-user OAuth library) and
test_flickr_album_import.py (public album by URL) - this covers:

- build_search_query - required-operator query assembly from pin/wiki names,
  aliases (nickname exclusion, address-derived exclusion, dedup), and state.
- FlickrSearchGateway - unauthenticated flickr.photos.search calls (used when
  an API key is configured), error handling, MediaItem mapping.
- build_feed_tag_queries / FlickrFeedSearchGateway - the keyless fallback
  (used when no API key is configured): tag-AND query decomposition and the
  public syndication feed calls.
- FlickrMediaPanelSource - search_terms dispatch by active gateway, gate.
- FlickrPlugin.get_panel_sources - picks the API gateway or the feed fallback
  based on whether a key is configured.

All HTTP calls are mocked; no real network access occurs.
"""

from __future__ import annotations

from decimal import Decimal
from unittest import mock

from hypothesis import given, strategies as st
from model_bakery import baker

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.models.aliases.model import AliasType, PinAlias, WikiAlias
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.plugins.builtin.flickr import FlickrPlugin
from urbanlens.dashboard.services.apis.flickr import search as flickr_search
from urbanlens.dashboard.services.apis.flickr.search import (
    FlickrFeedSearchGateway,
    FlickrMediaPanelSource,
    FlickrSearchGateway,
    build_feed_tag_queries,
    build_search_query,
)


def _mock_response(*, ok: bool = True, status_code: int = 200, json_data=None):
    resp = mock.MagicMock()
    resp.ok = ok
    resp.status_code = status_code
    resp.json.return_value = json_data
    resp.raise_for_status = mock.MagicMock() if ok else mock.MagicMock(side_effect=Exception("http error"))
    return resp


# -- _quoted_or_group ----------------------------------------------------------------


class QuotedOrGroupTests(SimpleTestCase):
    def test_empty_list_returns_empty_string(self) -> None:
        self.assertEqual(flickr_search._quoted_or_group([]), "")

    def test_dedupes_case_insensitively_preserving_first_occurrence(self) -> None:
        result = flickr_search._quoted_or_group(["HRSH", "hrsh", "HRPC"])
        self.assertEqual(result, '("HRSH" OR "HRPC")')

    def test_strips_embedded_quotes_so_the_group_stays_balanced(self) -> None:
        result = flickr_search._quoted_or_group(['The "Haunted" Hospital'])
        self.assertEqual(result, '("The Haunted Hospital")')

    @given(st.lists(st.text(min_size=0, max_size=20)))
    def test_output_is_always_balanced_or_empty(self, terms: list[str]) -> None:
        result = flickr_search._quoted_or_group(terms)
        if result:
            self.assertTrue(result.startswith("(") and result.endswith(")"))
            # Every quote in the assembled group opens or closes a term - an
            # odd count would mean a stray, unbalanced quote leaked through.
            self.assertEqual(result.count('"') % 2, 0)


# -- build_search_query --------------------------------------------------------------


class BuildSearchQueryTests(TestCase):
    def setUp(self) -> None:
        self.location = baker.make(
            "dashboard.Location",
            latitude=Decimal("41.700000"),
            longitude=Decimal("-73.930000"),
            administrative_area_level_1="New York",
        )
        self.pin = baker.make_recipe("dashboard.pin", location=self.location, name="Hudson River State Hospital")

    def test_includes_name_state_and_urbex_terms(self) -> None:
        query = build_search_query(self.pin)
        self.assertIn('"Hudson River State Hospital"', query)
        self.assertIn('"New York"', query)
        self.assertIn('"abandoned"', query)
        self.assertIn('"urbex"', query)
        self.assertIn('"urban exploration"', query)
        self.assertIn(" OR ", query)

    def test_no_state_returns_none(self) -> None:
        self.location.administrative_area_level_1 = ""
        self.location.save(update_fields=["administrative_area_level_1", "updated"])
        self.assertIsNone(build_search_query(self.pin))

    def test_no_meaningful_name_returns_none(self) -> None:
        # A fresh pin/location with no name at all - unlike clearing
        # self.pin's name after setUp, this has no lingering PinAlias from
        # Pin.save()'s auto-alias-on-meaningful-name-change sync.
        nameless_location = baker.make(
            "dashboard.Location",
            latitude=Decimal("41.800000"),
            longitude=Decimal("-73.800000"),
            administrative_area_level_1="New York",
            official_name="",
        )
        nameless_pin = baker.make_recipe("dashboard.pin", location=nameless_location, name="")
        self.assertIsNone(build_search_query(nameless_pin))

    def test_includes_non_nickname_pin_aliases(self) -> None:
        baker.make(PinAlias, pin=self.pin, name="HRSH", kind=AliasType.ALTERNATE)
        query = build_search_query(self.pin)
        self.assertIn('"HRSH"', query)

    def test_excludes_nickname_pin_aliases(self) -> None:
        baker.make(PinAlias, pin=self.pin, name="My Secret Spot", kind=AliasType.NICKNAME)
        query = build_search_query(self.pin)
        self.assertNotIn("My Secret Spot", query)

    def test_includes_non_nickname_wiki_aliases(self) -> None:
        wiki = baker.make(Wiki, location=self.location, name="Hudson River State Hospital")
        baker.make(WikiAlias, wiki=wiki, name="Hudson River Psychiatric Center", kind=AliasType.OFFICIAL)
        query = build_search_query(self.pin)
        self.assertIn('"Hudson River Psychiatric Center"', query)

    def test_excludes_nickname_wiki_aliases(self) -> None:
        wiki = baker.make(Wiki, location=self.location, name="Hudson River State Hospital")
        baker.make(WikiAlias, wiki=wiki, name="that creepy place", kind=AliasType.NICKNAME)
        query = build_search_query(self.pin)
        self.assertNotIn("that creepy place", query)

    def test_excludes_address_derived_alias_names(self) -> None:
        self.location.locality = "Poughkeepsie"
        self.location.save(update_fields=["locality", "updated"])
        baker.make(PinAlias, pin=self.pin, name="Poughkeepsie", kind=AliasType.ALTERNATE)
        query = build_search_query(self.pin)
        # "Poughkeepsie" alone identifies the surroundings, not the place -
        # it must not appear as a standalone quoted term in the name group.
        self.assertNotIn('"Poughkeepsie"', query)

    def test_no_wiki_does_not_raise(self) -> None:
        query = build_search_query(self.pin)
        self.assertIsNotNone(query)


# -- FlickrSearchGateway --------------------------------------------------------------


class FlickrSearchGatewayTests(TestCase):
    def _gateway(self) -> FlickrSearchGateway:
        return FlickrSearchGateway(session=mock.MagicMock())

    def test_generate_media_yields_items_from_search_results(self) -> None:
        gw = self._gateway()
        gw.session.get.return_value = _mock_response(
            json_data={
                "stat": "ok",
                "photos": {
                    "photo": [
                        {"id": "1", "owner": "12345@N00", "title": "Main Building", "url_o": "https://example.com/1_o.jpg", "url_s": "https://example.com/1_s.jpg"},
                        {"id": "2", "owner": "12345@N00", "title": "No usable size"},
                    ],
                },
            },
        )
        with mock.patch("urbanlens.dashboard.services.apis.flickr.search._consumer_credentials", return_value=("key", "secret")):
            items = list(gw._generate_media('("Hudson River State Hospital") "New York" ("abandoned")'))

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].url, "https://example.com/1_o.jpg")
        self.assertEqual(items[0].caption, "Main Building")
        self.assertEqual(items[0].page_url, "https://www.flickr.com/photos/12345@N00/1/")

    def test_empty_search_term_yields_nothing_without_a_call(self) -> None:
        gw = self._gateway()
        items = list(gw._generate_media(""))
        self.assertEqual(items, [])
        gw.session.get.assert_not_called()

    def test_not_configured_returns_no_results_instead_of_raising(self) -> None:
        gw = self._gateway()
        with mock.patch("urbanlens.dashboard.services.apis.flickr.search._consumer_credentials", side_effect=flickr_search.FlickrNotConfiguredError()):
            items = list(gw._generate_media("some query"))
        self.assertEqual(items, [])

    def test_flickr_error_status_returns_no_results(self) -> None:
        gw = self._gateway()
        gw.session.get.return_value = _mock_response(json_data={"stat": "fail", "message": "boom"})
        with mock.patch("urbanlens.dashboard.services.apis.flickr.search._consumer_credentials", return_value=("key", "secret")):
            items = list(gw._generate_media("some query"))
        self.assertEqual(items, [])

    def test_http_error_returns_no_results(self) -> None:
        gw = self._gateway()
        gw.session.get.return_value = _mock_response(ok=False, status_code=500)
        with mock.patch("urbanlens.dashboard.services.apis.flickr.search._consumer_credentials", return_value=("key", "secret")):
            items = list(gw._generate_media("some query"))
        self.assertEqual(items, [])


# -- build_feed_tag_queries ------------------------------------------------------------


class BuildFeedTagQueriesTests(TestCase):
    def setUp(self) -> None:
        self.location = baker.make(
            "dashboard.Location",
            latitude=Decimal("41.700000"),
            longitude=Decimal("-73.930000"),
            administrative_area_level_1="New York",
        )
        self.pin = baker.make_recipe("dashboard.pin", location=self.location, name="Hudson River State Hospital")

    def test_crosses_names_with_every_urbex_term(self) -> None:
        baker.make(PinAlias, pin=self.pin, name="HRSH", kind=AliasType.ALTERNATE)
        queries = build_feed_tag_queries(self.pin)
        # 2 names (own name + alias) x 3 urbex terms.
        self.assertEqual(len(queries), 6)
        self.assertIn("hudsonriverstatehospital,newyork,abandoned", queries)
        self.assertIn("hudsonriverstatehospital,newyork,urbex", queries)
        self.assertIn("hudsonriverstatehospital,newyork,urbanexploration", queries)
        self.assertIn("hrsh,newyork,abandoned", queries)

    def test_no_state_returns_empty_list(self) -> None:
        self.location.administrative_area_level_1 = ""
        self.location.save(update_fields=["administrative_area_level_1", "updated"])
        self.assertEqual(build_feed_tag_queries(self.pin), [])

    def test_dedupes_names_that_normalize_to_the_same_tag(self) -> None:
        # "H.R.S.H." and "HRSH" both normalize to the same tag token.
        baker.make(PinAlias, pin=self.pin, name="H.R.S.H.", kind=AliasType.ALTERNATE)
        baker.make(PinAlias, pin=self.pin, name="HRSH", kind=AliasType.ALTERNATE)
        queries = build_feed_tag_queries(self.pin)
        hrsh_queries = [q for q in queries if q.startswith("hrsh,")]
        self.assertEqual(len(hrsh_queries), 3)  # one per urbex term, not six

    def test_caps_distinct_names_queried(self) -> None:
        for i in range(10):
            baker.make(PinAlias, pin=self.pin, name=f"Alias Number {i}", kind=AliasType.ALTERNATE)
        queries = build_feed_tag_queries(self.pin)
        distinct_names = {q.split(",")[0] for q in queries}
        self.assertLessEqual(len(distinct_names), flickr_search._FEED_MAX_NAMES)


# -- FlickrFeedSearchGateway -----------------------------------------------------------


class FlickrFeedSearchGatewayTests(TestCase):
    def _gateway(self) -> FlickrFeedSearchGateway:
        return FlickrFeedSearchGateway(session=mock.MagicMock())

    def test_generate_media_yields_items_with_rebuilt_page_url(self) -> None:
        gw = self._gateway()
        gw.session.get.return_value = _mock_response(
            json_data={
                "items": [
                    {
                        "title": "Main Building",
                        "link": "https://www.flickr.com/photos/some-alias/55410591850/",
                        "media": {"m": "https://live.staticflickr.com/1_m.jpg"},
                        "author": 'nobody@flickr.com ("Someone")',
                        "author_id": "12345@N00",
                        "tags": "hudsonriverstatehospital newyork abandoned",
                    },
                    {"title": "No usable media", "link": "https://www.flickr.com/photos/some-alias/2/", "author_id": "12345@N00"},
                ],
            },
        )
        items = list(gw._generate_media("hudsonriverstatehospital,newyork,abandoned"))

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].url, "https://live.staticflickr.com/1_m.jpg")
        self.assertEqual(items[0].caption, "Main Building")
        # Rebuilt from author_id + the numeric id in `link`, not `link` itself -
        # keeps the dedup key consistent with the NSID-based form the other
        # two Flickr paths store (see photo_web_url).
        self.assertEqual(items[0].page_url, "https://www.flickr.com/photos/12345@N00/55410591850/")

    def test_empty_search_term_yields_nothing_without_a_call(self) -> None:
        gw = self._gateway()
        items = list(gw._generate_media(""))
        self.assertEqual(items, [])
        gw.session.get.assert_not_called()

    def test_http_error_returns_no_results(self) -> None:
        gw = self._gateway()
        gw.session.get.return_value = _mock_response(ok=False, status_code=500)
        items = list(gw._generate_media("abandoned,newyork,urbex"))
        self.assertEqual(items, [])

    def test_tagmode_all_is_used_so_tags_are_anded(self) -> None:
        gw = self._gateway()
        gw.session.get.return_value = _mock_response(json_data={"items": []})
        list(gw._generate_media("abandoned,newyork,urbex"))
        _args, kwargs = gw.session.get.call_args
        self.assertEqual(kwargs["params"]["tagmode"], "all")
        self.assertEqual(kwargs["params"]["tags"], "abandoned,newyork,urbex")


# -- FlickrMediaPanelSource ------------------------------------------------------------


class FlickrMediaPanelSourceTests(TestCase):
    def setUp(self) -> None:
        self.location = baker.make(
            "dashboard.Location",
            latitude=Decimal("41.700000"),
            longitude=Decimal("-73.930000"),
            administrative_area_level_1="New York",
        )
        self.pin = baker.make_recipe("dashboard.pin", location=self.location, name="Hudson River State Hospital")
        self.source = FlickrMediaPanelSource("flickr", FlickrSearchGateway.service_key, FlickrSearchGateway)

    def test_search_terms_wraps_the_built_query_for_the_api_gateway(self) -> None:
        terms = self.source.search_terms(self.pin, FlickrSearchGateway())
        self.assertEqual(terms, [build_search_query(self.pin)])

    def test_search_terms_uses_tag_queries_for_the_feed_gateway(self) -> None:
        terms = self.source.search_terms(self.pin, FlickrFeedSearchGateway())
        self.assertEqual(terms, build_feed_tag_queries(self.pin))

    def test_gate_false_when_pin_has_no_state(self) -> None:
        self.location.administrative_area_level_1 = ""
        self.location.save(update_fields=["administrative_area_level_1", "updated"])
        self.assertFalse(self.source.gate(self.pin))

    def test_gate_true_when_pin_has_a_name_and_state(self) -> None:
        self.assertTrue(self.source.gate(self.pin))


# -- FlickrPlugin.get_panel_sources ------------------------------------------------------

class FlickrPluginPanelSourceTests(SimpleTestCase):
    """The plugin picks the API gateway or the keyless feed fallback per current config."""

    def _factory(self):
        (source,) = FlickrPlugin().get_panel_sources()
        return source.make_gateway

    def test_uses_api_gateway_when_configured(self) -> None:
        with mock.patch("urbanlens.dashboard.services.apis.flickr.oauth.is_configured", return_value=True):
            gateway = self._factory()()
        self.assertIsInstance(gateway, FlickrSearchGateway)

    def test_uses_feed_gateway_when_not_configured(self) -> None:
        with mock.patch("urbanlens.dashboard.services.apis.flickr.oauth.is_configured", return_value=False):
            gateway = self._factory()()
        self.assertIsInstance(gateway, FlickrFeedSearchGateway)

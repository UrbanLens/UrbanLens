"""The News panel asks GDELT about the place by its real names and shows only articles about it.

Reproduces the staging report on the Hudson River State Hospital pin: the location's official name was
the service road it stands on ("Courtyard Drive"), the query was that street name alone, and GDELT's
translingual index answered with Chinese rural-revitalisation stories whose machine translations mention
a courtyard and "driving" development.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
import re
from typing import TYPE_CHECKING, Any
from unittest import mock

from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from model_bakery import baker

from hypothesis import given, strategies as st
from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.models.aliases.model import AliasType
from urbanlens.dashboard.models.cache.location_cache import (
    PARTIAL_ANSWER_STALE_AFTER,
    UNANSWERED_SOURCES_KEY,
    LocationCache,
)
from urbanlens.dashboard.plugins.builtin.gdelt import GdeltPanelSource
from urbanlens.dashboard.services.apis.locations.redata_context_gateway import LocationContextEnvelope
from urbanlens.dashboard.services.pins.external_data import fetch_blocked, run_panel_fetch
from urbanlens.dashboard.services.pins.news_query import MAX_QUERY_CHARACTERS, NewsQuery
from urbanlens.dashboard.services.pins.search_names import search_names
from urbanlens.dashboard.tests.hypothesis.redata_helpers import RedataConfiguredMixin

if TYPE_CHECKING:
    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.models.pin.model import Pin

_SEARCH_NEWS = "urbanlens.dashboard.services.apis.locations.redata_search_gateway.RedataNewsSearchGateway.search_news"

#: What staging's cache held for the HRSH pin, verbatim.
_STAGING_BATCH: list[dict[str, Any]] = [
    {
        "date": "20260719T170000Z",
        "snippet": "163.com",
        "title": "石峡村 长城情 六百年守护传文脉",
        "link": "https://www.163.com/a",
    },
    {
        "date": "20260912T064500Z",
        "snippet": "news.fznews.com.cn",
        "title": "晋安寿山乡文明乡风建设经验全省推广 _ 福州 _ 新闻频道 _ 福州新闻网",
        "link": "https://news.fznews.com.cn/b",
    },
    {
        "date": "20260902T053000Z",
        "snippet": "hebei.ifeng.com",
        "title": "石家庄市鹿泉区 ： 闲置小院微改造 ， 有看头更有赚头",
        "link": "https://hebei.ifeng.com/c",
    },
    {
        "date": "20260922T010000Z",
        "snippet": "baijiahao.baidu.com",
        "title": "工业向新 项目提速 乡村共富 巨野县下好  三步棋  推动县域经济高质量发展",
        "link": "https://baijiahao.baidu.com/d",
    },
    {
        "date": "20260709T050000Z",
        "snippet": "lyrb.com.cn",
        "title": "擎旗奋楫 实干兴浏丨葛家镇葛家园村马家党支部 ： 老人  炊事班  乡村  养护工  把小事干成暖心大事",
        "link": "https://lyrb.com.cn/e",
    },
    {
        "date": "20260812T040000Z",
        "snippet": "hljnews.cn",
        "title": "省政府新闻办举行我省巾帼兴粮节粮行动暨  美丽庭院 · 幸福家  建设工作进展新闻发布会 凝聚巾帼力量赋能乡村全面振兴",
        "link": "https://hljnews.cn/f",
    },
]

_RELEVANT = {
    "date": "20260801T120000Z",
    "snippet": "poughkeepsiejournal.com",
    "title": "Hudson River State Hospital redevelopment clears another hurdle",
    "link": "https://www.poughkeepsiejournal.com/hrsh",
}


def _answered(results: list[dict[str, Any]]) -> LocationContextEnvelope:
    """GDELT's own, complete answer."""
    return LocationContextEnvelope(count=len(results), complete=True, results=results)


def _hrsh_pin(*, city: str | None = "Poughkeepsie", state: str | None = "NY", country: str = "United States") -> Pin:
    """A pin shaped like staging's: named HRSH, on a Location whose official name is the service road."""
    location: Location = baker.make(
        "dashboard.Location",
        official_name="Courtyard Drive",
        latitude=Decimal("41.73266"),
        longitude=Decimal("-73.92736"),
        city=city,
        state=state,
        country=country,
    )
    pin: Pin = baker.make_recipe("dashboard.pin", location=location, name="HRSH", name_is_user_provided=True)
    baker.make("dashboard.PinAlias", pin=pin, name="Courtyard Drive", kind=AliasType.OFFICIAL, source="wiki_sync")
    LocationCache.set(
        location,
        "redata_historic_registers",
        {
            "resources": [
                {
                    "name": "Hudson River State Hospital, Main Building",
                    "scope": "structure",
                    "status": "Listed",
                    "provider": "nps_nrhp",
                    "resource_type": "building",
                    "contains_point": True,
                },
            ],
        },
        query_key="41.73266,-73.92736",
    )
    return pin


def _shared_query(pin: Pin) -> NewsQuery | None:
    return NewsQuery.for_pin(pin, search_names(pin).base)


def _own_query(pin: Pin) -> NewsQuery | None:
    own = search_names(pin).own
    return None if own is None else NewsQuery.for_pin(pin, own)


class NewsQueryTests(TestCase):
    """What the News panel sends to GDELT for a pin."""

    def test_queries_the_places_names_and_locality_not_its_street(self) -> None:
        pin = _hrsh_pin()
        shared, own = _shared_query(pin), _own_query(pin)

        assert shared is not None
        assert own is not None
        self.assertIn('"Hudson River State Hospital"', shared.gdelt_query())
        self.assertNotRegex(shared.gdelt_query(), r"(?i)\bHRSH\b")
        self.assertRegex(own.gdelt_query(), r"(?i)\bHRSH\b")
        for text in (shared.gdelt_query(), own.gdelt_query()):
            self.assertRegex(text, r"\bPoughkeepsie\b")
            self.assertNotIn("Courtyard", text)

    def test_single_words_are_not_quoted(self) -> None:
        """GDELT answers a quoted one-word phrase with "The specified phrase is too short." instead of results."""
        pin = _hrsh_pin()
        for query in (_shared_query(pin), _own_query(pin)):
            assert query is not None
            self.assertIsNone(re.search(r'"\w+"', query.gdelt_query()), query.gdelt_query())

    def test_restricts_language_and_source_country(self) -> None:
        query = _shared_query(_hrsh_pin())

        assert query is not None
        self.assertIn("sourcelang:english", query.gdelt_query())
        self.assertIn("sourcecountry:unitedstates", query.gdelt_query())

    def test_source_country_comes_from_the_coordinates_when_the_address_has_none(self) -> None:
        """Staging's HRSH location had no city, state or country at all."""
        query = _shared_query(_hrsh_pin(city=None, state=None, country=""))

        assert query is not None
        text = query.gdelt_query()
        self.assertIn("sourcecountry:unitedstates", text)
        self.assertIn('"Hudson River State Hospital"', text)
        self.assertNotIn("Courtyard", text)

    def test_no_query_when_the_only_names_are_street_names(self) -> None:
        location: Location = baker.make(
            "dashboard.Location", official_name="Courtyard Drive", latitude=Decimal("41.7"), longitude=Decimal("-73.9")
        )
        pin: Pin = baker.make_recipe("dashboard.pin", location=location, name=None)

        self.assertIsNone(_shared_query(pin))
        self.assertIsNone(search_names(pin).own)

    def test_a_one_word_register_head_is_not_a_place_name(self) -> None:
        """NRHP's "Roosevelt, Isaac, House" sits on the HRSH campus; "Roosevelt" alone would match every FDR story."""
        pin = _hrsh_pin()
        row = LocationCache.objects.get(location=pin.location, source="redata_historic_registers")
        row.data["resources"].insert(
            0,
            {
                "name": "Roosevelt, Isaac, House",
                "provider": "nps_nrhp",
                "resource_type": "building",
                "contains_point": True,
            },
        )
        row.save()

        query = _shared_query(pin)

        assert query is not None
        self.assertNotIn('"Roosevelt"', query.gdelt_query())
        self.assertIn('"Hudson River State Hospital"', query.gdelt_query())

    def test_a_listing_that_does_not_contain_the_point_names_nothing(self) -> None:
        """The Roosevelt house is half a kilometre off; only a listing whose boundary holds the pin names it."""
        pin = _hrsh_pin()
        row = LocationCache.objects.get(location=pin.location, source="redata_historic_registers")
        row.data["resources"].insert(
            0,
            {
                "name": "Roosevelt, Isaac, House",
                "provider": "nps_nrhp",
                "resource_type": "building",
                "contains_point": False,
            },
        )
        row.save()

        query = _shared_query(pin)

        assert query is not None
        self.assertNotIn("Roosevelt", query.gdelt_query())

    def test_the_query_fits_redatas_length_limit(self) -> None:
        pin = _hrsh_pin()
        for index in range(8):
            baker.make("dashboard.PinAlias", pin=pin, name=f"Alternate Campus Name Number {index} " + "x" * 60)

        query = _own_query(pin)

        assert query is not None
        self.assertLessEqual(len(query.gdelt_query()), MAX_QUERY_CHARACTERS)
        self.assertRegex(query.gdelt_query(), r"(?i)alternate campus name number")

    def test_nickname_aliases_stay_out_of_the_query(self) -> None:
        pin = _hrsh_pin()
        baker.make("dashboard.PinAlias", pin=pin, name="Grandmas Spooky Castle", kind=AliasType.NICKNAME)

        for query in (_shared_query(pin), _own_query(pin)):
            assert query is not None
            self.assertNotIn("spooky", query.gdelt_query().casefold())


class NewsPanelFetchTests(RedataConfiguredMixin, TestCase):
    """GdeltPanelSource end to end, with REData's answer replaced by the staging batch."""

    def test_only_the_relevant_article_is_shown(self) -> None:
        pin = _hrsh_pin()
        source = GdeltPanelSource()

        with mock.patch(_SEARCH_NEWS, return_value=_answered([*_STAGING_BATCH, _RELEVANT])) as search:
            source.fetch(pin)

        sent = [call.args[0] for call in search.call_args_list]
        self.assertEqual(len(sent), 2, "the shared search and the one for the pin's own name")
        self.assertIn('"Hudson River State Hospital"', sent[0])
        for text in sent:
            self.assertRegex(text, r"\bPoughkeepsie\b")
        data = source.cached_data(pin)
        assert data is not None
        context = source.render_context(pin, data)
        assert context is not None
        self.assertEqual([row["value"] for row in context["meta"]], [_RELEVANT["title"]])

    def test_nothing_is_shown_when_nothing_is_relevant(self) -> None:
        pin = _hrsh_pin()
        source = GdeltPanelSource()

        with mock.patch(_SEARCH_NEWS, return_value=_answered(list(_STAGING_BATCH))):
            source.fetch(pin)

        data = source.cached_data(pin)
        assert data is not None
        self.assertIsNone(source.render_context(pin, data))

    def test_a_batch_cached_before_the_fix_is_filtered_when_rendered(self) -> None:
        """A row fetched by an older query is judged against the pin's current names."""
        pin = _hrsh_pin()
        source = GdeltPanelSource()

        context = source.render_context(pin, {"articles": [*_STAGING_BATCH, _RELEVANT]})

        assert context is not None
        self.assertEqual([row["value"] for row in context["meta"]], [_RELEVANT["title"]])

    def test_the_pre_fix_cache_row_is_not_read(self) -> None:
        """Staging's bad rows sit under the old cache source; the panel must fetch afresh rather than show them."""
        pin = _hrsh_pin()
        LocationCache.set(pin.location, "gdelt", {"articles": list(_STAGING_BATCH)}, query_key='"Courtyard Drive"')

        self.assertIsNone(GdeltPanelSource().cached_data(pin))

    def test_the_query_is_recorded_on_the_cache_row(self) -> None:
        pin = _hrsh_pin()
        source = GdeltPanelSource()

        with mock.patch(_SEARCH_NEWS, return_value=_answered([])) as search:
            source.fetch(pin)

        rows = LocationCache.objects.filter(location=pin.location, source=source.cache_source)
        self.assertEqual(
            sorted(rows.values_list("query_key", flat=True)),
            sorted(call.args[0][:255] for call in search.call_args_list),
        )

    def test_no_redata_call_without_a_usable_name(self) -> None:
        location: Location = baker.make(
            "dashboard.Location", official_name="Courtyard Drive", latitude=Decimal("41.7"), longitude=Decimal("-73.9")
        )
        pin: Pin = baker.make_recipe("dashboard.pin", location=location, name=None)

        with mock.patch(_SEARCH_NEWS) as search:
            GdeltPanelSource().fetch(pin)

        search.assert_not_called()
        self.assertEqual(GdeltPanelSource().cached_data(pin), {"articles": []})


class NewsPanelEndpointTests(RedataConfiguredMixin, TestCase):
    """The rendered panel, through the view."""

    def test_the_panel_lists_only_the_relevant_article(self) -> None:
        user = baker.make(User)
        self.client.force_login(user)
        pin = _hrsh_pin()
        pin.profile = user.profile
        pin.save()
        LocationCache.set(pin.location, GdeltPanelSource.cache_source, {"articles": [*_STAGING_BATCH, _RELEVANT]})
        LocationCache.set(
            pin.location, GdeltPanelSource.cache_source, {"articles": []}, audience=search_names(pin).audience
        )

        response = self.client.get(reverse("pin.panel", args=[pin.slug, "gdelt"]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, _RELEVANT["link"])
        for article in _STAGING_BATCH:
            self.assertNotContains(response, article["link"])


_GET_JSON = "urbanlens.dashboard.services.apis.locations.redata_search_gateway.RedataNewsSearchGateway.get_json"


def _news_body(results: list[dict[str, Any]], *, gdelt: str | None) -> dict[str, Any]:
    """REData's ``/search/news/`` body: 0.3.7's with GDELT in state ``gdelt``, or 0.3.6's when ``gdelt`` is None."""
    body: dict[str, Any] = {"count": len(results), "results": results, "provider": "gdelt", "query": "q", "months": 24}
    if gdelt is None:
        return body
    answered = gdelt == "ok"
    return {
        **body,
        "complete": answered,
        "provider": "gdelt" if answered else "searxng",
        "degraded": not answered,
        "providers": [
            {"provider": "gdelt", "status": gdelt, "count": len(results) if answered else 0, "message": None},
            {
                "provider": "searxng",
                "status": "not_attempted" if answered else "ok",
                "count": 0 if answered else len(results),
                "message": None,
            },
        ],
    }


class NewsPartialAnswerTests(RedataConfiguredMixin, TestCase):
    """A news answer from SearXNG standing in for GDELT is a floor (UrbanLens N47 item 6): shown, never kept as "no news"."""

    def _fetch(self, body: dict[str, Any]) -> Pin:
        pin = _hrsh_pin()
        with mock.patch(_GET_JSON, return_value=body):
            run_panel_fetch(GdeltPanelSource.key, pin)
        return pin

    def _rows(self, pin: Pin) -> list[LocationCache]:
        return list(LocationCache.objects.filter(location=pin.location, source=GdeltPanelSource.cache_source))

    @staticmethod
    def _an_hour_later() -> mock._patch:
        return mock.patch(
            "django.utils.timezone.now", return_value=timezone.now() + PARTIAL_ANSWER_STALE_AFTER + timedelta(minutes=1)
        )

    def test_a_fallbacks_articles_are_shown_and_asked_for_again_within_the_hour(self) -> None:
        pin = self._fetch(_news_body([_RELEVANT], gdelt="rate_limited"))

        rows = self._rows(pin)
        self.assertEqual(len(rows), 2, "the shared search and the one for the pin's own name")
        for row in rows:
            self.assertEqual(row.data.get(UNANSWERED_SOURCES_KEY), ["gdelt"])
        data = GdeltPanelSource().cached_data(pin)
        assert data is not None
        context = GdeltPanelSource().render_context(pin, data)
        assert context is not None
        self.assertEqual([row["value"] for row in context["meta"]], [_RELEVANT["title"]])
        with self._an_hour_later():
            self.assertIsNone(GdeltPanelSource().cached_data(pin))

    def test_a_fallbacks_empty_answer_is_not_kept_as_no_news(self) -> None:
        pin = self._fetch(_news_body([], gdelt="rate_limited"))

        self.assertEqual(self._rows(pin), [])
        self.assertIsNone(GdeltPanelSource().cached_data(pin))
        self.assertTrue(
            fetch_blocked(GdeltPanelSource(), pin), "asked again after a short suppression, not on every poll"
        )

    def test_gdelts_own_empty_answer_is_no_news_for_the_whole_window(self) -> None:
        pin = self._fetch(_news_body([], gdelt="ok"))

        with self._an_hour_later():
            self.assertEqual(GdeltPanelSource().cached_data(pin), {"articles": []})

    def assert_kept_as_it_always_was(self, results: list[dict[str, Any]]) -> None:
        """REData before 0.3.7 sends no ``complete``; its answer is kept for the whole window, as it always was."""
        pin = self._fetch(_news_body(results, gdelt=None))

        for row in self._rows(pin):
            self.assertNotIn(UNANSWERED_SOURCES_KEY, row.data)
        with self._an_hour_later():
            self.assertEqual(GdeltPanelSource().cached_data(pin), {"articles": results})

    def test_an_empty_answer_from_redata_before_0_3_7_is_kept_as_it_always_was(self) -> None:
        self.assert_kept_as_it_always_was([])

    def test_articles_from_redata_before_0_3_7_are_kept_as_they_always_were(self) -> None:
        self.assert_kept_as_it_always_was([_RELEVANT])


_QUERY = NewsQuery(
    names=("Hudson River State Hospital", "HRSH"),
    localities=("Poughkeepsie",),
    language="english",
    country="unitedstates",
)


class NewsRelevanceTests(SimpleTestCase):
    """The post-filter, with no database."""

    def test_a_title_naming_only_the_locality_is_kept(self) -> None:
        self.assertTrue(_QUERY.is_relevant({"title": "Poughkeepsie council votes on waterfront plan"}))

    def test_an_english_title_naming_neither_is_dropped(self) -> None:
        self.assertFalse(_QUERY.is_relevant({"title": "Village courtyards drive rural revitalisation"}))

    def test_a_name_inside_a_longer_word_is_not_a_mention(self) -> None:
        self.assertFalse(_QUERY.is_relevant({"title": "HRSHQ announces quarterly results"}))

    def test_live_answers_to_the_fixed_query_that_are_not_about_the_place_are_dropped(self) -> None:
        """GDELT's real answers on 2026-09-23 to the fixed query shape: every one matched loosely on "Hudson River"."""
        for title in (
            "New York DOT Announces Fishkill Road Closure From Flooding",
            "CVS In Pawling Reopens As Mobile Pharmacy After July 4 Fire",
            "Why The Hudson River Suddenly Turned Brown Across New York",
        ):
            self.assertFalse(_QUERY.is_relevant({"title": title}), title)

    def test_a_foreign_script_title_is_dropped_even_when_it_names_the_place(self) -> None:
        self.assertFalse(
            _QUERY.is_relevant({"title": "哈德逊河州立医院 Hudson River State Hospital 旧址改造项目启动仪式举行"})
        )

    @given(st.text(alphabet=st.characters(min_codepoint=0x4E00, max_codepoint=0x9FFF), min_size=1, max_size=60))
    def test_no_chinese_title_is_relevant(self, title: str) -> None:
        self.assertFalse(_QUERY.is_relevant({"title": title, "snippet": "news.example.cn"}))

"""The Private Pin page asks once which enrichment panels have anything, and requests only those (P53).

A panel already known to answer 204 - its gate refuses the pin, its fetch is blocked, or the payload that landed has
nothing to show - gets no placeholder, so the page never requests it. A panel whose answer is not stored yet still
loads, and the decision never fetches anything itself.
"""

from __future__ import annotations

from dataclasses import asdict
from decimal import Decimal
import re
from unittest import mock

from django.contrib.auth.models import Permission, User
from django.core.cache import cache
from django.urls import reverse
import lxml.html
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.cache.location_cache import LocationCache
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.subscriptions import SiteFeature, SubscriptionRole, grant_subscription
from urbanlens.dashboard.plugins.builtin.cris_buildings import CrisBuildingPanelSource
from urbanlens.dashboard.services.apis.assets.base import MediaItem
from urbanlens.dashboard.services.apis.locations.redata_reference_documents_gateway import (
    DigitalCommonwealthMediaProvider,
)
from urbanlens.dashboard.services.geo.geo_boundary import GeoBoundary, state_boundary
from urbanlens.dashboard.services.photos.pin_photos import PIN_MEDIA_GALLERY_SOURCES
from urbanlens.dashboard.services.pins.external_data import (
    InfoPanelSource,
    LocationCachePanelSource,
    get_panel_source,
    panel_sources,
)
from urbanlens.dashboard.tests.hypothesis.redata_helpers import EveryPanelGateConfiguredMixin
from urbanlens.UrbanLens.settings.app import settings as app_settings

#: The page's own cards whose views are not the generic ``pin.panel`` dispatch.
_BESPOKE_CARDS = {
    "azure_maps": "pin.azure_maps",
    "parcel_buildings": "pin.parcel_buildings",
    "yelp": "pin.yelp",
    "nps": "pin.nps",
    "loopnet": "pin.loopnet",
    "usgs_topo": "pin.usgs_topo",
    "wikipedia": "pin.wikipedia",
}

_ON_OPEN = frozenset({"load", "ul:lazy-load"})
_LANE = re.compile(r"#pin-(?:panel|media)-lane-\d+:queue all")


def _events(trigger: str) -> set[str]:
    return {re.split(r"[\s\[]", spec.strip(), maxsplit=1)[0] for spec in trigger.split(",") if spec.strip()}


def _opening_requests(html: str) -> dict[str, lxml.html.HtmlElement]:
    """Each URL the page requests as it opens, mapped to the element that requests it."""
    tree = lxml.html.fromstring(html)
    return {
        element.get("hx-get"): element
        for element in tree.xpath("//*[@hx-get][@hx-trigger]")
        if _events(element.get("hx-trigger", "")) & _ON_OPEN
    }


def _media_item(n: int) -> dict:
    return asdict(
        MediaItem(
            url=f"https://upload.wikimedia.org/old-mill-{n}.jpg",
            thumb_url=f"https://upload.wikimedia.org/thumb/old-mill-{n}.jpg",
            caption="Old Mill Works",
            source="Wikimedia Commons",
            page_url=f"https://commons.wikimedia.org/wiki/File:Old_Mill_{n}.jpg",
        )
    )


#: Local stand-ins for the state outlines a cold process fetches from Census TIGERweb, as a warm process holds them.
_NEW_YORK = GeoBoundary.from_bboxes([(40.4, 45.1, -79.8, -71.8)])
_MASSACHUSETTS = GeoBoundary.from_bboxes([(41.2, 42.9, -73.5, -69.9)])


class PanelProbeTestCase(EveryPanelGateConfiguredMixin, TestCase):
    def setUp(self) -> None:
        super().setUp()
        for owner, boundary in (
            (CrisBuildingPanelSource, _NEW_YORK),
            (DigitalCommonwealthMediaProvider, _MASSACHUSETTS),
        ):
            patcher = mock.patch.object(owner, "geo_boundary", boundary)
            patcher.start()
            self.addCleanup(patcher.stop)
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.user = baker.make(User)
        self.location = baker.make(
            Location,
            latitude=Decimal("40.7128"),
            longitude=Decimal("-74.0060"),
            official_name="Old Mill Works",
            street_number="1",
            route="Mill Street",
            locality="New York",
            administrative_area_level_1="NY",
        )
        self.pin = baker.make(
            Pin, profile=self.user.profile, location=self.location, parent_pin=None, name="Old Mill Works"
        )
        self.client.force_login(self.user)

    # -- page helpers -----------------------------------------------------------------------------------------------

    def _html(self) -> str:
        response = self.client.get(reverse("pin.details", kwargs={"pin_slug": self.pin.slug}))
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def _card_url(self, key: str) -> str:
        name = _BESPOKE_CARDS.get(key)
        return reverse(name, args=[self.pin.slug]) if name else reverse("pin.panel", args=[self.pin.slug, key])

    def _media_url(self, key: str) -> str:
        return reverse("pin.media", args=[self.pin.slug, key])

    def _plugin_urls(self) -> set[str]:
        """Every URL a plugin panel or a media provider of this page could be requested at."""
        cards = {self._card_url(key) for key in _BESPOKE_CARDS}
        cards |= {
            self._card_url(source.key) for source in panel_sources().values() if isinstance(source, InfoPanelSource)
        }
        return cards | {self._media_url(key) for key in PIN_MEDIA_GALLERY_SOURCES}

    def _plugin_requests(self, html: str) -> set[str]:
        return set(_opening_requests(html)) & self._plugin_urls()

    # -- cache helpers ----------------------------------------------------------------------------------------------

    def _cache(self, key: str, data: dict, *, pin: Pin | None = None) -> None:
        """Store ``data`` as every row ``pin`` reads for the source ``key``."""
        source = get_panel_source(key)
        assert isinstance(source, LocationCachePanelSource), key
        for scope in source.search_scopes(pin or self.pin):
            LocationCache.set(self.location, source.cache_source, data, query_key="q", audience=scope.audience)

    def _settle_everything_empty(self) -> None:
        for source in panel_sources().values():
            if isinstance(source, LocationCachePanelSource):
                self._cache(source.key, {})


class KnownEmptyPanelsTests(PanelProbeTestCase):
    def test_a_place_whose_panels_all_came_back_empty_requests_none_of_them(self) -> None:
        self._settle_everything_empty()

        html = self._html()

        self.assertEqual(self._plugin_requests(html), set())
        requested = _opening_requests(html)
        self.assertIn(self._media_url("photos"), requested, "the pin's own photos always load")
        self.assertIn(reverse("pin.satellite_view", args=[self.pin.slug]), requested)

    def test_a_known_empty_card_leaves_no_placeholder_behind(self) -> None:
        self._settle_everything_empty()

        html = self._html()

        for section_id in (
            "azure-maps-section",
            "parcel-buildings-section",
            "building-permits-section",
            "media-loader-wikimedia",
        ):
            self.assertNotIn(f'id="{section_id}"', html)

    def test_an_empty_wikipedia_answer_hides_its_article_sub_tab(self) -> None:
        """What the panel's 204 did to the tab button (data-ext-panel-204-hide-tab), the page does up front."""
        self._settle_everything_empty()

        (button,) = lxml.html.fromstring(self._html()).xpath('//*[@id="article-subtab-btn-wikipedia"]')

        self.assertIsNotNone(button.get("hidden"))

    def test_a_panel_with_cached_content_still_loads(self) -> None:
        self._settle_everything_empty()
        self._cache("redata_permits", {"filings": [{"kind": "permit", "issued_at": "2020-01-02", "work_type": "Roof"}]})
        self._cache("azure_maps", {"formatted_address": "1 Mill Street, New York, NY", "poi": None})
        self._cache("nps", {"full_name": "Old Mill National Historic Site"})
        self._cache("wikimedia", {"items": [_media_item(1)]})

        requested = _opening_requests(self._html())

        expected = {
            self._card_url("redata_permits"),
            self._card_url("azure_maps"),
            self._card_url("nps"),
            self._media_url("wikimedia"),
        }
        self.assertEqual(set(requested) & self._plugin_urls(), expected)
        for url in expected:
            self.assertRegex(requested[url].get("hx-sync") or "", _LANE, f"{url} loads without queueing on a lane")

    def test_a_cold_panel_still_loads(self) -> None:
        html = self._html()

        requested = self._plugin_requests(html)
        for url in (
            self._card_url("redata_permits"),
            self._card_url("gdelt"),
            self._card_url("azure_maps"),
            self._card_url("wikipedia"),
            self._card_url("loopnet"),
            self._card_url("parcel_buildings"),
            self._card_url("usgs_topo"),
            self._media_url("wikimedia"),
            self._media_url("smithsonian"),
        ):
            self.assertIn(url, requested)

    def test_a_cold_panel_whose_fetch_would_be_refused_is_left_out(self) -> None:
        """The panel would schedule nothing and answer 204, so the page does not ask."""
        profile = self.user.profile
        profile.external_apis_enabled = False
        profile.save(update_fields=["external_apis_enabled"])

        self.assertEqual(self._plugin_requests(self._html()), set())

    def test_a_cold_panel_suppressed_after_a_failed_fetch_is_left_out(self) -> None:
        source = get_panel_source("redata_permits")
        assert source is not None
        cache.set(source.skip_key(self.pin), 1, 60)

        requested = self._plugin_requests(self._html())

        self.assertNotIn(self._card_url("redata_permits"), requested)
        self.assertIn(self._card_url("redata_underground"), requested)

    def test_a_panel_whose_gate_refuses_the_pin_is_left_out(self) -> None:
        with mock.patch.object(app_settings, "azure_maps_subscription_key", ""):
            requested = self._plugin_requests(self._html())

        self.assertNotIn(self._card_url("azure_maps"), requested)
        self.assertIn(self._card_url("redata_permits"), requested)

    def test_the_buildings_card_still_loads_for_a_pin_with_child_pins(self) -> None:
        """It lists the child pins whatever the parcel lookup found."""
        self._settle_everything_empty()
        baker.make(Pin, profile=self.user.profile, parent_pin=self.pin, location=baker.make(Location))

        self.assertIn(self._card_url("parcel_buildings"), self._plugin_requests(self._html()))

    def test_regional_data_opens_its_first_tab_with_something_to_show(self) -> None:
        """Census is the first tab; with nothing cached for it, Seismic opens instead of a "No data available."."""
        self._settle_everything_empty()
        self._cache(
            "usgs_earthquakes", {"events": [{"magnitude": 3.2, "title": "Near the mill", "occurred_at": "2021-05-01"}]}
        )

        requested = self._plugin_requests(self._html())

        self.assertIn(self._card_url("usgs_earthquakes"), requested)
        self.assertNotIn(self._card_url("census_tigerweb"), requested)

    def test_a_debug_overlay_viewer_still_loads_empty_media_providers(self) -> None:
        """An empty provider still answers them with what it searched for."""
        self._settle_everything_empty()
        self.user.user_permissions.add(*Permission.objects.filter(codename="view_site_admin"))
        self.user = User.objects.get(pk=self.user.pk)
        self.client.force_login(self.user)

        requested = self._plugin_requests(self._html())

        self.assertIn(self._media_url("wikimedia"), requested)
        self.assertNotIn(self._card_url("azure_maps"), requested)


class ProbeNeverFetchesTests(PanelProbeTestCase):
    def test_opening_a_cold_page_schedules_no_panel_fetch_and_calls_no_gateway(self) -> None:
        from urbanlens.dashboard.plugins.builtin.parcel_buildings import ParcelBuildingsPanelSource
        from urbanlens.dashboard.services.apis.assets.wikipedia import WikipediaGateway
        from urbanlens.dashboard.services.apis.locations.usgs import UsgsGateway
        from urbanlens.dashboard.services.apis.property_records.redata_gateway import RedataGateway

        with (
            mock.patch("urbanlens.dashboard.services.core.celery.safely_enqueue_task") as enqueue,
            mock.patch("requests.sessions.Session.request") as http,
            mock.patch.object(WikipediaGateway, "get_article_for_location") as wikipedia,
            mock.patch.object(UsgsGateway, "historical_topo_maps_for_coordinates") as usgs,
            mock.patch.object(RedataGateway, "lookup_parcel_uuid") as parcel,
            mock.patch.object(ParcelBuildingsPanelSource, "fetch") as buildings,
        ):
            html = self._html()

        self.assertTrue(self._plugin_requests(html), "nothing was probed, so nothing could have been fetched")
        fetches = [
            call for call in enqueue.call_args_list if getattr(call.args[0], "name", "").endswith("fetch_panel_source")
        ]
        self.assertEqual(fetches, [])
        http.assert_not_called()
        for gateway in (wikipedia, usgs, parcel, buildings):
            gateway.assert_not_called()
        for source in panel_sources().values():
            self.assertIsNone(cache.get(source.flight_key(self.pin)), f"{source.key} was marked in flight")

    def test_the_probe_itself_fetches_nothing(self) -> None:
        from urbanlens.dashboard.services.pins.panel_probe import probe_panels

        cards = {
            source.key: [source] for source in panel_sources().values() if isinstance(source, LocationCachePanelSource)
        }
        galleries = [source for key in PIN_MEDIA_GALLERY_SOURCES if (source := get_panel_source(key)) is not None]
        with (
            mock.patch("urbanlens.dashboard.services.core.celery.safely_enqueue_task") as enqueue,
            mock.patch("requests.sessions.Session.request") as http,
        ):
            probe = probe_panels(self.pin, cards=cards, galleries=galleries)

        enqueue.assert_not_called()
        http.assert_not_called()
        self.assertNotIn("redata_permits", probe.empty_cards, "a cold panel counts as may-have-content")

    def test_a_boundary_that_needs_a_census_request_is_left_to_the_panel(self) -> None:
        """A cold process has no New York outline yet; fetching it is the panel's own request's job, not the page's."""
        from urbanlens.dashboard.services.pins.panel_probe import probe_panels

        source = get_panel_source("cris_building")
        assert isinstance(source, CrisBuildingPanelSource)
        with (
            mock.patch.object(CrisBuildingPanelSource, "geo_boundary", state_boundary("NY")),
            mock.patch("urbanlens.dashboard.services.core.rate_limiter._RateLimitedSession._do_request") as request,
        ):
            probe = probe_panels(self.pin, cards={source.key: [source]}, galleries=[source])
            request.assert_not_called()
            source.gate(self.pin)
            request.assert_called()

        self.assertNotIn(source.key, probe.empty_cards, "an undecidable gate counts as may-have-content")
        self.assertNotIn(source.key, probe.empty_galleries)


class ExternalCallsForbiddenTests(TestCase):
    def test_a_gateway_request_inside_the_guard_never_goes_out(self) -> None:
        from urbanlens.dashboard.models.api_call_log.model import ApiCallLog
        from urbanlens.dashboard.services.core.rate_limiter import (
            ExternalCallForbiddenError,
            _RateLimitedSession,
            external_calls_forbidden,
        )

        session = _RateLimitedSession("census_tigerweb")
        with mock.patch("requests.sessions.Session.request", return_value=mock.Mock(ok=True, status_code=200)) as http:
            with external_calls_forbidden(), self.assertRaises(ExternalCallForbiddenError):
                session.get("https://tigerweb.example.test/")
            http.assert_not_called()
            self.assertFalse(ApiCallLog.objects.exists())

            session.get("https://tigerweb.example.test/")
            http.assert_called_once()


class ProbeRespectsWhatThePanelsRespectTests(PanelProbeTestCase):
    def _grant(self, feature: SiteFeature) -> None:
        grant_subscription(self.user, baker.make(SubscriptionRole, features=feature), self.user, None)

    def test_a_gated_panel_is_never_requested_by_a_viewer_without_the_feature(self) -> None:
        self._settle_everything_empty()
        self._cache("redata_incident_history", {"incidents": [{"category": "burglary", "occurred_at": "2020-01-01"}]})

        self.assertNotIn(self._card_url("redata_incident_history"), self._plugin_requests(self._html()))

    def test_a_gated_panel_with_content_loads_for_an_entitled_viewer(self) -> None:
        self._settle_everything_empty()
        self._cache("redata_incident_history", {"incidents": [{"category": "burglary", "occurred_at": "2020-01-01"}]})
        self._grant(SiteFeature.INCIDENT_HISTORY)

        self.assertIn(self._card_url("redata_incident_history"), self._plugin_requests(self._html()))

    def test_a_gated_panel_known_empty_is_left_out_for_an_entitled_viewer(self) -> None:
        self._settle_everything_empty()
        self._grant(SiteFeature.INCIDENT_HISTORY)

        self.assertNotIn(self._card_url("redata_incident_history"), self._plugin_requests(self._html()))

    def test_another_accounts_search_does_not_make_a_panel_load(self) -> None:
        """A row cached for someone else's names is not this pin's to read, and must not decide what it loads."""
        self._settle_everything_empty()
        other = baker.make(User)
        theirs = baker.make(Pin, profile=other.profile, location=self.location, parent_pin=None, name="Secret Bunker")
        source = get_panel_source("wikimedia")
        assert isinstance(source, LocationCachePanelSource)
        private = [scope for scope in source.search_scopes(theirs) if scope.audience]
        self.assertTrue(private, "the other pin has no search of its own, so this test checks nothing")
        for scope in private:
            LocationCache.set(
                self.location, source.cache_source, {"items": [_media_item(2)]}, query_key="q", audience=scope.audience
            )

        self.assertNotIn(self._media_url("wikimedia"), self._plugin_requests(self._html()))


class ProbeAgreesWithThePanelsTests(PanelProbeTestCase):
    """Each skipped panel is one whose own view would have answered 204, and each loaded one is not."""

    def _probe_says_empty(self, key: str) -> bool:
        return self._card_url(key) not in self._plugin_requests(self._html())

    def _view_says_empty(self, key: str) -> bool:
        with mock.patch("urbanlens.dashboard.services.core.celery.safely_enqueue_task"):
            return self.client.get(self._card_url(key)).status_code == 204

    def test_bespoke_and_generic_cards(self) -> None:
        payloads = {
            "azure_maps": [{}, {"poi": None}, {"formatted_address": "1 Mill Street"}, {"poi": {"name": "Mill"}}],
            "nps": [{}, {"full_name": "Old Mill NHS"}],
            "loopnet": [{}, {"listings": []}],
            "yelp": [{}, {"reviews": []}, {"business": {"name": "Mill Cafe"}}],
            "usgs_topo": [{}, {"items": []}],
            "parcel_buildings": [{}, {"buildings": []}],
            "redata_permits": [{}, {"filings": []}, {"filings": [{"kind": "permit", "issued_at": "2020-01-02"}]}],
            "redata_underground": [{}, {"structures": [{"kind": "tunnel", "name": "Mill race"}]}],
        }
        for key, cases in payloads.items():
            for data in cases:
                with self.subTest(key=key, data=data):
                    self._settle_everything_empty()
                    self._cache(key, data)
                    self.assertEqual(self._probe_says_empty(key), self._view_says_empty(key))

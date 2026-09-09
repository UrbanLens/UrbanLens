"""Tests for the NPS plugin's REData-backed panel and enrichment source.

``NpsPanelSource``/``NpsEnrichmentSource`` now call ``RedataNationalParksGateway``
instead of the direct NPS Developer API - these tests mock that gateway and
check the LocationCache row it produces, rather than any HTTP call.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest import mock

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.plugins.builtin.nps import (
    NpsEnrichmentSource,
    NpsPanelSource,
    _is_park_containing_location,
    facility_facets_visible,
)

if TYPE_CHECKING:
    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.models.pin.model import Pin

_GATEWAY_PATH = "urbanlens.dashboard.services.apis.locations.redata_national_parks_gateway.RedataNationalParksGateway"
_CONFIGURED_PATH = "urbanlens.dashboard.plugins.builtin.nps.redata_configured"


class NpsPanelSourceGateTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.source = NpsPanelSource()
        self.pin: Pin = baker.make_recipe("dashboard.pin", profile=baker.make(User).profile)

    def test_requires_redata_configured(self) -> None:
        with mock.patch(_CONFIGURED_PATH, return_value=False):
            self.assertFalse(self.source.gate(self.pin))
        with mock.patch(_CONFIGURED_PATH, return_value=True):
            self.assertTrue(self.source.gate(self.pin))


def _gateway_returning(
    mock_gateway_cls: mock.Mock,
    *,
    park: dict | None,
    alerts: list | None = None,
    visitor_centers: list | None = None,
    campgrounds: list | None = None,
) -> None:
    """Configure a mocked ``RedataNationalParksGateway`` class's instance methods.

    Every facet defaults to ``[]`` (rather than leaving a bare ``Mock`` in
    place) since the cache write is real ``JSONField`` storage in these
    tests' ``TestCase`` - an unconfigured facet call would otherwise fail
    to serialize instead of failing the assertion that matters.
    """
    instance = mock_gateway_cls.return_value
    instance.find_nearest_park.return_value = park
    instance.get_alerts.return_value = alerts if alerts is not None else []
    instance.get_visitor_centers.return_value = visitor_centers if visitor_centers is not None else []
    instance.get_campgrounds.return_value = campgrounds if campgrounds is not None else []


class NpsPanelSourceFetchTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.source = NpsPanelSource()
        self.location: Location = baker.make("dashboard.Location", latitude=44.6, longitude=-110.5)
        self.pin: Pin = baker.make_recipe("dashboard.pin", profile=baker.make(User).profile, location=self.location)

    def _cached(self) -> dict | None:
        from urbanlens.dashboard.models.cache.location_cache import LocationCache

        row = LocationCache.objects.filter(location=self.location, source="nps").first()
        return row.data if row else None

    def test_caches_the_nearest_park(self) -> None:
        park = {"park_code": "yell", "full_name": "Yellowstone National Park"}
        with mock.patch(_GATEWAY_PATH) as mock_gateway_cls:
            _gateway_returning(mock_gateway_cls, park=park)
            self.source.fetch(self.pin)

        cached = self._cached()
        assert cached is not None
        self.assertEqual(cached["park_code"], "yell")
        self.assertEqual(cached["full_name"], "Yellowstone National Park")
        mock_gateway_cls.return_value.find_nearest_park.assert_called_once_with(44.6, -110.5)

    def test_caches_an_empty_dict_when_nothing_is_within_range(self) -> None:
        with mock.patch(_GATEWAY_PATH) as mock_gateway_cls:
            _gateway_returning(mock_gateway_cls, park=None)
            self.source.fetch(self.pin)

        self.assertEqual(self._cached(), {})

    def test_does_not_fetch_facets_when_nothing_is_within_range(self) -> None:
        """No ``park_code`` to fetch per-park facets by - fetching them would be a wasted call."""
        with mock.patch(_GATEWAY_PATH) as mock_gateway_cls:
            _gateway_returning(mock_gateway_cls, park=None)
            self.source.fetch(self.pin)

        mock_gateway_cls.return_value.get_alerts.assert_not_called()
        mock_gateway_cls.return_value.get_visitor_centers.assert_not_called()
        mock_gateway_cls.return_value.get_campgrounds.assert_not_called()

    def test_caches_alerts_visitor_centers_and_campgrounds_once_a_park_is_found(self) -> None:
        park = {"park_code": "yell", "full_name": "Yellowstone National Park"}
        alerts = [{"id": 1, "title": "Road closed", "category": "Park Closure"}]
        visitor_centers = [{"id": 1, "name": "Old Faithful Visitor Center"}]
        campgrounds = [{"id": 1, "name": "Madison Campground"}]
        with mock.patch(_GATEWAY_PATH) as mock_gateway_cls:
            _gateway_returning(
                mock_gateway_cls, park=park, alerts=alerts, visitor_centers=visitor_centers, campgrounds=campgrounds
            )
            self.source.fetch(self.pin)

        cached = self._cached()
        assert cached is not None
        self.assertEqual(cached["alerts"], alerts)
        self.assertEqual(cached["visitor_centers"], visitor_centers)
        self.assertEqual(cached["campgrounds"], campgrounds)
        mock_gateway_cls.return_value.get_alerts.assert_called_once_with("yell")
        mock_gateway_cls.return_value.get_visitor_centers.assert_called_once_with("yell")
        mock_gateway_cls.return_value.get_campgrounds.assert_called_once_with("yell")


class NpsPanelSourceApiPayloadTests(TestCase):
    """The API's info card, including the alerts -> ``facts`` wiring."""

    def setUp(self) -> None:
        super().setUp()
        from urbanlens.dashboard.models.cache.location_cache import LocationCache

        # The first user in a fresh test DB is auto-promoted to site admin, and a
        # site admin holds every SiteFeature - a throwaway user absorbs that so
        # self.pin's owner is an ordinary, unsubscribed user (see test_panel_feature_gate.py).
        baker.make(User)
        self.source = NpsPanelSource()
        self.location: Location = baker.make("dashboard.Location", latitude=44.6, longitude=-110.5)
        self.pin: Pin = baker.make_recipe("dashboard.pin", profile=baker.make(User).profile, location=self.location)
        self.LocationCache = LocationCache

    def _cache(self, data: dict) -> None:
        self.LocationCache.set(self.location, "nps", data, query_key="44.60000,-110.50000")

    def test_an_active_danger_alert_shows_up_as_a_fact(self) -> None:
        self._cache(
            {
                "park_code": "yell",
                "full_name": "Yellowstone National Park",
                # Contained, so this viewer sees the gated facets for free -
                # see FacilityFacetsGateTests below for the not-contained case.
                "is_contained": True,
                "alerts": [
                    {
                        "id": 1,
                        "title": "Grizzly activity near trailhead",
                        "category": "Danger",
                        "url": "https://nps.gov/yell/alert1",
                    }
                ],
            }
        )

        payload = self.source.api_payload(self.pin)

        assert payload is not None
        facts = payload["info"]["facts"]
        self.assertEqual(len(facts), 1)
        self.assertEqual(
            facts[0],
            {
                "icon": "warning",
                "text": "Danger: Grizzly activity near trailhead",
                "href": "https://nps.gov/yell/alert1",
            },
        )

    def test_zero_alerts_renders_cleanly(self) -> None:
        self._cache({"park_code": "yell", "full_name": "Yellowstone National Park", "alerts": []})

        payload = self.source.api_payload(self.pin)

        assert payload is not None
        self.assertEqual(payload["info"]["facts"], [])

    def test_no_alerts_key_at_all_renders_cleanly(self) -> None:
        """A row cached before this feature shipped has no ``alerts`` key at all."""
        self._cache({"park_code": "yell", "full_name": "Yellowstone National Park"})

        payload = self.source.api_payload(self.pin)

        assert payload is not None
        self.assertEqual(payload["info"]["facts"], [])

    def test_visitor_centers_and_campgrounds_summarize_into_meta(self) -> None:
        self._cache(
            {
                "park_code": "yell",
                "full_name": "Yellowstone National Park",
                "is_contained": True,
                "visitor_centers": [{"id": 1, "name": "Old Faithful Visitor Center"}],
                "campgrounds": [{"id": 1, "name": "Madison Campground"}, {"id": 2, "name": "Bridge Bay Campground"}],
            }
        )

        payload = self.source.api_payload(self.pin)

        assert payload is not None
        meta = {row["label"]: row["value"] for row in payload["info"]["meta"]}
        self.assertEqual(meta["Visitor Centers"], "1 (Old Faithful Visitor Center)")
        self.assertEqual(meta["Campgrounds"], "2 (Madison Campground, Bridge Bay Campground)")

    def test_facets_are_hidden_when_not_contained_and_the_viewer_has_no_subscription(self) -> None:
        """The default case: nearest-by-proximity only, no SiteFeature.PLACES."""
        self._cache(
            {
                "park_code": "yell",
                "full_name": "Yellowstone National Park",
                "is_contained": False,
                "alerts": [{"id": 1, "title": "Bridge out", "category": "Danger", "url": "https://nps.gov/yell/a1"}],
                "visitor_centers": [{"id": 1, "name": "Old Faithful Visitor Center"}],
            }
        )

        payload = self.source.api_payload(self.pin)

        assert payload is not None
        self.assertEqual(payload["info"]["facts"], [])
        self.assertNotIn("Visitor Centers", {row["label"] for row in payload["info"]["meta"]})

    def test_facets_show_when_not_contained_but_the_viewer_holds_the_places_feature(self) -> None:
        from urbanlens.dashboard.models.subscriptions import SiteFeature, SubscriptionRole, grant_subscription

        role = baker.make(SubscriptionRole, features=SiteFeature.PLACES)
        grant_subscription(self.pin.profile.user, role, self.pin.profile.user, None)
        self._cache(
            {
                "park_code": "yell",
                "full_name": "Yellowstone National Park",
                "is_contained": False,
                "alerts": [{"id": 1, "title": "Bridge out", "category": "Danger", "url": "https://nps.gov/yell/a1"}],
            }
        )

        payload = self.source.api_payload(self.pin)

        assert payload is not None
        self.assertEqual(len(payload["info"]["facts"]), 1)


class NpsEnrichmentSourceTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.source = NpsEnrichmentSource()
        self.location: Location = baker.make("dashboard.Location", latitude=44.6, longitude=-110.5)

    def test_gate_requires_redata_configured(self) -> None:
        with mock.patch(_CONFIGURED_PATH, return_value=False):
            self.assertFalse(self.source.gate())
        with mock.patch(_CONFIGURED_PATH, return_value=True):
            self.assertTrue(self.source.gate())

    def test_fetch_returns_the_nearest_park_and_query_key(self) -> None:
        park = {"park_code": "yell", "full_name": "Yellowstone National Park"}
        with mock.patch(_GATEWAY_PATH) as mock_gateway_cls:
            _gateway_returning(mock_gateway_cls, park=park)
            data, query_key = self.source.fetch(self.location)

        assert data is not None
        self.assertEqual(data["park_code"], "yell")
        self.assertEqual(data["full_name"], "Yellowstone National Park")
        self.assertEqual(query_key, "44.60000,-110.50000")

    def test_fetch_returns_none_when_nothing_is_within_range(self) -> None:
        with mock.patch(_GATEWAY_PATH) as mock_gateway_cls:
            _gateway_returning(mock_gateway_cls, park=None)
            data, _query_key = self.source.fetch(self.location)

        self.assertIsNone(data)

    def test_fetch_does_not_fetch_facets_when_nothing_is_within_range(self) -> None:
        with mock.patch(_GATEWAY_PATH) as mock_gateway_cls:
            _gateway_returning(mock_gateway_cls, park=None)
            self.source.fetch(self.location)

        mock_gateway_cls.return_value.get_alerts.assert_not_called()

    def test_fetch_includes_alerts_visitor_centers_and_campgrounds(self) -> None:
        park = {"park_code": "yell", "full_name": "Yellowstone National Park"}
        alerts = [{"id": 1, "title": "Road closed", "category": "Park Closure"}]
        with mock.patch(_GATEWAY_PATH) as mock_gateway_cls:
            _gateway_returning(mock_gateway_cls, park=park, alerts=alerts)
            data, _query_key = self.source.fetch(self.location)

        assert data is not None
        self.assertEqual(data["alerts"], alerts)
        self.assertEqual(data["visitor_centers"], [])
        self.assertEqual(data["campgrounds"], [])
        mock_gateway_cls.return_value.get_alerts.assert_called_once_with("yell")


class NpsInfoViewTests(TestCase):
    """``PinController.nps_info`` - the web HTMX partial, not the JSON API.

    Alerts reach the JSON API through ``api_payload``'s ``facts``, but the web
    panel is bespoke markup (``pin_nps.html``) rather than the generic
    ``_simple_info_panel.html`` renderer, so it needs its own explicit wiring -
    this pins that a closure/hazard alert actually reaches the rendered page,
    not just the API.
    """

    def setUp(self) -> None:
        super().setUp()
        # See NpsPanelSourceApiPayloadTests.setUp for why: absorbs the first-user
        # site-admin auto-promotion so self.pin's owner is an ordinary user.
        baker.make(User)
        self.location: Location = baker.make("dashboard.Location", latitude=44.6, longitude=-110.5)
        self.pin: Pin = baker.make_recipe("dashboard.pin", profile=baker.make(User).profile, location=self.location)
        self.url = reverse("pin.nps", args=[self.pin.slug])

    def _seed_cache(self, *, park: dict | None, alerts: list | None = None) -> None:
        with mock.patch(_GATEWAY_PATH) as mock_gateway_cls, mock.patch(_CONFIGURED_PATH, return_value=True):
            _gateway_returning(mock_gateway_cls, park=park, alerts=alerts)
            NpsPanelSource().fetch(self.pin)

    def _seed_containment(self, park_code: str) -> None:
        """A Property Records cache row saying this pin sits inside ``park_code``.

        ``NpsPanelSource.fetch`` reads this (``_is_park_containing_location``)
        to decide ``is_contained`` - see the nps.py module docstring.
        """
        from urbanlens.dashboard.models.cache.location_cache import LocationCache
        from urbanlens.dashboard.plugins.builtin.property_records import PropertyRecordsPanelSource

        LocationCache.set(
            self.location,
            PropertyRecordsPanelSource.cache_source,
            {"containing_park": {"park_code": park_code, "full_name": "Yellowstone National Park"}},
        )

    def test_an_active_alert_renders_on_the_web_panel_when_the_pin_is_contained(self) -> None:
        self._seed_containment("yell")
        self._seed_cache(
            park={"park_code": "yell", "full_name": "Yellowstone National Park"},
            alerts=[{"id": 1, "title": "Road closed", "category": "Park Closure"}],
        )
        self.client.force_login(self.pin.profile.user)

        with mock.patch(
            "urbanlens.dashboard.services.apis.locations.redata_context_gateway.redata_configured", return_value=True
        ):
            response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Park Closure: Road closed")

    def test_an_active_alert_is_hidden_when_not_contained_and_unsubscribed(self) -> None:
        """The default case: nearest-by-proximity only, no SiteFeature.PLACES."""
        self._seed_cache(
            park={"park_code": "yell", "full_name": "Yellowstone National Park"},
            alerts=[{"id": 1, "title": "Road closed", "category": "Park Closure"}],
        )
        self.client.force_login(self.pin.profile.user)

        with mock.patch(
            "urbanlens.dashboard.services.apis.locations.redata_context_gateway.redata_configured", return_value=True
        ):
            response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Park Closure: Road closed")
        self.assertNotContains(response, "nps-alerts")

    def test_an_active_alert_renders_when_not_contained_but_subscribed(self) -> None:
        from urbanlens.dashboard.models.subscriptions import SiteFeature, SubscriptionRole, grant_subscription

        role = baker.make(SubscriptionRole, features=SiteFeature.PLACES)
        grant_subscription(self.pin.profile.user, role, self.pin.profile.user, None)
        self._seed_cache(
            park={"park_code": "yell", "full_name": "Yellowstone National Park"},
            alerts=[{"id": 1, "title": "Road closed", "category": "Park Closure"}],
        )
        self.client.force_login(self.pin.profile.user)

        with mock.patch(
            "urbanlens.dashboard.services.apis.locations.redata_context_gateway.redata_configured", return_value=True
        ):
            response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Park Closure: Road closed")

    def test_no_alerts_renders_cleanly_with_no_alert_markup(self) -> None:
        self._seed_containment("yell")
        self._seed_cache(park={"park_code": "yell", "full_name": "Yellowstone National Park"}, alerts=[])
        self.client.force_login(self.pin.profile.user)

        with mock.patch(
            "urbanlens.dashboard.services.apis.locations.redata_context_gateway.redata_configured", return_value=True
        ):
            response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "nps-alerts")


class IsParkContainingLocationTests(TestCase):
    """``_is_park_containing_location`` reads Property Records' own cached
    point-in-boundary result rather than re-querying REData - see the
    function's own docstring for why, and its "fail closed" default."""

    def setUp(self) -> None:
        super().setUp()
        self.location: Location = baker.make("dashboard.Location", latitude=44.6, longitude=-110.5)

    def _seed_property_records(self, data: dict) -> None:
        from urbanlens.dashboard.models.cache.location_cache import LocationCache
        from urbanlens.dashboard.plugins.builtin.property_records import PropertyRecordsPanelSource

        LocationCache.set(self.location, PropertyRecordsPanelSource.cache_source, data)

    def test_false_when_property_records_has_never_cached_anything(self) -> None:
        self.assertFalse(_is_park_containing_location(self.location, "yell"))

    def test_false_when_property_records_cached_no_containing_park(self) -> None:
        self._seed_property_records({"available": True, "situs_address": "1 Main St"})

        self.assertFalse(_is_park_containing_location(self.location, "yell"))

    def test_true_when_the_cached_containing_park_matches(self) -> None:
        self._seed_property_records({"containing_park": {"park_code": "yell", "full_name": "Yellowstone"}})

        self.assertTrue(_is_park_containing_location(self.location, "yell"))

    def test_false_when_the_cached_containing_park_is_a_different_unit(self) -> None:
        """Two overlapping park boundaries, or a stale/mismatched row - never trust it on a guess."""
        self._seed_property_records({"containing_park": {"park_code": "grte", "full_name": "Grand Teton"}})

        self.assertFalse(_is_park_containing_location(self.location, "yell"))


class FacilityFacetsVisibleTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        # See NpsPanelSourceApiPayloadTests.setUp for why.
        baker.make(User)
        self.pin: Pin = baker.make_recipe("dashboard.pin", profile=baker.make(User).profile)

    def test_true_when_contained_regardless_of_subscription(self) -> None:
        self.assertTrue(facility_facets_visible({"is_contained": True}, self.pin))

    def test_false_when_not_contained_and_unsubscribed(self) -> None:
        self.assertFalse(facility_facets_visible({"is_contained": False}, self.pin))

    def test_false_when_is_contained_key_is_missing_entirely(self) -> None:
        """A row cached before this feature shipped - fails closed, not open."""
        self.assertFalse(facility_facets_visible({}, self.pin))

    def test_true_when_not_contained_but_the_viewer_holds_the_places_feature(self) -> None:
        from urbanlens.dashboard.models.subscriptions import SiteFeature, SubscriptionRole, grant_subscription

        role = baker.make(SubscriptionRole, features=SiteFeature.PLACES)
        grant_subscription(self.pin.profile.user, role, self.pin.profile.user, None)

        self.assertTrue(facility_facets_visible({"is_contained": False}, self.pin))

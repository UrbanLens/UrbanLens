"""A pin's boundary drawing, its debug cache reset, its web-search refresh, and the old Memories photos redirect (P29).

Each asserts what the owner may do, that another user is refused with nothing changed, that anonymous is sent to log
in, and that a malformed body is a 4xx.
"""

from __future__ import annotations

from datetime import timedelta
import json
from unittest import mock

from django.conf import settings
from django.contrib.auth.models import Permission, User
from django.core.cache import cache
from django.urls import reverse
from django.utils import timezone
from model_bakery import baker

from urbanlens.core.cache_keys import make_cache_key
from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.account.model import ApiKey, ApiKeyScope
from urbanlens.dashboard.models.boundary.model import Boundary, BoundaryType
from urbanlens.dashboard.models.cache.location_cache import LocationCache
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.subscriptions import SiteFeature, SubscriptionRole, grant_subscription
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.services.pins.search_names import search_names

_SQUARE = {
    "type": "Polygon",
    "coordinates": [[[-73.0, 42.0], [-73.0, 42.001], [-72.999, 42.001], [-72.999, 42.0], [-73.0, 42.0]]],
}


class _Owners(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.stranger_user = baker.make(User)
        self.location = baker.make(
            Location, official_name="Old Mill", official_name_source="google_places", latitude=42.0, longitude=-73.0
        )
        self.pin = baker.make(Pin, profile=self.profile, location=self.location)

    def assert_login_redirect(self, response) -> None:
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response["Location"].startswith(settings.LOGIN_URL), response["Location"])


class PinBoundarySaveRouteTests(_Owners):
    def setUp(self) -> None:
        super().setUp()
        self.url = reverse("boundary.pin", args=[self.pin.slug])
        for name in ("schedule_panel_fetch", "schedule_location_boundary_generation"):
            self.enterContext(mock.patch(f"urbanlens.dashboard.controllers.boundary.{name}", return_value=False))
        self.enterContext(
            mock.patch("urbanlens.dashboard.controllers.boundary.boundary_generation_ran", return_value=True)
        )

    def _post(self, body):
        return self.client.post(self.url, json.dumps(body), content_type="application/json")

    def _drawn(self) -> bool:
        return Boundary.objects.filter(pin=self.pin, boundary_type=BoundaryType.PROPERTY).exists()

    def _draw(self) -> None:
        self.client.force_login(self.user)
        self.assertEqual(self._post({"boundary_type": "property", "polygon": _SQUARE}).status_code, 200)

    def test_the_owner_draws_then_clears_a_boundary(self) -> None:
        self._draw()
        self.assertTrue(self._drawn())

        self.assertEqual(self._post({"boundary_type": "property", "polygon": None}).status_code, 200)
        self.assertFalse(self._drawn())

    def test_a_missing_or_garbled_polygon_is_400_and_keeps_the_drawing(self) -> None:
        """The map always sends ``polygon``, null to clear; anything else empty is a garbled request, not a clear."""
        self._draw()

        bodies = (
            {"boundary_type": "property"},
            {"boundary_type": "property", "polygon": {}},
            {"boundary_type": "property", "polygon": ""},
            {"boundary_type": "property", "polygon": []},
            {"boundary_type": "property", "polygon": False},
        )
        for body in bodies:
            self.assertEqual(self._post(body).status_code, 400, body)
        self.assertTrue(self._drawn())

    def test_a_malformed_body_is_400(self) -> None:
        self.client.force_login(self.user)

        bodies = (
            ["x"],
            {"boundary_type": "garden", "polygon": _SQUARE},
            {"boundary_type": "property", "polygon": {"type": "Point", "coordinates": [0, 0]}},
            {"boundary_type": "property", "polygon": "not geojson"},
        )
        for body in bodies:
            self.assertEqual(self._post(body).status_code, 400, body)
        self.assertEqual(self.client.post(self.url, "{", content_type="application/json").status_code, 400)
        self.assertFalse(self._drawn())

    def test_someone_elses_pin_is_404_and_keeps_its_drawing(self) -> None:
        self._draw()
        self.client.force_login(self.stranger_user)

        self.assertEqual(self._post({"boundary_type": "property", "polygon": None}).status_code, 404)
        self.assertTrue(self._drawn())

    def test_anonymous_is_redirected_to_login(self) -> None:
        self.assert_login_redirect(self._post({"boundary_type": "property", "polygon": _SQUARE}))
        self.assertFalse(self._drawn())


class WikiBoundaryGarbledPolygonTests(_Owners):
    """Both wiki boundary routes parse ``polygon`` as the pin route does; a garbled one must not clear the drawing."""

    def setUp(self) -> None:
        super().setUp()
        from urbanlens.dashboard.services.auth.api_keys import generate_api_key
        from urbanlens.dashboard.services.geo.geo import parse_multipolygon_geojson
        from urbanlens.dashboard.services.geo.wiki_boundary_edits import save_wiki_boundary

        self.wiki = baker.make(Wiki, location=self.location, name="Old Mill")
        self.slug = self.location.ensure_slug()
        save_wiki_boundary(self.wiki, BoundaryType.PROPERTY, parse_multipolygon_geojson(_SQUARE), self.profile)
        for module in ("controllers.boundary", "external_api.views_wiki"):
            self.enterContext(
                mock.patch(f"urbanlens.dashboard.{module}.schedule_location_boundary_generation", return_value=False)
            )
        key, raw = generate_api_key(self.user, "wiki client")
        ApiKey.objects.filter(pk=key.pk).update(scopes=[ApiKeyScope.WIKI_READ.value, ApiKeyScope.WIKI_WRITE.value])
        self.auth = {"HTTP_AUTHORIZATION": f"Bearer {raw}"}

    def _drawn(self) -> bool:
        return Boundary.objects.row_for_wiki(self.wiki, BoundaryType.PROPERTY) is not None

    def test_the_wiki_page_refuses_a_missing_or_garbled_polygon(self) -> None:
        self.client.force_login(self.user)
        url = reverse("location.wiki.boundary", args=[self.slug])

        for body in ({"boundary_type": "property"}, {"boundary_type": "property", "polygon": {}}):
            response = self.client.post(url, json.dumps(body), content_type="application/json")
            self.assertEqual(response.status_code, 400, body)
        self.assertTrue(self._drawn())

    def test_the_external_api_refuses_a_garbled_polygon(self) -> None:
        url = reverse("external_api:wikis.boundary", args=[self.slug])

        for polygon in ({}, "", []):
            body = {"boundary_type": "property", "polygon": polygon}
            response = self.client.post(url, json.dumps(body), content_type="application/json", **self.auth)
            self.assertEqual(response.status_code, 400, polygon)
        self.assertTrue(self._drawn())


class PinDebugClearCacheRouteTests(_Owners):
    def setUp(self) -> None:
        super().setUp()
        self.user.user_permissions.add(Permission.objects.get(codename="view_site_admin"))
        self.url = reverse("pin.debug.clear_cache", args=[self.pin.slug])
        self.debug_entry = LocationCache.set(self.location, "wikipedia", {"x": 1})
        self.other_source = LocationCache.set(self.location, "boundary", {"x": 1})
        self.elsewhere = LocationCache.set(baker.make(Location), "wikipedia", {"x": 1})
        self.carousel_key = make_cache_key("satellite_view_esri", "42.00000", "-73.00000")
        cache.set(self.carousel_key, ["slide"], 60)
        self.addCleanup(cache.delete, self.carousel_key)

    def _remaining(self) -> set[int]:
        return set(LocationCache.objects.values_list("pk", flat=True))

    def test_a_site_admin_clears_the_debug_sources_at_their_pin_only(self) -> None:
        self.client.force_login(self.user)

        response = self.client.post(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["cleared"], 1)
        self.assertEqual(self._remaining(), {self.other_source.pk, self.elsewhere.pk})
        self.assertIsNone(cache.get(self.carousel_key))

    def test_a_site_admin_cannot_clear_through_someone_elses_pin(self) -> None:
        self.stranger_user.user_permissions.add(Permission.objects.get(codename="view_site_admin"))
        self.client.force_login(self.stranger_user)

        self.assertEqual(self.client.post(self.url).status_code, 404)
        self.assertIn(self.debug_entry.pk, self._remaining())
        self.assertIsNotNone(cache.get(self.carousel_key))

    def test_a_pin_owner_who_is_not_a_site_admin_is_403(self) -> None:
        self.user.user_permissions.clear()
        self.client.force_login(User.objects.get(pk=self.user.pk))

        self.assertEqual(self.client.post(self.url).status_code, 403)
        self.assertIn(self.debug_entry.pk, self._remaining())

    def test_anonymous_is_redirected_to_login(self) -> None:
        self.assert_login_redirect(self.client.post(self.url))
        self.assertIn(self.debug_entry.pk, self._remaining())


class PinWebSearchRefreshRouteTests(_Owners):
    def setUp(self) -> None:
        super().setUp()
        role = baker.make(SubscriptionRole, features=SiteFeature.SEARCH)
        grant_subscription(self.user, role, self.user, None)
        self.url = reverse("pin.web_search.refresh", args=[self.pin.slug])
        self.entry = LocationCache.set(
            self.location,
            "web_search",
            {"results": [{"title": "Old result", "link": "https://example.com/old"}]},
            query_key=self.pin.get_unique_search_name(
                search_names(self.pin).base, quote_name=True, quote_locality=True
            ),
        )
        self.search = self.enterContext(
            mock.patch(
                "urbanlens.dashboard.controllers.pin.search_web",
                return_value=[{"title": "New result", "link": "https://example.com/new", "snippet": "s"}],
            )
        )

    def _age(self, days: float) -> None:
        LocationCache.objects.filter(pk=self.entry.pk).update(updated=timezone.now() - timedelta(days=days))

    def _titles(self) -> list[str]:
        self.entry.refresh_from_db()
        return [result["title"] for result in self.entry.data["results"]]

    def test_the_owner_refreshes_results_a_day_old(self) -> None:
        self._age(1.1)
        self.client.force_login(self.user)

        response = self.client.post(self.url)

        self.assertEqual(response.status_code, 200)
        self.search.assert_called_once()
        self.assertEqual(self._titles(), ["New result"])

    def test_results_refreshed_too_recently_are_429_and_cost_nothing(self) -> None:
        self.client.force_login(self.user)

        self.assertEqual(self.client.post(self.url).status_code, 429)
        self.search.assert_not_called()
        self.assertEqual(self._titles(), ["Old result"])

    def test_an_owner_without_search_cannot_spend_it(self) -> None:
        self._age(1.1)
        Pin.objects.filter(pk=self.pin.pk).update(profile=self.stranger_user.profile)
        self.client.force_login(self.stranger_user)

        self.assertEqual(self.client.post(self.url).status_code, 403)
        self.assertEqual(self.client.post(f"{self.url}?surface=article").status_code, 200)
        self.search.assert_not_called()
        self.assertEqual(self._titles(), ["Old result"])

    def test_someone_elses_pin_is_404(self) -> None:
        self._age(1.1)
        role = baker.make(SubscriptionRole, features=SiteFeature.SEARCH)
        grant_subscription(self.stranger_user, role, self.stranger_user, None)
        self.client.force_login(self.stranger_user)

        self.assertEqual(self.client.post(self.url).status_code, 404)
        self.search.assert_not_called()

    def test_anonymous_is_redirected_to_login(self) -> None:
        self._age(1.1)
        self.assert_login_redirect(self.client.post(self.url))
        self.search.assert_not_called()


class MemoriesPhotosRedirectRouteTests(TestCase):
    """A ``RedirectView`` that answers every verb: it must move, never write."""

    def setUp(self) -> None:
        super().setUp()
        self.url = reverse("memories.photos.redirect")
        self.target = reverse("vault.photos")

    def test_every_verb_is_a_permanent_redirect_to_the_vault(self) -> None:
        self.client.force_login(baker.make(User))

        for method in ("get", "post", "put", "patch", "delete"):
            response = getattr(self.client, method)(self.url)
            self.assertEqual((response.status_code, response["Location"]), (301, self.target), method)

    def test_anonymous_is_redirected_to_the_page_that_asks_them_to_log_in(self) -> None:
        response = self.client.post(self.url, {"anything": "x"})

        self.assertEqual((response.status_code, response["Location"]), (301, self.target))
        followed = self.client.get(self.target)
        self.assertEqual(followed.status_code, 302)
        self.assertTrue(followed["Location"].startswith(settings.LOGIN_URL))

"""Tests for the web_search controller action and domain-extraction logic.

The controller is tested via request-response cycles using Django's test client
so we exercise the full view path without a real search API call.
The domain extraction helper is tested directly with Hypothesis.
"""
from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch
from urllib.parse import urlparse

from hypothesis import given, settings as hyp_settings, strategies as st
from model_bakery import baker
import pytest

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.subscriptions import SiteFeature, SubscriptionRole, grant_subscription
from urbanlens.dashboard.services.locations.naming import is_meaningful_name

if TYPE_CHECKING:
    from django.contrib.auth.models import User

_hyp = hyp_settings(max_examples=60, deadline=None)


# ---------------------------------------------------------------------------
# Domain extraction (logic duplicated from the view for unit coverage)
# ---------------------------------------------------------------------------

def _extract_domain(url: str) -> str:
    """Mirror the domain-extraction logic in PinController.web_search."""
    try:
        return urlparse(url).netloc.removeprefix("www.")
    except (ValueError, AttributeError):
        return ""


class DomainExtractionTests(SimpleTestCase):
    """Domain is correctly stripped from full URLs."""

    def test_simple_url_returns_domain(self):
        self.assertEqual(_extract_domain("https://example.com/page"), "example.com")

    def test_www_prefix_stripped(self):
        self.assertEqual(_extract_domain("https://www.example.com/path"), "example.com")

    def test_subdomain_preserved(self):
        self.assertEqual(_extract_domain("https://news.bbc.co.uk/article"), "news.bbc.co.uk")

    def test_www_only_on_subdomain_stripped(self):
        self.assertEqual(_extract_domain("https://www.bbc.co.uk/"), "bbc.co.uk")

    def test_empty_string_returns_empty(self):
        self.assertEqual(_extract_domain(""), "")

    def test_relative_url_returns_empty(self):
        self.assertEqual(_extract_domain("/relative/path"), "")

    def test_url_with_port_includes_port(self):
        result = _extract_domain("http://localhost:8000/page")
        self.assertIn("localhost", result)

    def test_none_like_empty_does_not_crash(self):
        # Covers the except branch for truly malformed values
        self.assertEqual(_extract_domain(""), "")

    @given(
        scheme=st.sampled_from(["http", "https"]),
        domain=st.from_regex(r"[a-z]{3,10}\.[a-z]{2,4}", fullmatch=True).filter(lambda d: not d.startswith("www.")),
        path=st.text(alphabet=st.characters(whitelist_categories=("L", "N")), max_size=20),
    )
    @_hyp
    def test_no_www_domain_returned_unchanged(self, scheme: str, domain: str, path: str):
        url = f"{scheme}://{domain}/{path}"
        self.assertEqual(_extract_domain(url), domain)

    @given(
        domain=st.from_regex(r"[a-z]{3,10}\.[a-z]{2,4}", fullmatch=True).filter(lambda d: not d.startswith("www.")),
    )
    @_hyp
    def test_www_variant_matches_non_www(self, domain: str):
        plain = _extract_domain(f"https://{domain}/")
        with_www = _extract_domain(f"https://www.{domain}/")
        self.assertEqual(plain, with_www)


# ---------------------------------------------------------------------------
# Location.has_place_name - placeholder / sentinel names
# ---------------------------------------------------------------------------

class LocationHasPlaceNameTests(TestCase):
    """has_place_name() returns False for placeholders and True for real names."""

    def _location_with_cached_name(self, name: str | None):
        from urbanlens.dashboard.models.location.model import Location

        loc = baker.prepare(Location, latitude="40.0", longitude="-74.0")
        if name is not None:
            google_place = type("GooglePlaceStub", (), {"cached_place_name": name, "pk": 1})()
            loc.google_place = google_place
            loc.google_place_id = 1
        return loc

    def test_none_cached_name_is_not_meaningful(self):
        from urbanlens.dashboard.models.location.model import Location

        loc = baker.prepare(Location, latitude="40.0", longitude="-74.0", google_place=None)
        with patch.object(Location, "get_place_name") as mock_get:
            self.assertFalse(loc.has_place_name())
        mock_get.assert_not_called()

    def test_sentinel_string_is_not_meaningful(self):
        loc = self._location_with_cached_name("No Information Available")
        self.assertFalse(loc.has_place_name())

    def test_abandoned_placeholder_is_not_meaningful(self):
        loc = self._location_with_cached_name("Abandoned Location")
        self.assertFalse(loc.has_place_name())

    def test_coordinate_string_is_not_meaningful(self):
        loc = self._location_with_cached_name("40.7128, -74.0060")
        self.assertFalse(loc.has_place_name())

    def test_real_name_is_meaningful(self):
        loc = self._location_with_cached_name("Riverside Mill")
        self.assertTrue(loc.has_place_name())

    def test_place_name_property_returns_cached_name_without_api_call(self):
        from urbanlens.dashboard.models.location.model import Location

        loc = self._location_with_cached_name("Old Factory")
        with patch.object(Location, "get_place_name") as mock_get:
            name = loc.place_name
        mock_get.assert_not_called()
        self.assertEqual(name, "Old Factory")

    @given(name=st.text(min_size=1, max_size=80).filter(is_meaningful_name))
    @_hyp
    def test_any_real_name_is_meaningful(self, name: str):
        loc = self._location_with_cached_name(name)
        self.assertTrue(loc.has_place_name())


class UniqueSearchNameQuoteLocalityTests(TestCase):
    """Pin.get_unique_search_name's quote_locality option: wraps "city state" as
    one exact-phrase term instead of two loose keywords, so a generic street
    address doesn't match the same address in an unrelated city - see the
    web_search view, which is the one caller that opts into this."""

    def _make_pin(self, *, city: str | None = "Cincinnati", state: str | None = "Ohio", county: str | None = None) -> Pin:
        from urbanlens.dashboard.models.location.model import Location
        from urbanlens.dashboard.models.profile.model import Profile

        loc = baker.make(Location, official_name="118 W 9th St", latitude=39.1, longitude=-84.5, city=city, state=state, county=county)
        user: User = baker.make("auth.User")
        profile = Profile.objects.get(user=user)
        return baker.make(Pin, location=loc, profile=profile)

    def test_city_and_state_are_quoted_together(self) -> None:
        pin = self._make_pin(city="Cincinnati", state="Ohio")
        result = pin.get_unique_search_name(quote_name=True, quote_locality=True)
        assert result is not None
        self.assertIn('"Cincinnati Ohio"', result)

    def test_without_quote_locality_city_and_state_are_loose_keywords(self) -> None:
        pin = self._make_pin(city="Cincinnati", state="Ohio")
        result = pin.get_unique_search_name(quote_name=True, quote_locality=False)
        assert result is not None
        self.assertNotIn('"Cincinnati Ohio"', result)
        self.assertIn("Cincinnati", result)
        self.assertIn("Ohio", result)

    def test_county_used_when_no_city(self) -> None:
        pin = self._make_pin(city=None, state="Ohio", county="Hamilton County")
        result = pin.get_unique_search_name(quote_name=True, quote_locality=True)
        assert result is not None
        self.assertIn('"Hamilton County Ohio"', result)

    def test_state_only_is_still_quoted(self) -> None:
        pin = self._make_pin(city=None, state="Ohio", county=None)
        result = pin.get_unique_search_name(quote_name=True, quote_locality=True)
        assert result is not None
        self.assertIn('"Ohio"', result)

    def test_no_locality_data_omits_the_locality_term_entirely(self) -> None:
        pin = self._make_pin(city=None, state=None, county=None)
        result = pin.get_unique_search_name(quote_name=True, quote_locality=True)
        assert result is not None
        self.assertNotIn('""', result)

    def test_address_is_quoted_as_an_exact_phrase_when_quote_name_is_set(self) -> None:
        from urbanlens.dashboard.models.location.model import Location
        from urbanlens.dashboard.models.profile.model import Profile

        loc = baker.make(Location, official_name="Old Mill Factory", latitude=39.1, longitude=-84.5, city="Cincinnati", state="Ohio", street_number="118", route="W 9th St")
        user: User = baker.make("auth.User")
        profile = Profile.objects.get(user=user)
        pin = baker.make(Pin, location=loc, profile=profile)

        result = pin.get_unique_search_name(quote_name=True)
        assert result is not None
        self.assertIn('"118 W 9th St"', result)


class SearchSubscriptionFeatureTests(TestCase):
    """Search subscription feature defaults."""

    def test_vip_defaults_grant_search_feature(self):
        SubscriptionRole.ensure_defaults()

        vip = SubscriptionRole.objects.get(slug="vip")

        self.assertTrue(vip.grants(SiteFeature.SEARCH))

# ---------------------------------------------------------------------------
# web_search controller - via Django test client
# ---------------------------------------------------------------------------


class WebSearchViewTests(TestCase):
    """web_search view integrates search gateway and enriches results with domain."""

    def _make_pin(self, *, subscribe: bool = True) -> Pin:
        from urbanlens.dashboard.models.location.model import Location
        from urbanlens.dashboard.models.profile.model import Profile
        loc = baker.make(
            Location,
            official_name="Official Test Location",
            latitude=41.0,
            longitude=-81.5,
        )
        baker.make("auth.User")  # first user is auto-promoted to bootstrap site admin
        user = baker.make("auth.User")
        # The post_save signal creates a Profile automatically; retrieve it rather
        # than letting baker create a second one (which would violate the unique constraint).
        profile = Profile.objects.get(user=user)
        pin = baker.make(Pin, location=loc, profile=profile)
        if subscribe:
            role = baker.make(SubscriptionRole, features=SiteFeature.SEARCH)
            grant_subscription(user, role, user, None)
        return pin

    def test_nonexistent_pin_slug_returns_404(self):
        from django.test import RequestFactory

        from urbanlens.dashboard.controllers.pin import PinController

        rf = RequestFactory()
        request = rf.get("/")
        request.user = baker.make("auth.User")
        view = PinController()
        response = view.web_search(request, pin_slug="nonexistent-pin-slug")
        self.assertEqual(response.status_code, 404)

    def test_unsubscribed_user_cannot_access_search_gateway(self):
        from django.test import RequestFactory

        from urbanlens.dashboard.controllers.pin import PinController

        pin = self._make_pin(subscribe=False)
        rf = RequestFactory()
        request = rf.get("/")
        request.user = pin.profile.user

        with (
            patch("urbanlens.dashboard.controllers.pin.search_web") as mock_search_web,
            patch.object(Pin.objects, "select_related") as mock_select_related,
        ):
            mock_select_related.return_value.get.return_value = pin
            view = PinController()
            response = view.web_search(request, pin_slug=pin.slug)

        self.assertEqual(response.status_code, 403)
        mock_search_web.assert_not_called()

    def test_successful_search_returns_200(self):
        from django.test import RequestFactory

        from urbanlens.dashboard.controllers.pin import PinController

        pin = self._make_pin()
        pin.name = "User Edited Location"
        rf = RequestFactory()
        request = rf.get("/")
        request.user = pin.profile.user

        mock_results = [{"title": "Result", "link": "http://example.com/page", "snippet": "A snippet"}]

        with (
            patch("urbanlens.dashboard.controllers.pin.search_web") as mock_search_web,
            patch.object(Pin.objects, "select_related") as mock_select_related,
        ):
            mock_search_web.return_value = mock_results
            mock_select_related.return_value.get.return_value = pin

            view = PinController()
            response = view.web_search(request, pin_slug=pin.slug)

        self.assertEqual(response.status_code, 200)
        mock_search_web.assert_called_once()
        self.assertIn("Official Test Location", mock_search_web.call_args.args[0])
        self.assertNotIn("User Edited Location", mock_search_web.call_args.args[0])

    def test_result_carries_a_bookmark_button_posting_to_pin_links(self) -> None:
        """Bookmarking a web search result reuses the same pin.links endpoint (and
        therefore the same Wayback-archiving-on-create signal) as the Links card's
        own add-link dialog - see _pin_link_add_dialog.html."""
        from django.test import RequestFactory
        from django.urls import reverse

        from urbanlens.dashboard.controllers.pin import PinController

        pin = self._make_pin()
        rf = RequestFactory()
        request = rf.get("/")
        request.user = pin.profile.user

        mock_results = [{"title": "Old Mill Historical Society", "link": "http://example.com/page", "snippet": "A snippet"}]

        with (
            patch("urbanlens.dashboard.controllers.pin.search_web") as mock_search_web,
            patch.object(Pin.objects, "select_related") as mock_select_related,
        ):
            mock_search_web.return_value = mock_results
            mock_select_related.return_value.get.return_value = pin

            view = PinController()
            response = view.web_search(request, pin_slug=pin.slug)

        content = response.content.decode()
        self.assertIn(f'hx-post="{reverse("pin.links", args=[pin.slug])}"', content)
        self.assertIn('hx-target="#pin-links-row"', content)
        self.assertIn("Old Mill Historical Society", content)
        self.assertIn("http://example.com/page", content)

    def test_empty_fresh_results_return_204_not_a_no_results_card(self) -> None:
        """Regression guard: an empty search must hide the panel (204, the
        site-wide data-ext-panel-204 convention) rather than rendering a
        visible "No results found." card."""
        from django.test import RequestFactory

        from urbanlens.dashboard.controllers.pin import PinController

        pin = self._make_pin()
        rf = RequestFactory()
        request = rf.get("/")
        request.user = pin.profile.user

        with (
            patch("urbanlens.dashboard.controllers.pin.search_web") as mock_search_web,
            patch.object(Pin.objects, "select_related") as mock_select_related,
        ):
            mock_search_web.return_value = []
            mock_select_related.return_value.get.return_value = pin
            view = PinController()
            response = view.web_search(request, pin_slug=pin.slug)

        self.assertEqual(response.status_code, 204)

    def test_empty_cached_results_return_204(self) -> None:
        from django.test import RequestFactory

        from urbanlens.dashboard.controllers.pin import PinController
        from urbanlens.dashboard.models.cache.location_cache import LocationCache

        pin = self._make_pin()
        search_name = pin.get_unique_search_name(quote_name=True, quote_locality=True)
        assert search_name is not None
        LocationCache.set(pin.location, "web_search", {"results": []}, query_key=search_name)
        rf = RequestFactory()
        request = rf.get("/")
        request.user = pin.profile.user

        with (
            patch("urbanlens.dashboard.controllers.pin.search_web") as mock_search_web,
            patch.object(Pin.objects, "select_related") as mock_select_related,
        ):
            mock_select_related.return_value.get.return_value = pin
            view = PinController()
            response = view.web_search(request, pin_slug=pin.slug)

        self.assertEqual(response.status_code, 204)
        mock_search_web.assert_not_called()

    def test_search_skips_pins_without_official_name(self):
        from django.test import RequestFactory

        from urbanlens.dashboard.controllers.pin import PinController

        pin = self._make_pin()
        pin.name = "User Edited Location"
        pin.location.official_name = ""
        rf = RequestFactory()
        request = rf.get("/")
        request.user = pin.profile.user

        with (
            patch("urbanlens.dashboard.controllers.pin.search_web") as mock_search_web,
            patch.object(Pin.objects, "select_related") as mock_select_related,
        ):
            mock_select_related.return_value.get.return_value = pin
            view = PinController()
            response = view.web_search(request, pin_slug=pin.slug)

        self.assertEqual(response.status_code, 204)
        mock_search_web.assert_not_called()

    def test_domain_key_added_to_each_result(self):
        from django.test import RequestFactory

        from urbanlens.dashboard.controllers.pin import PinController

        pin = self._make_pin()
        rf = RequestFactory()
        request = rf.get("/")
        request.user = pin.profile.user

        captured: list[dict] = []

        def fake_render(req, template, ctx):
            captured.extend(ctx.get("search_results", []))
            from django.http import HttpResponse
            return HttpResponse("")

        mock_results = [
            {"title": "A", "link": "https://www.example.com/article", "snippet": "s"},
            {"title": "B", "link": "https://news.bbc.co.uk/story", "snippet": "s"},
        ]

        with (
            patch("urbanlens.dashboard.controllers.pin.search_web") as mock_search_web,
            patch.object(Pin.objects, "select_related") as mock_select_related,
            patch("urbanlens.dashboard.controllers.pin.render", side_effect=fake_render),
        ):
            mock_search_web.return_value = mock_results
            mock_select_related.return_value.get.return_value = pin

            view = PinController()
            view.web_search(request, pin_slug=pin.slug)

        self.assertEqual(len(captured), 2)
        self.assertEqual(captured[0]["domain"], "example.com")
        self.assertEqual(captured[1]["domain"], "news.bbc.co.uk")

    def test_cache_shared_between_two_pins_at_same_location(self):
        """Two pins at the same Location with no custom name issue the same query and share one fetch."""
        from django.test import RequestFactory

        from urbanlens.dashboard.controllers.pin import PinController
        from urbanlens.dashboard.models.profile.model import Profile

        pin_a = self._make_pin()
        # A Pin is unique per (location, profile), so a second pin at the same
        # Location needs a different (subscribed) owner.
        user_b = baker.make("auth.User")
        role = baker.make(SubscriptionRole, features=SiteFeature.SEARCH)
        grant_subscription(user_b, role, user_b, None)
        pin_b = baker.make(Pin, location=pin_a.location, profile=Profile.objects.get(user=user_b))
        rf = RequestFactory()

        mock_results = [{"title": "Result", "link": "http://example.com/page", "snippet": "s"}]

        with (
            patch("urbanlens.dashboard.controllers.pin.search_web") as mock_search_web,
            patch.object(Pin.objects, "select_related") as mock_select_related,
        ):
            mock_search_web.return_value = mock_results
            mock_select_related.return_value.get.side_effect = [pin_a, pin_b]

            view = PinController()
            request_a = rf.get("/")
            request_a.user = pin_a.profile.user
            response_a = view.web_search(request_a, pin_slug=pin_a.slug)

            request_b = rf.get("/")
            request_b.user = pin_b.profile.user
            response_b = view.web_search(request_b, pin_slug=pin_b.slug)

        self.assertEqual(response_a.status_code, 200)
        self.assertEqual(response_b.status_code, 200)
        mock_search_web.assert_called_once()

    def test_refresh_rejected_when_cache_is_too_recent(self):
        from django.test import RequestFactory

        from urbanlens.dashboard.controllers.pin import PinController
        from urbanlens.dashboard.models.cache.location_cache import LocationCache

        pin = self._make_pin()
        LocationCache.set(pin.location, "web_search", {"results": []}, query_key=pin.get_unique_search_name(quote_name=True, quote_locality=True))

        rf = RequestFactory()
        request = rf.post("/")
        request.user = pin.profile.user

        with patch.object(Pin.objects, "select_related") as mock_select_related:
            mock_select_related.return_value.get.return_value = pin
            view = PinController()
            response = view.web_search_refresh(request, pin_slug=pin.slug)

        self.assertEqual(response.status_code, 429)

    def test_refresh_allowed_once_cache_is_a_day_old(self):
        from datetime import timedelta

        from django.test import RequestFactory
        from django.utils import timezone

        from urbanlens.dashboard.controllers.pin import PinController
        from urbanlens.dashboard.models.cache.location_cache import LocationCache

        pin = self._make_pin()
        entry = LocationCache.set(pin.location, "web_search", {"results": []}, query_key=pin.get_unique_search_name(quote_name=True, quote_locality=True))
        LocationCache.objects.filter(pk=entry.pk).update(updated=timezone.now() - timedelta(days=1, minutes=1))

        rf = RequestFactory()
        request = rf.post("/")
        request.user = pin.profile.user

        mock_results = [{"title": "Fresh Result", "link": "http://example.com/new", "snippet": "s"}]

        with (
            patch("urbanlens.dashboard.controllers.pin.search_web") as mock_search_web,
            patch.object(Pin.objects, "select_related") as mock_select_related,
        ):
            mock_search_web.return_value = mock_results
            mock_select_related.return_value.get.return_value = pin

            view = PinController()
            response = view.web_search_refresh(request, pin_slug=pin.slug)

        self.assertEqual(response.status_code, 200)
        mock_search_web.assert_called_once()

    def test_gateway_exception_returns_error_template(self):
        from django.test import RequestFactory

        from urbanlens.dashboard.controllers.pin import PinController

        pin = self._make_pin()
        rf = RequestFactory()
        request = rf.get("/")
        request.user = pin.profile.user

        captured_ctx: dict = {}

        def fake_render(req, template, ctx):
            captured_ctx.update(ctx)
            from django.http import HttpResponse
            return HttpResponse("")

        with (
            patch("urbanlens.dashboard.controllers.pin.search_web") as mock_search_web,
            patch.object(Pin.objects, "select_related") as mock_select_related,
            patch("urbanlens.dashboard.controllers.pin.render", side_effect=fake_render),
        ):
            mock_search_web.side_effect = RuntimeError("API down")
            mock_select_related.return_value.get.return_value = pin

            view = PinController()
            view.web_search(request, pin_slug=pin.slug)

        self.assertIn("error", captured_ctx)
        self.assertNotIn("search_results", captured_ctx)

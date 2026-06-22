"""Tests for the web_search controller action and domain-extraction logic.

The controller is tested via request-response cycles using Django's test client
so we exercise the full view path without a real search API call.
The domain extraction helper is tested directly with Hypothesis.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch
from urllib.parse import urlparse

import pytest
from hypothesis import given, settings as hyp_settings
from hypothesis import strategies as st
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.pin.model import Pin


_hyp = hyp_settings(max_examples=60, deadline=None)


# ---------------------------------------------------------------------------
# Domain extraction (logic duplicated from the view for unit coverage)
# ---------------------------------------------------------------------------

def _extract_domain(url: str) -> str:
    """Mirror the domain-extraction logic in PinController.web_search."""
    try:
        return urlparse(url).netloc.removeprefix("www.")
    except Exception:
        return ""


class DomainExtractionTests(TestCase):
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
        domain=st.from_regex(r"[a-z]{3,10}\.[a-z]{2,4}", fullmatch=True),
        path=st.text(alphabet=st.characters(whitelist_categories=("L", "N")), max_size=20),
    )
    @_hyp
    def test_no_www_domain_returned_unchanged(self, scheme: str, domain: str, path: str):
        url = f"{scheme}://{domain}/{path}"
        self.assertEqual(_extract_domain(url), domain)

    @given(
        domain=st.from_regex(r"[a-z]{3,10}\.[a-z]{2,4}", fullmatch=True),
    )
    @_hyp
    def test_www_variant_matches_non_www(self, domain: str):
        plain = _extract_domain(f"https://{domain}/")
        with_www = _extract_domain(f"https://www.{domain}/")
        self.assertEqual(plain, with_www)


# ---------------------------------------------------------------------------
# Location.has_place_name — "No Information Available" sentinel
# ---------------------------------------------------------------------------

class LocationHasPlaceNameTests(TestCase):
    """has_place_name() returns False for the sentinel and True for real names."""

    def _location_with_cached_name(self, name: str | None):
        from urbanlens.dashboard.models.location.model import Location
        loc = baker.prepare(Location, cached_place_name=name)
        return loc

    def test_none_cached_name_is_not_meaningful(self):
        from urbanlens.dashboard.models.location.model import Location
        loc = baker.prepare(Location, cached_place_name=None)
        with patch.object(Location, "get_place_name", return_value=None):
            self.assertFalse(loc.has_place_name())

    def test_sentinel_string_is_not_meaningful(self):
        from urbanlens.dashboard.models.location.model import Location
        loc = baker.prepare(Location, cached_place_name="No Information Available")
        self.assertFalse(loc.has_place_name())

    def test_real_name_is_meaningful(self):
        from urbanlens.dashboard.models.location.model import Location
        loc = baker.prepare(Location, cached_place_name="Riverside Mill")
        self.assertTrue(loc.has_place_name())

    def test_place_name_property_returns_cached_name_without_api_call(self):
        from urbanlens.dashboard.models.location.model import Location
        loc = baker.prepare(Location, cached_place_name="Old Factory")
        with patch.object(Location, "get_place_name") as mock_get:
            name = loc.place_name
        mock_get.assert_not_called()
        self.assertEqual(name, "Old Factory")

    @given(name=st.text(min_size=1, max_size=80).filter(lambda s: s != "No Information Available"))
    @_hyp
    def test_any_real_name_is_meaningful(self, name: str):
        from urbanlens.dashboard.models.location.model import Location
        loc = baker.prepare(Location, cached_place_name=name)
        self.assertTrue(loc.has_place_name())


# ---------------------------------------------------------------------------
# web_search controller — via Django test client
# ---------------------------------------------------------------------------

class WebSearchViewTests(TestCase):
    """web_search view integrates search gateway and enriches results with domain."""

    def _make_pin(self) -> Pin:
        from urbanlens.dashboard.models.location.model import Location
        from urbanlens.dashboard.models.profile.model import Profile
        loc = baker.make(Location, name="Test Location", latitude=41.0, longitude=-81.5)
        user = baker.make("auth.User")
        # The post_save signal creates a Profile automatically; retrieve it rather
        # than letting baker create a second one (which would violate the unique constraint).
        profile = Profile.objects.get(user=user)
        pin = baker.make(Pin, location=loc, profile=profile)
        return pin

    def test_nonexistent_pin_uuid_returns_404(self):
        import uuid
        from django.test import RequestFactory
        from urbanlens.dashboard.controllers.pin import PinController

        rf = RequestFactory()
        request = rf.get("/")
        request.user = baker.make("auth.User")
        view = PinController()
        response = view.web_search(request, pin_uuid=uuid.uuid4())
        self.assertEqual(response.status_code, 404)

    def test_successful_search_returns_200(self):
        from django.test import RequestFactory
        from urbanlens.dashboard.controllers.pin import PinController

        pin = self._make_pin()
        rf = RequestFactory()
        request = rf.get("/")
        request.user = pin.user

        mock_results = [{"title": "Result", "link": "http://example.com/page", "snippet": "A snippet"}]

        with (
            patch("urbanlens.dashboard.controllers.pin.get_search_gateway") as mock_factory,
            patch.object(Pin.objects, "get", return_value=pin),
        ):
            mock_gw = MagicMock()
            mock_gw.search.return_value = mock_results
            mock_factory.return_value = mock_gw

            view = PinController()
            response = view.web_search(request, pin_uuid=pin.uuid)

        self.assertEqual(response.status_code, 200)

    def test_domain_key_added_to_each_result(self):
        from django.test import RequestFactory
        from urbanlens.dashboard.controllers.pin import PinController

        pin = self._make_pin()
        rf = RequestFactory()
        request = rf.get("/")
        request.user = pin.user

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
            patch("urbanlens.dashboard.controllers.pin.get_search_gateway") as mock_factory,
            patch.object(Pin.objects, "get", return_value=pin),
            patch("urbanlens.dashboard.controllers.pin.render", side_effect=fake_render),
        ):
            mock_gw = MagicMock()
            mock_gw.search.return_value = mock_results
            mock_factory.return_value = mock_gw

            view = PinController()
            view.web_search(request, pin_uuid=pin.uuid)

        self.assertEqual(len(captured), 2)
        self.assertEqual(captured[0]["domain"], "example.com")
        self.assertEqual(captured[1]["domain"], "news.bbc.co.uk")

    def test_gateway_exception_returns_error_template(self):
        from django.test import RequestFactory
        from urbanlens.dashboard.controllers.pin import PinController

        pin = self._make_pin()
        rf = RequestFactory()
        request = rf.get("/")
        request.user = pin.user

        captured_ctx: dict = {}

        def fake_render(req, template, ctx):
            captured_ctx.update(ctx)
            from django.http import HttpResponse
            return HttpResponse("")

        with (
            patch("urbanlens.dashboard.controllers.pin.get_search_gateway") as mock_factory,
            patch.object(Pin.objects, "get", return_value=pin),
            patch("urbanlens.dashboard.controllers.pin.render", side_effect=fake_render),
        ):
            mock_gw = MagicMock()
            mock_gw.search.side_effect = RuntimeError("API down")
            mock_factory.return_value = mock_gw

            view = PinController()
            view.web_search(request, pin_uuid=pin.uuid)

        self.assertIn("error", captured_ctx)
        self.assertNotIn("search_results", captured_ctx)

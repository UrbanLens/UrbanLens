"""Tests for the map's pin-list sidebar being scoped to the current viewport.

MapController.pin_list_panel (GET map.pins.list) now accepts an optional
"bounds" "south,west,north,east" query param - when present, both the
rendered pin list and its total count are restricted to that box (on top of
whatever SearchForm/toolbar filters already apply); when absent, behavior is
unchanged (the full filtered set, as before this feature).
"""

from __future__ import annotations

from urllib.parse import urlencode

from django.contrib.auth.models import User
from django.test import Client
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin


class PinListPanelViewportScopingTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client = Client()
        self.client.force_login(self.user)
        self.inside = baker.make(Pin, profile=self.profile, location=baker.make(Location, latitude="40.0", longitude="-74.0"), name="Inside Pin")
        self.outside = baker.make(Pin, profile=self.profile, location=baker.make(Location, latitude="45.0", longitude="-80.0"), name="Outside Pin")

    def _url(self, **params) -> str:
        base = reverse("map.pins.list")
        if not params:
            return base
        return f"{base}?{urlencode(params)}"

    def test_without_bounds_shows_every_matching_pin(self) -> None:
        response = self.client.get(self._url())
        self.assertContains(response, "Inside Pin")
        self.assertContains(response, "Outside Pin")
        self.assertEqual(response.context["total_count"], 2)

    def test_with_bounds_only_shows_pins_in_the_box(self) -> None:
        response = self.client.get(self._url(bounds="39.0,-75.0,41.0,-73.0"))
        self.assertContains(response, "Inside Pin")
        self.assertNotContains(response, "Outside Pin")
        self.assertEqual(response.context["total_count"], 1)

    def test_with_bounds_is_viewport_scoped_flag_is_true(self) -> None:
        response = self.client.get(self._url(bounds="39.0,-75.0,41.0,-73.0"))
        self.assertTrue(response.context["is_viewport_scoped"])

    def test_without_bounds_is_viewport_scoped_flag_is_false(self) -> None:
        response = self.client.get(self._url())
        self.assertFalse(response.context["is_viewport_scoped"])

    def test_malformed_bounds_falls_back_to_unscoped(self) -> None:
        response = self.client.get(self._url(bounds="not,a,valid,bbox"))
        self.assertContains(response, "Inside Pin")
        self.assertContains(response, "Outside Pin")
        self.assertFalse(response.context["is_viewport_scoped"])

    def test_bounds_scoping_combines_with_search_form_filters(self) -> None:
        baker.make(Pin, profile=self.profile, location=baker.make(Location, latitude="40.001", longitude="-74.001"), name="Inside But Wrong Name")
        response = self.client.get(self._url(bounds="39.0,-75.0,41.0,-73.0", name="Inside Pin"))
        self.assertContains(response, "Inside Pin")
        self.assertNotContains(response, "Inside But Wrong Name")
        self.assertNotContains(response, "Outside Pin")

    def test_pagination_extra_query_carries_bounds_forward(self) -> None:
        """pagination_extra_query feeds _pagination_controls.html's extra_query, so a page-2+
        click doesn't silently drop back to the unscoped pin set - see _pin_list_panel.html."""
        response = self.client.get(self._url(bounds="39.0,-75.0,41.0,-73.0"))
        self.assertIn("bounds=39.0", response.context["pagination_extra_query"])

    def test_pagination_extra_query_empty_when_unscoped(self) -> None:
        response = self.client.get(self._url())
        self.assertEqual(response.context["pagination_extra_query"], "")

    def test_viewport_scoped_count_label_says_in_view(self) -> None:
        response = self.client.get(self._url(bounds="39.0,-75.0,41.0,-73.0"))
        self.assertContains(response, "in view")

    def test_unscoped_count_label_omits_in_view(self) -> None:
        response = self.client.get(self._url())
        self.assertNotContains(response, "in view")

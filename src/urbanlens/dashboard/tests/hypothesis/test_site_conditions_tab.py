"""P227: Site Conditions is a tab of the Location Data card, requested only when its tab opens."""

from __future__ import annotations

from decimal import Decimal
import json
import re
from typing import TYPE_CHECKING
from unittest import mock

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.cache.location_cache import LocationCache
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.services.pins.external_data import PanelPlacement, get_panel_source
from urbanlens.dashboard.tests.hypothesis.redata_helpers import RedataConfiguredMixin

if TYPE_CHECKING:
    from urbanlens.dashboard.models.pin.model import Pin

_KEY = "redata_site_conditions"
_SOIL = {"soil": [{"component_name": "Clarion", "component_percent": 85.0, "drainage_class": "Well drained"}]}
_LOCATION_DATA_KEYS = ("nominatim", "photon", "open_elevation")

#: An opening tag that issues an HTMX request.
_REQUESTING_TAG = re.compile(r"<(?:div|button|section|span|a)\b[^>]*\bhx-get=\"[^\"]*\"[^>]*>", re.DOTALL)


class _Base(RedataConfiguredMixin, TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.user = baker.make(User)
        self.client.force_login(self.user)
        self.pin: Pin = baker.make_recipe("dashboard.pin", profile=self.user.profile)
        self.url = reverse("pin.panel", args=[self.pin.slug, _KEY])

    def _page(self) -> str:
        response = self.client.get(reverse("pin.details", args=[self.pin.slug]))
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def _requesting_tags(self) -> list[str]:
        return [tag for tag in _REQUESTING_TAG.findall(self._page()) if f'hx-get="{self.url}' in tag]


class SiteConditionsPlacementTests(_Base):
    """The panel declares Location Data, and the page renders it there and nowhere else."""

    def test_it_declares_location_data(self) -> None:
        source = get_panel_source(_KEY)
        assert source is not None
        self.assertEqual(getattr(source, "placement", None), PanelPlacement.LOCATION)

    def test_the_page_requests_it_from_exactly_one_element(self) -> None:
        self.assertEqual(len(self._requesting_tags()), 1, "Site Conditions is requested from more than one element")

    def test_that_element_is_a_location_data_tab(self) -> None:
        content = self._page()
        strip = content.split('id="location-data-section"')[1].split('id="location-data-body"')[0]
        self.assertIn(f'hx-get="{self.url}"', strip)
        self.assertIn(f'data-panel-key="{_KEY}"', strip)
        self.assertIn(">Site Conditions<", strip)

    def test_nothing_requests_it_when_the_page_opens(self) -> None:
        """A tab loads when it is opened (P53): its button keeps htmx's default click trigger."""
        (tag,) = self._requesting_tags()
        self.assertNotIn("hx-trigger", tag)

    def test_its_own_card_is_gone(self) -> None:
        self.assertNotIn('id="site-conditions-section"', self._page())
        self.assertNotIn(_KEY, [panel.key for panel in self._context()["simple_info_panels"]])

    def _context(self):
        return self.client.get(reverse("pin.details", args=[self.pin.slug])).context


class SiteConditionsTabTests(_Base):
    """Opened, the tab renders the panel inside the card."""

    def test_the_tab_renders_nested(self) -> None:
        LocationCache.set(self.pin.location, _KEY, _SOIL, query_key="")

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'class="simple-info-panel nested"')
        self.assertContains(response, "Clarion (85%)")


class LocationDataOverviewSiteConditionsTests(_Base):
    """The Overview fetches the tab's data so an empty tab can be hidden, but never outside the panel's gate."""

    def _overview(self, pin: Pin | None = None):
        with mock.patch("urbanlens.dashboard.tasks.fetch_panel_source") as fetch_task:
            response = self.client.get(reverse("pin.location_data_overview", args=[(pin or self.pin).slug]))
        scheduled = {call.kwargs["args"][0] for call in fetch_task.apply_async.call_args_list}
        return response, scheduled

    @staticmethod
    def _hidden(response) -> list[str]:
        if "HX-Trigger" not in response:
            return []
        return json.loads(response["HX-Trigger"])["pinTabsEmpty"]["keys"]

    def test_an_unfetched_tab_is_fetched_by_the_overview(self) -> None:
        _response, scheduled = self._overview()
        self.assertIn(_KEY, scheduled)

    def test_a_tab_with_data_is_kept(self) -> None:
        for key in _LOCATION_DATA_KEYS:
            LocationCache.set(self.pin.location, key, {}, query_key="")
        LocationCache.set(self.pin.location, _KEY, _SOIL, query_key="")

        response, _scheduled = self._overview()

        self.assertNotIn(_KEY, self._hidden(response))

    def test_an_empty_answer_hides_the_tab(self) -> None:
        for key in _LOCATION_DATA_KEYS:
            LocationCache.set(self.pin.location, key, {}, query_key="")
        LocationCache.set(self.pin.location, _KEY, {}, query_key="")

        response, _scheduled = self._overview()

        self.assertIn(_KEY, self._hidden(response))

    def test_outside_the_usa_it_is_neither_fetched_nor_kept(self) -> None:
        """The panel is USA-only; scheduling it from the Overview would spend REData calls the tab would never show."""
        abroad: Pin = baker.make_recipe(
            "dashboard.pin",
            profile=self.user.profile,
            location=baker.make(Location, latitude=Decimal("51.5072"), longitude=Decimal("-0.1276")),
        )

        response, scheduled = self._overview(abroad)

        self.assertNotIn(_KEY, scheduled)
        self.assertIn(_KEY, self._hidden(response))

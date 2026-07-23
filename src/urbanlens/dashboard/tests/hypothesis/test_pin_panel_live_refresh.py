"""Live-refresh notifications for the pin detail page's external-data panels.

Some panels (Wikipedia, Nominatim, EPA ECHO's exact-site detail) have side
effects beyond their own content - an auto-added alias/link, or a changed
pin display name (see services.locations.naming). Those mutations happen in
a background Celery task with no HTTP response of their own to signal from,
so PinController._notify_panel_ready attaches an HX-Trigger header the next
time the panel that caused them is rendered from the now-fresh cache - see
PinController.wikipedia_info/nominatim_info/panel_info.
"""

from __future__ import annotations

import json

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.cache.location_cache import LocationCache
from urbanlens.dashboard.models.profile.model import Profile


def _hx_trigger_events(response) -> dict:
    header = response.get("HX-Trigger")
    return json.loads(header) if header else {}


class PinPanelLiveRefreshTests(TestCase):
    """PinController._notify_panel_ready: only fires on a poll (attempt >= 1)."""

    def setUp(self) -> None:
        baker.make(User)  # first user is auto-promoted to bootstrap site admin
        user = baker.make(User)
        self.profile = Profile.objects.get(user=user)
        self.pin = baker.make_recipe("dashboard.pin", profile=self.profile)
        self.client.force_login(user)

    def test_wikipedia_first_synchronous_load_does_not_fire(self) -> None:
        LocationCache.set(self.pin.location, "wikipedia", {"title": "Old Mill", "extract": "A mill.", "url": "https://en.wikipedia.org/wiki/Old_Mill"}, query_key="")
        response = self.client.get(reverse("pin.wikipedia", args=[self.pin.slug]))
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("HX-Trigger", response)

    def test_wikipedia_poll_completion_fires_aliases_changed(self) -> None:
        LocationCache.set(self.pin.location, "wikipedia", {"title": "Old Mill", "extract": "A mill.", "url": "https://en.wikipedia.org/wiki/Old_Mill"}, query_key="")
        response = self.client.get(reverse("pin.wikipedia", args=[self.pin.slug]), {"attempt": "1"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(_hx_trigger_events(response), {"pinAliasesChanged": True})

    def test_nominatim_poll_completion_fires_all_three_events(self) -> None:
        LocationCache.set(self.pin.location, "nominatim", {"website": "https://example.com"}, query_key="")
        response = self.client.get(reverse("pin.nominatim", args=[self.pin.slug]), {"attempt": "1"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(_hx_trigger_events(response), {"pinAliasesChanged": True, "pinLinksChanged": True, "pinOverviewChanged": True})

    def test_nominatim_first_synchronous_load_does_not_fire(self) -> None:
        LocationCache.set(self.pin.location, "nominatim", {"website": "https://example.com"}, query_key="")
        response = self.client.get(reverse("pin.nominatim", args=[self.pin.slug]))
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("HX-Trigger", response)

    def test_epa_echo_detail_poll_completion_fires_links_changed(self) -> None:
        LocationCache.set(self.pin.location, "epa_echo", {"exact_site": {"name": "Acme Plant", "registry_id": "12345", "programs": []}}, query_key="")
        response = self.client.get(reverse("pin.panel", args=[self.pin.slug, "epa_echo_detail"]), {"attempt": "1"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(_hx_trigger_events(response), {"pinLinksChanged": True})

    def test_other_generic_panels_never_fire(self) -> None:
        """Only epa_echo_detail is known to have a side effect - every other
        InfoPanelSource-backed panel must stay silent even on a poll."""
        LocationCache.set(self.pin.location, "photon", {"name": "Old Mill", "kind_label": "Building"}, query_key="")
        response = self.client.get(reverse("pin.panel", args=[self.pin.slug, "photon"]), {"attempt": "1"})
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("HX-Trigger", response)


class PinDetailPageLiveRefreshMarkupTests(TestCase):
    """The listening side: panels that can be mutated by another panel's fetch
    must actually listen for the corresponding event."""

    def setUp(self) -> None:
        baker.make(User)
        user = baker.make(User)
        self.profile = Profile.objects.get(user=user)
        self.pin = baker.make_recipe("dashboard.pin", profile=self.profile)
        self.client.force_login(user)

    def _page(self):
        return self.client.get(reverse("pin.details", args=[self.pin.slug]))

    def test_aliases_panel_listens_for_the_event(self) -> None:
        response = self._page()
        self.assertContains(response, "pinAliasesChanged from:body")

    def test_links_card_listens_for_the_event(self) -> None:
        response = self._page()
        self.assertContains(response, "pinLinksChanged from:body")

    def test_overview_card_listens_for_the_event(self) -> None:
        response = self._page()
        self.assertContains(response, "pinOverviewChanged from:body")

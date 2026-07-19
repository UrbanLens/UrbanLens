"""Regression tests for the pin detail page's "external panel 204 removal" marker.

A hardcoded JS Set of section ids (previously named `_extSections`) decided which
auto-loading external-data cards got their "Loading..." placeholder removed when
a fetch legitimately found nothing (204 No Content - HTMX does not swap on 204,
so without this handling the placeholder is stuck forever). That Set had already
drifted out of sync with the actual panels on the page - missing Azure Maps, GDELT
(News), Photon, and (once added) EPA Site Details and Building Characteristics -
so any of those returning 204 left a permanently-spinning card with no console
error and no server-side error either, since the fetch itself succeeded.

Replaced with a `data-ext-panel-204` attribute set directly on each qualifying
card (including the generic simple_info_panels loop, so any future panel added
there is covered automatically) and a JS handler keyed off that attribute instead
of a hand-maintained id list. These tests just confirm every card that's supposed
to carry the marker actually renders it - the JS 204/error/timeout handling itself
isn't unit-testable here (no browser), matching this page's existing JS-only fixes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest import mock

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase

if TYPE_CHECKING:
    from urbanlens.dashboard.models.pin.model import Pin


class ExtPanel204MarkerTests(TestCase):
    """Every auto-loading external-data card must carry data-ext-panel-204."""

    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)
        self.pin: Pin = baker.make_recipe("dashboard.pin", profile=self.profile)

    def _content(self) -> str:
        response = self.client.get(reverse("pin.details", args=[self.pin.slug]))
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def test_bespoke_cards_carry_the_marker(self) -> None:
        """Cards with their own dedicated controller/route (not the generic simple_info_panels loop).

        Nominatim is deliberately excluded here - it moved into the "Location
        Data" tab strip (see LocationDataTabsTests) and is no longer a
        standalone auto-loading card, so it no longer carries this marker.
        """
        content = self._content()
        for section_id in (
            "wikipedia-section",
            "azure-maps-section",
            "yelp-section",
            "nps-section",
            "loopnet-section",
            "usgs-topo-section",
            "satellite-view-section",
            "street-view-section",
            "pin-markup-maps-panel",
        ):
            self.assertIn(
                f'id="{section_id}"',
                content,
                f"{section_id} not found in rendered page",
            )
            # The marker must be on the same element as the id, not just anywhere on
            # the page - check they're within a short distance of each other.
            idx = content.index(f'id="{section_id}"')
            self.assertIn("data-ext-panel-204", content[max(0, idx - 200) : idx + 200], f"{section_id} is missing data-ext-panel-204")

    def test_web_search_section_carries_the_marker_when_rendered(self) -> None:
        from urbanlens.dashboard.models.subscriptions import SiteFeature, SubscriptionRole, grant_subscription

        self.pin.location.official_name = "Old Mill Factory"
        self.pin.location.save(update_fields=["official_name"])

        role = baker.make(SubscriptionRole, features=SiteFeature.SEARCH)
        grant_subscription(self.user, role, self.user, None)

        content = self._content()
        self.assertIn('id="web-search-section"', content)
        idx = content.index('id="web-search-section"')
        self.assertIn("data-ext-panel-204", content[max(0, idx - 200) : idx + 200])

    def test_generic_loop_panels_carry_the_marker(self) -> None:
        """gdelt/epa_echo_detail - panels from the same bug report stuck on "Loading..."
        forever - come from the same simple_info_panels loop, which now carries the
        marker unconditionally.

        photon/overture_building_attributes are deliberately excluded here - they
        moved into the "Location Data" tab strip (see LocationDataTabsTests) and
        are no longer part of simple_info_panels.
        """
        content = self._content()
        response = self.client.get(reverse("pin.details", args=[self.pin.slug]))
        panel_keys = [panel.key for panel in response.context["simple_info_panels"]]
        self.assertTrue(panel_keys, "simple_info_panels was empty - can't verify the marker on it")
        self.assertNotIn("photon", panel_keys, "photon should be excluded to simple_info_panels - it belongs to location_data_tabs now")
        self.assertNotIn("overture_building_attributes", panel_keys, "overture_building_attributes should be excluded from simple_info_panels - it belongs to location_data_tabs now")
        for key in ("gdelt", "epa_echo_detail"):
            self.assertIn(key, panel_keys, f"{key} is no longer part of simple_info_panels - update this test")

        # Every div opened by the simple_info_panels loop shares one hx-get pattern;
        # confirm the marker attribute appears at least once per rendered panel by
        # counting div-opens for the loop's pin.panel route against marker occurrences
        # scoped to those same divs.
        for key in panel_keys:
            marker = f'hx-get="{reverse('pin.panel', args=[self.pin.slug, key])}"'
            self.assertIn(marker, content, f"panel {key} not rendered with the expected hx-get")
            idx = content.index(marker)
            # data-ext-panel-204 is on the same <div ...> as the id, which precedes hx-get.
            self.assertIn("data-ext-panel-204", content[max(0, idx - 300) : idx], f"panel {key} is missing data-ext-panel-204")

    def test_js_handler_uses_the_attribute_not_a_hardcoded_list(self) -> None:
        """Regression guard against reintroducing a hand-maintained id Set that can drift out of sync."""
        content = self._content()
        self.assertIn("hasAttribute('data-ext-panel-204')", content)
        self.assertNotIn("_extSections", content)


class PendingPanelPlaceholderMarkerTests(TestCase):
    """The self-polling "still fetching" placeholder (panel_pending.html) must carry
    the same data-ext-panel-204 marker as the panel it stands in for.

    It didn't: the marker was applied to the section's initial (first-load)
    render only, not to panel_pending.html's own outer div - so once a panel's
    background fetch hadn't landed yet and the page swapped in this
    self-polling placeholder via outerHTML, the marker was gone from that
    point on. If the poll budget then ran out (MAX_POLL_ATTEMPTS) and the
    server's final response was a 204, the page's 204-removal handler no
    longer recognized the element (isExtPanel204 checks the attribute, which
    no longer existed) and the "Loading..." placeholder never got removed -
    only a full page reload (which reconstructs the section fresh, with the
    marker) fixed it. This is the "stuck in a loading state" bug report.
    """

    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)
        self.pin: Pin = baker.make_recipe("dashboard.pin", profile=self.profile)
        # schedule_panel_fetch's apply_async would otherwise try to reach a
        # real Celery broker/result-backend - not available in this test
        # environment (matches the established pattern in
        # test_external_apis_toggle.py).
        patcher = mock.patch("urbanlens.dashboard.tasks.fetch_panel_source")
        self.addCleanup(patcher.stop)
        patcher.start()

    def test_pending_placeholder_carries_the_marker(self) -> None:
        # A freshly-created pin's panel cache is cold, so this hits the
        # not-ready branch and returns panel_pending.html's fragment directly.
        response = self.client.get(reverse("pin.satellite_view", args=[self.pin.slug]))
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertIn("data-ext-panel-204", content)
        self.assertIn('id="satellite-view-section"', content)

    def test_pending_placeholder_keeps_the_marker_on_a_second_poll(self) -> None:
        first = self.client.get(reverse("pin.satellite_view", args=[self.pin.slug]))
        self.assertIn("data-ext-panel-204", first.content.decode())
        second = self.client.get(reverse("pin.satellite_view", args=[self.pin.slug]), {"attempt": "1"})
        self.assertEqual(second.status_code, 200)
        self.assertIn("data-ext-panel-204", second.content.decode())


class ConsistentLoadingPlaceholderTests(TestCase):
    """Every initial "Loading..." placeholder on the pin detail page uses the same
    small per-panel .view-loading spinner - a user reported the one at the very
    top of the page (under the title, before #pin-overview's first hx-load swap)
    as a jarring full-width gray bar, distinct from every other panel's spinner
    because it used ad-hoc inline styles instead of the shared class. Several
    other sections further down the page (Visit History, Categories, Aliases,
    Custom Fields, Ownership, Markup Maps) had the identical anti-pattern."""

    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)
        self.pin: Pin = baker.make_recipe("dashboard.pin", profile=self.profile)

    def _content(self) -> str:
        response = self.client.get(reverse("pin.details", args=[self.pin.slug]))
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def test_no_ad_hoc_inline_styled_loading_placeholder_remains(self) -> None:
        content = self._content()
        self.assertNotIn('style="padding:1rem;color:#888;font-size:.85rem;"', content)

    def test_top_of_page_overview_placeholder_uses_the_shared_spinner(self) -> None:
        content = self._content()
        idx = content.index('id="pin-overview"')
        self.assertIn("view-loading", content[idx : idx + 600])


class LocationDataTabsTests(TestCase):
    """OpenStreetMap (Nominatim), Photon, and Building Characteristics used to be
    three separate standalone cards with no explanation of how they related to
    one another - merged into one "Location Data" card with tabs (see
    _pin_location_data_tabs.html and PinController.view's location_data_tabs)."""

    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)
        self.pin: Pin = baker.make_recipe("dashboard.pin", profile=self.profile)

    def _content(self) -> str:
        response = self.client.get(reverse("pin.details", args=[self.pin.slug]))
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def test_renders_one_card_with_all_three_tabs(self) -> None:
        content = self._content()
        self.assertIn('id="location-data-section"', content)
        self.assertIn(f'hx-get="{reverse("pin.nominatim", args=[self.pin.slug])}"', content)
        self.assertIn(f'hx-get="{reverse("pin.panel", args=[self.pin.slug, "photon"])}"', content)
        self.assertIn(f'hx-get="{reverse("pin.panel", args=[self.pin.slug, "overture_building_attributes"])}"', content)

    def test_no_longer_renders_as_separate_standalone_cards(self) -> None:
        content = self._content()
        self.assertNotIn('id="nominatim-section"', content)

    def test_tab_204_handler_present_to_avoid_a_stuck_spinner(self) -> None:
        """Regression guard: a tab button's hx-target is a shared body div, not
        itself, so the generic data-ext-panel-204 handler (which just removes
        the element carrying the marker) can't apply here - there must be a
        dedicated handler keyed off .pin-plugin-tab-btn instead."""
        content = self._content()
        self.assertIn("isPluginTabBtn", content)

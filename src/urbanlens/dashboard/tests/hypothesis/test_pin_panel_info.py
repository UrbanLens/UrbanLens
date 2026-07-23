"""Tests for PinController.panel_info(), the generic dispatch for InfoPanelSource panels.

Covers the shared plumbing (404/204/pending/render paths) that every
InfoPanelSource-based plugin panel (Photon, US Census Geography, EPA
Regulated Facilities, iNaturalist, News, Building Characteristics, Recent
Seismic Activity) now relies on instead of hand-written controller methods.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest import mock

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.models.cache.location_cache import LocationCache
from urbanlens.dashboard.services.external_data import InfoPanelSource, panel_sources

if TYPE_CHECKING:
    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.models.pin.model import Pin


class PanelInfoDispatchTests(TestCase):
    """PinController.panel_info() - generic routing shared by every InfoPanelSource."""

    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)
        self.pin: Pin = baker.make_recipe("dashboard.pin", profile=self.profile)

    def test_unknown_panel_key_returns_404(self) -> None:
        response = self.client.get(reverse("pin.panel", args=[self.pin.slug, "not_a_real_panel"]))
        self.assertEqual(response.status_code, 404)

    def test_panel_key_for_a_non_info_panel_returns_404(self) -> None:
        """core/bespoke panels (e.g. "boundary", "wikipedia") aren't reachable through this route."""
        response = self.client.get(reverse("pin.panel", args=[self.pin.slug, "boundary"]))
        self.assertEqual(response.status_code, 404)

    def test_pin_owned_by_another_user_returns_404(self) -> None:
        other_pin: Pin = baker.make_recipe("dashboard.pin")
        response = self.client.get(reverse("pin.panel", args=[other_pin.slug, "photon"]))
        self.assertEqual(response.status_code, 404)

    def test_coordinate_gated_panel_returns_204_at_null_island(self) -> None:
        """(0, 0) is the "never geocoded" sentinel - effective_latitude/longitude are never
        actually None (Location.latitude/longitude are non-nullable, and immutable once
        set), so the gate checks falsiness, which only (0, 0) coordinates satisfy."""
        location: Location = baker.make("dashboard.Location", latitude=0, longitude=0)
        pin: Pin = baker.make_recipe("dashboard.pin", profile=self.profile, location=location)
        response = self.client.get(reverse("pin.panel", args=[pin.slug, "photon"]))
        self.assertEqual(response.status_code, 204)

    def test_ungated_panel_proceeds_at_null_island(self) -> None:
        """gdelt has no coordinate gate - it should reach the cache-miss/pending path, not 204 early."""
        location: Location = baker.make("dashboard.Location", latitude=0, longitude=0)
        pin: Pin = baker.make_recipe("dashboard.pin", profile=self.profile, location=location)
        with mock.patch("urbanlens.dashboard.tasks.fetch_panel_source"):
            response = self.client.get(reverse("pin.panel", args=[pin.slug, "gdelt"]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "News")

    def test_cache_miss_schedules_fetch_and_returns_pending_placeholder(self) -> None:
        with mock.patch("urbanlens.dashboard.tasks.fetch_panel_source") as fetch_task:
            response = self.client.get(reverse("pin.panel", args=[self.pin.slug, "photon"]))
        self.assertEqual(response.status_code, 200)
        fetch_task.apply_async.assert_called_once_with(args=("photon", self.pin.pk), kwargs={}, queue="panel_fetch")
        self.assertContains(response, "Photon (OpenStreetMap)")

    def test_render_context_returning_none_yields_204(self) -> None:
        """An empty/unhelpful cached result (render_context -> None) degrades to 204."""
        LocationCache.set(self.pin.location, "photon", {}, query_key="")
        response = self.client.get(reverse("pin.panel", args=[self.pin.slug, "photon"]))
        self.assertEqual(response.status_code, 204)

    def test_renders_the_panel_when_cached_data_is_present(self) -> None:
        LocationCache.set(
            self.pin.location,
            "photon",
            {"name": "Test Building", "osm_value": "historic_building", "city": "Poughkeepsie"},
            query_key="",
        )
        response = self.client.get(reverse("pin.panel", args=[self.pin.slug, "photon"]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Test Building")
        self.assertContains(response, "Poughkeepsie")

    def test_list_shaped_panel_renders_its_items(self) -> None:
        LocationCache.set(
            self.pin.location,
            "usgs_earthquakes",
            {"events": [{"magnitude": 4.2, "place": "10km N of Nowhere", "occurred_at": "2026-01-01T00:00:00Z", "url": "https://example.com"}]},
            query_key="",
        )
        response = self.client.get(reverse("pin.panel", args=[self.pin.slug, "usgs_earthquakes"]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "10km N of Nowhere")


class PanelAiExtractButtonTests(TestCase):
    """AI extract buttons inside generic panels: opt-in per link, gated on the AI feature."""

    def setUp(self) -> None:
        super().setUp()
        # The first user created in a test DB is auto-promoted to site admin,
        # which carries every subscription feature - burn that promotion on a
        # bystander so self.user's feature set is actually gate-controlled.
        baker.make(User)
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)
        self.pin: Pin = baker.make_recipe("dashboard.pin", profile=self.profile)
        LocationCache.set(
            self.pin.location,
            "gdelt",
            {"articles": [{"date": "2024-01-01", "title": "Mill fire investigated", "url": "https://news.example.com/mill-fire"}]},
            query_key="",
        )

    def _grant_ai(self) -> None:
        from urbanlens.dashboard.models.site_settings import SiteSettings
        from urbanlens.dashboard.models.subscriptions.model import SiteFeature

        settings_obj = SiteSettings.get_current()
        SiteSettings.objects.filter(pk=settings_obj.pk).update(default_features=SiteFeature.AI)

    def test_no_button_without_the_ai_feature(self) -> None:
        response = self.client.get(reverse("pin.panel", args=[self.pin.slug, "gdelt"]))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "ai-extract-btn")

    def test_flagged_links_get_the_button_with_the_feature(self) -> None:
        self._grant_ai()
        response = self.client.get(reverse("pin.panel", args=[self.pin.slug, "gdelt"]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "ai-extract-btn")
        self.assertContains(response, "https://news.example.com/mill-fire")

    def test_unflagged_links_never_get_the_button(self) -> None:
        """photon's footer_link doesn't opt in, so no button renders even with the feature."""
        self._grant_ai()
        LocationCache.set(
            self.pin.location,
            "photon",
            {"name": "Test Building", "osm_url": "https://www.openstreetmap.org/node/1"},
            query_key="",
        )
        response = self.client.get(reverse("pin.panel", args=[self.pin.slug, "photon"]))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "ai-extract-btn")


class SimpleInfoPanelsRegistryTests(SimpleTestCase):
    """panel_sources() correctly classifies the migrated InfoPanelSource plugins."""

    def test_all_seven_migrated_panels_are_registered_as_info_panels(self) -> None:
        sources = panel_sources()
        expected_keys = {
            "photon",
            "census_tigerweb",
            "epa_echo",
            "epa_echo_detail",
            "inaturalist",
            "gdelt",
            "overture_building_attributes",
            "usgs_earthquakes",
        }
        for key in expected_keys:
            self.assertIn(key, sources, f"{key} missing from panel_sources()")
            self.assertIsInstance(sources[key], InfoPanelSource, f"{key} is not an InfoPanelSource")

    def test_core_panels_are_not_info_panels(self) -> None:
        sources = panel_sources()
        for key in ("boundary", "satellite", "street_view"):
            self.assertNotIsInstance(sources[key], InfoPanelSource)


class PinDetailPageSimpleInfoPanelsContextTests(TestCase):
    """PinController.view() exposes simple_info_panels for the generic template loop."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # first user is auto-promoted to bootstrap site admin (has_perm bypasses feature gating)
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)
        self.pin: Pin = baker.make_recipe("dashboard.pin", profile=self.profile)

    def test_pin_details_page_lists_migrated_panels_via_the_generic_route(self) -> None:
        response = self.client.get(reverse("pin.details", args=[self.pin.slug]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, reverse("pin.panel", args=[self.pin.slug, "photon"]))
        self.assertContains(response, reverse("pin.panel", args=[self.pin.slug, "usgs_earthquakes"]))


class PinDetailHeroSubnavTests(TestCase):
    """The pin detail page has a standard page hero + subnav (like every other page)."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # first user is auto-promoted to bootstrap site admin
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)
        self.pin: Pin = baker.make_recipe("dashboard.pin", profile=self.profile, name="Old Mill", name_is_user_provided=True)

    def test_hero_renders_the_pin_name(self) -> None:
        response = self.client.get(reverse("pin.details", args=[self.pin.slug]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="pin-detail-hero"')
        self.assertContains(response, 'id="pin-detail-hero-title"')
        content = response.content.decode()
        hero_title = content.split('id="pin-detail-hero-title"')[1].split("</h1>")[0]
        self.assertIn("Old Mill", hero_title)

    def test_subnav_uses_the_shared_subnav_classes(self) -> None:
        response = self.client.get(reverse("pin.details", args=[self.pin.slug]))
        content = response.content.decode()
        self.assertIn("ul-page-subnav", content)
        self.assertIn("ul-subnav-tabs", content)
        # Four tabs, still driven by the same data-tab attributes page-tabs.js reads.
        for tab in ("overview", "article", "comments", "history"):
            self.assertIn(f'data-tab="{tab}"', content)

    def test_content_is_wrapped_in_the_standard_page_content_div(self) -> None:
        response = self.client.get(reverse("pin.details", args=[self.pin.slug]))
        self.assertContains(response, "page-content")

    def test_hero_and_subnav_render_outside_the_htmx_overview_panel(self) -> None:
        """The hero/subnav must be present on the initial full page load, not
        only after the #pin-overview panel's own hx-trigger="load" fetch completes -
        otherwise every tab (Article/Comments/History) would flash with no hero."""
        response = self.client.get(reverse("pin.details", args=[self.pin.slug]))
        content = response.content.decode()
        hero_pos = content.find('id="pin-detail-hero"')
        overview_panel_pos = content.find('id="pin-overview"')
        self.assertNotEqual(hero_pos, -1)
        self.assertNotEqual(overview_panel_pos, -1)
        self.assertLess(hero_pos, overview_panel_pos)

    def test_condensed_panels_are_excluded_from_the_autoloading_list(self) -> None:
        """Census/iNaturalist/Seismic/EPA's nearby-list all move into the single "Regional Data"
        tab strip (panel_tabs) instead of auto-loading as their own standalone cards. EPA's
        exact-site detail card is a different key (epa_echo_detail) and still auto-loads
        unconditionally."""
        response = self.client.get(reverse("pin.details", args=[self.pin.slug]))
        keys = [panel.key for panel in response.context["simple_info_panels"]]
        for condensed_key in ("census_tigerweb", "epa_echo", "inaturalist", "usgs_earthquakes"):
            self.assertNotIn(condensed_key, keys)
        self.assertIn("photon", keys)
        self.assertIn("epa_echo_detail", keys)

    def test_panel_tabs_context_has_the_expected_order_and_labels(self) -> None:
        """Without the Nearby Research feature, panel_tabs is just the always-available ones -
        see NearbyResearchTabGatingTests for the combined-with-EPA case."""
        response = self.client.get(reverse("pin.details", args=[self.pin.slug]))
        tabs = response.context["panel_tabs"]
        self.assertEqual([tab["key"] for tab in tabs], ["census_tigerweb", "inaturalist", "usgs_earthquakes"])
        self.assertEqual([tab["label"] for tab in tabs], ["US Census", "Wildlife", "Seismic"])

    def test_page_renders_the_regional_data_tab_strip(self) -> None:
        response = self.client.get(reverse("pin.details", args=[self.pin.slug]))
        self.assertContains(response, "Regional Data")
        self.assertContains(response, "pin-plugin-tab-btn")
        self.assertContains(response, ">US Census<")
        self.assertContains(response, ">Wildlife<")
        self.assertContains(response, ">Seismic<")
        # Each tab button still points at the same generic per-key dispatch
        # route used everywhere else - just triggered by a click, not page load.
        self.assertContains(response, reverse("pin.panel", args=[self.pin.slug, "census_tigerweb"]))

    def test_condensed_panels_no_longer_have_an_autoload_trigger(self) -> None:
        response = self.client.get(reverse("pin.details", args=[self.pin.slug]))
        content = response.content.decode()
        for condensed_key in ("census_tigerweb", "epa_echo", "inaturalist", "usgs_earthquakes"):
            self.assertNotIn(f"hx-trigger=\"load[!window.ulSectionCollapsed('pin','{condensed_key}')]", content)

    def test_epa_exact_site_detail_panel_still_has_an_autoload_trigger(self) -> None:
        """Unlike epa_echo (nearby list), epa_echo_detail is not tab-gated - it's a normal auto-loading card."""
        response = self.client.get(reverse("pin.details", args=[self.pin.slug]))
        content = response.content.decode()
        self.assertIn("hx-trigger=\"load[!window.ulSectionCollapsed('pin','epa_echo_detail')]", content)

    def test_tab_body_carries_the_chrome_stripping_class(self) -> None:
        """Regression guard: the nested-card-chrome-stripping CSS rule
        (_pin-detail.scss's .pin-plugin-tab-body) is a class selector, but the
        tab body div used to only ever carry that string as its id - a class
        selector never matches an id, so it silently never applied to any
        panel rendered inside a tab."""
        response = self.client.get(reverse("pin.details", args=[self.pin.slug]))
        self.assertContains(response, 'id="pin-plugin-tab-body" class="card__body pin-plugin-tab-body"')


class NearbyResearchTabGatingTests(TestCase):
    """EPA's nearby-facility list tab (appended to the single "Regional Data" tab
    strip - see PinController.view's panel_tabs) is subscription-gated; the always-
    available tabs (Census/Wildlife/Seismic) show either way, in the same card."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # first user is auto-promoted to bootstrap site admin (has_perm bypasses feature gating)
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)
        self.pin: Pin = baker.make_recipe("dashboard.pin", profile=self.profile)

    def test_hidden_without_the_feature(self) -> None:
        response = self.client.get(reverse("pin.details", args=[self.pin.slug]))
        self.assertNotContains(response, reverse("pin.panel", args=[self.pin.slug, "epa_echo"]))
        tabs = response.context["panel_tabs"]
        self.assertNotIn("epa_echo", [tab["key"] for tab in tabs])
        # The always-available tabs still render in the same "Regional Data" card.
        self.assertContains(response, "Regional Data")

    def test_shown_with_the_feature(self) -> None:
        from urbanlens.dashboard.models.subscriptions import SiteFeature, SubscriptionRole, grant_subscription

        role = baker.make(SubscriptionRole, features=SiteFeature.NEARBY_RESEARCH)
        grant_subscription(self.user, role, self.user, None)

        response = self.client.get(reverse("pin.details", args=[self.pin.slug]))
        self.assertContains(response, reverse("pin.panel", args=[self.pin.slug, "epa_echo"]))
        self.assertContains(response, ">EPA<")

    def test_panel_tabs_context_has_epa_appended_last(self) -> None:
        from urbanlens.dashboard.models.subscriptions import SiteFeature, SubscriptionRole, grant_subscription

        role = baker.make(SubscriptionRole, features=SiteFeature.NEARBY_RESEARCH)
        grant_subscription(self.user, role, self.user, None)

        response = self.client.get(reverse("pin.details", args=[self.pin.slug]))
        tabs = response.context["panel_tabs"]
        self.assertEqual([tab["key"] for tab in tabs], ["census_tigerweb", "inaturalist", "usgs_earthquakes", "epa_echo"])
        self.assertEqual(tabs[-1]["label"], "EPA")

    def test_only_one_tab_strip_card_renders_on_the_page(self) -> None:
        """Regression guard: Regional Data and Nearby Research used to be two
        separate cards - now there's exactly one, so the old second section id
        must never appear."""
        from urbanlens.dashboard.models.subscriptions import SiteFeature, SubscriptionRole, grant_subscription

        role = baker.make(SubscriptionRole, features=SiteFeature.NEARBY_RESEARCH)
        grant_subscription(self.user, role, self.user, None)

        response = self.client.get(reverse("pin.details", args=[self.pin.slug]))
        self.assertContains(response, 'id="pin-plugin-tabs-section"')
        self.assertNotContains(response, "pin-nearby-research-section")

    def test_epa_nearby_list_not_in_autoloading_panels_even_with_the_feature(self) -> None:
        """Nearby Research tabs are still click-to-load, like Regional Data - the feature only
        controls whether the tab strip is shown at all, not whether it auto-fetches."""
        from urbanlens.dashboard.models.subscriptions import SiteFeature, SubscriptionRole, grant_subscription

        role = baker.make(SubscriptionRole, features=SiteFeature.NEARBY_RESEARCH)
        grant_subscription(self.user, role, self.user, None)

        response = self.client.get(reverse("pin.details", args=[self.pin.slug]))
        keys = [panel.key for panel in response.context["simple_info_panels"]]
        self.assertNotIn("epa_echo", keys)

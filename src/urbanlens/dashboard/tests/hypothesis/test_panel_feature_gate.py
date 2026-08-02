"""The subscription gate on a ``PanelSource``, enforced on the server and not only in the UI.

``PanelSource.required_feature`` states, as a fact about the source itself,
which :class:`~urbanlens.dashboard.models.subscriptions.SiteFeature` a viewer
must hold before that source's data may be shown to them. Today exactly one
source declares it - EPA ECHO's *nearby*-facilities list (its exact-site
compliance card is deliberately free) - but the property being pinned here is
not about EPA at all:

1. **The gate is enforced where the data is served, not only where the tab is
   drawn.** Hiding the tab strip is a presentation choice; ``panel_info`` is a
   plain URL, and until this test existed any logged-in user could type
   ``/map/pin/<slug>/panel/epa_echo/`` and receive the gated payload in full.
   That is the live bug these tests were written against.
2. **A gated panel is refused as 404, never 403.** A 403 answers "yes, that
   panel exists and has data for this pin, you just aren't paying" - which is
   most of what the paywalled panel knows. The refusal is therefore made
   byte-identical to the response for a panel key that does not exist at all.
3. **Tab assembly reads the source's own declaration.** The controller used to
   carry its own hardcoded list of which tabs were gated; two records of the
   same fact drift, and the direction they drift in is a panel that is hidden
   in one place and served in another. The tests below prove the gate follows
   ``required_feature`` by moving it onto a source that has never been gated
   and watching that source disappear.
4. **The fetch is never scheduled for a viewer who may not see the result.**
   Refusing after ``_pending_panel`` would still spend an upstream API call (and
   EPA ECHO's budget is 5 calls/minute) on behalf of someone who will never be
   shown the answer.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest import mock

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.cache.location_cache import LocationCache
from urbanlens.dashboard.models.subscriptions import SiteFeature, SubscriptionRole, grant_subscription
from urbanlens.dashboard.plugins.builtin.epa_echo import EpaEchoDetailPanelSource, EpaEchoNearbyPanelSource
from urbanlens.dashboard.services.pins.external_data import get_panel_source

if TYPE_CHECKING:
    from urbanlens.dashboard.models.pin.model import Pin

#: A cached EPA payload rich enough that both panels render a real card - the
#: gate must be what refuses the request, not an empty payload's own 204, which
#: would make a broken gate look like a working one.
_EPA_DATA = {
    "facilities": [
        {"registry_id": "110000000001", "name": "Nearby Plating Works", "address": "1 Industrial Way", "compliance_status": "Violation", "significant_violator": True, "latitude": 40.02},
        {"registry_id": "110000000002", "name": "Nearby Rendering Plant", "address": "2 Industrial Way", "compliance_status": "No Violation", "latitude": 40.03},
    ],
    "exact_site": {"registry_id": "110000000009", "name": "The Pinned Facility", "address": "9 Industrial Way", "latitude": 40.0, "longitude": -74.0, "programs": []},
}


class GatedPanelServingTests(TestCase):
    """``PinController.panel_info`` must refuse a source the viewer's plan excludes."""

    def setUp(self) -> None:
        """A subscription-less pin owner, with EPA data already cached for their pin.

        The very first user created in a fresh test database is auto-promoted to
        bootstrap site admin, and ``user_has_feature`` grants a site admin every
        feature - so a throwaway user absorbs that promotion and the user under
        test is an ordinary one.
        """
        super().setUp()
        baker.make(User)
        self.user = baker.make(User)
        self.client.force_login(self.user)
        self.pin: Pin = baker.make_recipe("dashboard.pin", profile=self.user.profile)
        LocationCache.set(self.pin.location, "epa_echo", _EPA_DATA, query_key="40.00000,-74.00000")

    def _grant_nearby_research(self) -> None:
        """Give the user under test an active subscription carrying the gated feature."""
        role = baker.make(SubscriptionRole, features=SiteFeature.NEARBY_RESEARCH)
        grant_subscription(self.user, role, self.user, None)

    def test_viewer_without_the_feature_cannot_fetch_the_gated_panel_directly(self) -> None:
        """The live bug: the UI hid the tab, the URL served the data anyway."""
        response = self.client.get(reverse("pin.panel", args=[self.pin.slug, "epa_echo"]))
        self.assertEqual(response.status_code, 404)

    def test_refusal_is_indistinguishable_from_an_unknown_panel_key(self) -> None:
        """403 would confirm the panel exists and holds data for this pin.

        Compared against a key no source has ever claimed, so this stays true
        even if the "no such panel" response itself is ever changed.
        """
        gated = self.client.get(reverse("pin.panel", args=[self.pin.slug, "epa_echo"]))
        nonexistent = self.client.get(reverse("pin.panel", args=[self.pin.slug, "not_a_real_panel"]))
        self.assertEqual(gated.status_code, nonexistent.status_code)
        self.assertEqual(gated.content, nonexistent.content)

    def test_viewer_without_the_feature_never_schedules_the_upstream_fetch(self) -> None:
        """Refusing late would still spend EPA's 5-calls/minute budget for nothing."""
        LocationCache.objects.filter(location=self.pin.location, source="epa_echo").delete()
        with mock.patch("urbanlens.dashboard.tasks.fetch_panel_source") as fetch_task:
            response = self.client.get(reverse("pin.panel", args=[self.pin.slug, "epa_echo"]))
        self.assertEqual(response.status_code, 404)
        fetch_task.apply_async.assert_not_called()

    def test_viewer_with_the_feature_still_gets_the_panel(self) -> None:
        """The gate must refuse non-subscribers without breaking subscribers."""
        self._grant_nearby_research()
        response = self.client.get(reverse("pin.panel", args=[self.pin.slug, "epa_echo"]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Nearby Plating Works")

    def test_the_ungated_sibling_panel_is_untouched(self) -> None:
        """EPA's exact-site card is the integration's free, primary purpose.

        Both panels read the same cache row, so a gate keyed on the row (rather
        than on the source) would take the free card down with the paid one.
        """
        response = self.client.get(reverse("pin.panel", args=[self.pin.slug, "epa_echo_detail"]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "The Pinned Facility")

    def test_an_ungated_panel_gains_the_gate_when_its_source_declares_one(self) -> None:
        """The controller consults the source, not a private list of panel keys.

        Photon has never been gated; moving ``required_feature`` onto it must be
        enough to make ``panel_info`` refuse it, or the controller is still
        deciding this for itself somewhere.
        """
        photon = get_panel_source("photon")
        assert photon is not None
        with mock.patch.object(type(photon), "required_feature", SiteFeature.NEARBY_RESEARCH):
            response = self.client.get(reverse("pin.panel", args=[self.pin.slug, "photon"]))
        self.assertEqual(response.status_code, 404)

    def test_a_gated_panel_loses_the_gate_when_its_source_drops_it(self) -> None:
        """The mirror image, so the test above can't pass by refusing everything."""
        with mock.patch.object(EpaEchoNearbyPanelSource, "required_feature", None):
            response = self.client.get(reverse("pin.panel", args=[self.pin.slug, "epa_echo"]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Nearby Plating Works")


class GatedPanelTabAssemblyTests(TestCase):
    """The pin page's tab strip omits a tab whose source the viewer may not see."""

    def setUp(self) -> None:
        """An ordinary (non-admin, unsubscribed) pin owner viewing their own pin."""
        super().setUp()
        baker.make(User)
        self.user = baker.make(User)
        self.client.force_login(self.user)
        self.pin: Pin = baker.make_recipe("dashboard.pin", profile=self.user.profile)

    def _tab_keys(self) -> list[str]:
        """Render the pin detail page and return its tab strip's source keys.

        Returns:
            The ``key`` of every tab in the page's ``panel_tabs`` context.
        """
        response = self.client.get(reverse("pin.details", args=[self.pin.slug]))
        self.assertEqual(response.status_code, 200)
        return [tab["key"] for tab in response.context["panel_tabs"]]

    def test_gated_tab_is_omitted_without_the_feature(self) -> None:
        """Baseline, and the behaviour the UI already had before the gate moved."""
        self.assertNotIn("epa_echo", self._tab_keys())

    def test_gated_tab_appears_with_the_feature(self) -> None:
        """Still appended after the always-available tabs, in its usual position."""
        role = baker.make(SubscriptionRole, features=SiteFeature.NEARBY_RESEARCH)
        grant_subscription(self.user, role, self.user, None)
        self.assertEqual(self._tab_keys()[-1], "epa_echo")

    def test_a_newly_gated_source_drops_out_of_the_tab_strip(self) -> None:
        """Tab assembly reads ``required_feature``, not a hardcoded key list.

        US Census is an always-available "Regional Data" tab that no controller
        constant marks as gated; declaring the feature on its source alone has
        to be enough to hide it, or the two records of "which panels are gated"
        have already begun to drift.
        """
        census = get_panel_source("census_tigerweb")
        assert census is not None
        with mock.patch.object(type(census), "required_feature", SiteFeature.NEARBY_RESEARCH):
            self.assertNotIn("census_tigerweb", self._tab_keys())

    def test_ungated_sibling_source_keeps_its_own_surfaces(self) -> None:
        """The free exact-site card is not a tab and must not be gated away."""
        self.assertIsNone(EpaEchoDetailPanelSource.required_feature)
        response = self.client.get(reverse("pin.details", args=[self.pin.slug]))
        self.assertIn("epa_echo_detail", [panel.key for panel in response.context["simple_info_panels"]])

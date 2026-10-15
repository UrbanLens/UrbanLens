"""The Ownership card and setup checklist render without any plugin that supplies their optional parts."""

from __future__ import annotations

from django.test import SimpleTestCase
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.controllers.setup import _build_feature_groups
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.property_owner.meta import OwnerSource
from urbanlens.dashboard.models.property_owner.model import WikiOwner
from urbanlens.dashboard.plugins.points import SETUP_FEATURE_GROUPS, SLOT_OWNERSHIP_WITHHELD
from urbanlens.UrbanLens.settings.app import settings as app_settings


class OwnershipCardWithoutPluginTests(TestCase):
    def setUp(self) -> None:
        baker.make("auth.User")  # the first user is promoted to site admin, which may see official owners
        self.user = baker.make("auth.User")
        self.profile = Profile.objects.get(user=self.user)
        self.location = baker.make(Location, latitude="41.400000", longitude="-73.400000")
        self.pin = baker.make(Pin, profile=self.profile, location=self.location, name="My Pin")
        self.county_owner = baker.make(WikiOwner, name="County Owner", source=OwnerSource.OFFICIAL)
        self.county_owner.locations.add(self.location)
        self.client.force_login(self.user)

    def test_the_card_renders_without_the_plugin_and_keeps_user_owners(self) -> None:
        self.client.post(reverse("pin.ownership", args=[self.pin.slug]), {"name": "My Landlord"})
        with SLOT_OWNERSHIP_WITHHELD.overridden([]):
            response = self.client.get(reverse("pin.ownership", args=[self.pin.slug]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "My Landlord")
        self.assertNotContains(response, "County Owner")


class SetupWebSearchRowTests(SimpleTestCase):
    def test_no_web_search_group_without_a_contributed_provider(self) -> None:
        with SETUP_FEATURE_GROUPS.overridden([]):
            labels = [group["label"] for group in _build_feature_groups(app_settings)]

        self.assertNotIn("Web Search", labels)
        self.assertIn("Maps & Geocoding", labels)

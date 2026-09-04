"""Relinking a pin must not be a way to *earn* access to a location.

Wiki visibility is deliberately gated on discovery: you see a location's
community wiki only once you hold a pin at it (or anywhere in its access
domain). ``location_visible_to`` grants on an exact ``Location`` match, so
"which Location does my pin point at" is not a neutral preference - it is the
thing that confers access.

``PinRelinkView`` scopes the *pin* to the requester but resolved the target
``Location`` straight from the URL slug with no visibility check. Since a
Location's slug is its ``official_name`` when it has one, the slug of any
notable place is guessable, and pointing your own pin at it grants you its wiki.

Every legitimate target already passes the check: the picker offers the pin's
current location plus ``competing_wiki_locations``, which filters to
``accessible_domain_ids``, and the wiki page's switch button offers the same
accessible candidates.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from django.test import override_settings
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.services.wiki.wiki_access import location_visible_to


@override_settings(CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}})
class PinRelinkAccessTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)

        # The requester's own, unrelated pin.
        self.own_location = Location.objects.create(latitude=40.0, longitude=-74.0)
        self.pin = baker.make(Pin, profile=self.profile, location=self.own_location)

        # A place they have never discovered, with a community wiki.
        self.undiscovered = Location.objects.create(
            latitude=41.5, longitude=-73.5, official_name="Hudson River State Hospital"
        )
        self.wiki = baker.make(Wiki, location=self.undiscovered, name="Hudson River State Hospital")

    def test_the_undiscovered_wiki_starts_out_of_reach(self) -> None:
        """Baseline - without this the rest of the test proves nothing."""
        self.assertFalse(location_visible_to(self.undiscovered, self.profile))
        self.assertEqual(self.client.get(reverse("location.wiki", args=[self.undiscovered.slug])).status_code, 404)

    def test_its_slug_is_guessable_from_the_place_name(self) -> None:
        """The attack needs a slug worth guessing; a named Location has one."""
        self.assertEqual(self.undiscovered.slug, "hudson-river-state-hospital")

    def test_relinking_a_pin_to_an_unreachable_location_is_refused(self) -> None:
        response = self.client.post(reverse("pin.link.to", args=[self.pin.slug, self.undiscovered.slug]))

        self.assertEqual(response.status_code, 404)
        self.pin.refresh_from_db()
        self.assertEqual(self.pin.location_id, self.own_location.pk, "the pin must not have been moved")

    def test_a_refused_relink_grants_no_wiki_access(self) -> None:
        """The point of the gate: the relink must not become a way in."""
        self.client.post(reverse("pin.link.to", args=[self.pin.slug, self.undiscovered.slug]))

        self.assertFalse(location_visible_to(self.undiscovered, self.profile))
        self.assertEqual(self.client.get(reverse("location.wiki", args=[self.undiscovered.slug])).status_code, 404)

    def test_relinking_to_a_location_the_viewer_can_already_see_still_works(self) -> None:
        """The legitimate flow - switching between overlapping properties - must survive."""
        reachable = Location.objects.create(latitude=40.000001, longitude=-74.000001)
        baker.make(Pin, profile=self.profile, location=reachable)
        self.assertTrue(location_visible_to(reachable, self.profile))

        response = self.client.post(reverse("pin.link.to", args=[self.pin.slug, reachable.slug]))

        self.assertNotEqual(response.status_code, 404)

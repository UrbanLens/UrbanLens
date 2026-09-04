"""Regression tests for ``location_slug`` in the external pin-detail payload.

The pin-detail payload shipped a ``wiki_slug`` field that reads naturally as
"the slug to navigate to this pin's wiki with", but
``services.wiki.wiki_access.resolve_visible_wiki`` - the resolver every wiki route
goes through - takes a *Location* slug/uuid, not a ``Wiki.slug``. The two are
independent fields on unrelated models, so a client that fed ``wiki_slug`` to
``GET /wikis/{location_slug}/`` got a 404 for a wiki it could plainly see.

These tests pin down the fix: the payload also carries ``location_slug``, and
that value actually resolves the wiki.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.services.auth.api_keys import generate_api_key
from urbanlens.dashboard.services.pins.pin_creation import create_pin_for_profile
from urbanlens.dashboard.services.pins.pin_detail import build_pin_detail
from urbanlens.dashboard.tests.hypothesis.test_external_api_wiki_oracle import grant_wiki_scopes


def _bearer(raw_key: str) -> dict:
    """Build the bearer-auth header kwargs for the test client."""
    return {"HTTP_AUTHORIZATION": f"Bearer {raw_key}"}


class PinDetailLocationSlugTests(TestCase):
    """The payload exposes a location slug that resolves the pin's wiki."""

    def setUp(self) -> None:
        baker.make(User)  # first user auto-promoted to bootstrap site admin
        self.user = baker.make(User)
        self.profile = Profile.objects.get(user=self.user)
        _api_key, self.raw_key = generate_api_key(self.user, "Detail client")
        # A new PAT gets only profile/pins/push by default; the wiki round-trip
        # below needs wiki:read as well.
        grant_wiki_scopes(self.user)
        self.pin = create_pin_for_profile(self.profile, name="Old Mill", latitude=42.5, longitude=-73.5).pin

    def test_payload_carries_the_locations_slug(self) -> None:
        """``location_slug`` is present and matches the pin's Location."""
        payload = build_pin_detail(self.pin, self.profile)
        self.assertEqual(payload["location_slug"], self.pin.location.ensure_slug())

    def test_location_slug_is_exposed_over_the_api(self) -> None:
        """The serializer passes ``location_slug`` through to the response."""
        response = self.client.get(
            f"/dashboard/api/external/v1/pins/{self.pin.slug or self.pin.uuid}/", **_bearer(self.raw_key)
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["location_slug"], self.pin.location.ensure_slug())

    def test_location_slug_resolves_the_wiki_but_wiki_slug_does_not(self) -> None:
        """The documented defect: only ``location_slug`` routes to the wiki.

        ``Wiki.slug`` is set deliberately different from the Location's here to
        make the independence explicit - that is exactly the shipped state that
        made ``wiki_slug`` unusable for navigation.
        """
        wiki = Wiki.objects.create(location=self.pin.location, name="Old Mill")
        Wiki.objects.filter(pk=wiki.pk).update(slug="a-totally-different-wiki-slug")
        wiki.refresh_from_db()
        # build_pin_detail reads wiki_slug from pin.wiki, not from the
        # Location's wiki - without this link the field is legitimately None
        # and the comparison below would prove nothing.
        Pin.objects.filter(pk=self.pin.pk).update(wiki=wiki)
        self.pin.refresh_from_db()

        payload = build_pin_detail(self.pin, self.profile)
        self.assertEqual(payload["wiki_slug"], "a-totally-different-wiki-slug")
        self.assertNotEqual(payload["location_slug"], payload["wiki_slug"])

        headers = _bearer(self.raw_key)
        ok = self.client.get(f"/dashboard/api/external/v1/wikis/{payload['location_slug']}/", **headers)
        self.assertEqual(ok.status_code, 200)

        broken = self.client.get(f"/dashboard/api/external/v1/wikis/{payload['wiki_slug']}/", **headers)
        self.assertEqual(broken.status_code, 404)

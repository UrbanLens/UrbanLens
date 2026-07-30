"""Manual pin <-> wiki child-marker sync over the external API.

The matching logic itself belongs to ``services.pin_wiki_sync`` and is tested
there; what these cover is everything the *endpoint* is responsible for, which
is exactly the set of things a naive port gets wrong:

1. **Ownership.** The service trusts the Pin it is handed - it writes child
   wikis attributed to a profile and creates pins owned by ``pin.profile``
   without re-checking anything. Resolving through the owner-scoped lookup is
   what stops a caller pushing someone else's pins onto a wiki.
2. **A missing wiki is a 200 with ``wiki_exists: false``**, not a 404. A 404
   here would be indistinguishable from "no such pin", and the client needs to
   tell "nothing to do" from "this property has no community page yet".
3. **Both scopes are required.** ``HasApiKeyScope`` requires every declared
   scope, so push needs ``pins:write`` *and* ``wiki:write`` - half the consent
   is not consent.
4. **The request body is bounded.** The service silently slices at
   ``MAX_SYNC_ITEMS``, so an unbounded list would be accepted, truncated, and
   reported as a complete success.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

from django.contrib.auth.models import User
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.account.model import ApiKey, ApiKeyScope
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.services.api_keys import generate_api_key
from urbanlens.dashboard.services.pin_creation import create_pin_for_profile

if TYPE_CHECKING:
    from urbanlens.dashboard.models.pin.model import Pin

BASE = "/dashboard/api/external/v1/pins"

_SYNC_SCOPES = [
    ApiKeyScope.PINS_READ.value,
    ApiKeyScope.PINS_WRITE.value,
    ApiKeyScope.WIKI_READ.value,
    ApiKeyScope.WIKI_WRITE.value,
]


class PinWikiSyncApiTests(TestCase):
    """POST push/pull between a pin's child pins and its wiki's child wikis."""

    def setUp(self) -> None:
        """Create the key owner, a parent pin, and a bystander."""
        baker.make(User)  # first user is auto-promoted to bootstrap site admin
        self.user = baker.make(User, username="owner")
        self.profile = Profile.objects.get(user=self.user)
        key, self.raw_key = generate_api_key(self.user, "Sync client")
        # A newly issued PAT carries only the four default scopes (profile:read,
        # pins:*, push:manage) - no wiki access at all - so the fixture key has
        # to be widened explicitly. The scope-denial tests below narrow it again
        # on purpose-built keys rather than relying on that default, which would
        # silently stop testing anything the day a scope picker ships.
        ApiKey.objects.filter(pk=key.pk).update(scopes=_SYNC_SCOPES)
        self.other_profile = Profile.objects.get(user=baker.make(User, username="bystander"))

        self.pin = create_pin_for_profile(self.profile, name="Old Mill", latitude=42.5, longitude=-73.5).pin

    def _headers(self, raw_key: str | None = None) -> dict:
        """Bearer-header kwargs for the fixture key, or an explicitly given one.

        Args:
            raw_key: A raw key to use instead of the fixture's.

        Returns:
            Request kwargs carrying the Authorization header.
        """
        return {"HTTP_AUTHORIZATION": f"Bearer {raw_key or self.raw_key}"}

    def _url(self, direction: str, *, pin_slug: str | None = None) -> str:
        """Build a wiki-sync URL.

        Args:
            direction: ``"push"`` or ``"pull"``.
            pin_slug: Pin to address; defaults to the fixture pin.

        Returns:
            The fully-built URL.
        """
        slug = pin_slug or self.pin.slug or str(self.pin.uuid)
        return f"{BASE}/{slug}/wiki-sync/{direction}/"

    def _key_with_scopes(self, scopes: list[str]) -> str:
        """Issue a second key carrying exactly *scopes*.

        Args:
            scopes: Raw scope values to store on the row.

        Returns:
            The raw key value.
        """
        api_key, raw = generate_api_key(self.user, "Scoped")
        ApiKey.objects.filter(pk=api_key.pk).update(scopes=scopes)
        return raw

    def _wiki(self) -> Wiki:
        """Give the fixture pin's location a community wiki.

        Returns:
            The created wiki.
        """
        return baker.make("dashboard.Wiki", location=self.pin.location, name="Old Mill")

    def _child_pin(self, latitude: float, longitude: float, name: str) -> Pin:
        """Add one child pin under the fixture pin.

        Args:
            latitude: The child's latitude.
            longitude: The child's longitude.
            name: The child's name.

        Returns:
            The created child pin.
        """
        location, _created = Location.objects.get_exact_or_create(latitude, longitude)
        return baker.make("dashboard.Pin", profile=self.profile, parent_pin=self.pin, location=location, name=name)

    def _push(self, uuids: list[str], *, raw_key: str | None = None):
        """POST a push with the given child-pin uuids.

        Args:
            uuids: Raw uuid strings for the body.
            raw_key: A raw key to use instead of the fixture's.

        Returns:
            The Django test-client response.
        """
        return self.client.post(self._url("push"), {"child_pin_uuids": uuids}, content_type="application/json", **self._headers(raw_key))

    def test_push_without_a_wiki_reports_wiki_exists_false(self) -> None:
        """A property with no community page is a 200 the client can explain."""
        child = self._child_pin(42.5001, -73.5001, "Garage")

        response = self._push([str(child.uuid)])

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"created": 0, "wiki_exists": False})

    def test_pull_without_a_wiki_reports_wiki_exists_false(self) -> None:
        """Same contract in the other direction, and no pins are invented."""
        response = self.client.post(self._url("pull"), **self._headers())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"created": 0, "wiki_exists": False})
        self.assertEqual(self.pin.detail_pins.count(), 0)

    def test_push_creates_a_child_wiki_per_selected_child_pin(self) -> None:
        """The happy path, straight through ``services.pin_wiki_sync``."""
        wiki = self._wiki()
        child = self._child_pin(42.5100, -73.5100, "Garage")

        response = self._push([str(child.uuid)])

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"created": 1, "wiki_exists": True})
        self.assertEqual(wiki.child_wikis.count(), 1)
        self.assertEqual(wiki.child_wikis.get().name, "Garage")

    def test_push_is_idempotent_because_the_marker_is_already_covered(self) -> None:
        """Re-pushing the same child matches the wiki it created and creates nothing."""
        self._wiki()
        child = self._child_pin(42.5100, -73.5100, "Garage")

        self._push([str(child.uuid)])
        second = self._push([str(child.uuid)])

        self.assertEqual(second.json(), {"created": 0, "wiki_exists": True})

    def test_push_ignores_uuids_that_are_not_this_pins_children(self) -> None:
        """A uuid naming another user's pin cannot be used to publish it.

        The filter is scoped to ``pin.detail_pins``, so a foreign uuid simply
        does not match - the same no-op the internal toolbar performs when a
        selection has gone stale.
        """
        self._wiki()
        their_pin = create_pin_for_profile(self.other_profile, name="Theirs", latitude=1.0, longitude=1.0).pin

        response = self._push([str(their_pin.uuid), str(uuid4())])

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"created": 0, "wiki_exists": True})
        self.assertEqual(Wiki.objects.filter(parent_wiki__isnull=False).count(), 0)

    def test_pull_creates_a_child_pin_per_uncovered_child_wiki(self) -> None:
        """The inverse direction fills in what the community has documented."""
        wiki = self._wiki()
        child_location, _created = Location.objects.get_exact_or_create(42.5200, -73.5200)
        baker.make("dashboard.Wiki", parent_wiki=wiki, location=child_location, name="Boiler house")

        response = self.client.post(self._url("pull"), **self._headers())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"created": 1, "wiki_exists": True})
        created_pin = self.pin.detail_pins.get()
        self.assertEqual(created_pin.name, "Boiler house")
        self.assertEqual(created_pin.profile, self.profile)

    def test_empty_selection_is_a_400(self) -> None:
        """An empty push is never a meaningful request, so it must not 200."""
        self._wiki()

        response = self._push([])

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "Invalid request.")
        self.assertIn("child_pin_uuids", response.json()["fields"])

    def test_an_oversized_selection_is_refused_rather_than_truncated(self) -> None:
        """The service slices at MAX_SYNC_ITEMS; silently succeeding would lie."""
        self._wiki()

        response = self._push([str(uuid4()) for _ in range(501)])

        self.assertEqual(response.status_code, 400)
        self.assertIn("child_pin_uuids", response.json()["fields"])

    def test_another_users_pin_is_not_found(self) -> None:
        """Both directions resolve through the owner-scoped pin lookup.

        Skipping that would not be a metadata leak - it would let a caller write
        to a stranger's pin hierarchy and to their property's community wiki.
        """
        their_pin = create_pin_for_profile(self.other_profile, name="Theirs", latitude=1.0, longitude=1.0).pin
        slug = their_pin.slug or str(their_pin.uuid)
        baker.make("dashboard.Wiki", location=their_pin.location, name="Theirs")

        push = self.client.post(
            self._url("push", pin_slug=slug),
            {"child_pin_uuids": [str(uuid4())]},
            content_type="application/json",
            **self._headers(),
        )
        pull = self.client.post(self._url("pull", pin_slug=slug), **self._headers())

        self.assertEqual(push.status_code, 404)
        self.assertEqual(push.json(), {"error": "Not found."})
        self.assertEqual(pull.status_code, 404)
        self.assertEqual(their_pin.detail_pins.count(), 0)

    def test_push_needs_both_pins_write_and_wiki_write(self) -> None:
        """Half the consent is not consent - the scope set is a genuine AND."""
        self._wiki()
        child = self._child_pin(42.5100, -73.5100, "Garage")

        for scopes in ([ApiKeyScope.PINS_WRITE.value], [ApiKeyScope.WIKI_WRITE.value]):
            with self.subTest(scopes=scopes):
                raw = self._key_with_scopes(scopes)
                response = self._push([str(child.uuid)], raw_key=raw)
                self.assertEqual(response.status_code, 403)

        self.assertEqual(Wiki.objects.filter(parent_wiki__isnull=False).count(), 0)

    def test_pull_needs_pins_write_and_wiki_read(self) -> None:
        """The inverse split: pulling creates pins, so read-only pin scope is refused."""
        wiki = self._wiki()
        child_location, _created = Location.objects.get_exact_or_create(42.5200, -73.5200)
        baker.make("dashboard.Wiki", parent_wiki=wiki, location=child_location, name="Boiler house")

        raw = self._key_with_scopes([ApiKeyScope.PINS_READ.value, ApiKeyScope.WIKI_READ.value])
        response = self.client.post(self._url("pull"), **self._headers(raw))

        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.pin.detail_pins.count(), 0)

    def test_full_sync_scopes_are_accepted(self) -> None:
        """The positive control for the two scope-denial cases above."""
        self._wiki()
        child = self._child_pin(42.5100, -73.5100, "Garage")
        raw = self._key_with_scopes(_SYNC_SCOPES)

        self.assertEqual(self._push([str(child.uuid)], raw_key=raw).status_code, 200)

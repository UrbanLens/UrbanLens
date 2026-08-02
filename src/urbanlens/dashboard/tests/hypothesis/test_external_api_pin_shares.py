"""Accepting and rejecting a pin someone shared with you, over the external API.

Three distinct risks are covered here, and only the first is ordinary CRUD.

**Anti-enumeration is load-bearing, and it is a write.** ``PinShare`` primary
keys are sequential integers. If the lookup were ``pk=share_id`` alone, a caller
could walk other people's inboxes and *accept or reject* their shares - not just
read metadata. ``to_profile=`` in the same ``get_object_or_404`` is the whole
defence, so it gets its own tests rather than being assumed.

**The provenance trap.** The project rule is that any pin/location share path
calls ``resolve_origin_share`` + ``record_share_exposure``. Applying it to
*acceptance* is wrong: the exposure already fired when the share was created,
and a second one duplicates the ``LocationExposure`` row that
``resolve_origin_share`` uses to pick a reshare chain's ancestor. The test below
asserts the row count is unchanged across an accept, so a future "fix" that
adds the call fails immediately instead of quietly corrupting lineage.

**Scope choice.** ``pins:write``, not ``messages:*``. The same ``PinShare`` is
delivered by bare notification as well as by message, and ``messages:*`` is
OAuth2-only, so scoping this to messaging would lock personal-access-token
holders out of notification-delivered shares entirely.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.account.model import ApiKey, ApiKeyScope
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.pin_share import LocationExposure, PinShare, PinShareStatus
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.services.auth.api_keys import generate_api_key
from urbanlens.dashboard.services.pins.pin_creation import create_pin_for_profile
from urbanlens.dashboard.services.sharing.share_provenance import record_share_exposure

BASE = "/dashboard/api/external/v1/pin-shares"


class PinShareRespondApiTests(TestCase):
    """POST accept/reject on a share addressed to the calling key's owner."""

    def setUp(self) -> None:
        """Create a recipient with a key, a sender, and a pending share between them."""
        baker.make(User)  # first user is auto-promoted to bootstrap site admin
        self.user = baker.make(User, username="recipient")
        self.profile = Profile.objects.get(user=self.user)
        _key, self.raw_key = generate_api_key(self.user, "Share client")

        self.sender = Profile.objects.get(user=baker.make(User, username="sender"))
        self.sender_pin = create_pin_for_profile(self.sender, name="Old Mill", latitude=42.5, longitude=-73.5).pin
        self.share = self._make_share(self.profile)

    def _make_share(self, recipient: Profile) -> PinShare:
        """Create a pending share of the sender's pin, exposure included.

        The exposure is recorded here because that is what happens in
        production at share creation - and it is exactly the row the
        acceptance path must not duplicate.

        Args:
            recipient: The profile the share is addressed to.

        Returns:
            The created, pending share.
        """
        share = PinShare.objects.create(
            pin=self.sender_pin,
            location=self.sender_pin.location,
            from_profile=self.sender,
            to_profile=recipient,
            status=PinShareStatus.PENDING,
        )
        record_share_exposure(share)
        return share

    def _headers(self, raw_key: str | None = None) -> dict:
        """Bearer-header kwargs for the fixture key, or an explicitly given one.

        Args:
            raw_key: A raw key to use instead of the fixture's.

        Returns:
            Request kwargs carrying the Authorization header.
        """
        return {"HTTP_AUTHORIZATION": f"Bearer {raw_key or self.raw_key}"}

    def _respond(self, action: str, *, share_id: int | None = None, raw_key: str | None = None):
        """POST one accept/reject decision.

        Args:
            action: The raw ``action`` value to submit.
            share_id: The share to address; defaults to the fixture share.
            raw_key: A raw key to use instead of the fixture's.

        Returns:
            The Django test-client response.
        """
        pk = self.share.pk if share_id is None else share_id
        return self.client.post(f"{BASE}/{pk}/respond/", {"action": action}, content_type="application/json", **self._headers(raw_key))

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

    def test_accept_materialises_the_pin_and_reports_its_slug(self) -> None:
        """The happy path: one POST puts the pin on the recipient's map."""
        response = self._respond("accept")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], PinShareStatus.ACCEPTED)
        self.assertTrue(body["detail"])

        self.share.refresh_from_db()
        self.assertEqual(self.share.status, PinShareStatus.ACCEPTED)
        new_pin = Pin.objects.get(profile=self.profile, source_share=self.share)
        self.assertEqual(body["pin_slug"], new_pin.slug or str(new_pin.uuid))

    def test_accept_stamps_lineage_through_source_share(self) -> None:
        """Acceptance records provenance on the pin, not as a new exposure."""
        self._respond("accept")

        new_pin = Pin.objects.get(profile=self.profile, source_share=self.share)
        self.assertEqual(new_pin.source_share_id, self.share.pk)

    def test_accept_does_not_record_a_second_location_exposure(self) -> None:
        """The provenance trap, asserted directly.

        ``record_share_exposure`` already fired when the share was created. A
        second call on accept would add a duplicate ``LocationExposure`` for the
        same (profile, location), and ``resolve_origin_share`` picks the
        *earliest* exposure to parent a reshare chain - so a duplicate inflates
        exposure counts and can re-parent the chain onto the wrong ancestor.
        Anyone "fixing" the acceptance path to obey the project-wide
        ``record_share_exposure`` rule breaks this test, which is the point.
        """
        before = LocationExposure.objects.count()

        self._respond("accept")

        self.assertEqual(LocationExposure.objects.count(), before)

    def test_reject_marks_the_share_and_creates_no_pin(self) -> None:
        """Rejecting is a pure status change."""
        response = self._respond("reject")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], PinShareStatus.REJECTED)
        self.assertIsNone(response.json()["pin_slug"])
        self.assertFalse(Pin.objects.filter(profile=self.profile).exists())

    def test_responding_twice_is_a_400_and_does_not_re_run_acceptance(self) -> None:
        """A retried accept must not create a second pin.

        "Already handled" is safe to say here precisely because the scoped
        lookup already proved the share is addressed to this caller.
        """
        self._respond("accept")

        second = self._respond("accept")

        self.assertEqual(second.status_code, 400)
        self.assertEqual(second.json(), {"error": "This shared pin has already been handled."})
        self.assertEqual(Pin.objects.filter(profile=self.profile).count(), 1)

    def test_a_share_addressed_to_someone_else_is_not_found(self) -> None:
        """The scoped lookup is the entire anti-enumeration story for a write."""
        stranger = Profile.objects.get(user=baker.make(User, username="stranger"))
        theirs = self._make_share(stranger)

        response = self._respond("accept", share_id=theirs.pk)

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json(), {"error": "Not found."})
        theirs.refresh_from_db()
        self.assertEqual(theirs.status, PinShareStatus.PENDING)
        self.assertFalse(Pin.objects.filter(profile=stranger).exists())

    def test_an_unknown_share_id_answers_identically(self) -> None:
        """Someone else's id and a nonexistent id must be byte-identical."""
        theirs = self._make_share(Profile.objects.get(user=baker.make(User, username="stranger2")))

        unknown = self._respond("accept", share_id=self.share.pk + 100_000)
        foreign = self._respond("accept", share_id=theirs.pk)

        self.assertEqual(unknown.status_code, foreign.status_code)
        self.assertEqual(unknown.json(), foreign.json())

    def test_an_unrecognized_action_is_a_400_and_leaves_the_share_pending(self) -> None:
        """The service would answer "Unknown action." with a 200; the API must not.

        Silently succeeding is worse than failing here: a client sending
        ``"Accept"`` would believe the share had been accepted while it sat
        pending forever.
        """
        response = self._respond("Accept")

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "Invalid request.")
        self.assertIn("action", response.json()["fields"])
        self.share.refresh_from_db()
        self.assertEqual(self.share.status, PinShareStatus.PENDING)

    def test_accepting_a_bundle_recreates_the_child_hierarchy(self) -> None:
        """Bundled child shares are materialised under the accepted root."""
        child_pin = create_pin_for_profile(self.sender, name="Garage", latitude=42.6, longitude=-73.6).pin
        Pin.objects.filter(pk=child_pin.pk).update(parent_pin=self.sender_pin)
        child_share = PinShare.objects.create(
            pin=child_pin,
            location=child_pin.location,
            from_profile=self.sender,
            to_profile=self.profile,
            bundled_with=self.share,
            status=PinShareStatus.PENDING,
        )

        body = self._respond("accept").json()

        child_share.refresh_from_db()
        self.assertEqual(child_share.status, PinShareStatus.ACCEPTED)
        self.assertIn("child pin", body["detail"])
        root = Pin.objects.get(profile=self.profile, source_share=self.share)
        self.assertTrue(Pin.objects.filter(profile=self.profile, source_share=child_share, parent_pin=root).exists())

    def test_messages_scope_cannot_respond(self) -> None:
        """The share also arrives by bare notification, so it is not a messaging action."""
        raw = self._key_with_scopes([ApiKeyScope.MESSAGES_READ.value, ApiKeyScope.MESSAGES_WRITE.value])

        response = self._respond("accept", raw_key=raw)

        self.assertEqual(response.status_code, 403)
        self.share.refresh_from_db()
        self.assertEqual(self.share.status, PinShareStatus.PENDING)

    def test_read_only_pin_scope_cannot_respond(self) -> None:
        """Responding creates a pin, so it is a write however it is framed."""
        raw = self._key_with_scopes([ApiKeyScope.PINS_READ.value])

        response = self._respond("reject", raw_key=raw)

        self.assertEqual(response.status_code, 403)
        self.share.refresh_from_db()
        self.assertEqual(self.share.status, PinShareStatus.PENDING)

"""Tests for PinShareDetailView - the page a share recipient lands on from a notification.

Covers: the page renders for the recipient, 404s for anyone else, and its map
initializes (assigns window.map) unconditionally so the shared top-right
toolbar's screenshot tool never falls back to its own hardcoded default.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.pin_share.meta import PinShareOrigin, PinShareStatus
from urbanlens.dashboard.models.pin_share.model import PinShare

if TYPE_CHECKING:
    from django.contrib.auth.models import User

    from urbanlens.dashboard.models.profile.model import Profile


class PinShareDetailViewTests(TestCase):
    def setUp(self) -> None:
        self.sender: Profile = baker.make("auth.User").profile
        self.recipient: Profile = baker.make("auth.User").profile
        self.pin = baker.make(Pin, profile=self.sender, parent_pin=None)
        self.share = baker.make(PinShare, pin=self.pin, from_profile=self.sender, to_profile=self.recipient)

    def test_recipient_can_view(self) -> None:
        self.client.force_login(self.recipient.user)
        response = self.client.get(reverse("pin.share.detail", kwargs={"share_id": self.share.pk}))
        self.assertEqual(response.status_code, 200)

    def test_other_users_get_404(self) -> None:
        outsider: User = baker.make("auth.User")
        self.client.force_login(outsider)
        response = self.client.get(reverse("pin.share.detail", kwargs={"share_id": self.share.pk}))
        self.assertEqual(response.status_code, 404)

    def test_map_initializes_unconditionally(self) -> None:
        """window.map must be assigned even when there's no early-return path
        skipped - regression guard for the screenshot-tool-defaults-to-Manhattan
        bug class (the map used to only initialize when coordinates existed)."""
        self.client.force_login(self.recipient.user)
        response = self.client.get(reverse("pin.share.detail", kwargs={"share_id": self.share.pk}))
        self.assertContains(response, "window.map = map;")
        self.assertContains(response, "L.map('shared-pin-map'")


class PinShareDetailViewPrivateNotesLeakTests(TestCase):
    """The sender's ``pin.description`` (private personal notes, per Pin.description's own
    docstring - distinct from the public Location.description) was rendered unconditionally
    on this page for ANY share the recipient could reach, including a DETECTED share the
    recipient never consented to and a share long since ACCEPTED/REJECTED. Nothing about
    consenting to share a *pin* implies consenting to share its owner's private notes about
    it - that's a live reference into the sender's pin, exactly what
    docs/GOALS.md's sharing model forbids. See docs/GOALS_CODE_AUDIT.md ("Pin-to-pin sharing").
    """

    def setUp(self) -> None:
        self.sender: Profile = baker.make("auth.User").profile
        self.recipient: Profile = baker.make("auth.User").profile
        self.pin = baker.make(Pin, profile=self.sender, parent_pin=None, description="My secret hideout notes")
        self.client.force_login(self.recipient.user)

    def _get(self, share: PinShare):
        return self.client.get(reverse("pin.share.detail", kwargs={"share_id": share.pk}))

    def test_description_is_never_shown_regardless_of_status_or_origin(self) -> None:
        """Loops every (status, origin) combination the row can actually hold - a
        status-only or origin-only check would miss the other axis leaking. Each
        combo gets its own recipient: PinShare enforces at most one pending share,
        and at most one map_detected share, per (pin, to_profile) pair, so reusing
        self.recipient across combos would trip those constraints, not the code
        under test."""
        for status in PinShareStatus.values:
            for origin in PinShareOrigin.values:
                with self.subTest(status=status, origin=origin):
                    recipient: Profile = baker.make("auth.User").profile
                    share = PinShare.objects.create(
                        pin=self.pin,
                        location=self.pin.location,
                        from_profile=self.sender,
                        to_profile=recipient,
                        status=status,
                        origin=origin,
                    )
                    self.client.force_login(recipient.user)
                    response = self._get(share)
                    self.assertEqual(response.status_code, 200)
                    self.assertNotContains(response, "My secret hideout notes")

    def test_description_stays_hidden_after_the_sender_edits_it_post_share(self) -> None:
        share = PinShare.objects.create(pin=self.pin, location=self.pin.location, from_profile=self.sender, to_profile=self.recipient, status=PinShareStatus.PENDING)
        self.pin.description = "An even more secret note added after sharing"
        self.pin.save(update_fields=["description"])

        response = self._get(share)

        self.assertNotContains(response, "An even more secret note added after sharing")

    def test_pin_address_is_still_shown(self) -> None:
        """The fix must not over-hide - address_basic proxies the shared Location,
        not sender-private data, so it should render same as before."""
        self.pin.location.street_number = "742"
        self.pin.location.route = "Evergreen Terrace"
        self.pin.location.save(update_fields=["street_number", "route"])
        share = PinShare.objects.create(pin=self.pin, location=self.pin.location, from_profile=self.sender, to_profile=self.recipient, status=PinShareStatus.PENDING)

        response = self._get(share)

        self.assertContains(response, "742 Evergreen Terrace")

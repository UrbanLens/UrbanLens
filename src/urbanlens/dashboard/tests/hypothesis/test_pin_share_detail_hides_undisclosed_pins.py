"""The shared-pin detail page must not read through to a pin nobody offered.

``PinShareStatus.DETECTED`` rows are provenance bookkeeping, auto-recorded when
a place was revealed indirectly - a shared map's geometry, a location mentioned
in a DM, a pin added to a shared trip. Nobody offered the pin and nobody
accepted it, so the sender's live ``Pin`` is not the recipient's to see: its
current name, its address, and whatever the sender renames it to next.

The detail view is addressable by primary key and filtered only by recipient,
so reaching it does not require the Sharing page to have linked it. The listing
page already refused to read through (``controllers.memories._safe_incoming_place_label``);
this covers the page it links to.

An explicit PENDING share is the opposite case and is asserted alongside, since
previewing the pin is the entire point of an offer awaiting accept/reject.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.pin_share import PinShare, PinShareStatus
from urbanlens.dashboard.models.pin_share.meta import PinShareOrigin

_UNDISCLOSED_NAME = "Rosewood Sanatorium Boiler House"
_LAT, _LNG = 40.0, -74.0


class PinShareDetailDisclosureTests(TestCase):
    """What the recipient of each kind of share may read off the detail page."""

    def setUp(self):
        super().setUp()
        self.sender_user = baker.make(User)
        self.recipient_user = baker.make(User)
        self.sender = self.sender_user.profile
        self.recipient = self.recipient_user.profile
        self.location = baker.make(Location, latitude=f"{_LAT:.6f}", longitude=f"{_LNG:.6f}", official_name="Unnamed Location")
        self.pin = Pin.objects.create(profile=self.sender, location=self.location, name=_UNDISCLOSED_NAME)

    def _detail(self, share: PinShare):
        self.client.force_login(self.recipient_user)
        return self.client.get(reverse("pin.share.detail", kwargs={"share_id": share.pk}))

    def _share(self, status: str, **extra) -> PinShare:
        return PinShare.objects.create(
            pin=self.pin,
            location=self.location,
            from_profile=self.sender,
            to_profile=self.recipient,
            status=status,
            **extra,
        )

    def test_a_detected_share_does_not_disclose_the_pin_name(self):
        share = self._share(PinShareStatus.DETECTED, origin=PinShareOrigin.MAP_DETECTED)

        response = self._detail(share)

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, _UNDISCLOSED_NAME)
        self.assertIsNone(response.context["pin"], "the template must not be handed the sender's live pin")

    def test_a_detected_share_still_renders_the_place_it_recorded(self):
        """The page is not blank - the exposure it documents is still shown.

        The map reads ``share.shared_location``, the snapshot taken when the
        share happened, so it neither depends on the live pin nor follows it if
        the sender moves it later.
        """
        share = self._share(PinShareStatus.DETECTED, origin=PinShareOrigin.MAP_DETECTED)

        response = self._detail(share)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["share"].shared_location_id, self.location.pk)

    def test_a_pending_share_does_disclose_the_pin(self):
        """An explicit offer is meant to be previewed before accept or reject."""
        share = self._share(PinShareStatus.PENDING)

        response = self._detail(share)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, _UNDISCLOSED_NAME)
        self.assertEqual(response.context["pin"], self.pin)

    def test_a_renamed_pin_does_not_follow_a_detected_share(self):
        """The live row keeps changing; a detected share must not track it."""
        share = self._share(PinShareStatus.DETECTED, origin=PinShareOrigin.TRIP_ACTIVITY)
        self.pin.name = "Renamed After The Fact"
        self.pin.save(update_fields=["name"])

        response = self._detail(share)

        self.assertNotContains(response, "Renamed After The Fact")

    def test_another_recipients_share_is_not_reachable(self):
        """The pk-addressable view is still scoped to its own recipient."""
        outsider = baker.make(User)
        share = self._share(PinShareStatus.PENDING)
        self.client.force_login(outsider)

        response = self.client.get(reverse("pin.share.detail", kwargs={"share_id": share.pk}))

        self.assertEqual(response.status_code, 404)


class SafePinAccessorTests(TestCase):
    """``PinShare.safe_pin`` / ``safe_place_label`` are the shared primitive."""

    def setUp(self):
        super().setUp()
        self.sender = baker.make(User).profile
        self.recipient = baker.make(User).profile
        self.location = baker.make(Location, latitude=f"{_LAT:.6f}", longitude=f"{_LNG:.6f}", official_name="Unnamed Location", address="12 Mill Road")
        self.pin = Pin.objects.create(profile=self.sender, location=self.location, name=_UNDISCLOSED_NAME)

    def _share(self, status: str) -> PinShare:
        return PinShare.objects.create(pin=self.pin, location=self.location, from_profile=self.sender, to_profile=self.recipient, status=status)

    def test_detected_shares_withhold_the_pin(self):
        share = self._share(PinShareStatus.DETECTED)

        self.assertFalse(share.reveals_live_pin)
        self.assertIsNone(share.safe_pin)
        self.assertNotIn(_UNDISCLOSED_NAME, share.safe_place_label)

    def test_every_other_status_reveals_it(self):
        for status in (PinShareStatus.PENDING, PinShareStatus.ACCEPTED, PinShareStatus.REJECTED, PinShareStatus.ALREADY_PINNED):
            with self.subTest(status=status):
                share = self._share(status)

                self.assertTrue(share.reveals_live_pin)
                self.assertEqual(share.safe_pin, self.pin)
                self.assertEqual(share.safe_place_label, share.place_label)

    def test_the_withheld_label_falls_back_to_the_snapshot(self):
        share = self._share(PinShareStatus.DETECTED)

        self.assertEqual(share.safe_place_label, "12 Mill Road")

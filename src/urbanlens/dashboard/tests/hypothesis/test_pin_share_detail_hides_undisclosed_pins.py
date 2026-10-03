"""The shared-Private Pin page must not read through to a pin nobody offered."""

from __future__ import annotations

from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import translation
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
        self.location = baker.make(
            Location, latitude=f"{_LAT:.6f}", longitude=f"{_LNG:.6f}", official_name="Unnamed Location"
        )
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

    def test_the_map_reads_coordinates_that_a_comma_locale_does_not_reformat(self):
        share = self._share(PinShareStatus.PENDING)
        with translation.override("de"):
            response = self._detail(share)
        self.assertContains(response, f'data-lat="{self.location.latitude}"')
        self.assertContains(response, f'data-lng="{self.location.longitude}"')
        self.assertIn(".", str(self.location.latitude))

    def test_a_detected_share_does_not_disclose_the_pin_name(self):
        share = self._share(PinShareStatus.DETECTED, origin=PinShareOrigin.MAP_DETECTED)

        response = self._detail(share)

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, _UNDISCLOSED_NAME)
        self.assertNotIn("pin", response.context, "the template must not be handed the sender's live pin")

    def test_a_detected_share_still_renders_the_place_it_recorded(self):
        """The page is not blank - the exposure it documents is still shown.

        The map reads ``share.shared_location``, the snapshot taken when the share happened, so it neither
        depends on the live pin nor follows it if the sender moves it later."""
        share = self._share(PinShareStatus.DETECTED, origin=PinShareOrigin.MAP_DETECTED)

        response = self._detail(share)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["share"].shared_location_id, self.location.pk)

    def test_a_pending_share_names_the_place_not_the_pin(self):
        """An explicit offer is previewed from what the share carries; the sender's name for the pin is not
        part of it unless they typed it as ``shared_name``."""
        share = self._share(PinShareStatus.PENDING)

        response = self._detail(share)

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, _UNDISCLOSED_NAME)
        self.assertNotIn("pin", response.context)

    def test_a_pending_share_shows_a_name_the_sender_chose_to_share(self):
        share = self._share(PinShareStatus.PENDING, shared_name="The boiler house")

        self.assertContains(self._detail(share), "The boiler house")

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


class SafePlaceLabelTests(TestCase):
    """``PinShare.safe_place_label`` is the shared primitive: the only label any recipient surface uses."""

    def setUp(self):
        super().setUp()
        self.sender = baker.make(User).profile
        self.recipient = baker.make(User).profile
        # Location.address is composed from its components, not a stored field.
        self.location = baker.make(
            Location,
            latitude=f"{_LAT:.6f}",
            longitude=f"{_LNG:.6f}",
            official_name="Unnamed Location",
            street_number="12",
            route="Mill Road",
            locality="",
            administrative_area_level_1="",
            zipcode="",
        )
        self.pin = Pin.objects.create(profile=self.sender, location=self.location, name=_UNDISCLOSED_NAME)

    def _share(self, status: str) -> PinShare:
        return PinShare.objects.create(
            pin=self.pin, location=self.location, from_profile=self.sender, to_profile=self.recipient, status=status
        )

    def test_no_status_reads_the_pin_name(self):
        for status in PinShareStatus.values:
            with self.subTest(status=status):
                share = self._share(status)

                self.assertNotIn(_UNDISCLOSED_NAME, share.safe_place_label)
                self.assertIn(_UNDISCLOSED_NAME, share.place_label, "the sender's own label still reads it")

    def test_a_shared_name_is_used_for_every_status(self):
        for status in PinShareStatus.values:
            with self.subTest(status=status):
                share = self._share(status)
                share.shared_name = "The boiler house"

                self.assertEqual(share.safe_place_label, "The boiler house")

    def test_the_label_falls_back_to_the_snapshot(self):
        share = self._share(PinShareStatus.DETECTED)

        self.assertEqual(share.safe_place_label, self.location.address)
        self.assertIn("Mill Road", share.safe_place_label)

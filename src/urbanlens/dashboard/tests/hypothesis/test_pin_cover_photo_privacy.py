"""Eligibility rules for the pin hero-banner cover photo.

``PinCoverPhotoView`` accepts any image "tied to this pin, or already associated
with its Location". The Location half of that rule is what makes another user's
photo reachable: every pin upload stamps ``Image.location``, so two users pinning
the same place share a Location id. Without a visibility filter the rule lets one
of them mount the other's private upload as their own hero banner - and the
response body hands back the file URL directly.

The wiki twin (``WikiCoverPhotoView``) already filters through
``Image.objects.visible_to``; these tests pin the pin-side to the same rule.
"""

from __future__ import annotations

import json
import tempfile

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.meta import VisibilityChoice

_MEDIA_ROOT = tempfile.mkdtemp(prefix="urbanlens-test-media-")


def _make_image(**kwargs) -> Image:
    return Image.objects.create(
        image=SimpleUploadedFile("photo.jpg", b"fake image bytes", content_type="image/jpeg"), **kwargs
    )


@override_settings(MEDIA_ROOT=_MEDIA_ROOT)
class PinCoverPhotoEligibilityTests(TestCase):
    """Only photos the requester may actually see can become their cover."""

    def setUp(self) -> None:
        self.location = baker.make(Location)

        self.user = baker.make(User)
        self.profile = self.user.profile
        self.pin = baker.make(Pin, profile=self.profile, location=self.location)

        self.other_user = baker.make(User)
        self.other_profile = self.other_user.profile
        self.other_profile.photo_upload_visibility = VisibilityChoice.NO_ONE
        self.other_profile.save(update_fields=["photo_upload_visibility"])
        self.other_pin = baker.make(Pin, profile=self.other_profile, location=self.location)
        self.other_image = _make_image(pin=self.other_pin, location=self.location, profile=self.other_profile)

        self.client.force_login(self.user)

    def _post(self, image_id: int | None, pin: Pin | None = None):
        return self.client.post(
            reverse("pin.cover_photo", args=[(pin or self.pin).slug]),
            data=json.dumps({"image_id": image_id}),
            content_type="application/json",
        )

    def test_cannot_mount_another_users_hidden_photo_from_the_shared_location(self) -> None:
        response = self._post(self.other_image.pk)

        self.assertEqual(response.status_code, 404)
        self.pin.refresh_from_db()
        self.assertIsNone(self.pin.cover_photo_id)

    def test_own_photo_on_the_pin_is_still_accepted(self) -> None:
        own_image = _make_image(pin=self.pin, location=self.location, profile=self.profile)

        response = self._post(own_image.pk)

        self.assertEqual(response.status_code, 200)
        self.pin.refresh_from_db()
        self.assertEqual(self.pin.cover_photo_id, own_image.pk)

    def test_visible_photo_from_the_same_location_is_still_accepted(self) -> None:
        """The Location half of the rule keeps working for photos the viewer may see.

        "May see" is two gates, not one: the owner has to have shared the photo,
        and their setting has to admit this viewer. This test used to open the
        second only, which passed while a photo nobody had shared was reachable
        by anyone the setting happened to admit.
        """
        from urbanlens.dashboard.models.wiki.model import Wiki

        Image.objects.filter(pk=self.other_image.pk).update(wiki=baker.make(Wiki, location=self.location))
        self.other_profile.photo_upload_visibility = VisibilityChoice.ANYONE
        self.other_profile.save(update_fields=["photo_upload_visibility"])
        self.profile.viewer_photo_filter = VisibilityChoice.ANYONE
        self.profile.save(update_fields=["viewer_photo_filter"])

        response = self._post(self.other_image.pk)

        self.assertEqual(response.status_code, 200)
        self.pin.refresh_from_db()
        self.assertEqual(self.pin.cover_photo_id, self.other_image.pk)

    def test_a_photo_from_an_unrelated_location_is_rejected(self) -> None:
        elsewhere = baker.make(Location)
        stray_image = _make_image(pin=self.pin, location=elsewhere, profile=self.other_profile)
        stray_image.pin = None
        stray_image.save(update_fields=["pin"])

        response = self._post(stray_image.pk)

        self.assertEqual(response.status_code, 404)
        self.pin.refresh_from_db()
        self.assertIsNone(self.pin.cover_photo_id)

    def test_clearing_the_cover_photo_still_works(self) -> None:
        own_image = _make_image(pin=self.pin, location=self.location, profile=self.profile)
        self.pin.cover_photo = own_image
        self.pin.save(update_fields=["cover_photo"])

        response = self._post(None)

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(json.loads(response.content)["cover_photo"])
        self.pin.refresh_from_db()
        self.assertIsNone(self.pin.cover_photo_id)

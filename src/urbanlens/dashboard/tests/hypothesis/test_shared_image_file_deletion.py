"""Deleting your own photo must not break it for people you shared it with.

``create_pin_from_share`` copies a photo by assigning the *same* storage key
(``image=image.image.name``) - the bytes are deliberately not duplicated. Deleting an
``Image`` row, however, calls ``image.image.delete()``, which removes that file from
storage outright. Nothing checks whether another row still points at it.
"""

from __future__ import annotations

from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.pin_share.model import PinShare, PinShareStatus
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.services.media.images import delete_stored_file
from urbanlens.dashboard.services.sharing.pin_sharing import create_pin_from_share


class SharedImageFileDeletionTests(TestCase):
    """A shared photo's file outlives the sender deleting their own copy."""

    def setUp(self):
        super().setUp()
        self.sender: Profile = baker.make("auth.User").profile
        self.recipient: Profile = baker.make("auth.User").profile
        self.location = Location.objects.create(latitude=43.1, longitude=-72.4)
        self.pin = Pin.objects.create(profile=self.sender, location=self.location, name="Shared with photos")

        self.image = Image.objects.create(pin=self.pin, location=self.location, profile=self.sender, file_size=11)
        self.image.image.save("shared-photo.jpg", ContentFile(b"jpeg-bytes"), save=True)
        self.stored_name = self.image.image.name

        share = PinShare.objects.create(
            pin=self.pin,
            location=self.location,
            from_profile=self.sender,
            to_profile=self.recipient,
            status=PinShareStatus.PENDING,
        )
        share.images.set([self.image])
        self.recipient_pin = create_pin_from_share(share)
        self.copy = Image.objects.get(profile=self.recipient)

    def test_the_copy_reuses_the_same_stored_file(self):
        # Establishes the premise: the bytes are shared, not duplicated.
        self.assertEqual(self.copy.image.name, self.stored_name)
        self.assertTrue(default_storage.exists(self.stored_name))

    def _sender_deletes_their_photo(self) -> None:
        """Exactly what the gallery delete endpoint does."""
        delete_stored_file(self.image)
        self.image.delete()

    def test_the_recipients_photo_still_has_its_file(self):
        self._sender_deletes_their_photo()

        self.assertTrue(
            default_storage.exists(self.copy.image.name),
            "the sender deleting their own photo removed the file the recipient's copy points at",
        )

    def test_the_recipients_row_survives(self):
        self._sender_deletes_their_photo()

        self.assertTrue(Image.objects.filter(pk=self.copy.pk).exists())

    def test_deleting_an_unshared_photo_still_removes_its_file(self):
        # The reference check must not turn every delete into a leak.
        solo = Image.objects.create(pin=self.pin, location=self.location, profile=self.sender, file_size=9)
        solo.image.save("solo-photo.jpg", ContentFile(b"solo-bytes"), save=True)
        name = solo.image.name

        delete_stored_file(solo)
        solo.delete()

        self.assertFalse(default_storage.exists(name))

    def test_the_file_goes_once_the_last_row_is_deleted(self):
        self._sender_deletes_their_photo()
        remaining = Image.objects.get(pk=self.copy.pk)

        delete_stored_file(remaining)
        remaining.delete()

        self.assertFalse(default_storage.exists(self.stored_name))

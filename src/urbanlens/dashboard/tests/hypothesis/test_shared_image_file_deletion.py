"""Deleting your own photo must not break it for people you shared it with."""

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
from urbanlens.dashboard.services.profile.account_deletion import hard_delete_profile
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


class AnalysisThumbnailDeletionTests(SharedImageFileDeletionTests):
    """The sandbox's downscaled analysis copy belongs to one row and goes with it (P14)."""

    def setUp(self):
        super().setUp()
        self.image.analysis_thumbnail.save("shared-photo-analysis.jpg", ContentFile(b"analysis"), save=True)
        self.analysis_name = self.image.analysis_thumbnail.name

    def test_deleting_an_unshared_photo_removes_its_analysis_copy(self):
        solo = Image.objects.create(pin=self.pin, location=self.location, profile=self.sender, file_size=9)
        solo.image.save("solo-photo.jpg", ContentFile(b"solo-bytes"), save=True)
        solo.analysis_thumbnail.save("solo-analysis.jpg", ContentFile(b"analysis"), save=True)
        analysis = solo.analysis_thumbnail.name

        delete_stored_file(solo)
        solo.delete()

        self.assertFalse(default_storage.exists(analysis))

    def test_a_shared_original_still_removes_its_own_analysis_copy(self):
        self._sender_deletes_their_photo()

        self.assertTrue(default_storage.exists(self.stored_name), "the premise failed: the shared file went")
        self.assertFalse(
            default_storage.exists(self.analysis_name),
            "the sender's analysis copy outlived its row because the original file was still shared",
        )


class AccountDeletionPhotoFileTests(SharedImageFileDeletionTests):
    """Hard-deleting an account applies the same shared-file rule as deleting one photo."""

    def test_deleting_the_senders_account_keeps_the_file_a_recipient_was_shared(self):
        hard_delete_profile(self.sender)

        self.assertTrue(
            default_storage.exists(self.stored_name),
            "deleting the sender's account removed the file the recipient's shared copy points at",
        )

    def test_deleting_an_account_removes_every_derived_file_of_its_photos(self):
        solo = Image.objects.create(pin=self.pin, location=self.location, profile=self.sender, file_size=9)
        solo.image.save("solo-photo.jpg", ContentFile(b"solo-bytes"), save=False)
        for field_name in ("thumbnail", "marker_thumbnail", "analysis_thumbnail"):
            getattr(solo, field_name).save(f"solo-{field_name}.jpg", ContentFile(b"derived"), save=False)
        solo.save()
        names = [solo.image.name, solo.thumbnail.name, solo.marker_thumbnail.name, solo.analysis_thumbnail.name]
        self.assertTrue(
            all(default_storage.exists(name) for name in names), "the premise failed: a file was not written"
        )

        hard_delete_profile(self.sender)

        self.assertEqual([name for name in names if default_storage.exists(name)], [])

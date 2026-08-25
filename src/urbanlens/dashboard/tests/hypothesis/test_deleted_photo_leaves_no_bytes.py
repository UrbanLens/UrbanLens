"""Deleting a photo removes its bytes, whichever path deleted it.

The media gate serves a file whose owning row has gone to any authenticated user -
a documented decision (docs/PROBLEMS.md, "Authenticated media gate - residual
per-family risk"), reasonable while orphans are rare. They were not rare: Django
has not removed a FileField's file on row delete since 1.3, file cleanup lived
only in `delete_stored_file`, and nothing called it on a cascade. So a photo
deleted with its owner's account, or by any queryset delete, left bytes that
anyone holding the URL could still fetch.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from django.core.files.storage import default_storage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from model_bakery import baker

from urbanlens.dashboard.models.images.model import Image, ImageSource
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin


def _photo(profile, **kwargs) -> Image:
    return Image.objects.create(
        image=SimpleUploadedFile("gone.jpg", b"not-a-real-jpeg", content_type="image/jpeg"),
        profile=profile,
        **kwargs,
    )


class DeletedPhotoLeavesNoBytesTests(TestCase):
    """Every route to deleting a row."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.location = baker.make(Location, latitude=41.7361, longitude=-73.9361)
        self.pin = baker.make(Pin, profile=self.profile, location=self.location, parent_pin=None)

    def test_deleting_the_row_removes_the_file(self) -> None:
        image = _photo(self.profile, pin=self.pin)
        path = image.image.name
        self.assertTrue(default_storage.exists(path))

        image.delete()

        self.assertFalse(default_storage.exists(path), "the photo's bytes outlived the row")

    def test_a_queryset_delete_removes_the_file(self) -> None:
        """The path a cascade takes - no per-instance delete() call at all."""
        image = _photo(self.profile, pin=self.pin)
        path = image.image.name

        Image.objects.filter(pk=image.pk).delete()

        self.assertFalse(default_storage.exists(path), "a queryset delete left the bytes behind")

    def test_deleting_the_pin_removes_its_photos_bytes(self) -> None:
        """The cascade a user actually performs."""
        image = _photo(self.profile, pin=self.pin)
        path = image.image.name

        self.pin.delete()

        if Image.objects.filter(pk=image.pk).exists():
            self.skipTest("Image.pin is SET_NULL, so deleting the pin does not delete the photo")
        self.assertFalse(default_storage.exists(path), "the cascade left the bytes behind")

    def test_a_file_shared_by_two_rows_survives_the_first_delete(self) -> None:
        """Sharing a pin reuses one storage key across rows, so the bytes go
        only when the last row pointing at them does."""
        first = _photo(self.profile, pin=self.pin)
        path = first.image.name
        other = baker.make(User).profile
        second = Image.objects.create(image=path, profile=other, checksum=first.checksum, source=ImageSource.UPLOAD)

        first.delete()

        self.assertTrue(default_storage.exists(path), "deleting one row broke the other row's photo")

        second.delete()

        self.assertFalse(default_storage.exists(path), "the last row went and the bytes stayed")

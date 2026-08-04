"""Tests for PinController._photos_media_preview's tile data attributes.

The combined Media section's "photos" tab (the pin owner's own uploads,
previewed inline in the "All" grid - see pin_media_items.html) needs each
tile's real Image id and coordinates so the shared photo lightbox
(_photo_lightbox.html) can draw its small "where was this taken" map and,
for coordinates, let the marker be dragged to update them via the existing
gallery reposition endpoint.
"""

from __future__ import annotations

from decimal import Decimal

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.models.pin.model import Pin

_PNG_BYTES = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\xcf\xc0\x00\x00\x03\x01\x01\x00\x18\xdd\x8d\xb0\x00\x00\x00\x00IEND\xaeB`\x82"


def _uploaded_photo(name: str = "photo.png") -> SimpleUploadedFile:
    # model_bakery doesn't populate ImageField with a real file by default -
    # the view's own-photos query excludes blank `image` values, so every
    # Image baked here needs one to actually be picked up.
    return SimpleUploadedFile(name, _PNG_BYTES, content_type="image/png")


class PhotosMediaPreviewTests(TestCase):
    def setUp(self) -> None:
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)
        self.pin = baker.make(Pin, profile=self.profile)

    def _get(self):
        return self.client.get(reverse("pin.media", kwargs={"pin_slug": self.pin.slug, "source": "photos"}))

    def test_own_photo_tile_carries_image_id_and_coordinates(self) -> None:
        image = baker.make(Image, pin=self.pin, profile=self.profile, image=_uploaded_photo(), latitude=Decimal("40.123456"), longitude=Decimal("-74.654321"))
        response = self._get()
        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertIn(f'data-image-id="{image.pk}"', body)
        self.assertIn('data-lat="40.123456"', body)
        self.assertIn('data-lng="-74.654321"', body)

    def test_own_photo_tile_without_coordinates_renders_empty_lat_lng(self) -> None:
        image = baker.make(Image, pin=self.pin, profile=self.profile, image=_uploaded_photo(), latitude=None, longitude=None)
        response = self._get()
        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertIn(f'data-image-id="{image.pk}"', body)
        self.assertIn('data-lat=""', body)
        self.assertIn('data-lng=""', body)

    def test_other_users_photos_are_never_included(self) -> None:
        other = baker.make(User)
        other_pin = baker.make(Pin, profile=other.profile)
        baker.make(Image, pin=other_pin, profile=other.profile, image=_uploaded_photo())
        response = self._get()
        self.assertEqual(response.status_code, 204)

    def test_higher_redata_confidence_sorts_first_regardless_of_upload_order(self) -> None:
        """services.photos.redata_relevance's cached confidence should rank an
        older, more-confidently-relevant photo ahead of a newer, unscored one."""
        older_but_confident = baker.make(Image, pin=self.pin, profile=self.profile, image=_uploaded_photo("older.png"), redata_confidence=0.9)
        newer_but_unscored = baker.make(Image, pin=self.pin, profile=self.profile, image=_uploaded_photo("newer.png"), redata_confidence=None)
        body = self._get().content.decode()
        self.assertLess(body.index(f'data-image-id="{older_but_confident.pk}"'), body.index(f'data-image-id="{newer_but_unscored.pk}"'))

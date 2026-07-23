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
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.models.pin.model import Pin


class PhotosMediaPreviewTests(TestCase):
    def setUp(self) -> None:
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)
        self.pin = baker.make(Pin, profile=self.profile)

    def _get(self):
        return self.client.get(reverse("pin.media", kwargs={"pin_slug": self.pin.slug, "source": "photos"}))

    def test_own_photo_tile_carries_image_id_and_coordinates(self) -> None:
        image = baker.make(Image, pin=self.pin, profile=self.profile, latitude=Decimal("40.123456"), longitude=Decimal("-74.654321"))
        response = self._get()
        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertIn(f'data-image-id="{image.pk}"', body)
        self.assertIn('data-lat="40.123456"', body)
        self.assertIn('data-lng="-74.654321"', body)

    def test_own_photo_tile_without_coordinates_renders_empty_lat_lng(self) -> None:
        image = baker.make(Image, pin=self.pin, profile=self.profile, latitude=None, longitude=None)
        response = self._get()
        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertIn(f'data-image-id="{image.pk}"', body)
        self.assertIn('data-lat=""', body)
        self.assertIn('data-lng=""', body)

    def test_other_users_photos_are_never_included(self) -> None:
        other = baker.make(User)
        other_pin = baker.make(Pin, profile=other.profile)
        baker.make(Image, pin=other_pin, profile=other.profile)
        response = self._get()
        self.assertEqual(response.status_code, 204)

"""MapPinPayloadService.serialize()'s cover_photo_url: explicit cover photo,
else the pin's earliest own photo that this profile hasn't voted irrelevant.

See models.images.model.Image:246 (Pin.cover_photo) and
services.media.media_relevance.effective_relevance's docs on why a plain
personal upload (no media_item_key) is trusted by default.
"""

from __future__ import annotations

import tempfile

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.images.model import Image, MediaKind
from urbanlens.dashboard.models.images.relevance import MediaRelevance
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.services.map_pins import MapPinPayloadService

_MEDIA_ROOT = tempfile.mkdtemp(prefix="urbanlens-test-media-cover-")


def _make_image(**kwargs) -> Image:
    return Image.objects.create(image=SimpleUploadedFile("photo.jpg", b"fake image bytes", content_type="image/jpeg"), media_type=MediaKind.PHOTO, **kwargs)


@override_settings(MEDIA_ROOT=_MEDIA_ROOT)
class CoverPhotoUrlTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.profile = baker.make(User).profile
        self.location = baker.make(Location, official_name="Cover Photo Place", latitude="40.0", longitude="-74.0")
        self.pin = baker.make(Pin, profile=self.profile, location=self.location, name="Cover Photo Pin")

    def _serialize(self) -> dict:
        service = MapPinPayloadService(self.profile)
        prepared = service.prepare_queryset(Pin.objects.filter(pk=self.pin.pk)).get()
        return service.serialize(prepared)

    def test_no_photos_gives_no_thumbnail(self) -> None:
        self.assertIsNone(self._serialize()["cover_photo_url"])

    def test_explicit_cover_photo_wins(self) -> None:
        cover = _make_image(pin=self.pin, profile=self.profile)
        _make_image(pin=self.pin, profile=self.profile)  # a decoy
        self.pin.cover_photo = cover
        self.pin.save(update_fields=["cover_photo"])

        self.assertEqual(self._serialize()["cover_photo_url"], cover.thumb_url)

    def test_falls_back_to_earliest_own_upload_when_no_cover_photo(self) -> None:
        first = _make_image(pin=self.pin, profile=self.profile)
        _make_image(pin=self.pin, profile=self.profile)

        self.assertEqual(self._serialize()["cover_photo_url"], first.thumb_url)

    def test_a_plain_upload_is_trusted_even_with_no_relevance_history(self) -> None:
        """No media_item_key means there's nothing to have voted on - never excluded."""
        photo = _make_image(pin=self.pin, profile=self.profile)

        self.assertEqual(self._serialize()["cover_photo_url"], photo.thumb_url)

    def test_skips_a_materialized_photo_this_profile_voted_irrelevant(self) -> None:
        voted_down = _make_image(pin=self.pin, profile=self.profile, location=self.location, media_source_key="wikimedia", media_item_key="abc123")
        MediaRelevance.objects.create(profile=self.profile, location=self.location, source="wikimedia", item_key="abc123", is_relevant=False)
        fallback = _make_image(pin=self.pin, profile=self.profile)

        result = self._serialize()["cover_photo_url"]
        self.assertEqual(result, fallback.thumb_url)
        self.assertNotEqual(result, voted_down.thumb_url)

    def test_a_video_is_never_used_as_the_fallback_thumbnail(self) -> None:
        Image.objects.create(image=SimpleUploadedFile("clip.mp4", b"fake video bytes", content_type="video/mp4"), media_type=MediaKind.VIDEO, pin=self.pin, profile=self.profile)

        self.assertIsNone(self._serialize()["cover_photo_url"])

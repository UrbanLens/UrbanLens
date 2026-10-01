"""The wiki gallery must know when deleting a photo only takes it off the wiki (P55).

`WikiImageView.delete` keeps a photo that is still on its owner's pin and only unlinks it from the
wiki, so the gallery's "removed permanently" prompt and "Photo deleted." toast are false for it. The
tile tells the delete handler which case it is in, and only for the viewer's own photos: whether
someone else's photo is on a pin is theirs to keep.
"""

from __future__ import annotations

import re
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
from urbanlens.dashboard.models.wiki.model import Wiki

_MEDIA_ROOT = tempfile.mkdtemp(prefix="urbanlens-test-media-")


def _make_image(**kwargs) -> Image:
    return Image.objects.create(
        image=SimpleUploadedFile("photo.jpg", b"fake image bytes", content_type="image/jpeg"), **kwargs
    )


@override_settings(MEDIA_ROOT=_MEDIA_ROOT)
class WikiGalleryTileKnowsItsPinTests(TestCase):
    def setUp(self) -> None:
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)
        self.location = baker.make(Location)
        self.wiki = baker.make(Wiki, location=self.location)
        self.pin = baker.make(Pin, profile=self.profile, location=self.location)

    def _on_pin_attribute(self, image: Image) -> str | None:
        response = self.client.get(reverse("location.wiki.gallery", args=[self.location.slug]))
        self.assertEqual(response.status_code, 200)
        tile = re.search(
            rf'<li class="gallery-item" id="gallery-item-{image.pk}"(.*?)>', response.content.decode(), re.DOTALL
        )
        self.assertIsNotNone(tile, "the photo is not in the wiki gallery")
        assert tile is not None
        found = re.search(r'data-on-pin="(\w+)"', tile.group(1))
        return found.group(1) if found else None

    def test_own_photo_still_on_a_pin_is_marked(self) -> None:
        image = _make_image(pin=self.pin, wiki=self.wiki, location=self.location, profile=self.profile)

        self.assertEqual(self._on_pin_attribute(image), "true")

    def test_own_wiki_only_photo_is_not_marked(self) -> None:
        image = _make_image(wiki=self.wiki, location=self.location, profile=self.profile)

        self.assertEqual(self._on_pin_attribute(image), "false")

    def test_another_users_pinned_photo_does_not_reveal_the_pin(self) -> None:
        other = baker.make(User).profile
        other_pin = baker.make(Pin, profile=other, location=self.location)
        image = _make_image(pin=other_pin, wiki=self.wiki, location=self.location, profile=other)

        self.assertEqual(self._on_pin_attribute(image), "false")

"""The gallery payload carries what the delete prompt has to decide from.

``galleryDelete`` asks a different question depending on whether removing a photo
from a pin would also take it off a community wiki, and on whether withdrawing it
from there is the owner's to do at all. Both facts come from the server - a tile
that lost them would silently fall back to "not on a wiki" and delete the
contribution without asking, which is the behaviour this whole change removed.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from django.contrib.auth.models import User
from django.test import TestCase
from model_bakery import baker

from urbanlens.dashboard.models.images.model import Image, ImageSource
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.services.media.images import image_to_gallery_json


class GalleryJsonDeleteFlagsTests(TestCase):
    def setUp(self) -> None:
        self.profile = baker.make(User).profile
        self.location = baker.make(Location, latitude=41.7361, longitude=-73.9361)
        self.wiki = baker.make(Wiki, location=self.location)
        self.request = MagicMock()
        self.request.build_absolute_uri.return_value = "https://example.test/media/x.jpg"

    def _json(self, **fields) -> dict:
        image = baker.make(Image, profile=self.profile, image="pin_images/x.jpg", **fields)
        return image_to_gallery_json(image, self.request, self.profile)

    def test_a_photo_on_no_wiki_says_so(self) -> None:
        self.assertFalse(self._json(wiki=None)["on_wiki"])

    def test_a_photo_on_a_wiki_says_so(self) -> None:
        self.assertTrue(self._json(wiki=self.wiki)["on_wiki"])

    def test_an_upload_is_flagged_as_withdrawable(self) -> None:
        self.assertTrue(self._json(wiki=self.wiki, source=ImageSource.UPLOAD)["uploaded"])

    def test_a_fetched_photo_is_not(self) -> None:
        """External photos are never withdrawn from a pin screen."""
        self.assertFalse(self._json(wiki=self.wiki, source=ImageSource.LINKED_URL)["uploaded"])

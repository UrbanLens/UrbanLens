"""A photo uploaded to a pin is not on the location's wiki until somebody puts it there.

Uploading to a pin used to stamp `wiki=Wiki.objects.get_for_location(location)` on
the row, so a photo of your own house appeared in that place's community Photos
panel - and became votable there - without you choosing to contribute it. The
uploader's `photo_upload_visibility` narrowed *who* saw it, which is a control
over the audience for things you have shared; it is not consent to share.

Everything on a wiki is there because a person put it there.
"""

from __future__ import annotations

import io

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from model_bakery import baker
from PIL import Image as PILImage

from urbanlens.dashboard.models.images.attachment import ImageAttachment
from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.services.photos.attachment import attach_to_wiki
from urbanlens.dashboard.services.photos.uploads import upload_photo_for_owner


def _file(name: str = "house.jpg") -> SimpleUploadedFile:
    buf = io.BytesIO()
    PILImage.new("RGB", (60, 40), color=(10, 20, 30)).save(buf, format="JPEG")
    return SimpleUploadedFile(name, buf.getvalue(), content_type="image/jpeg")


class PinPhotoIsNotAutoSharedTests(TestCase):
    """The upload path, and what it does and does not attach."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)
        self.profile = baker.make(User).profile
        self.location = baker.make(Location, latitude=41.7361, longitude=-73.9361)
        self.pin = baker.make(Pin, profile=self.profile, location=self.location, parent_pin=None)
        self.wiki = baker.make(Wiki, location=self.location)

    def test_uploading_to_a_pin_does_not_put_the_photo_on_the_wiki(self) -> None:
        result = upload_photo_for_owner(self.pin, self.profile, _file())

        self.assertIsInstance(result, Image, f"upload was rejected: {result}")
        self.assertEqual(result.pin_id, self.pin.pk)
        self.assertIsNone(result.wiki_id, "a pin upload was published to the location's wiki")
        self.assertFalse(ImageAttachment.objects.filter(image=result, wiki=self.wiki).exists())

    def test_uploading_to_a_wiki_does_put_it_there(self) -> None:
        """The other half: a deliberate wiki upload is still a wiki photo."""
        result = upload_photo_for_owner(self.wiki, self.profile, _file("shared.jpg"))

        self.assertIsInstance(result, Image, f"upload was rejected: {result}")
        self.assertEqual(result.wiki_id, self.wiki.pk)
        self.assertIsNone(result.pin_id)

    def test_sending_it_to_the_wiki_afterwards_is_what_shares_it(self) -> None:
        image = upload_photo_for_owner(self.pin, self.profile, _file())
        self.assertIsInstance(image, Image)

        attach_to_wiki(image, self.wiki, added_by=self.profile)
        Image.objects.filter(pk=image.pk).update(wiki=self.wiki)

        image.refresh_from_db()
        self.assertEqual(image.wiki_id, self.wiki.pk)
        attachment = ImageAttachment.objects.get(image=image, wiki=self.wiki)
        self.assertEqual(attachment.added_by_id, self.profile.pk, "a contribution should record who made it")

    def test_the_wiki_photo_panel_shows_only_what_was_shared(self) -> None:
        """What a visitor to the wiki actually sees, through the panel's own query."""
        kept_private = upload_photo_for_owner(self.pin, self.profile, _file("private.jpg"))
        shared = upload_photo_for_owner(self.wiki, self.profile, _file("shared.jpg"))
        self.assertIsInstance(kept_private, Image)
        self.assertIsInstance(shared, Image)

        on_the_wiki = set(Image.objects.filter(wiki=self.wiki).values_list("pk", flat=True))

        self.assertIn(shared.pk, on_the_wiki)
        self.assertNotIn(kept_private.pk, on_the_wiki, "the pin photo is on the wiki panel without being contributed")

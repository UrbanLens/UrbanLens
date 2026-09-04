"""Taking a photo off your pin is not consent to withdraw it from the wiki.

Deleting from the pin gallery used to drop the ``Image`` row outright, which
removed the photo from the community wiki too - silently, from a screen that
never mentioned the wiki. Contributing something to a wiki is a deliberate act,
and undoing it should be one as well.

So the pin gallery detaches, and the wiki keeps the photo unless the owner says
otherwise. Silence means no. Two cases differ:

- **Uploaded** photos may be withdrawn from the wiki, if the owner explicitly
  asks (``?from_wiki=1``) - it is their photo.
- **External** photos, fetched from a URL, stay. They were already public
  resources online before the app ever saw them, so there is no consent to
  withdraw; removing one is something you do on the wiki itself.
"""

from __future__ import annotations

import io

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse
from model_bakery import baker
from PIL import Image as PILImage

from urbanlens.dashboard.models.images.model import Image, ImageSource
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.services.photos.uploads import upload_photo_for_owner


def _jpeg_bytes() -> bytes:
    buf = io.BytesIO()
    PILImage.new("RGB", (60, 40), color=(10, 20, 30)).save(buf, format="JPEG")
    return buf.getvalue()


class DeleteFromPinGalleryTests(TestCase):
    def setUp(self) -> None:
        self.owner_user = baker.make(User)
        self.owner = self.owner_user.profile
        self.location = baker.make(Location, latitude=41.7361, longitude=-73.9361)
        self.pin = baker.make(Pin, profile=self.owner, location=self.location, parent_pin=None)
        self.wiki = baker.make(Wiki, location=self.location)
        self.client.force_login(self.owner_user)

    def _photo(self, *, on_wiki: bool, source: str = ImageSource.UPLOAD) -> Image:
        result = upload_photo_for_owner(
            self.pin, self.owner, SimpleUploadedFile("p.jpg", _jpeg_bytes(), content_type="image/jpeg"), "caption"
        )
        assert isinstance(result, Image), f"fixture upload was rejected: {result}"
        fields = {"source": source}
        if on_wiki:
            fields["wiki"] = self.wiki
        Image.objects.filter(pk=result.pk).update(**fields)
        result.refresh_from_db()
        return result

    def _delete(self, image: Image, *, from_wiki: bool = False) -> int:
        url = reverse("pin.gallery.image", kwargs={"pin_slug": self.pin.slug, "image_id": image.pk})
        return self.client.delete(f"{url}?from_wiki=1" if from_wiki else url).status_code

    def test_a_photo_on_no_wiki_is_deleted_outright(self) -> None:
        """The ordinary case has to keep working - delete still deletes."""
        photo = self._photo(on_wiki=False)

        self.assertEqual(self._delete(photo), 204)
        self.assertFalse(Image.objects.filter(pk=photo.pk).exists())

    def test_a_photo_on_a_wiki_stays_on_the_wiki(self) -> None:
        photo = self._photo(on_wiki=True)

        self.assertEqual(self._delete(photo), 204)

        photo.refresh_from_db()
        self.assertEqual(photo.wiki_id, self.wiki.pk, "the wiki lost a contribution nobody withdrew")

    def test_it_does_leave_the_pin(self) -> None:
        """Otherwise the delete did nothing at all."""
        photo = self._photo(on_wiki=True)

        self._delete(photo)

        photo.refresh_from_db()
        self.assertIsNone(photo.pin_id, "the photo is still on the pin it was deleted from")

    def test_saying_yes_withdraws_it_from_the_wiki(self) -> None:
        """An explicit answer is the only thing that takes it off."""
        photo = self._photo(on_wiki=True)

        self.assertEqual(self._delete(photo, from_wiki=True), 204)
        self.assertFalse(Image.objects.filter(pk=photo.pk).exists())

    def test_an_external_photo_stays_even_when_asked(self) -> None:
        """Nothing was consented to, so there is nothing to withdraw here."""
        photo = self._photo(on_wiki=True, source=ImageSource.LINKED_URL)

        self.assertEqual(self._delete(photo, from_wiki=True), 204)

        photo.refresh_from_db()
        self.assertEqual(photo.wiki_id, self.wiki.pk, "an external photo was pulled off the wiki from a pin screen")

    def test_somebody_elses_photo_is_still_untouchable(self) -> None:
        photo = self._photo(on_wiki=True)
        other = baker.make(User)
        self.client.force_login(other)

        self.assertEqual(self._delete(photo), 404)
        self.assertTrue(Image.objects.filter(pk=photo.pk).exists())

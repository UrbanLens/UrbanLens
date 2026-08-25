"""Only the owner of a photo can put it in front of a community.

Two surfaces let one person publish another person's picture. The wiki cover
photo accepted any image whose *location* matched, and a pin photo carries the
location - so a neighbour who could see your photo, which anyone with a pin at
the same place generally can, could install it on the front of a page everyone
reads. The wiki's cover is rendered with no visibility gate of its own, so that
choice was the only gate there was.

The gallery JSON printed `profile.username` straight off the row, while every
other surface that names somebody - the external API's `owner_slug`, wiki edit
attribution - resolves it through identity visibility first.
"""

from __future__ import annotations

import json

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import RequestFactory, TestCase
from django.urls import reverse
from model_bakery import baker

from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import VisibilityChoice
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.services.media.images import image_to_gallery_json
from urbanlens.dashboard.services.photos.attachment import attach_to_wiki


class WikiCoverPhotoTests(TestCase):
    """Who may choose the picture at the top of a shared page."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)
        self.owner_user = baker.make(User)
        self.owner = self.owner_user.profile
        self.neighbour_user = baker.make(User)
        self.neighbour = self.neighbour_user.profile
        self.location = baker.make(Location, latitude=41.7361, longitude=-73.9361)
        self.owner_pin = baker.make(Pin, profile=self.owner, location=self.location, parent_pin=None)
        baker.make(Pin, profile=self.neighbour, location=self.location, parent_pin=None)
        self.wiki = baker.make(Wiki, location=self.location)
        # Visible to the neighbour - that is the ordinary case, and the point:
        # being able to see a photo is not permission to publish it.
        self.owner.photo_upload_visibility = VisibilityChoice.ANYONE
        self.owner.save(update_fields=["photo_upload_visibility"])
        self.photo = Image.objects.create(
            image=SimpleUploadedFile("mine.jpg", b"not-a-real-jpeg", content_type="image/jpeg"),
            profile=self.owner,
            pin=self.owner_pin,
            location=self.location,
        )

    def _set_cover(self, image: Image):
        return self.client.post(
            reverse("location.wiki.cover_photo", kwargs={"location_slug": self.location.slug}),
            data=json.dumps({"image_id": image.pk}),
            content_type="application/json",
        )

    def test_a_neighbour_cannot_publish_your_pin_photo_as_the_wiki_cover(self) -> None:
        self.client.force_login(self.neighbour_user)

        response = self._set_cover(self.photo)

        self.assertEqual(response.status_code, 404, "a neighbour published another user's photo to the whole wiki")
        self.wiki.refresh_from_db()
        self.assertIsNone(self.wiki.cover_photo_id)

    def test_not_even_the_owner_can_until_they_have_contributed_it(self) -> None:
        """Publishing is one decision, not a side effect of another."""
        self.client.force_login(self.owner_user)

        response = self._set_cover(self.photo)

        self.assertEqual(response.status_code, 404)

    def test_a_contributed_photo_can_be_the_cover(self) -> None:
        """The positive control - this must not pass by refusing everything."""
        attach_to_wiki(self.photo, self.wiki, added_by=self.owner)
        Image.objects.filter(pk=self.photo.pk).update(wiki=self.wiki)
        self.client.force_login(self.owner_user)

        response = self._set_cover(self.photo)

        self.assertEqual(response.status_code, 200)
        self.wiki.refresh_from_db()
        self.assertEqual(self.wiki.cover_photo_id, self.photo.pk)


class GalleryJsonIdentityTests(TestCase):
    """Naming the uploader."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)
        self.owner = baker.make(User).profile
        self.viewer = baker.make(User).profile
        self.photo = Image.objects.create(
            image=SimpleUploadedFile("named.jpg", b"not-a-real-jpeg", content_type="image/jpeg"),
            profile=self.owner,
        )
        self.request = RequestFactory().get("/")

    def test_the_uploader_sees_their_own_name(self) -> None:
        payload = image_to_gallery_json(self.photo, self.request, self.owner)

        self.assertEqual(payload["uploader"], self.owner.username)

    def test_a_hidden_identity_is_not_named(self) -> None:
        """Whatever the profile's own visibility rules decide, the gallery must
        ask them rather than printing the username off the row."""
        self.owner.profile_visibility = VisibilityChoice.NO_ONE
        self.owner.save(update_fields=["profile_visibility"])

        payload = image_to_gallery_json(self.photo, self.request, self.viewer)

        self.assertNotEqual(payload["uploader"], self.owner.username, "the gallery named a profile that hides itself")

    def test_an_anonymous_viewer_is_not_told_either(self) -> None:
        self.owner.profile_visibility = VisibilityChoice.NO_ONE
        self.owner.save(update_fields=["profile_visibility"])

        payload = image_to_gallery_json(self.photo, self.request, None)

        self.assertNotEqual(payload["uploader"], self.owner.username)

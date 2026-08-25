"""What a wiki co-editor can and cannot see of somebody else's pin.

A wiki is shared by everyone with a pin at its place, so "another user with wiki
access" is not an attacker - it is the ordinary situation, and it is the one in
which private pin data is most likely to be exposed by accident. Two defects of
exactly that shape were found and fixed while this file was being written:
uploading to your own pin published the photo to the location's wiki, and photo
search returned other people's photos along with the *name of the pin* they
belong to.

Each test drives the app's own entry point - the view, or the exact queryset a
view uses - rather than asserting on the model layer. A rule enforced in a
queryset nobody calls protects nobody.

The suite deliberately carries positive controls: for every "the neighbour must
not see this" there is a "and the owner still can", or "and a deliberately shared
one still appears". Without them the whole file could pass by breaking the
feature it guards.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from model_bakery import baker

from urbanlens.dashboard.models.images.attachment import ImageAttachment
from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import VisibilityChoice
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.services.photos.attachment import attach_to_wiki
from urbanlens.dashboard.services.photos.uploads import upload_photo_for_owner

#: Distinctive enough that finding it anywhere in a response is unambiguous.
PRIVATE_CAPTION = "zzq-private-caption-nonce"
PRIVATE_PIN_NAME = "zzq-private-pin-name"


class WikiNeighbourTestCase(TestCase):
    """Two users with their own pins at one place, and the wiki they share."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)
        self.owner_user = baker.make(User)
        self.owner = self.owner_user.profile
        self.neighbour_user = baker.make(User)
        self.neighbour = self.neighbour_user.profile

        self.location = baker.make(Location, latitude=41.7361, longitude=-73.9361)
        self.owner_pin = baker.make(Pin, profile=self.owner, location=self.location, parent_pin=None, name=PRIVATE_PIN_NAME)
        self.neighbour_pin = baker.make(Pin, profile=self.neighbour, location=self.location, parent_pin=None)
        self.wiki = baker.make(Wiki, location=self.location)

    def _private_photo(self) -> Image:
        """A photo uploaded to the owner's own pin, never contributed anywhere."""
        result = upload_photo_for_owner(self.owner_pin, self.owner, SimpleUploadedFile("private.jpg", b"not-a-real-jpeg", content_type="image/jpeg"), PRIVATE_CAPTION)
        assert isinstance(result, Image), f"fixture upload was rejected: {result}"
        return result

    def _shared_photo(self) -> Image:
        """A photo the owner deliberately contributed to the wiki."""
        result = upload_photo_for_owner(self.owner_pin, self.owner, SimpleUploadedFile("shared.jpg", b"also-not-a-jpeg", content_type="image/jpeg"), "deliberately shared")
        assert isinstance(result, Image)
        attach_to_wiki(result, self.wiki, added_by=self.owner)
        Image.objects.filter(pk=result.pk).update(wiki=self.wiki)
        result.refresh_from_db()
        return result

    def _as_neighbour(self) -> None:
        self.client.force_login(self.neighbour_user)

    def _as_owner(self) -> None:
        self.client.force_login(self.owner_user)


class PrivatePinPhotoIsNotOnTheWikiTests(WikiNeighbourTestCase):
    """The wiki's own photo surfaces."""

    def test_the_wiki_photo_query_excludes_a_private_pin_upload(self) -> None:
        """The exact queryset the wiki gallery and Media panel both build."""
        private = self._private_photo()
        shared = self._shared_photo()

        on_the_wiki = set(Image.objects.filter(wiki=self.wiki).visible_to(self.neighbour).values_list("pk", flat=True))

        self.assertIn(shared.pk, on_the_wiki, "a deliberately shared photo stopped appearing on the wiki")
        self.assertNotIn(private.pk, on_the_wiki, "a private pin upload is on the wiki")

    def test_a_private_upload_creates_no_wiki_attachment(self) -> None:
        private = self._private_photo()

        self.assertFalse(ImageAttachment.objects.filter(image=private, wiki=self.wiki).exists())

    def test_widening_your_own_visibility_is_not_consent_to_publish(self) -> None:
        """`photo_upload_visibility` governs the audience for things you have
        shared. It is not a decision to share, and a future shortcut reading
        "ANYONE means it is public anyway" must fail here."""
        self.owner.photo_upload_visibility = VisibilityChoice.ANYONE
        self.owner.save(update_fields=["photo_upload_visibility"])
        private = self._private_photo()

        on_the_wiki = set(Image.objects.filter(wiki=self.wiki).visible_to(self.neighbour).values_list("pk", flat=True))

        self.assertNotIn(private.pk, on_the_wiki)

    def test_the_overlay_picker_shows_the_same_photos_as_the_gallery(self) -> None:
        """Two surfaces onto one wiki's photos must not disagree about which
        photos those are."""
        self._private_photo()
        shared = self._shared_photo()
        # Restrictive, so the two surfaces would actually disagree if only one of
        # them asked. On the default setting a neighbour holds a common pin and is
        # admitted either way, which makes the comparison pass without proving
        # anything.
        self.owner.photo_upload_visibility = VisibilityChoice.NO_ONE
        self.owner.save(update_fields=["photo_upload_visibility"])
        self._as_neighbour()

        from django.urls import reverse

        response = self.client.get(reverse("location.wiki.overlays.media", kwargs={"location_slug": self.location.slug}))

        self.assertEqual(response.status_code, 200, "the wiki overlay picker was not reachable")
        listed = {entry["id"] for entry in response.json()["images"]}
        gallery = set(Image.objects.filter(wiki=self.wiki).visible_to(self.neighbour).values_list("pk", flat=True))
        self.assertEqual(listed, gallery, "the overlay picker and the wiki gallery disagree about the wiki's photos")
        self.assertNotIn(shared.pk, listed, "the picker listed a photo its uploader shows to no one")


class PrivatePinDataIsNotReachableTests(WikiNeighbourTestCase):
    """The pin's own surfaces, requested by somebody who is not its owner."""

    def test_the_pin_gallery_is_not_readable_by_a_neighbour(self) -> None:
        self._private_photo()
        self._as_neighbour()

        response = self.client.get(f"/dashboard/map/pin/{self.owner_pin.slug}/gallery/")

        self.assertIn(response.status_code, (403, 404), f"a neighbour read another user's pin gallery ({response.status_code})")

    def test_the_child_pin_subtree_is_not_readable_by_a_neighbour(self) -> None:
        """Child pins are entrances, hazards and stairs - the most operationally
        sensitive thing a pin carries."""
        baker.make(Pin, profile=self.owner, location=self.location, parent_pin=self.owner_pin, name="zzq-hidden-entrance")
        self._as_neighbour()

        response = self.client.get(f"/dashboard/map/pin/{self.owner_pin.slug}/gallery/?children=1")

        self.assertIn(response.status_code, (403, 404))
        self.assertNotContains(response, "zzq-hidden-entrance", status_code=response.status_code)

    def test_the_owner_can_read_their_own_pin_gallery(self) -> None:
        """The positive control for the two above."""
        self._private_photo()
        self._as_owner()

        response = self.client.get(f"/dashboard/map/pin/{self.owner_pin.slug}/gallery/")

        self.assertEqual(response.status_code, 200, "the owner lost access to their own gallery")


class PrivatePhotoBytesTests(WikiNeighbourTestCase):
    """The media gate, which is what actually protects the file itself."""

    def _fetch_bytes(self, image: Image) -> int:
        return self.client.get(f"/media/{image.image.name}").status_code

    def test_an_unshared_pin_photo_is_refused_whatever_the_setting_says(self) -> None:
        """A photo on your pin is your record of a place, not a publication.

        `photo_upload_visibility` decides who may see a photo you have *shared*.
        It used to decide who may see an unshared one too, and its default
        (`ANYTHING_IN_COMMON`) accepts `common_pin` - so pinning the same place
        as somebody was enough to read their pin photos.
        """
        private = self._private_photo()
        self._as_neighbour()

        for setting in (VisibilityChoice.NO_ONE, VisibilityChoice.ANYONE, VisibilityChoice.ANYTHING_IN_COMMON):
            with self.subTest(setting=setting):
                self.owner.photo_upload_visibility = setting
                self.owner.save(update_fields=["photo_upload_visibility"])
                self.assertEqual(self._fetch_bytes(private), 404, f"a co-pinner fetched an unshared pin photo under {setting}")

    def test_once_shared_the_setting_decides_again(self) -> None:
        """The other half: sharing is what hands the setting its job back."""
        shared = self._shared_photo()
        self._as_neighbour()

        for setting, allowed in (
            (VisibilityChoice.NO_ONE, False),
            (VisibilityChoice.ANYONE, True),
            (VisibilityChoice.ANYTHING_IN_COMMON, True),
        ):
            with self.subTest(setting=setting):
                self.owner.photo_upload_visibility = setting
                self.owner.save(update_fields=["photo_upload_visibility"])
                status = self._fetch_bytes(shared)
                expected = 200 if allowed else 404
                self.assertEqual(status, expected, f"a shared photo under {setting} returned {status}")

    def test_the_owner_can_always_fetch_their_own_file(self) -> None:
        private = self._private_photo()
        self.owner.photo_upload_visibility = VisibilityChoice.NO_ONE
        self.owner.save(update_fields=["photo_upload_visibility"])
        self._as_owner()

        self.assertEqual(self._fetch_bytes(private), 200, "the uploader lost access to their own photo")

    def test_an_anonymous_visitor_cannot_fetch_a_private_file(self) -> None:
        private = self._private_photo()
        self.owner.photo_upload_visibility = VisibilityChoice.ANYTHING_IN_COMMON
        self.owner.save(update_fields=["photo_upload_visibility"])

        self.assertNotEqual(self._fetch_bytes(private), 200, "an anonymous request fetched a private photo")

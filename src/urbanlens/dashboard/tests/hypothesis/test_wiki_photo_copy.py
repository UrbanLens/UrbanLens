"""Tests for copying a wiki photo onto the copier's own pin, with durable provenance.

Covers ``services.photos.wiki_copy.copy_wiki_photo_to_pin`` directly (the headline
requirement - a copy must survive the original being deleted, and must not duplicate storage),
``CopyWikiPhotoView`` (authorization must be scoped like every other wiki-photo lookup, never
like ``PhotoActionView``'s owned-image-only lookup), and ``ImageQuerySet.copied_from_others``.
"""

from __future__ import annotations

from unittest import mock

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.middleware.csrf import get_token
from django.test import Client, RequestFactory
from django.urls import reverse
from model_bakery import baker

from hypothesis import given, settings, strategies as st
from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.images.model import Image, QuotaExemption
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.services.photos.wiki_copy import copy_wiki_photo_to_pin


def _wiki_image(*, wiki: Wiki, location: Location, profile, **kwargs) -> Image:
    return Image.objects.create(
        image=SimpleUploadedFile("original.jpg", b"bytes", content_type="image/jpeg"),
        wiki=wiki,
        location=location,
        profile=profile,
        **kwargs,
    )


class CopyWikiPhotoToPinTests(TestCase):
    def setUp(self) -> None:
        self.uploader = baker.make(User).profile
        self.copier = baker.make(User).profile
        self.location = baker.make(Location)
        self.wiki = baker.make(Wiki, location=self.location)
        self.pin = baker.make(Pin, profile=self.copier, location=self.location)
        self.image = _wiki_image(
            wiki=self.wiki,
            location=self.location,
            profile=self.uploader,
            author="",
            caption="don't copy this",
            latitude="40.000000",
            longitude="-74.000000",
        )

    def test_creates_an_independent_row(self) -> None:
        copy, created = copy_wiki_photo_to_pin(self.image, self.pin, self.copier)

        self.assertTrue(created)
        self.assertNotEqual(copy.pk, self.image.pk)
        self.assertEqual(copy.pin_id, self.pin.pk)
        self.assertEqual(copy.profile_id, self.copier.pk)

    def test_reuses_the_stored_file_without_duplicating_bytes(self) -> None:
        copy, _created = copy_wiki_photo_to_pin(self.image, self.pin, self.copier)

        self.assertEqual(copy.image.name, self.image.image.name)
        self.assertEqual(copy.quota_exempt_reason, QuotaExemption.WIKI_COPY)

    def test_does_not_copy_caption_or_exif(self) -> None:
        """Mirrors create_pin_from_share's rule: caption is the owner's own account of the photo."""
        copy, _created = copy_wiki_photo_to_pin(self.image, self.pin, self.copier)

        self.assertIsNone(copy.caption)

    def test_copies_lat_lng_so_the_copy_can_be_placed_on_a_map(self) -> None:
        copy, _created = copy_wiki_photo_to_pin(self.image, self.pin, self.copier)

        self.assertEqual(copy.latitude, self.image.latitude)
        self.assertEqual(copy.longitude, self.image.longitude)

    def test_survives_the_original_image_being_deleted(self) -> None:
        """The headline requirement: deleting the wiki photo must not delete or break the copy."""
        copy, _created = copy_wiki_photo_to_pin(self.image, self.pin, self.copier)

        self.image.delete()
        copy.refresh_from_db()

        self.assertIsNotNone(copy.pk)
        # copied_from points at the Image row itself, so it goes null...
        self.assertIsNone(copy.copied_from_id)
        # ...but copied_from_profile/location point at the Profile/Location,
        # neither of which was touched by deleting the Image row, so live
        # attribution is still available beyond just the denormalized text.
        self.assertEqual(copy.copied_from_profile_id, self.uploader.pk)
        self.assertEqual(copy.author, f"Uploaded by {self.uploader.username}")

    def test_survives_the_original_uploaders_account_being_deleted(self) -> None:
        """The extreme case: even the uploader's account disappearing must not lose attribution."""
        copy, _created = copy_wiki_photo_to_pin(self.image, self.pin, self.copier)
        original_uploader_name = self.uploader.username

        self.uploader.user.delete()
        copy.refresh_from_db()

        self.assertIsNotNone(copy.pk)
        self.assertIsNone(copy.copied_from_profile_id)
        # The denormalized text is what's left once every live FK is gone.
        self.assertEqual(copy.author, f"Uploaded by {original_uploader_name}")
        self.assertEqual(copy.copied_from_label, self.location.display_name)

    def test_unattributed_photo_is_credited_to_its_uploader(self) -> None:
        copy, _created = copy_wiki_photo_to_pin(self.image, self.pin, self.copier)

        self.assertEqual(copy.author, f"Uploaded by {self.uploader.username}")

    def test_already_credited_photo_keeps_its_existing_credit(self) -> None:
        self.image.author = "Jane Photographer"
        self.image.save(update_fields=["author"])

        copy, _created = copy_wiki_photo_to_pin(self.image, self.pin, self.copier)

        self.assertEqual(copy.author, "Jane Photographer")

    def test_label_captures_the_location_at_copy_time(self) -> None:
        copy, _created = copy_wiki_photo_to_pin(self.image, self.pin, self.copier)

        self.assertEqual(copy.copied_from_label, self.location.display_name)

    def test_copying_the_same_photo_twice_is_idempotent(self) -> None:
        first, first_created = copy_wiki_photo_to_pin(self.image, self.pin, self.copier)
        second, second_created = copy_wiki_photo_to_pin(self.image, self.pin, self.copier)

        self.assertTrue(first_created)
        self.assertFalse(second_created)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(Image.objects.filter(pin=self.pin, copied_from=self.image).count(), 1)

    def test_two_different_profiles_copying_the_same_photo_each_get_their_own_row(self) -> None:
        # A profile may have at most one pin per location (db_pin_unique_location_per_profile),
        # so "a different pin for the same copier" isn't a real scenario - the idempotency
        # check has to be exercised across two different copiers instead.
        other_copier = baker.make(User).profile
        other_pin = baker.make(Pin, profile=other_copier, location=self.location)

        first, first_created = copy_wiki_photo_to_pin(self.image, self.pin, self.copier)
        second, second_created = copy_wiki_photo_to_pin(self.image, other_pin, other_copier)

        self.assertTrue(first_created)
        self.assertTrue(second_created)
        self.assertNotEqual(first.pk, second.pk)


class AttributionFallbackPropertyTests(TestCase):
    """Property: the copy's author is always either the original's own credit, or a derived one."""

    # Real DB writes per example (Profile/Location/Wiki/Pin/Image) always exceed
    # Hypothesis's default 200ms deadline - expected for a DB-backed property test.
    @settings(deadline=None)
    @given(existing_author=st.one_of(st.none(), st.text(min_size=1, max_size=100).filter(lambda s: "\x00" not in s)))
    def test_author_fallback_is_deterministic(self, existing_author: str | None) -> None:
        uploader = baker.make(User).profile
        copier = baker.make(User).profile
        location = baker.make(Location)
        wiki = baker.make(Wiki, location=location)
        pin = baker.make(Pin, profile=copier, location=location)
        image = _wiki_image(wiki=wiki, location=location, profile=uploader, author=existing_author)

        copy, _created = copy_wiki_photo_to_pin(image, pin, copier)

        if existing_author:
            self.assertEqual(copy.author, existing_author)
        else:
            self.assertEqual(copy.author, f"Uploaded by {uploader.username}")


class ImageQuerySetCopiedFromOthersTests(TestCase):
    def test_filters_to_photos_with_copy_provenance(self) -> None:
        profile = baker.make(User).profile
        uploader = baker.make(User).profile
        location = baker.make(Location)
        wiki = baker.make(Wiki, location=location)
        pin = baker.make(Pin, profile=profile, location=location)
        original = _wiki_image(wiki=wiki, location=location, profile=uploader)
        own_upload = Image.objects.create(
            image=SimpleUploadedFile("mine.jpg", b"bytes", content_type="image/jpeg"), pin=pin, profile=profile
        )

        copy, _created = copy_wiki_photo_to_pin(original, pin, profile)

        others = Image.objects.filter(profile=profile).copied_from_others()
        self.assertIn(copy, others)
        self.assertNotIn(own_upload, others)


class VaultPhotosShowFromOthersTests(TestCase):
    """?show=from_others on Vault Photos (VaultPhotosView._sorted_gallery) browses copies only."""

    def setUp(self) -> None:
        self.client = Client()
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)

        uploader = baker.make(User).profile
        location = baker.make(Location)
        wiki = baker.make(Wiki, location=location)
        pin = baker.make(Pin, profile=self.profile, location=location)
        original = _wiki_image(wiki=wiki, location=location, profile=uploader)
        self.copy, _created = copy_wiki_photo_to_pin(original, pin, self.profile)
        self.own_upload = Image.objects.create(
            image=SimpleUploadedFile("mine.jpg", b"bytes", content_type="image/jpeg"), pin=pin, profile=self.profile
        )

    def test_default_view_shows_every_owned_photo(self) -> None:
        response = self.client.get(reverse("vault.photos"))

        ids = {img.pk for img in response.context["images"]}
        self.assertEqual(ids, {self.copy.pk, self.own_upload.pk})
        self.assertEqual(response.context["from_others_count"], 1)

    def test_from_others_view_shows_only_the_copy(self) -> None:
        response = self.client.get(reverse("vault.photos"), {"show": "from_others"})

        ids = {img.pk for img in response.context["images"]}
        self.assertEqual(ids, {self.copy.pk})


class CopyWikiPhotoViewTests(TestCase):
    def setUp(self) -> None:
        self.client = Client(enforce_csrf_checks=True)
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)
        self.csrf_token = get_token(RequestFactory().get("/"))
        self.client.cookies["csrftoken"] = self.csrf_token

        self.uploader = baker.make(User).profile
        self.location = baker.make(Location)
        self.wiki = baker.make(Wiki, location=self.location)
        self.pin = baker.make(Pin, profile=self.profile, location=self.location)
        # Profile.photo_upload_visibility/viewer_photo_filter both default to
        # ANYTHING_IN_COMMON, not ANYONE - a pin at the same location is what
        # makes ImageQuerySet.visible_to (which CopyWikiPhotoView's lookup
        # goes through, same as every other wiki-photo view) admit a viewer
        # who isn't the uploader and shares no friendship/trip with them.
        baker.make(Pin, profile=self.uploader, location=self.location)
        self.image = _wiki_image(wiki=self.wiki, location=self.location, profile=self.uploader)

    def _post(self, image_id: int):
        return self.client.post(
            reverse("location.wiki.media.copy_to_pin", args=[self.location.slug, image_id]),
            HTTP_X_CSRFTOKEN=self.csrf_token,
        )

    def test_copies_the_photo_to_the_viewers_pin(self) -> None:
        response = self._post(self.image.pk)

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["copied"])
        self.assertFalse(body["already_copied"])
        self.assertEqual(body["pin_slug"], self.pin.slug)
        self.assertTrue(Image.objects.filter(pin=self.pin, copied_from=self.image, profile=self.profile).exists())

    def test_repeat_copy_reports_already_copied(self) -> None:
        self._post(self.image.pk)
        response = self._post(self.image.pk)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["already_copied"])
        self.assertEqual(Image.objects.filter(pin=self.pin, copied_from=self.image).count(), 1)

    def test_refuses_an_image_not_on_this_wiki(self) -> None:
        other_location = baker.make(Location)
        other_wiki = baker.make(Wiki, location=other_location)
        baker.make(Pin, profile=self.profile, location=other_location)
        elsewhere_image = _wiki_image(wiki=other_wiki, location=other_location, profile=self.uploader)

        response = self._post(elsewhere_image.pk)

        self.assertEqual(response.status_code, 404)
        self.assertFalse(Image.objects.filter(copied_from=elsewhere_image).exists())

    def test_refuses_a_wiki_the_viewer_cannot_see(self) -> None:
        stranger = baker.make(User)
        client = Client(enforce_csrf_checks=True)
        client.force_login(stranger)
        csrf_token = get_token(RequestFactory().get("/"))
        client.cookies["csrftoken"] = csrf_token

        response = client.post(
            reverse("location.wiki.media.copy_to_pin", args=[self.location.slug, self.image.pk]),
            HTTP_X_CSRFTOKEN=csrf_token,
        )

        # resolve_visible_wiki refuses a stranger before the image lookup even runs.
        self.assertNotEqual(response.status_code, 200)
        self.assertFalse(Image.objects.filter(copied_from=self.image).exists())

    def test_refuses_when_the_viewer_has_no_pin_here(self) -> None:
        # self.pin can't simply be deleted to test this: it's also what earns
        # this profile wiki access and image visibility in the first place (a
        # pin inside the place's boundary - see wiki_access/visible_to), so
        # removing it would 404 at the image lookup, before this view's own
        # "no target pin" check ever runs. The real case this branch exists
        # for is domain-widened access - wiki access earned via a pin on a
        # *different* building on the same parcel, with no pin at this exact
        # location - isolated here by mocking only the `location` resolve_
        # visible_wiki hands back (image lookup keys off `wiki`, not
        # `location`, so self.pin still legitimately makes the image visible;
        # only the target-pin lookup, which does use `location`, sees no pin).
        mock_location = mock.MagicMock()
        mock_location.pins.filter.return_value.first.return_value = None
        with mock.patch(
            "urbanlens.dashboard.controllers.wiki_media.resolve_visible_wiki",
            return_value=(mock_location, self.wiki, self.profile),
        ):
            response = self._post(self.image.pk)

        self.assertEqual(response.status_code, 400)
        self.assertFalse(Image.objects.filter(copied_from=self.image).exists())

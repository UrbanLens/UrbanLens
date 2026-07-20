"""Tests for the profile page's photo strip (services.profile_photos + the
PhotoAttachmentPointsView lightbox side panel).

The central invariant, stated explicitly in the original request: a photo
must never be shown to a viewer who doesn't already have some other,
independent way to see it - a bare pin-only upload (the overwhelming
default) is never eligible at all, even on the owner's own strip, and a
wiki-attached photo is only shown to a second viewer who has genuinely
pinned that location themselves. Every "shown" test here has a matching
"NOT shown" counterpart proving the boundary, not just the positive case.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.direct_messages.model import DirectMessage
from urbanlens.dashboard.models.images.model import Image, MediaKind
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.meta import VisibilityChoice
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.services.profile_photos import attachment_points_for_image, strip_photos_for_owner, strip_photos_visible_to


def _location(**kwargs) -> Location:
    return baker.make(Location, latitude=40.0, longitude=-74.0, **kwargs)


def _wiki_with_pin(profile, **wiki_kwargs) -> tuple[Location, Wiki]:
    """A Location with a Wiki, and `profile` pinned there (so it's wiki-visible to them)."""
    location = _location()
    wiki = baker.make(Wiki, location=location, **wiki_kwargs)
    baker.make(Pin, profile=profile, location=location)
    return location, wiki


class StripPhotosForOwnerTests(TestCase):
    def setUp(self) -> None:
        self.user = baker.make(User)
        self.profile = self.user.profile

    def test_pin_only_photo_is_never_included(self) -> None:
        """The overwhelming default - a bare upload attached to nothing but a
        private pin must never appear, even to its own owner."""
        pin = baker.make(Pin, profile=self.profile, location=_location())
        baker.make(Image, profile=self.profile, pin=pin, wiki=None, direct_message=None, media_type=MediaKind.PHOTO)
        self.assertEqual(list(strip_photos_for_owner(self.profile)), [])

    def test_bare_upload_with_no_host_at_all_is_not_included(self) -> None:
        baker.make(Image, profile=self.profile, pin=None, wiki=None, direct_message=None, media_type=MediaKind.PHOTO)
        self.assertEqual(list(strip_photos_for_owner(self.profile)), [])

    def test_wiki_attached_photo_is_included(self) -> None:
        _location_unused, wiki = _wiki_with_pin(self.profile)
        image = baker.make(Image, profile=self.profile, wiki=wiki, pin=None, media_type=MediaKind.PHOTO)
        self.assertEqual(list(strip_photos_for_owner(self.profile)), [image])

    def test_dm_attached_photo_is_included(self) -> None:
        other = baker.make(User).profile
        message = baker.make(DirectMessage, sender=self.profile, recipient=other, body="hi")
        image = baker.make(Image, profile=self.profile, direct_message=message, pin=None, media_type=MediaKind.PHOTO)
        self.assertEqual(list(strip_photos_for_owner(self.profile)), [image])

    def test_another_profiles_photos_are_never_included(self) -> None:
        other_profile = baker.make(User).profile
        _location_unused, wiki = _wiki_with_pin(other_profile)
        baker.make(Image, profile=other_profile, wiki=wiki, pin=None, media_type=MediaKind.PHOTO)
        self.assertEqual(list(strip_photos_for_owner(self.profile)), [])

    def test_video_media_type_is_excluded(self) -> None:
        _location_unused, wiki = _wiki_with_pin(self.profile)
        baker.make(Image, profile=self.profile, wiki=wiki, pin=None, media_type=MediaKind.VIDEO)
        self.assertEqual(list(strip_photos_for_owner(self.profile)), [])


class StripPhotosVisibleToTests(TestCase):
    """The other-user-viewing-a-profile case - the strictest, most security-sensitive path."""

    def setUp(self) -> None:
        self.owner = baker.make(User).profile
        self.owner.photo_upload_visibility = VisibilityChoice.ANYONE
        self.owner.save(update_fields=["photo_upload_visibility"])
        self.viewer = baker.make(User).profile

    def test_wiki_photo_visible_when_viewer_has_pinned_the_location(self) -> None:
        location = _location()
        wiki = baker.make(Wiki, location=location)
        baker.make(Pin, profile=self.owner, location=location)
        baker.make(Pin, profile=self.viewer, location=location)
        image = baker.make(Image, profile=self.owner, wiki=wiki, pin=None, media_type=MediaKind.PHOTO)

        self.assertEqual(list(strip_photos_visible_to(self.owner, self.viewer)), [image])

    def test_wiki_photo_hidden_when_viewer_has_not_pinned_the_location(self) -> None:
        """The core invariant: no independent access path -> never shown, full stop."""
        location = _location()
        wiki = baker.make(Wiki, location=location)
        baker.make(Pin, profile=self.owner, location=location)
        # Deliberately no pin for self.viewer anywhere near this location.
        baker.make(Image, profile=self.owner, wiki=wiki, pin=None, media_type=MediaKind.PHOTO)

        self.assertEqual(list(strip_photos_visible_to(self.owner, self.viewer)), [])

    def test_pin_only_photo_is_never_visible_to_another_profile(self) -> None:
        pin = baker.make(Pin, profile=self.owner, location=_location())
        baker.make(Image, profile=self.owner, pin=pin, wiki=None, media_type=MediaKind.PHOTO)
        self.assertEqual(list(strip_photos_visible_to(self.owner, self.viewer)), [])

    def test_dm_attached_photo_is_never_shown_to_a_second_viewer(self) -> None:
        """Deliberate scope decision (see profile_photos.py's own docstring) -
        DM attachments only ever make the OWNER's strip non-empty, never a
        second viewer's view of someone else's profile."""
        message = baker.make(DirectMessage, sender=self.owner, recipient=self.viewer, body="hi")
        baker.make(Image, profile=self.owner, direct_message=message, pin=None, media_type=MediaKind.PHOTO)
        self.assertEqual(list(strip_photos_visible_to(self.owner, self.viewer)), [])

    def test_uploader_visibility_setting_still_composes_with_wiki_access(self) -> None:
        """Wiki-location access is necessary but not sufficient - the uploader's
        own photo_upload_visibility (a second, independent gate) must still
        permit this specific viewer, exactly as it already does for the real
        wiki gallery (ImageQuerySet.visible_to)."""
        self.owner.photo_upload_visibility = VisibilityChoice.NO_ONE
        self.owner.save(update_fields=["photo_upload_visibility"])
        location = _location()
        wiki = baker.make(Wiki, location=location)
        baker.make(Pin, profile=self.owner, location=location)
        baker.make(Pin, profile=self.viewer, location=location)
        baker.make(Image, profile=self.owner, wiki=wiki, pin=None, media_type=MediaKind.PHOTO)

        self.assertEqual(list(strip_photos_visible_to(self.owner, self.viewer)), [])


class AttachmentPointsForImageTests(TestCase):
    def setUp(self) -> None:
        self.user = baker.make(User)
        self.profile = self.user.profile

    def test_pin_only_photo_has_no_attachment_points(self) -> None:
        pin = baker.make(Pin, profile=self.profile, location=_location())
        image = baker.make(Image, profile=self.profile, pin=pin, wiki=None, media_type=MediaKind.PHOTO)
        self.assertEqual(attachment_points_for_image(image), [])

    def test_wiki_attached_photo_has_a_wiki_point(self) -> None:
        _location_unused, wiki = _wiki_with_pin(self.profile, name="Old Mill")
        image = baker.make(Image, profile=self.profile, wiki=wiki, pin=None, media_type=MediaKind.PHOTO)
        points = attachment_points_for_image(image)
        self.assertEqual(len(points), 1)
        self.assertIn("Old Mill", points[0]["label"])
        self.assertEqual(points[0]["url"], reverse("location.wiki", args=[wiki.location.slug]))

    def test_dm_attached_photo_has_a_recipient_point(self) -> None:
        other = baker.make(User, username="dm_partner")
        message = baker.make(DirectMessage, sender=self.profile, recipient=other.profile, body="hi")
        image = baker.make(Image, profile=self.profile, direct_message=message, pin=None, media_type=MediaKind.PHOTO)
        points = attachment_points_for_image(image)
        self.assertEqual(len(points), 1)
        self.assertIn("dm_partner", points[0]["label"])


class PhotoAttachmentPointsViewTests(TestCase):
    def setUp(self) -> None:
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)

    def test_owner_sees_their_own_photos_attachment_points(self) -> None:
        _location_unused, wiki = _wiki_with_pin(self.profile)
        image = baker.make(Image, profile=self.profile, wiki=wiki, pin=None, media_type=MediaKind.PHOTO)
        response = self.client.get(reverse("profile.photo.attachments", args=[image.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Wiki:")

    def test_non_owner_gets_204_never_the_attachment_info(self) -> None:
        other_profile = baker.make(User).profile
        _location_unused, wiki = _wiki_with_pin(other_profile)
        image = baker.make(Image, profile=other_profile, wiki=wiki, pin=None, media_type=MediaKind.PHOTO)
        response = self.client.get(reverse("profile.photo.attachments", args=[image.pk]))
        self.assertEqual(response.status_code, 204)

    def test_photo_with_no_attachments_gets_204(self) -> None:
        pin = baker.make(Pin, profile=self.profile, location=_location())
        image = baker.make(Image, profile=self.profile, pin=pin, wiki=None, media_type=MediaKind.PHOTO)
        response = self.client.get(reverse("profile.photo.attachments", args=[image.pk]))
        self.assertEqual(response.status_code, 204)


class ProfilePageShowsPhotoStripTests(TestCase):
    """End-to-end: the actual profile page view wires strip_photos_* in correctly."""

    def setUp(self) -> None:
        self.owner = baker.make(User)
        self.owner.profile.photo_upload_visibility = VisibilityChoice.ANYONE
        self.owner.profile.profile_visibility = VisibilityChoice.ANYONE
        self.owner.profile.save(update_fields=["photo_upload_visibility", "profile_visibility"])

    def test_own_profile_page_shows_wiki_attached_photo_not_pin_only_one(self) -> None:
        self.client.force_login(self.owner)
        _location_unused, wiki = _wiki_with_pin(self.owner.profile)
        pin = baker.make(Pin, profile=self.owner.profile, location=_location())
        shown = baker.make(Image, profile=self.owner.profile, wiki=wiki, pin=None, media_type=MediaKind.PHOTO)
        hidden = baker.make(Image, profile=self.owner.profile, pin=pin, wiki=None, media_type=MediaKind.PHOTO)

        response = self.client.get(reverse("profile.view"))

        self.assertContains(response, f'data-photo-id="{shown.pk}"')
        self.assertNotContains(response, f'data-photo-id="{hidden.pk}"')

    def test_other_viewer_without_wiki_access_sees_no_strip_at_all(self) -> None:
        viewer = baker.make(User)
        self.client.force_login(viewer)
        location = _location()
        wiki = baker.make(Wiki, location=location)
        baker.make(Pin, profile=self.owner.profile, location=location)
        image = baker.make(Image, profile=self.owner.profile, wiki=wiki, pin=None, media_type=MediaKind.PHOTO)

        response = self.client.get(reverse("profile.view_user", kwargs={"profile_slug": self.owner.profile.slug or self.owner.profile.ensure_slug()}))

        self.assertNotContains(response, f'data-photo-id="{image.pk}"')
        self.assertNotContains(response, ">Photos<")

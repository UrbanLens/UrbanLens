"""A photo on a wiki is visible only to viewers who can reach *that* wiki.

``visible_to`` enforces two gates - the photo must have been shared into a
container, and the uploader's settings must admit this viewer. The container
half asked only whether the photo was on *a* wiki, never whether the viewer
could reach *that* one. Since the permissive end of the upload setting admits
anyone with a pin in common, a photo contributed to a wiki at one place was
readable by somebody whose only pin is somewhere else.

The uploader's setting is opened to ANYONE throughout, so gate two cannot be
what excludes anything - only reachability of the wiki can. Each negative
carries a positive control, or the file could pass by breaking the feature.
"""

from __future__ import annotations

import io

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from model_bakery import baker
from PIL import Image as PILImage

from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile, VisibilityChoice
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.services.photos.attachment import attach_to_wiki
from urbanlens.dashboard.services.photos.uploads import upload_photo_for_owner


def _jpeg_bytes() -> bytes:
    buf = io.BytesIO()
    PILImage.new("RGB", (60, 40), color=(10, 20, 30)).save(buf, format="JPEG")
    return buf.getvalue()


class WikiReachabilityTestCase(TestCase):
    """One place both users pinned, and one place only the uploader pinned."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)
        self.owner_user = baker.make(User)
        self.owner = self.owner_user.profile
        self.viewer_user = baker.make(User)
        self.viewer = self.viewer_user.profile

        # Gate two wide open in both directions, so only gate one can exclude.
        Profile.objects.filter(pk=self.owner.pk).update(photo_upload_visibility=VisibilityChoice.ANYONE)
        Profile.objects.filter(pk=self.viewer.pk).update(viewer_photo_filter=VisibilityChoice.ANYONE)
        self.owner.refresh_from_db()
        self.viewer.refresh_from_db()

        self.near = baker.make(Location, latitude=41.7361, longitude=-73.9361)
        self.far = baker.make(Location, latitude=47.6062, longitude=-122.3321)

        self.owner_near_pin = baker.make(Pin, profile=self.owner, location=self.near, parent_pin=None)
        self.owner_far_pin = baker.make(Pin, profile=self.owner, location=self.far, parent_pin=None)
        # The viewer has pinned only the near place - the far wiki is out of reach.
        baker.make(Pin, profile=self.viewer, location=self.near, parent_pin=None)

        self.near_wiki = baker.make(Wiki, location=self.near)
        self.far_wiki = baker.make(Wiki, location=self.far)

    def _contribute(self, pin: Pin, wiki: Wiki, name: str) -> Image:
        """Upload a photo to *pin* and deliberately contribute it to *wiki*."""
        result = upload_photo_for_owner(pin, self.owner, SimpleUploadedFile(f"{name}.jpg", _jpeg_bytes(), content_type="image/jpeg"), name)
        assert isinstance(result, Image), f"fixture upload was rejected: {result}"
        attach_to_wiki(result, wiki, added_by=self.owner)
        Image.objects.filter(pk=result.pk).update(wiki=wiki)
        result.refresh_from_db()
        return result

    @staticmethod
    def _visible(image: Image, viewer: Profile | None) -> bool:
        """Narrow first, exactly as ``visible_to``'s docstring requires."""
        return Image.objects.filter(pk=image.pk).visible_to(viewer).exists()


class PhotosOnUnreachableWikisTests(WikiReachabilityTestCase):
    def test_a_photo_on_an_unreachable_wiki_is_not_visible(self) -> None:
        """The viewer has no pin at the far place, so its wiki is not theirs to read."""
        photo = self._contribute(self.owner_far_pin, self.far_wiki, "far")

        self.assertFalse(self._visible(photo, self.viewer), "a photo on a wiki the viewer cannot reach was visible to them")

    def test_a_photo_on_a_reachable_wiki_is_still_visible(self) -> None:
        """Positive control: the near wiki is shared, and contribution still works."""
        photo = self._contribute(self.owner_near_pin, self.near_wiki, "near")

        self.assertTrue(self._visible(photo, self.viewer), "a deliberately contributed photo stopped being visible on a shared wiki")

    def test_the_owner_always_sees_their_own_photo(self) -> None:
        """Positive control: reachability never applies to your own uploads."""
        photo = self._contribute(self.owner_far_pin, self.far_wiki, "far")

        self.assertTrue(self._visible(photo, self.owner), "the uploader lost sight of their own photo")

    def test_gaining_a_pin_at_the_far_place_grants_the_photo(self) -> None:
        """The gate tracks reachability rather than anything cached at upload time."""
        photo = self._contribute(self.owner_far_pin, self.far_wiki, "far")
        self.assertFalse(self._visible(photo, self.viewer))

        baker.make(Pin, profile=self.viewer, location=self.far, parent_pin=None)

        self.assertTrue(self._visible(photo, self.viewer), "pinning the far place did not grant its wiki's photos")


class AnonymousViewersTests(WikiReachabilityTestCase):
    """A signed-out visitor has pinned nothing, so no wiki is within reach."""

    def test_a_wiki_photo_is_not_visible_to_a_signed_out_visitor(self) -> None:
        photo = self._contribute(self.owner_near_pin, self.near_wiki, "near")

        self.assertFalse(self._visible(photo, None), "a wiki photo was readable without signing in")

    def test_the_most_permissive_setting_does_not_admit_a_signed_out_visitor(self) -> None:
        """ANYONE is labelled 'Anyone (Logged In)' and is scoped to exactly that."""
        photo = self._contribute(self.owner_near_pin, self.near_wiki, "near")
        Profile.objects.filter(pk=self.owner.pk).update(photo_upload_visibility=VisibilityChoice.ANYONE)

        self.assertFalse(self._visible(photo, None), "the ANYONE setting leaked a photo to a signed-out visitor")


class TripActivityPhotosTests(WikiReachabilityTestCase):
    """A pin on a trip activity does *not* share its photos with the trip.

    It used to. Adding a place to an itinerary says where the group is going;
    it is not a per-photo decision, and ``docs/GOALS.md`` requires one before
    a pin's contents reach anybody else. The grant was also live - a photo
    uploaded to that pin months later joined the exposure by itself - which is
    the shape GOALS rules out outright.
    """

    def setUp(self) -> None:
        super().setUp()
        from urbanlens.dashboard.models.trips.model import Trip, TripActivity, TripMembership

        self.member_user = baker.make(User)
        self.member = self.member_user.profile
        Profile.objects.filter(pk=self.member.pk).update(viewer_photo_filter=VisibilityChoice.ANYONE)
        self.member.refresh_from_db()

        self.trip = baker.make(Trip, creator=self.owner)
        baker.make(TripActivity, trip=self.trip, pin=self.owner_far_pin)
        baker.make(TripMembership, trip=self.trip, profile=self.member)

        self.photo = self._pin_photo(self.owner_far_pin, "trip-activity")

    def _pin_photo(self, pin: Pin, name: str) -> Image:
        """A photo on the owner's pin, contributed to no wiki."""
        result = upload_photo_for_owner(pin, self.owner, SimpleUploadedFile(f"{name}.jpg", _jpeg_bytes(), content_type="image/jpeg"), name)
        assert isinstance(result, Image), f"fixture upload was rejected: {result}"
        return result

    def test_a_trip_member_cannot_see_a_photo_on_an_activity_pin(self) -> None:
        """Even with both visibility settings wide open.

        The member's ``viewer_photo_filter`` is ANYONE and the uploader's
        ``photo_upload_visibility`` is left at its permissive default, so
        membership is the only thing that could admit them - and it must not.
        """
        self.assertFalse(self._visible(self.photo, self.member), "a trip member reached the whole gallery of a pin on the itinerary")

    def test_somebody_not_on_the_trip_cannot_either(self) -> None:
        """The viewer pins the same near place, so only trip membership differs."""
        self.assertFalse(self._visible(self.photo, self.viewer), "a photo on a trip activity leaked to somebody not on the trip")

    def test_the_owner_still_sees_their_own_photo(self) -> None:
        """Tightening the gate must not hide a pin's photos from its uploader."""
        self.assertTrue(self._visible(self.photo, self.owner))

    def test_a_later_upload_does_not_join_the_trip_either(self) -> None:
        """The live half of the old grant: photos added after the fact.

        A photo uploaded long after the pin went on the itinerary was swept in
        by the same query, so the exposure kept growing with no further act by
        anyone.
        """
        later = self._pin_photo(self.owner_far_pin, "uploaded-later")

        self.assertFalse(self._visible(later, self.member))

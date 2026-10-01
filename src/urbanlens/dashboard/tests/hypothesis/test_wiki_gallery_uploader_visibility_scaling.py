"""The wiki photo map re-answers "may I see this uploader" once per photo.

N21 H28's second half. `bound_map_layer` already caps how many photos the layer
plots, but every one of the survivors still calls `_visible_uploader_name`,
which calls `Profile.can_view_profile` - and at the `COMMON_PIN` visibility
setting that reaches `_have_common_pin`, which reads *both* accounts' entire
pin sets into Python. So one viewer opening a community wiki pays that twice
per photo, and a wiki collects photos from everyone who has been there.

The answer cannot vary between two photos by the same uploader for the same
viewer, so the whole cost past the first is re-deriving something already
known. Memoised per uploader for the life of one response - deliberately not
cached across requests, because a visibility setting a user just changed must
take effect on their next page load, not when a TTL expires.
"""

from __future__ import annotations

import tempfile

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.meta import VisibilityChoice
from urbanlens.dashboard.models.wiki.model import Wiki

_MEDIA_ROOT = tempfile.mkdtemp(prefix="urbanlens-test-media-")


@override_settings(MEDIA_ROOT=_MEDIA_ROOT)
class _GalleryCase(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # the first user is auto-promoted to site admin
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)

        self.location = baker.make(Location)
        self.wiki = baker.make(Wiki, location=self.location)
        baker.make(Pin, profile=self.profile, location=self.location)

        # The setting that makes the check expensive: COMMON_PIN sends
        # can_view_profile through _have_common_pin, which scans both accounts.
        self.uploader = baker.make(User).profile
        self.uploader.profile_visibility = VisibilityChoice.COMMON_PIN
        self.uploader.save(update_fields=["profile_visibility"])
        baker.make(Pin, profile=self.uploader, location=self.location)

    def _add_photos(self, count: int) -> None:
        for index in range(count):
            Image.objects.create(
                image=SimpleUploadedFile(f"photo{index}.jpg", b"fake image bytes", content_type="image/jpeg"),
                wiki=self.wiki,
                profile=self.uploader,
                latitude=40.0 + index * 0.0001,
                longitude=-74.0,
            )

    def _queries_for(self, photos: int) -> int:
        self._add_photos(photos)
        with CaptureQueriesContext(connection) as captured:
            response = self.client.get(reverse("location.wiki.gallery.json", args=[self.location.slug]))
        assert response.status_code == 200, response.status_code  # nosec B101
        assert len(response.json()["images"]) == photos, response.json()  # nosec B101
        return len(captured.captured_queries)


class TheUploaderCheckIsAnsweredOnceTests(_GalleryCase):
    """One uploader, many photos: the verdict cannot differ between them."""

    def test_more_photos_from_one_uploader_do_not_cost_more_queries(self) -> None:
        small = self._queries_for(2)
        Image.objects.all().delete()
        large = self._queries_for(12)

        self.assertLessEqual(
            large - small,
            2,
            f"ten more photos from the same uploader cost {large - small} more queries",
        )


class TheAnswerIsStillRightTests(_GalleryCase):
    """A memo that returns the wrong name is worse than the cost it saves.

    Photo visibility and identity visibility are separate gates: `visible_to`
    decides whether the row appears at all, `can_view_profile` only decides
    whether it carries a name. These fix the first and vary the second, so a
    masked name is a masked name rather than a dropped row.
    """

    def _names(self) -> set[str]:
        response = self.client.get(reverse("location.wiki.gallery.json", args=[self.location.slug]))
        assert response.status_code == 200, response.status_code  # nosec B101
        return {entry["uploader"] for entry in response.json()["images"]}

    def test_an_uploader_the_viewer_may_see_is_named(self) -> None:
        self._add_photos(2)

        self.assertEqual(self._names(), {self.uploader.username}, "a shared pin should make the uploader visible")

    def test_an_uploader_the_viewer_may_not_see_is_masked(self) -> None:
        """The anti-vacuity half: a memo that always says yes would leak this name."""
        from urbanlens.dashboard.services.profile.identity_visibility import DEFAULT_MASKED_PLACEHOLDER

        self.uploader.profile_visibility = VisibilityChoice.NO_ONE
        self.uploader.save(update_fields=["profile_visibility"])
        self._add_photos(2)

        self.assertEqual(self._names(), {DEFAULT_MASKED_PLACEHOLDER})

    def test_two_uploaders_are_answered_separately(self) -> None:
        """One memo entry per uploader, not one verdict for the whole response."""
        from urbanlens.dashboard.services.profile.identity_visibility import DEFAULT_MASKED_PLACEHOLDER

        self._add_photos(2)
        # A pin at the same location keeps this uploader's photos visible, so
        # the only thing that differs from the one above is their identity.
        hidden = baker.make(User).profile
        hidden.profile_visibility = VisibilityChoice.NO_ONE
        hidden.save(update_fields=["profile_visibility"])
        baker.make(Pin, profile=hidden, location=self.location)
        Image.objects.create(
            image=SimpleUploadedFile("other.jpg", b"fake image bytes", content_type="image/jpeg"),
            wiki=self.wiki,
            profile=hidden,
            latitude=41.0,
            longitude=-74.0,
        )

        self.assertEqual(self._names(), {self.uploader.username, DEFAULT_MASKED_PLACEHOLDER})

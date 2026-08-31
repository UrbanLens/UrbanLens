"""Two more columns reachable from a request with nothing enforcing their width.

Carried from the chunk-559 sweep, which found these but had not driven them:

- ``Image.caption`` is ``CharField(500)`` and six write paths take it straight
  from a request - a safety check-in photo, two pin media paths, wiki media,
  albums, and map overlays (which stores the submitted *name* as the caption).
  The chunk-559 scan reported only one of the six, because the others reach the
  column through a service call rather than a visible ``Image.objects.create``.

- ``SiteSettings.default_name_source_priority`` is ``CharField(500)`` built by
  comma-joining submitted slugs. Each token is filtered by
  ``re.fullmatch(r"[a-z0-9_-]+", slug)``, which constrains the *characters* and
  not the length: one long token, or enough short ones, overflows the column.

Both are ``DataError`` 500s. Captions get a 400 rather than truncation for the
same reason names do - the user wrote the words and should be told, not have
them silently clipped.
"""

from __future__ import annotations

import base64

from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.models.site_settings.model import SiteSettings
from urbanlens.dashboard.services.core.text_limits import column_max_length

#: Smallest valid PNG - the upload path needs a real image, not arbitrary bytes.
_PNG_BYTES = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")


class SiteSettingNameSourceLengthTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.admin = baker.make("auth.User", is_staff=True, is_superuser=True)
        self.client.force_login(self.admin)

    def test_one_overlong_slug_does_not_reach_the_column(self) -> None:
        """The regex constrains characters, not length."""
        before = SiteSettings.get_current().default_name_source_priority
        oversized = "a" * (column_max_length(SiteSettings, "default_name_source_priority") + 1)

        response = self.client.post(reverse("site_admin"), {"default_name_source_priority": oversized})

        # A rejected value is reported (400), not silently truncated (200/302).
        self.assertEqual(response.status_code, 400)
        self.assertEqual(SiteSettings.get_current().default_name_source_priority, before)

    def test_many_valid_slugs_do_not_reach_the_column(self) -> None:
        """Each token is short and legal; the joined result is not."""
        before = SiteSettings.get_current().default_name_source_priority
        width = column_max_length(SiteSettings, "default_name_source_priority")

        response = self.client.post(reverse("site_admin"), {"default_name_source_priority": ",".join(["osm"] * width)})

        self.assertEqual(response.status_code, 400)
        self.assertEqual(SiteSettings.get_current().default_name_source_priority, before)

    def test_a_slug_at_the_exact_length_limit_is_accepted(self) -> None:
        """The positive edge of the same boundary: exactly the column width fits."""
        exact = "a" * column_max_length(SiteSettings, "default_name_source_priority")

        response = self.client.post(reverse("site_admin"), {"default_name_source_priority": exact})

        self.assertEqual(response.status_code, 302)
        self.assertEqual(SiteSettings.get_current().default_name_source_priority, exact)


class SafetyPhotoCaptionLengthTests(TestCase):
    """The check-in gallery upload takes the file directly, so it is drivable.

    The map-overlay path stores the submitted name as a caption too, but it
    fetches a remote image first, which the test network guard refuses - a test
    against it passes without ever reaching the column, proving nothing.
    """

    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make("auth.User")
        self.profile = self.user.profile
        self.client.force_login(self.user)

    def test_an_overlong_caption_is_refused(self) -> None:
        checkin = baker.make("dashboard.SafetyCheckin", profile=self.profile, title="Hike")
        oversized = "c" * (column_max_length(Image, "caption") + 1)
        upload = SimpleUploadedFile("photo.png", _PNG_BYTES, content_type="image/png")

        response = self.client.post(reverse("safety.checkin.gallery", kwargs={"checkin_slug": checkin.slug}), {"image": upload, "caption": oversized})

        # A rejected caption is reported (400), not silently truncated (201).
        self.assertEqual(response.status_code, 400)
        self.assertFalse(Image.objects.filter(safety_checkin=checkin).exists())

    def test_a_caption_at_the_exact_length_limit_is_accepted(self) -> None:
        """The positive edge of the same boundary: exactly the column width fits."""
        checkin = baker.make("dashboard.SafetyCheckin", profile=self.profile, title="Hike")
        exact = "c" * column_max_length(Image, "caption")
        upload = SimpleUploadedFile("photo.png", _PNG_BYTES, content_type="image/png")

        response = self.client.post(reverse("safety.checkin.gallery", kwargs={"checkin_slug": checkin.slug}), {"image": upload, "caption": exact})

        self.assertEqual(response.status_code, 201)
        self.assertTrue(Image.objects.filter(safety_checkin=checkin, caption=exact).exists())

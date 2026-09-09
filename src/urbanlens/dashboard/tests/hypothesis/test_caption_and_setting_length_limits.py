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

An earlier version of this file said the map-overlay path could not be driven
because "it fetches a remote image first, which the test network guard refuses".
That is true of `_image_from_request`'s `media_url`/`image_url` branches and not
of its **direct-upload** branch, which takes `request.FILES["image"]` and
`request.POST["name"]` straight to `services.photos.photo_upload.upload_photo` -
a plain multipart POST with no network call, exactly like the safety-checkin path
above it. `MapOverlayCaptionLengthTests` drives it (P57).
"""

from __future__ import annotations

import base64

from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.models.map_overlay.model import MapImageOverlay
from urbanlens.dashboard.models.site_settings.model import SiteSettings
from urbanlens.dashboard.services.core.text_limits import column_max_length

#: Smallest valid PNG - the upload path needs a real image, not arbitrary bytes.
_PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


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
    """The check-in gallery upload takes the file directly, so it is drivable."""

    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make("auth.User")
        self.profile = self.user.profile
        self.client.force_login(self.user)

    def test_an_overlong_caption_is_refused(self) -> None:
        checkin = baker.make("dashboard.SafetyCheckin", profile=self.profile, title="Hike")
        oversized = "c" * (column_max_length(Image, "caption") + 1)
        upload = SimpleUploadedFile("photo.png", _PNG_BYTES, content_type="image/png")

        response = self.client.post(
            reverse("safety.checkin.gallery", kwargs={"checkin_slug": checkin.slug}),
            {"image": upload, "caption": oversized},
        )

        # A rejected caption is reported (400), not silently truncated (201).
        self.assertEqual(response.status_code, 400)
        self.assertFalse(Image.objects.filter(safety_checkin=checkin).exists())

    def test_a_caption_at_the_exact_length_limit_is_accepted(self) -> None:
        """The positive edge of the same boundary: exactly the column width fits."""
        checkin = baker.make("dashboard.SafetyCheckin", profile=self.profile, title="Hike")
        exact = "c" * column_max_length(Image, "caption")
        upload = SimpleUploadedFile("photo.png", _PNG_BYTES, content_type="image/png")

        response = self.client.post(
            reverse("safety.checkin.gallery", kwargs={"checkin_slug": checkin.slug}),
            {"image": upload, "caption": exact},
        )

        self.assertEqual(response.status_code, 201)
        self.assertTrue(Image.objects.filter(safety_checkin=checkin, caption=exact).exists())


class MapOverlayCaptionLengthTests(TestCase):
    """The overlay's submitted *name* becomes the Image caption, and is bounded.

    `_image_from_request`'s upload branch hands `request.POST["name"]` to
    `upload_photo` as the caption, and that service checks the column width
    (`photo_upload.py:142`) before anything reaches the database. No remote fetch
    is involved, so the network guard is irrelevant here - see this module's
    docstring for the claim that said otherwise.
    """

    def setUp(self) -> None:
        super().setUp()
        baker.make("auth.User")  # absorbs the bootstrap site-admin promotion
        self.user = baker.make("auth.User")
        self.profile = self.user.profile
        self.pin = baker.make("dashboard.Pin", profile=self.profile)
        self.client.force_login(self.user)
        self.url = reverse("pin.overlays", kwargs={"pin_slug": self.pin.slug})

    def _post(self, name: str, *, as_json: bool = False):
        upload = SimpleUploadedFile("overlay.png", _PNG_BYTES, content_type="image/png")
        headers = {"HTTP_ACCEPT": "application/json"} if as_json else {}
        return self.client.post(self.url, {"name": name, "image": upload}, **headers)

    def test_an_overlong_overlay_name_is_refused(self) -> None:
        """Refused as a rendered error, not a status code.

        `fail()` answers 400 only for the JSON caller (the lightbox's "use as
        floorplan overlay"); the HTMX dialog gets 200 with the message swapped
        into the list partial. So the assertion carrying the meaning is that
        nothing was stored - asserting only on the status would have called this
        broken when it is not, which is what a first draft of this test did.
        """
        oversized = "o" * (column_max_length(Image, "caption") + 1)

        response = self._post(oversized)

        self.assertEqual(response.status_code, 200)
        self.assertFalse(Image.objects.filter(caption=oversized).exists(), "the over-width caption must not be stored")
        self.assertFalse(MapImageOverlay.objects.filter(parent_pin=self.pin).exists(), "and no overlay may be created")

    def test_the_json_caller_gets_a_400_for_the_same_input(self) -> None:
        """The other half of `fail()`, and what makes the 200 above a deliberate shape."""
        oversized = "o" * (column_max_length(Image, "caption") + 1)

        response = self._post(oversized, as_json=True)

        self.assertEqual(response.status_code, 400)
        self.assertFalse(Image.objects.filter(caption=oversized).exists())

    def test_a_name_at_the_exact_length_limit_is_accepted(self) -> None:
        """The positive edge, and the anti-vacuity control.

        Without it the 400 above could equally come from the route rejecting
        every upload - which is what a "drivable" claim has to rule out.
        """
        exact = "o" * column_max_length(Image, "caption")

        response = self._post(exact)

        self.assertNotEqual(
            response.status_code, 400, f"an exactly-fitting caption must be accepted: {response.status_code}"
        )
        self.assertTrue(Image.objects.filter(caption=exact).exists(), "and must actually be stored")
        self.assertTrue(MapImageOverlay.objects.filter(parent_pin=self.pin).exists(), "and the overlay created")

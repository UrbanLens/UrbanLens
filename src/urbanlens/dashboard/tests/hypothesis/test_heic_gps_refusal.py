"""A photo whose location cannot be removed is refused, not silently kept.

Filed 2026-08-12: `heic`/`heif` are accepted formats, but Pillow cannot open
them without `pillow-heif`, which is not installed. So for a user who had asked
the app not to keep photo locations, the strip raised, the failure was logged,
and the file was stored with its full GPS IFD intact - the app promised a scrub
and kept the coordinates.

The filing offered two routes and called both an owner's decision: add the
dependency (a bundled libheif, with licensing to review), or refuse. This takes
the second, narrowed so it costs nothing to anyone it does not protect: only
profiles that actually asked for stripping are refused, and the message says
what to do about it. If `pillow-heif` is ever adopted, `_UNSCRUBBABLE_EXTENSIONS`
goes empty and the refusal disappears on its own.

The invariant either way: a format the app *accepts* must be scrubbable or
refused, never silently stored with the coordinates the uploader asked to have
removed.
"""

from __future__ import annotations

from unittest import mock

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.services.photos.uploads import unscrubbable_format_error

_ALLOWED = "urbanlens.dashboard.services.visits.visits.visit_logging_allowed"


class UnscrubbableFormatTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.profile = baker.make(User).profile

    def _error(self, filename: str, *, logging_allowed: bool) -> str | None:
        upload = SimpleUploadedFile(filename, b"\x00\x01", content_type="image/heic")
        with mock.patch(_ALLOWED, return_value=logging_allowed):
            return unscrubbable_format_error(upload, self.profile)

    def test_a_heic_is_refused_when_the_user_asked_for_a_strip(self) -> None:
        error = self._error("IMG_0001.heic", logging_allowed=False)

        self.assertIsNotNone(error)
        self.assertIn("cannot have its location removed", error)

    def test_the_message_says_what_to_do(self) -> None:
        """A refusal a user cannot act on is just a failure."""
        error = self._error("IMG_0001.heic", logging_allowed=False)

        self.assertIn("Convert it to JPEG", error)

    def test_heif_is_treated_the_same(self) -> None:
        self.assertIsNotNone(self._error("IMG_0001.heif", logging_allowed=False))

    def test_case_does_not_matter(self) -> None:
        self.assertIsNotNone(self._error("IMG_0001.HEIC", logging_allowed=False))

    def test_a_user_who_keeps_visit_logging_can_still_upload_heic(self) -> None:
        """They were never promised a scrub; refusing costs them the format for nothing."""
        self.assertIsNone(self._error("IMG_0001.heic", logging_allowed=True))
        self.assertIsNotNone(self._error("IMG_0001.heic", logging_allowed=False), "the same file must still be refused for a user who did ask")

    def test_a_scrubbable_format_is_never_refused(self) -> None:
        self.assertIsNone(self._error("IMG_0001.jpg", logging_allowed=False))
        self.assertIsNone(self._error("IMG_0001.tiff", logging_allowed=False))
        self.assertIsNone(self._error("IMG_0001.avif", logging_allowed=False))

    def test_a_file_without_an_extension_is_not_refused_here(self) -> None:
        """Extension-less uploads are the content sniffer's problem, not this check's."""
        self.assertIsNone(self._error("IMG_0001", logging_allowed=False))

"""A photo upload has to be an image in its bytes, not just in its name.

Found by the integration suite on 2026-08-24: a shell script uploaded as
`not-really.png` with `Content-Type: image/png` was stored and served back from
this app's origin as an image. Both signals the upload path trusted - the
extension and the declared content type - are supplied by the caller, so neither
is evidence of anything.

The reason no existing test caught it is worth stating, because it is the theme
running through `docs/audits/TEST_COVERAGE_GAPS.md`: every upload test uploads a real
image. The adversarial case was never tried, because a test written alongside a
feature is written by somebody thinking about the feature working.

`content_type_mismatch_error` only fires on a *confirmed* mismatch. Bytes
`filetype` cannot place at all return None from it - deliberately, since not
every legitimate document format has a magic-byte signature - and a shell script
is unrecognisable rather than mismatched. Photos now require a positive
identification instead.

**The half of this worth reading before changing anything:** failing closed is
only safe because every extension in the photo allowlist has a signature
`filetype` knows. Two did not agree by *name* - the library reports a TIFF as
`tif` and an animated PNG as `apng`, and neither string was in the image set -
so making photos fail closed without noticing that would have started rejecting
genuine TIFF and APNG uploads. The alias tests below exist to keep that from
being reintroduced.
"""

from __future__ import annotations

import io

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.images.model import MediaKind
from urbanlens.dashboard.services.media.images import image_upload_error
from urbanlens.dashboard.services.security.content_sniffing import (
    _IMAGE_EXTENSIONS,
    _SNIFFED_IMAGE_EXTENSIONS,
    photo_is_not_an_image_error,
    sniff_media_kind,
)

#: A real 1x1 PNG.
PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c6360000002000100ffff03000006000557bfabd40000000049454e44ae426082"
)

#: A real 1x1 GIF.
GIF = bytes.fromhex("47494638396101000100800000000000ffffff21f90401000000002c00000000010001000002024401003b")

#: A minimal little-endian TIFF header. `filetype` reports this as `tif`, which
#: is the alias the photo allowlist did not have.
TIFF = b"II\x2a\x00" + b"\x08\x00\x00\x00" + b"\x00" * 32


class PhotoBytesTests(SimpleTestCase):
    """The check itself, without a database or an upload."""

    def test_a_real_png_is_accepted(self) -> None:
        """The check must not be satisfiable by rejecting everything."""
        self.assertIsNone(photo_is_not_an_image_error(io.BytesIO(PNG)))

    def test_a_real_gif_is_accepted(self) -> None:
        self.assertIsNone(photo_is_not_an_image_error(io.BytesIO(GIF)))

    def test_a_shell_script_is_refused(self) -> None:
        script = io.BytesIO(b"#!/bin/sh\necho this is not a png\n")

        self.assertIsNotNone(photo_is_not_an_image_error(script))

    def test_an_empty_file_is_refused(self) -> None:
        self.assertIsNotNone(photo_is_not_an_image_error(io.BytesIO(b"")))

    def test_html_is_refused(self) -> None:
        """The case that would be served from this origin and rendered."""
        self.assertIsNotNone(photo_is_not_an_image_error(io.BytesIO(b"<html><script>alert(1)</script></html>")))


class SniffAliasTests(SimpleTestCase):
    """`filetype`'s names for our formats have to be names we recognise.

    This is the regression guard for the trap described in the module docstring:
    a format we allow by extension but whose sniffed name we do not recognise
    reads as "not an image" and gets rejected, and it looks like a broken
    upload rather than a naming mismatch.
    """

    def test_a_tiff_sniffs_as_a_photo(self) -> None:
        self.assertEqual(sniff_media_kind(io.BytesIO(TIFF)), MediaKind.PHOTO)

    def test_every_allowed_image_extension_is_recognised_by_name(self) -> None:
        """Each allowlisted extension must be one the sniffer maps to PHOTO.

        `jpeg`/`heif` are the user-facing spellings of formats `filetype` calls
        `jpg`/`heic`; both spellings are in the sniffed set, so either answer
        maps correctly. Anything added to the allowlist in future that the
        library names differently fails here rather than in production.
        """
        unmapped = sorted(extension for extension in _IMAGE_EXTENSIONS if extension not in _SNIFFED_IMAGE_EXTENSIONS)

        self.assertFalse(
            unmapped,
            f"these allowed photo extensions are not in the sniffed-name set, so a genuine upload of one would be rejected: {unmapped}",
        )


class PhotoUploadPipelineTests(TestCase):
    """End to end through the shared admission pipeline both uploaders use.

    Needs a database, unlike the checks above: the pipeline's first step reads
    the site-wide maximum upload size out of `SiteSettings`.
    """

    def test_a_script_named_png_is_refused_with_a_400(self) -> None:
        upload = SimpleUploadedFile("not-really.png", b"#!/bin/sh\necho nope\n", content_type="image/png")

        result = image_upload_error(upload, MediaKind.PHOTO, skip_malware_scan=True)

        self.assertIsNotNone(result, "a shell script named .png passed the upload pipeline")
        assert result is not None
        _message, status = result
        self.assertEqual(status, 400)

    def test_a_real_png_passes_the_pipeline(self) -> None:
        upload = SimpleUploadedFile("holiday.png", PNG, content_type="image/png")

        self.assertIsNone(image_upload_error(upload, MediaKind.PHOTO, skip_malware_scan=True))

    def test_a_real_tiff_passes_the_pipeline(self) -> None:
        """The regression the alias fix exists to prevent."""
        upload = SimpleUploadedFile("scan.tif", TIFF, content_type="image/tiff")

        self.assertIsNone(image_upload_error(upload, MediaKind.PHOTO, skip_malware_scan=True))

    def test_a_document_with_unrecognisable_bytes_is_still_allowed(self) -> None:
        """Documents keep failing open, which is why this is scoped to photos.

        Not every legitimate document format has a magic-byte signature, so the
        stricter rule must not leak across into them.
        """
        upload = SimpleUploadedFile("notes.pdf", b"not really a pdf either", content_type="application/pdf")

        self.assertIsNone(image_upload_error(upload, MediaKind.DOCUMENT, skip_malware_scan=True))

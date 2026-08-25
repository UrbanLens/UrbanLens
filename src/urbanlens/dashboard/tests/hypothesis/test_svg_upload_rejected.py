"""A photo upload must not be able to store an active document type.

`image_upload_error`'s magic-byte check deliberately fails *open* for formats
`filetype` cannot fingerprint - correct for documents, but **SVG has no
magic-byte signature at all**. A scripted `.svg` therefore passed sniffing,
passed antivirus (script in markup matches no virus signature), and was stored.

That mattered because the stored *extension* decides the Content-Type the file
is later served with: nginx's mime.types maps `.svg` to `image/svg+xml`, this
app sets no Content-Security-Policy, and `X-Content-Type-Options: nosniff` is no
help because nothing is being sniffed - the file genuinely is an SVG. Navigating
to it executes its script with the app's own origin. Avatars are the worst case:
`media.MediaGateView` serves `avatars/` to any signed-in user, by design, because
they render site-wide.

Photos are now allowlisted by extension. Documents are deliberately *not* - they
legitimately arrive as `.docx` and friends and are converted after upload - so
these tests pin that asymmetry too.
"""

from __future__ import annotations

from pathlib import Path
import shutil
import tempfile

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from model_bakery import baker

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.models.images.model import MediaKind
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.services.media.images import image_upload_error
from urbanlens.dashboard.services.security.content_sniffing import unsupported_image_extension_error

_SCRIPTED_SVG = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(document.domain)</script></svg>'

#: A real 1x1 PNG, so the accept-path tests are not passing on a rejected fixture.
_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
    b"\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)


class ImageExtensionAllowlistTests(SimpleTestCase):
    def test_svg_is_refused(self) -> None:
        self.assertIsNotNone(unsupported_image_extension_error("avatar.svg"))

    def test_other_active_types_are_refused(self) -> None:
        for name in ("payload.html", "payload.htm", "payload.xml", "payload.js", "payload.php"):
            self.assertIsNotNone(unsupported_image_extension_error(name), name)

    def test_a_file_with_no_extension_is_refused(self) -> None:
        self.assertIsNotNone(unsupported_image_extension_error("screenshot"))

    def test_ordinary_image_extensions_pass(self) -> None:
        for name in ("a.jpg", "a.JPEG", "a.png", "a.gif", "a.webp", "a.heic", "a.bmp", "a.tiff", "a.avif"):
            self.assertIsNone(unsupported_image_extension_error(name), name)


class PhotoUploadGauntletTests(TestCase):
    # TestCase, not SimpleTestCase: image_upload_error reads the site-wide size
    # cap from SiteSettings, so the gauntlet touches the database.
    def test_scripted_svg_never_reaches_storage(self) -> None:
        upload = SimpleUploadedFile("avatar.svg", _SCRIPTED_SVG, content_type="image/svg+xml")

        error = image_upload_error(upload, MediaKind.PHOTO, skip_malware_scan=True)

        self.assertIsNotNone(error, "a scripted SVG must not pass the photo upload checks")
        self.assertEqual(error[1], 400)

    def test_renaming_the_svg_to_jpg_is_now_refused_outright(self) -> None:
        """Was "accepted but harmless"; it is refused now, which is strictly better.

        The extension allowlist's reasoning has not changed and is still why it
        exists: the *stored extension* determines the served Content-Type, so
        SVG bytes under a `.jpg` name were already inert - the browser is told
        image/jpeg and `nosniff` keeps it that way.

        What changed is the layer underneath. Photo uploads now require the bytes
        to positively identify as an image, added after the integration suite
        found a shell script being stored as `not-really.png`. SVG has no
        magic-byte signature, so it fails that check too - accidentally, but
        correctly: a file whose bytes are not an image has no business in the
        photo library whatever it is named.

        Kept as a test of the *outcome* rather than deleted, because "inert" and
        "refused" are different guarantees and it is worth recording which one is
        in force.
        """
        upload = SimpleUploadedFile("avatar.jpg", _SCRIPTED_SVG, content_type="image/jpeg")

        error = image_upload_error(upload, MediaKind.PHOTO, skip_malware_scan=True)

        self.assertIsNotNone(error, "SVG bytes under a .jpg name were accepted into the photo library")
        assert error is not None
        self.assertEqual(error[1], 400)

    def test_a_real_png_still_uploads(self) -> None:
        """The guard must not break ordinary uploads."""
        upload = SimpleUploadedFile("photo.png", _PNG, content_type="image/png")

        self.assertIsNone(image_upload_error(upload, MediaKind.PHOTO, skip_malware_scan=True))

    def test_documents_are_not_extension_restricted(self) -> None:
        """`.docx` has no entry in the document extension set and is converted after upload."""
        upload = SimpleUploadedFile("report.docx", b"PK\x03\x04 not really a docx", content_type="application/octet-stream")

        self.assertIsNone(image_upload_error(upload, MediaKind.DOCUMENT, skip_malware_scan=True))


class AvatarSvgTests(TestCase):
    """End to end through the avatar path, which serves `avatars/` site-wide."""

    def test_avatar_upload_refuses_a_scripted_svg(self) -> None:
        from urbanlens.dashboard.services.profile.avatar import AvatarUploadError, set_profile_avatar

        root = tempfile.mkdtemp(prefix="ul_svg_avatar_")
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        (Path(root) / "avatars").mkdir(parents=True, exist_ok=True)
        profile = Profile.objects.get(user=baker.make("auth.User"))

        with override_settings(MEDIA_ROOT=root), self.assertRaises(AvatarUploadError):
            set_profile_avatar(profile, SimpleUploadedFile("avatar.svg", _SCRIPTED_SVG, content_type="image/svg+xml"))

        profile.refresh_from_db()
        self.assertFalse(profile.avatar, "nothing may be stored when the check fails")

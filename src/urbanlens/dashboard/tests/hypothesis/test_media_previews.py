"""Tests for server-side previews of non-web-renderable Media-gallery items.

Covers the format decisions (:mod:`services.media.previews`), the actual
raster conversion for TIFF/PDF/HEIC-shaped sources, and the signed generic
endpoint's refusal to fetch anything this server didn't itself emit.

No real network access occurs - the endpoint's own fetch is patched.
"""

from __future__ import annotations

from io import BytesIO
from unittest.mock import patch

from django.urls import reverse
from hypothesis import HealthCheck, given, settings, strategies as st

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.services.media.previews import (
    gallery_thumb_url,
    is_web_safe,
    needs_server_side_preview,
    preview_thumb_url,
    render_preview,
    sign_source_url,
)


def _image_bytes(fmt: str, *, size: tuple[int, int] = (40, 30), mode: str = "RGB") -> bytes:
    from PIL import Image as PILImage

    buffer = BytesIO()
    PILImage.new(mode, size, "red").save(buffer, format=fmt)
    return buffer.getvalue()


class FormatDecisionTests(SimpleTestCase):
    def test_a_declared_content_type_wins_over_the_extension(self) -> None:
        """API-generated URLs routinely carry a misleading or absent extension."""
        self.assertTrue(is_web_safe("/proxy/attachment/7/", "image/jpeg"))
        self.assertFalse(is_web_safe("/photo.jpg", "application/pdf"))

    def test_charset_parameters_are_ignored(self) -> None:
        self.assertTrue(is_web_safe("/x", "image/png; charset=binary"))

    def test_known_bad_formats_need_a_preview(self) -> None:
        for url, content_type in (("/x.tif", ""), ("/x.pdf", ""), ("/x", "image/tiff"), ("/x", "application/pdf"), ("/x.heic", "")):
            with self.subTest(url=url, content_type=content_type):
                self.assertTrue(needs_server_side_preview(url, content_type))

    def test_web_safe_formats_need_no_preview(self) -> None:
        for url in ("/x.jpg", "/x.png", "/x.webp", "/x.gif", "/x.avif"):
            with self.subTest(url=url):
                self.assertFalse(needs_server_side_preview(url))

    def test_unconvertible_formats_are_left_alone(self) -> None:
        """A preview attempt guaranteed to fail is worse than the icon tile."""
        for url in ("/x.zip", "/x.docx", "/x.txt"):
            with self.subTest(url=url):
                self.assertFalse(needs_server_side_preview(url))

    def test_an_empty_url_needs_nothing(self) -> None:
        self.assertFalse(needs_server_side_preview("", "application/pdf"))


class PreviewUrlTests(TestCase):
    def test_an_in_app_proxy_url_gets_the_preview_flag(self) -> None:
        """It already holds the bytes - a signed round trip would re-download them."""
        self.assertEqual(preview_thumb_url("/dashboard/cris/attachment/r1/2/", "application/pdf"), "/dashboard/cris/attachment/r1/2/?preview=1")

    def test_an_existing_query_string_is_preserved(self) -> None:
        self.assertEqual(preview_thumb_url("/x/?a=b", "image/tiff"), "/x/?a=b&preview=1")

    def test_a_remote_url_goes_through_the_signed_endpoint(self) -> None:
        url = preview_thumb_url("https://upload.wikimedia.org/scan.tif")
        self.assertTrue(url.startswith(reverse("media.preview")))
        self.assertIn("sig=", url)

    def test_a_non_http_url_is_refused(self) -> None:
        self.assertEqual(preview_thumb_url("data:image/tiff;base64,AAAA"), "")


class GalleryThumbTests(TestCase):
    def test_a_web_safe_thumbnail_is_used_as_is(self) -> None:
        self.assertEqual(gallery_thumb_url("https://x/full.tif", "https://x/thumb.jpg"), "https://x/thumb.jpg")

    def test_a_tiff_thumbnail_is_converted(self) -> None:
        """Several archives serve the original file as the "thumbnail"."""
        thumb = gallery_thumb_url("https://x/full.tif", "https://x/thumb.tif")
        self.assertTrue(thumb.startswith(reverse("media.preview")))

    def test_a_document_with_no_thumbnail_previews_the_document_itself(self) -> None:
        """A scanned inventory form is a photograph of the building - it should
        be a picture in the gallery, not an anonymous grey icon."""
        thumb = gallery_thumb_url("/dashboard/cris/attachment/r1/2/", "", "application/pdf")
        self.assertEqual(thumb, "/dashboard/cris/attachment/r1/2/?preview=1")

    def test_an_unpreviewable_item_with_no_thumbnail_yields_nothing(self) -> None:
        self.assertEqual(gallery_thumb_url("https://x/record.txt", ""), "")

    def test_an_extensionless_thumbnail_is_still_attempted(self) -> None:
        """Providers do serve extension-less URLs that are plain JPEG."""
        self.assertEqual(gallery_thumb_url("https://x/full", "https://x/thumb"), "https://x/thumb")


class RenderPreviewTests(SimpleTestCase):
    def test_a_tiff_becomes_a_jpeg(self) -> None:
        result = render_preview(_image_bytes("TIFF"), "image/tiff")
        assert result is not None
        content, content_type = result
        self.assertEqual(content_type, "image/jpeg")
        self.assertEqual(content[:2], b"\xff\xd8")

    def test_transparency_is_preserved_as_png(self) -> None:
        result = render_preview(_image_bytes("PNG", mode="RGBA"), "image/png")
        assert result is not None
        self.assertEqual(result[1], "image/png")

    def test_magic_bytes_beat_a_wrong_declared_type(self) -> None:
        """CRIS and several archives mislabel scanned files."""
        result = render_preview(_image_bytes("TIFF"), "application/octet-stream")
        assert result is not None
        self.assertEqual(result[1], "image/jpeg")

    def test_oversized_sources_are_scaled_down(self) -> None:
        from PIL import Image as PILImage

        result = render_preview(_image_bytes("TIFF", size=(3000, 2000)), "image/tiff", max_dimension=200)
        assert result is not None
        self.assertEqual(max(PILImage.open(BytesIO(result[0])).size), 200)

    def test_undecodable_bytes_yield_none(self) -> None:
        self.assertIsNone(render_preview(b"not an image at all", "image/tiff"))

    def test_empty_bytes_yield_none(self) -> None:
        self.assertIsNone(render_preview(b"", "image/tiff"))

    @settings(max_examples=25, suppress_health_check=[HealthCheck.too_slow], deadline=None)
    @given(
        width=st.integers(min_value=1, max_value=300),
        height=st.integers(min_value=1, max_value=300),
        fmt=st.sampled_from(["TIFF", "BMP", "PNG", "JPEG"]),
    )
    def test_any_decodable_raster_produces_a_web_safe_image(self, width: int, height: int, fmt: str) -> None:
        result = render_preview(_image_bytes(fmt, size=(width, height)), "")
        assert result is not None
        self.assertIn(result[1], ("image/jpeg", "image/png"))


class MediaPreviewViewTests(TestCase):
    """The endpoint fetches a client-supplied URL, so the signature is what
    stops it being an open image-fetching relay."""

    def setUp(self) -> None:
        super().setUp()
        self.url = reverse("media.preview")
        # Rendered previews are cached by source URL, and the cache outlives an
        # individual test - so each test gets its own URL rather than
        # inheriting whatever a previously-run one left cached for a shared
        # one.
        self.source = f"https://upload.wikimedia.org/{self.id().rsplit('.', 1)[-1]}.tif"

    def test_an_unsigned_request_is_refused_without_fetching(self) -> None:
        with patch("urbanlens.dashboard.controllers.media_preview._fetch_source") as mock_fetch:
            response = self.client.get(self.url, {"u": self.source})
        self.assertEqual(response.status_code, 404)
        mock_fetch.assert_not_called()

    def test_a_forged_signature_is_refused_without_fetching(self) -> None:
        with patch("urbanlens.dashboard.controllers.media_preview._fetch_source") as mock_fetch:
            response = self.client.get(self.url, {"u": self.source, "sig": "nope"})
        self.assertEqual(response.status_code, 404)
        mock_fetch.assert_not_called()

    def test_a_signature_does_not_transfer_to_another_url(self) -> None:
        with patch("urbanlens.dashboard.controllers.media_preview._fetch_source") as mock_fetch:
            response = self.client.get(self.url, {"u": "https://evil.test/internal", "sig": sign_source_url(self.source)})
        self.assertEqual(response.status_code, 404)
        mock_fetch.assert_not_called()

    def test_a_signed_request_serves_a_converted_image(self) -> None:
        with patch("urbanlens.dashboard.controllers.media_preview._fetch_source", return_value=(_image_bytes("TIFF"), "image/tiff")):
            response = self.client.get(self.url, {"u": self.source, "sig": sign_source_url(self.source)})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "image/jpeg")

    def test_an_unreachable_source_is_a_404(self) -> None:
        with patch("urbanlens.dashboard.controllers.media_preview._fetch_source", return_value=None):
            response = self.client.get(self.url, {"u": self.source, "sig": sign_source_url(self.source)})
        self.assertEqual(response.status_code, 404)

    def test_an_unconvertible_source_is_not_refetched(self) -> None:
        signed = {"u": self.source, "sig": sign_source_url(self.source)}
        with patch("urbanlens.dashboard.controllers.media_preview._fetch_source", return_value=(b"junk", "image/tiff")) as mock_fetch:
            self.assertEqual(self.client.get(self.url, signed).status_code, 404)
            self.assertEqual(self.client.get(self.url, signed).status_code, 404)
        self.assertEqual(mock_fetch.call_count, 1, "a failed conversion must be cached, not retried per tile render")

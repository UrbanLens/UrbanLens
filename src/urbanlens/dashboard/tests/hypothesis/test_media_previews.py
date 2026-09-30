"""Tests for server-side previews of non-web-renderable Media-gallery items."""

from __future__ import annotations

from io import BytesIO

from hypothesis import HealthCheck, given, settings, strategies as st
from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.services.media.previews import (
    is_web_safe,
    needs_server_side_preview,
    render_preview,
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
        for url, content_type in (
            ("/x.tif", ""),
            ("/x.pdf", ""),
            ("/x", "image/tiff"),
            ("/x", "application/pdf"),
            ("/x.heic", ""),
        ):
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

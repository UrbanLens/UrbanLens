"""`_resize_custom_icon` promises to return the original when the file is unreadable.

Its docstring says "Resized file, or the original if already small enough **or
unreadable**", and it catches `(OSError, ValueError)` to deliver that. But
`Image.open()` raises `DecompressionBombError` on the *header* - before any
decode - and that inherits from `Exception`, not `OSError`, so an oversized icon
escaped the handler and 500'd the request instead of falling back to the
original.

This is the same exception-hierarchy gap found in the photo pipeline; this one
sits on a synchronous request path, so the user saw the failure directly.
"""

from __future__ import annotations

import io
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from PIL import Image as PILImage

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.controllers.labels import _ICON_MAX_PX, _resize_custom_icon


def _png(size: tuple[int, int]) -> SimpleUploadedFile:
    buffer = io.BytesIO()
    PILImage.new("RGB", size, (40, 90, 140)).save(buffer, format="PNG")
    return SimpleUploadedFile("icon.png", buffer.getvalue(), content_type="image/png")


class IconResizeBombTests(SimpleTestCase):
    def test_the_fixture_really_trips_pillows_guard(self) -> None:
        """Otherwise the fallback test below could pass with nothing raised."""
        upload = _png((_ICON_MAX_PX * 4, _ICON_MAX_PX * 4))

        with patch.object(PILImage, "MAX_IMAGE_PIXELS", 16), self.assertRaises(PILImage.DecompressionBombError):
            PILImage.open(upload)

    def test_an_oversized_icon_falls_back_instead_of_raising(self) -> None:
        upload = _png((_ICON_MAX_PX * 4, _ICON_MAX_PX * 4))

        with patch.object(PILImage, "MAX_IMAGE_PIXELS", 16):
            result = _resize_custom_icon(upload)

        self.assertIs(result, upload, "the documented contract is to return the original")

    def test_an_ordinary_large_icon_is_still_resized(self) -> None:
        """The guard must not short-circuit the normal resize."""
        upload = _png((_ICON_MAX_PX * 3, _ICON_MAX_PX * 3))

        result = _resize_custom_icon(upload)

        result.seek(0)
        self.assertLessEqual(max(PILImage.open(result).size), _ICON_MAX_PX)

    def test_a_small_icon_is_returned_untouched(self) -> None:
        upload = _png((16, 16))

        self.assertIs(_resize_custom_icon(upload), upload)

"""Tests for the LoopNet photo / CRIS attachment download proxy views.

Both stream a REData media file's bytes server-side so REData's API key
never reaches the browser (same reasoning as the Immich thumbnail proxy).
Unlike that one, neither requires login: this data is public (LoopNet
marketing photos, CRIS government historic-preservation records), and
services.media.media_materialize.materialize_media_item re-downloads this same URL
server-side with no session of its own - a login requirement would break it.

django.core.cache.cache is mocked directly rather than exercised for real,
so these tests don't depend on (or get blocked by) the test environment's
cache backend.
"""

from __future__ import annotations

from unittest.mock import patch

from django.test import Client
from django.urls import reverse

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.services.apis.property_records.redata_gateway import PropertyRecordsUnavailableError, RedataGateway


class PinLoopnetPhotoViewTests(SimpleTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.client = Client()

    def test_anonymous_request_succeeds(self) -> None:
        """No login required - materialize_media_item's own server-side fetch has no session."""
        with (
            patch("urbanlens.dashboard.controllers.pin.cache.get", return_value=None),
            patch("urbanlens.dashboard.controllers.pin.cache.set"),
            patch.object(RedataGateway, "__post_init__", lambda _self: None),
            patch.object(RedataGateway, "download_listing_photo", return_value=(b"jpeg-bytes", "image/jpeg")),
        ):
            response = self.client.get(reverse("pin.loopnet.photo", args=["listing-1", 1]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"jpeg-bytes")
        self.assertEqual(response["Content-Type"], "image/jpeg")

    def test_cached_response_skips_the_gateway_call(self) -> None:
        with (
            patch("urbanlens.dashboard.controllers.pin.cache.get", return_value=(b"cached-bytes", "image/jpeg")),
            patch.object(RedataGateway, "download_listing_photo") as mock_download,
        ):
            response = self.client.get(reverse("pin.loopnet.photo", args=["listing-1", 1]))
        mock_download.assert_not_called()
        self.assertEqual(response.content, b"cached-bytes")

    def test_unavailable_photo_returns_404(self) -> None:
        with (
            patch("urbanlens.dashboard.controllers.pin.cache.get", return_value=None),
            patch.object(RedataGateway, "download_listing_photo", side_effect=PropertyRecordsUnavailableError("photo_unavailable", "gone")),
        ):
            response = self.client.get(reverse("pin.loopnet.photo", args=["listing-1", 1]))
        self.assertEqual(response.status_code, 404)

    def test_unconfigured_gateway_returns_404_not_500(self) -> None:
        """RedataGateway() raises ValueError (not PropertyRecordsUnavailableError) when unconfigured.

        The unconfigured state is forced rather than assumed: this previously
        relied on the machine running the tests having no REData credentials,
        so on a dev box that does have them the gateway constructed happily,
        went on to make a real call, and died on a DB write from a
        SimpleTestCase - a 500, which is precisely what this test exists to
        rule out.
        """
        with (
            patch("urbanlens.dashboard.controllers.pin.cache.get", return_value=None),
            patch.object(RedataGateway, "__post_init__", side_effect=ValueError("REData is not configured")),
        ):
            response = self.client.get(reverse("pin.loopnet.photo", args=["listing-1", 1]))
        self.assertEqual(response.status_code, 404)


class PinCrisAttachmentViewTests(SimpleTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.client = Client()

    def test_anonymous_request_succeeds(self) -> None:
        with (
            patch("urbanlens.dashboard.controllers.pin.cache.get", return_value=None),
            patch("urbanlens.dashboard.controllers.pin.cache.set"),
            patch.object(RedataGateway, "__post_init__", lambda _self: None),
            patch.object(RedataGateway, "download_cultural_resource_attachment", return_value=(b"pdf-bytes", "application/pdf")),
        ):
            response = self.client.get(reverse("pin.cris.attachment", args=["res-1", 5]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"pdf-bytes")
        self.assertEqual(response["Content-Type"], "application/pdf")

    def test_unavailable_attachment_returns_404(self) -> None:
        with (
            patch("urbanlens.dashboard.controllers.pin.cache.get", return_value=None),
            patch.object(RedataGateway, "download_cultural_resource_attachment", side_effect=PropertyRecordsUnavailableError("attachment_unavailable", "gone")),
        ):
            response = self.client.get(reverse("pin.cris.attachment", args=["res-1", 5]))
        self.assertEqual(response.status_code, 404)

    def test_unconfigured_gateway_returns_404_not_500(self) -> None:
        """The unconfigured state is forced, not assumed - see the matching
        Loopnet test for why relying on ambient credentials broke this."""
        with (
            patch("urbanlens.dashboard.controllers.pin.cache.get", return_value=None),
            patch.object(RedataGateway, "__post_init__", side_effect=ValueError("REData is not configured")),
        ):
            response = self.client.get(reverse("pin.cris.attachment", args=["res-1", 5]))
        self.assertEqual(response.status_code, 404)


class CrisAttachmentPreviewModeTests(SimpleTestCase):
    """``?preview=1`` means "give me something an <img> can render".

    CRIS attachments are routinely scanned PDFs and TIFFs, which no browser
    displays - the Media gallery pointed an ``<img>`` at them and got a broken
    tile (or, for documents, an anonymous grey icon) even though the file is a
    photograph of the building.
    """

    def setUp(self) -> None:
        super().setUp()
        self.client = Client()
        self.url = reverse("pin.cris.attachment", args=["res-1", 5])

    @staticmethod
    def _tiff_bytes() -> bytes:
        from io import BytesIO

        from PIL import Image as PILImage

        buffer = BytesIO()
        PILImage.new("RGB", (20, 20), "red").save(buffer, format="TIFF")
        return buffer.getvalue()

    def test_a_tiff_attachment_is_converted(self) -> None:
        # Two requests, because the decode now happens between them: the view
        # fetches and queues, tasks.render_media_preview decodes in the sandbox
        # worker, and the second request is the one that serves a preview. The
        # first 404 is what the gallery's onerror retry is for - see the same
        # pattern in test_media_previews.py.
        from urbanlens.dashboard.tasks import render_media_preview

        with (
            patch.object(RedataGateway, "__post_init__", lambda _self: None),
            patch.object(RedataGateway, "download_cultural_resource_attachment", return_value=(self._tiff_bytes(), "image/tiff")),
            patch("urbanlens.dashboard.services.core.celery.safely_enqueue_task") as enqueue,
        ):
            self.assertEqual(self.client.get(self.url, {"preview": "1"}).status_code, 404)
            _task, source_key, preview_key, ttl, failure_ttl = enqueue.call_args.args
            render_media_preview(source_key, preview_key, ttl, failure_ttl)

            response = self.client.get(self.url, {"preview": "1"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "image/jpeg")

    def test_an_already_displayable_attachment_passes_through_unconverted(self) -> None:
        """Re-encoding a JPEG would cost quality for nothing."""
        with (
            patch("urbanlens.dashboard.controllers.pin.cache.get", return_value=None),
            patch("urbanlens.dashboard.controllers.pin.cache.set"),
            patch.object(RedataGateway, "__post_init__", lambda _self: None),
            patch.object(RedataGateway, "download_cultural_resource_attachment", return_value=(b"jpeg-bytes", "image/jpeg")),
        ):
            response = self.client.get(self.url, {"preview": "1"})
        self.assertEqual(response.content, b"jpeg-bytes")
        self.assertEqual(response["Content-Type"], "image/jpeg")

    def test_an_unconvertible_attachment_returns_404(self) -> None:
        """The gallery's onerror handler then falls back to the icon tile."""
        with (
            patch("urbanlens.dashboard.controllers.pin.cache.get", return_value=None),
            patch("urbanlens.dashboard.controllers.pin.cache.set"),
            patch.object(RedataGateway, "__post_init__", lambda _self: None),
            patch.object(RedataGateway, "download_cultural_resource_attachment", return_value=(b"not a document", "application/zip")),
        ):
            response = self.client.get(self.url, {"preview": "1"})
        self.assertEqual(response.status_code, 404)

    def test_without_the_flag_the_original_bytes_are_served(self) -> None:
        with (
            patch("urbanlens.dashboard.controllers.pin.cache.get", return_value=None),
            patch("urbanlens.dashboard.controllers.pin.cache.set"),
            patch.object(RedataGateway, "__post_init__", lambda _self: None),
            patch.object(RedataGateway, "download_cultural_resource_attachment", return_value=(self._tiff_bytes(), "image/tiff")),
        ):
            response = self.client.get(self.url)
        self.assertEqual(response["Content-Type"], "image/tiff")

"""Tests for the LoopNet photo / CRIS attachment download proxy views.

Both stream a REData media file's bytes server-side so REData's API key
never reaches the browser (same reasoning as the Immich thumbnail proxy).
Unlike that one, neither requires login: this data is public (LoopNet
marketing photos, CRIS government historic-preservation records), and
services.media_materialize.materialize_media_item re-downloads this same URL
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
        """RedataGateway() raises ValueError (not PropertyRecordsUnavailableError) when unconfigured."""
        with patch("urbanlens.dashboard.controllers.pin.cache.get", return_value=None):
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
        with patch("urbanlens.dashboard.controllers.pin.cache.get", return_value=None):
            response = self.client.get(reverse("pin.cris.attachment", args=["res-1", 5]))
        self.assertEqual(response.status_code, 404)

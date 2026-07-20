"""Regression tests for the pin detail Media gallery's DRF-routed endpoints.

Both bugs covered here only reproduce through the full URL-routing +
CSRF-enforcement stack, not through calling the view function directly:

- ``set_media_sort``/``media_relevance`` used ``json.loads(request.body)``.
  ``SessionAuthentication.enforce_csrf`` reads ``request.POST`` (via DRF's
  ``Request`` wrapper) before the view runs, which consumes the underlying
  WSGI stream without caching ``request.body``, so the manual re-read raised
  ``RawPostDataException`` -> 500 (see pin.py).
- ``media/relevance/`` and ``media/send-to-wiki/`` were declared *after* the
  catch-all ``<slug:pin_slug>/media/<str:source>/`` route in urls.py, so
  Django matched the catch-all first and POST to those URLs 405'd.
"""

from __future__ import annotations

import json
from unittest import mock

from django.contrib.auth.models import User
from django.middleware.csrf import get_token
from django.test import Client, RequestFactory
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin


class CsrfEnforcedPinMediaEndpointTests(TestCase):
    """Exercises PinController's POST actions through real CSRF enforcement."""

    def setUp(self) -> None:
        self.client = Client(enforce_csrf_checks=True)
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)
        self.csrf_token = get_token(RequestFactory().get("/"))
        self.client.cookies["csrftoken"] = self.csrf_token

    def _post_json(self, url: str, body: dict):
        return self.client.post(
            url,
            data=json.dumps(body),
            content_type="application/json",
            HTTP_X_CSRFTOKEN=self.csrf_token,
        )

    def test_set_media_sort_does_not_500_under_csrf_enforcement(self) -> None:
        response = self._post_json(reverse("pin.media.sort"), {"sort": "recent"})
        self.assertEqual(response.status_code, 200)
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.media_gallery_sort, "recent")

    def test_set_map_height_does_not_500_under_csrf_enforcement(self) -> None:
        """set_map_height (see PinController) uses the same request.data pattern
        as set_media_sort - regression guard against the same RawPostDataException
        class of bug documented in this file's module docstring."""
        response = self._post_json(reverse("pin.map_height"), {"height": 700})
        self.assertEqual(response.status_code, 200)
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.pin_detail_map_height, 700)

    def test_media_relevance_route_reaches_the_post_handler(self) -> None:
        """Marking relevant now also materializes the item (downloads it as a
        real Image row - see media_materialize.py) - mock the download so
        this route/CSRF regression test doesn't depend on network access."""
        location = baker.make(Location)
        pin = baker.make(Pin, profile=self.profile, location=location)
        response_mock = mock.Mock(content=b"fake-jpeg-bytes")
        response_mock.raise_for_status = mock.Mock()
        response_mock.raw.read.return_value = b"fake-jpeg-bytes"
        with mock.patch("urbanlens.dashboard.services.media_materialize.requests.get", return_value=response_mock):
            response = self._post_json(
                reverse("pin.media.relevance", args=[pin.slug]),
                {"source": "wikipedia", "url": "https://example.com/photo.jpg", "is_relevant": True},
            )
        self.assertNotEqual(response.status_code, 405, "media/relevance/ must not be shadowed by the media/<source>/ catch-all")
        self.assertNotEqual(response.status_code, 500)
        self.assertEqual(response.status_code, 200)

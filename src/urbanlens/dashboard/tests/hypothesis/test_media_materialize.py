"""Tests for services.media_materialize - turning a transient Media gallery
item into a persisted Image row.

Covers the two things changed to support "mark relevant -> save locally"
(see docs/prompts/completed.md's "persist relevant media locally" entry):
- materialize_media_item's new `pin` parameter, and the dedup scoping that
  comes with it (a personal "save this for me" action must never reuse -
  or be reused by - another profile's already-materialized copy of the same
  external item, unlike the shared wiki-send path).
- the panel-key -> ImageSource translation for sources whose gallery key
  doesn't already match its ImageSource value (only "loc" today).
"""

from __future__ import annotations

from unittest import mock

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker
import pytest

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.images.model import Image, ImageSource
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.services.media_materialize import MaterializeError, materialize_media_item


def _ok_response(content: bytes = b"fake-jpeg-bytes") -> mock.Mock:
    response = mock.Mock()
    response.raise_for_status = mock.Mock()
    response.raw.read.return_value = content
    response.is_redirect = False
    return response


#: These tests fetch "https://example.test/..." (RFC 2606 non-resolving),
#: so the SSRF guard's hostname resolution is mocked to a fixed public IP -
#: the guard itself is covered separately by MaterializeMediaItemSsrfTests.
_FAKE_DNS_RESULT = [(2, 1, 6, "", ("93.184.216.34", 0))]


class MaterializeMediaItemTests(TestCase):
    def setUp(self) -> None:
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.location = baker.make(Location)
        self._dns_patch = mock.patch("socket.getaddrinfo", return_value=_FAKE_DNS_RESULT)
        self._dns_patch.start()
        self.addCleanup(self._dns_patch.stop)

    def test_downloads_and_creates_an_image_row(self) -> None:
        with mock.patch("urbanlens.dashboard.services.media_materialize.requests.get", return_value=_ok_response()):
            image = materialize_media_item(location=self.location, profile=self.profile, source="wikimedia", url="https://example.test/photo.jpg", caption="A photo")
        self.assertEqual(image.location_id, self.location.pk)
        self.assertEqual(image.profile_id, self.profile.pk)
        self.assertEqual(image.source, ImageSource.WIKIMEDIA)
        self.assertEqual(image.caption, "A photo")
        self.assertTrue(image.checksum)

    def test_panel_key_loc_translates_to_library_of_congress(self) -> None:
        """The "loc" panel key never matched ImageSource.LIBRARY_OF_CONGRESS's
        real value ("library_of_congress") - without the translation this
        used to silently fall back to plain ImageSource.UPLOAD."""
        with mock.patch("urbanlens.dashboard.services.media_materialize.requests.get", return_value=_ok_response()):
            image = materialize_media_item(location=self.location, profile=self.profile, source="loc", url="https://example.test/photo.jpg")
        self.assertEqual(image.source, ImageSource.LIBRARY_OF_CONGRESS)

    def test_unknown_source_falls_back_to_upload(self) -> None:
        with mock.patch("urbanlens.dashboard.services.media_materialize.requests.get", return_value=_ok_response()):
            image = materialize_media_item(location=self.location, profile=self.profile, source="not_a_real_source", url="https://example.test/photo.jpg")
        self.assertEqual(image.source, ImageSource.UPLOAD)

    def test_download_failure_raises_materialize_error(self) -> None:
        import requests

        with mock.patch("urbanlens.dashboard.services.media_materialize.requests.get", side_effect=requests.exceptions.ConnectionError("boom")), pytest.raises(MaterializeError):
            materialize_media_item(location=self.location, profile=self.profile, source="wikimedia", url="https://example.test/photo.jpg")
        self.assertFalse(Image.objects.filter(location=self.location).exists())

    def test_oversize_download_raises_materialize_error(self) -> None:
        huge = b"x" * (20 * 1024 * 1024 + 1)
        with mock.patch("urbanlens.dashboard.services.media_materialize.requests.get", return_value=_ok_response(huge)), pytest.raises(MaterializeError):
            materialize_media_item(location=self.location, profile=self.profile, source="wikimedia", url="https://example.test/photo.jpg")

    def test_without_pin_dedupes_purely_by_location_source_and_url(self) -> None:
        """Existing behavior (media_send_to_wiki) - a shared, community
        materialization dedupes regardless of who triggered it."""
        with mock.patch("urbanlens.dashboard.services.media_materialize.requests.get", return_value=_ok_response()) as mocked:
            first = materialize_media_item(location=self.location, profile=self.profile, source="wikimedia", url="https://example.test/photo.jpg")
            other_profile = baker.make(User).profile
            second = materialize_media_item(location=self.location, profile=other_profile, source="wikimedia", url="https://example.test/photo.jpg")
        self.assertEqual(first.pk, second.pk)
        mocked.assert_called_once()

    def test_with_pin_scopes_dedup_to_the_marking_profile(self) -> None:
        """The core correctness fix: a personal "mark relevant" materialization
        must never reuse another profile's copy of the same external item,
        even though it's the same (location, source, source_url)."""
        pin_a = baker.make(Pin, profile=self.profile, location=self.location)
        other_profile = baker.make(User).profile
        pin_b = baker.make(Pin, profile=other_profile, location=self.location)

        with mock.patch("urbanlens.dashboard.services.media_materialize.requests.get", return_value=_ok_response()) as mocked:
            image_a = materialize_media_item(location=self.location, profile=self.profile, source="wikimedia", url="https://example.test/photo.jpg", pin=pin_a)
            image_b = materialize_media_item(location=self.location, profile=other_profile, source="wikimedia", url="https://example.test/photo.jpg", pin=pin_b)

        self.assertNotEqual(image_a.pk, image_b.pk)
        self.assertEqual(image_a.profile_id, self.profile.pk)
        self.assertEqual(image_a.pin_id, pin_a.pk)
        self.assertEqual(image_b.profile_id, other_profile.pk)
        self.assertEqual(image_b.pin_id, pin_b.pk)
        self.assertEqual(mocked.call_count, 2)

    def test_with_pin_reuses_the_same_profiles_existing_materialization(self) -> None:
        pin = baker.make(Pin, profile=self.profile, location=self.location)
        with mock.patch("urbanlens.dashboard.services.media_materialize.requests.get", return_value=_ok_response()) as mocked:
            first = materialize_media_item(location=self.location, profile=self.profile, source="wikimedia", url="https://example.test/photo.jpg", pin=pin)
            second = materialize_media_item(location=self.location, profile=self.profile, source="wikimedia", url="https://example.test/photo.jpg", pin=pin)
        self.assertEqual(first.pk, second.pk)
        mocked.assert_called_once()


class MaterializeMediaItemSsrfTests(TestCase):
    """The `url` a caller supplies is untrusted (comes straight from a client
    request body via PinController.media_relevance/media_send_to_wiki) - it
    must never let a caller direct the server's download at an internal
    address, either directly or via a redirect.
    """

    def setUp(self) -> None:
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.location = baker.make(Location)

    def test_a_literal_private_ip_target_is_rejected(self) -> None:
        with mock.patch("urbanlens.dashboard.services.media_materialize.requests.get") as mocked, pytest.raises(MaterializeError):
            materialize_media_item(location=self.location, profile=self.profile, source="wikimedia", url="http://169.254.169.254/latest/meta-data/")
        mocked.assert_not_called()

    def test_a_hostname_that_resolves_to_a_private_ip_is_rejected(self) -> None:
        with (
            mock.patch("socket.getaddrinfo", return_value=[(2, 1, 6, "", ("127.0.0.1", 0))]),
            mock.patch("urbanlens.dashboard.services.media_materialize.requests.get") as mocked,
            pytest.raises(MaterializeError),
        ):
            materialize_media_item(location=self.location, profile=self.profile, source="wikimedia", url="https://attacker-controlled.example/photo.jpg")
        mocked.assert_not_called()

    def test_a_redirect_to_a_private_ip_is_rejected(self) -> None:
        redirect_response = mock.Mock(status_code=302, headers={"Location": "http://127.0.0.1/internal"}, is_redirect=True)
        with (
            mock.patch("socket.getaddrinfo", return_value=[(2, 1, 6, "", ("93.184.216.34", 0))]),
            mock.patch("urbanlens.dashboard.services.media_materialize.requests.get", return_value=redirect_response),
            pytest.raises(MaterializeError),
        ):
            materialize_media_item(location=self.location, profile=self.profile, source="wikimedia", url="https://example.test/photo.jpg")
        self.assertFalse(Image.objects.filter(location=self.location).exists())


class MediaRelevanceMaterializesTests(TestCase):
    """PinController.media_relevance: marking relevant materializes; other actions don't."""

    def setUp(self) -> None:
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)
        self.location = baker.make(Location)
        self.pin = baker.make(Pin, profile=self.profile, location=self.location)
        self._dns_patch = mock.patch("socket.getaddrinfo", return_value=_FAKE_DNS_RESULT)
        self._dns_patch.start()
        self.addCleanup(self._dns_patch.stop)

    def _post(self, payload: dict):
        return self.client.post(reverse("pin.media.relevance", args=[self.pin.slug]), payload, content_type="application/json")

    def test_marking_relevant_materializes_and_returns_the_local_url(self) -> None:
        with mock.patch("urbanlens.dashboard.services.media_materialize.requests.get", return_value=_ok_response()):
            response = self._post({"source": "wikimedia", "url": "https://example.test/photo.jpg", "is_relevant": True, "page_url": "https://example.test/page", "caption": "Cap"})
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("image_id", data)
        self.assertIn("image_url", data)
        image = Image.objects.get(pk=data["image_id"])
        self.assertEqual(image.pin_id, self.pin.pk)
        self.assertEqual(image.profile_id, self.profile.pk)
        self.assertEqual(image.source, ImageSource.WIKIMEDIA)

    def test_marking_not_relevant_does_not_materialize(self) -> None:
        with mock.patch("urbanlens.dashboard.services.media_materialize.requests.get") as mocked:
            response = self._post({"source": "wikimedia", "url": "https://example.test/photo.jpg", "is_relevant": False})
        self.assertEqual(response.status_code, 200)
        mocked.assert_not_called()
        self.assertFalse(Image.objects.filter(pin=self.pin).exists())

    def test_clearing_relevance_does_not_delete_an_already_materialized_image(self) -> None:
        with mock.patch("urbanlens.dashboard.services.media_materialize.requests.get", return_value=_ok_response()):
            first = self._post({"source": "wikimedia", "url": "https://example.test/photo.jpg", "is_relevant": True})
        image_id = first.json()["image_id"]

        response = self._post({"source": "wikimedia", "url": "https://example.test/photo.jpg", "is_relevant": None})

        self.assertEqual(response.status_code, 200)
        self.assertTrue(Image.objects.filter(pk=image_id).exists())

    def test_download_failure_still_saves_the_relevance_mark(self) -> None:
        import requests

        from urbanlens.dashboard.models.images.relevance import MediaRelevance

        with mock.patch("urbanlens.dashboard.services.media_materialize.requests.get", side_effect=requests.exceptions.ConnectionError("boom")):
            response = self._post({"source": "wikimedia", "url": "https://example.test/photo.jpg", "is_relevant": True})

        self.assertEqual(response.status_code, 200)
        self.assertIn("materialize_error", response.json())
        self.assertTrue(MediaRelevance.objects.filter(profile=self.profile, location=self.location, is_relevant=True).exists())

    def test_dropping_onto_the_map_materializes_and_sets_coordinates(self) -> None:
        """The drag/drop-onto-map flow (map-annotations.ts's drop handler)
        sends latitude/longitude alongside is_relevant=True, so the freshly
        materialized Image never has a moment with no coordinates."""
        with mock.patch("urbanlens.dashboard.services.media_materialize.requests.get", return_value=_ok_response()):
            response = self._post({"source": "wikimedia", "url": "https://example.test/photo.jpg", "is_relevant": True, "latitude": "40.123456", "longitude": "-74.654321"})
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertAlmostEqual(data["latitude"], 40.123456)
        self.assertAlmostEqual(data["longitude"], -74.654321)
        image = Image.objects.get(pk=data["image_id"])
        self.assertAlmostEqual(float(image.latitude), 40.123456)
        self.assertAlmostEqual(float(image.longitude), -74.654321)

    def test_invalid_coordinates_reject_before_materializing(self) -> None:
        with mock.patch("urbanlens.dashboard.services.media_materialize.requests.get") as mocked:
            response = self._post({"source": "wikimedia", "url": "https://example.test/photo.jpg", "is_relevant": True, "latitude": "not-a-number", "longitude": "-74.0"})
        self.assertEqual(response.status_code, 400)
        mocked.assert_not_called()
        self.assertFalse(Image.objects.filter(pin=self.pin).exists())

    def test_one_missing_coordinate_is_rejected(self) -> None:
        with mock.patch("urbanlens.dashboard.services.media_materialize.requests.get") as mocked:
            response = self._post({"source": "wikimedia", "url": "https://example.test/photo.jpg", "is_relevant": True, "latitude": "40.0"})
        self.assertEqual(response.status_code, 400)
        mocked.assert_not_called()

    def test_marking_relevant_without_coordinates_leaves_them_unset(self) -> None:
        """Existing (non-drag) relevance-marking path - no latitude/longitude
        keys sent at all - must keep behaving exactly as before."""
        with mock.patch("urbanlens.dashboard.services.media_materialize.requests.get", return_value=_ok_response()):
            response = self._post({"source": "wikimedia", "url": "https://example.test/photo.jpg", "is_relevant": True})
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertNotIn("latitude", data)
        self.assertNotIn("longitude", data)
        image = Image.objects.get(pk=data["image_id"])
        self.assertIsNone(image.latitude)
        self.assertIsNone(image.longitude)

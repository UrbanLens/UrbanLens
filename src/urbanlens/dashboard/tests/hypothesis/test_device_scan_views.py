"""Tests for the external-API device-scan endpoints.

Two invariants matter most here, mirroring how the rest of this API's tests
are framed (see test_external_api_photos.py's module docstring):

1. **The new scopes are opt-in only.** A key issued before this feature
   existed (the "default" grant) must be refused on both endpoints.
2. **``nearby/`` never leaks an undiscovered wiki's markers.** It must reuse
   the exact same visibility gate every other wiki-scoped read in this app
   uses - a marker on a wiki the caller hasn't found is indistinguishable
   from a marker that doesn't exist.
"""

from __future__ import annotations

from http import HTTPStatus
from typing import TYPE_CHECKING
from unittest.mock import patch

from django.contrib.auth.models import User
from django.contrib.gis.geos import Point
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.account.model import ApiKeyScope
from urbanlens.dashboard.models.device_scan.model import DeviceScanEntry, DeviceScanUpload, DeviceSignalReading, MarkerStatus, ScannedDevice, WikiDeviceMarker
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.services.api_keys import generate_api_key

if TYPE_CHECKING:
    from collections.abc import Iterable

_ENQUEUE = "urbanlens.dashboard.external_api.views_device_scans.safely_enqueue_task"

_UPLOAD_URL = "external_api:device_scans.upload"
_NEARBY_URL = "external_api:device_scans.nearby"

_LOCMEM_CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}


def _bearer(raw_key: str) -> dict:
    return {"HTTP_AUTHORIZATION": f"Bearer {raw_key}"}


def _key_with_scopes(user: User, scopes: Iterable[ApiKeyScope]) -> str:
    api_key, raw_key = generate_api_key(user, "Test Key")
    api_key.scopes = [scope.value for scope in scopes]
    api_key.save(update_fields=["scopes"])
    return raw_key


def _valid_payload(**overrides) -> dict:
    device = {
        "mac_address": "AA:BB:CC:DD:EE:FF",
        "device_name": "Some Camera",
        "device_type_guess": None,
        "detected": True,
        "estimated_latitude": 0.0,
        "estimated_longitude": 0.0,
        "readings": [{"latitude": 0.0, "longitude": 0.0, "signal_strength": -65, "observed_at": "2026-01-01T00:00:00Z"}],
    }
    device.update(overrides)
    return {"devices": [device]}


@override_settings(CACHES=_LOCMEM_CACHES)
class _DeviceScanApiTestCase(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.profile = Profile.objects.get(user=self.user)
        self.write_key = _key_with_scopes(self.user, [ApiKeyScope.DEVICE_SCANS_WRITE])
        self.read_key = _key_with_scopes(self.user, [ApiKeyScope.DEVICE_SCANS_READ])


class ScopeEnforcementTests(_DeviceScanApiTestCase):
    """A key without the device-scan scopes reaches neither endpoint."""

    def test_default_scoped_key_is_refused_on_upload(self) -> None:
        _api_key, legacy_raw = generate_api_key(self.user, "Legacy")
        response = self.client.post(reverse(_UPLOAD_URL), _valid_payload(), content_type="application/json", **_bearer(legacy_raw))
        self.assertEqual(response.status_code, HTTPStatus.FORBIDDEN)

    def test_default_scoped_key_is_refused_on_nearby(self) -> None:
        _api_key, legacy_raw = generate_api_key(self.user, "Legacy")
        response = self.client.get(reverse(_NEARBY_URL), {"latitude": 0.0, "longitude": 0.0}, **_bearer(legacy_raw))
        self.assertEqual(response.status_code, HTTPStatus.FORBIDDEN)

    def test_read_scope_cannot_upload(self) -> None:
        with patch(_ENQUEUE):
            response = self.client.post(reverse(_UPLOAD_URL), _valid_payload(), content_type="application/json", **_bearer(self.read_key))
        self.assertEqual(response.status_code, HTTPStatus.FORBIDDEN)

    def test_write_scope_cannot_read_nearby(self) -> None:
        response = self.client.get(reverse(_NEARBY_URL), {"latitude": 0.0, "longitude": 0.0}, **_bearer(self.write_key))
        self.assertEqual(response.status_code, HTTPStatus.FORBIDDEN)

    def test_unauthenticated_upload_is_rejected(self) -> None:
        response = self.client.post(reverse(_UPLOAD_URL), _valid_payload(), content_type="application/json")
        self.assertIn(response.status_code, (HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN))


class UploadValidationTests(_DeviceScanApiTestCase):
    """Payload validation - missing/invalid data is rejected with 400, not a 500."""

    def test_empty_devices_list_is_rejected(self) -> None:
        response = self.client.post(reverse(_UPLOAD_URL), {"devices": []}, content_type="application/json", **_bearer(self.write_key))
        self.assertEqual(response.status_code, HTTPStatus.BAD_REQUEST)

    def test_invalid_mac_address_is_rejected(self) -> None:
        response = self.client.post(reverse(_UPLOAD_URL), _valid_payload(mac_address="not-a-mac"), content_type="application/json", **_bearer(self.write_key))
        self.assertEqual(response.status_code, HTTPStatus.BAD_REQUEST)

    def test_out_of_range_latitude_is_rejected(self) -> None:
        response = self.client.post(reverse(_UPLOAD_URL), _valid_payload(estimated_latitude=200.0), content_type="application/json", **_bearer(self.write_key))
        self.assertEqual(response.status_code, HTTPStatus.BAD_REQUEST)


class UploadPersistenceTests(_DeviceScanApiTestCase):
    """A valid upload persists its entries/readings and queues background processing."""

    def test_upload_returns_202_with_an_upload_uuid(self) -> None:
        with patch(_ENQUEUE) as enqueue, self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(reverse(_UPLOAD_URL), _valid_payload(), content_type="application/json", **_bearer(self.write_key))

        self.assertEqual(response.status_code, HTTPStatus.ACCEPTED)
        self.assertIn("upload_uuid", response.json())
        upload = DeviceScanUpload.objects.get(uuid=response.json()["upload_uuid"])
        enqueue.assert_called_once()
        self.assertEqual(enqueue.call_args.args[1], upload.pk)

    def test_upload_creates_entry_and_readings(self) -> None:
        with patch(_ENQUEUE), self.captureOnCommitCallbacks(execute=True):
            self.client.post(reverse(_UPLOAD_URL), _valid_payload(), content_type="application/json", **_bearer(self.write_key))

        self.assertEqual(DeviceScanEntry.objects.count(), 1)
        self.assertEqual(DeviceSignalReading.objects.count(), 1)
        self.assertEqual(ScannedDevice.objects.get().mac_address, "AA:BB:CC:DD:EE:FF")

    def test_upload_attributed_to_caller_by_default(self) -> None:
        self.assertTrue(self.profile.track_device_scans)
        with patch(_ENQUEUE), self.captureOnCommitCallbacks(execute=True):
            self.client.post(reverse(_UPLOAD_URL), _valid_payload(), content_type="application/json", **_bearer(self.write_key))

        upload = DeviceScanUpload.objects.get()
        self.assertEqual(upload.profile_id, self.profile.pk)

    def test_upload_is_anonymous_when_track_device_scans_is_disabled(self) -> None:
        self.profile.track_device_scans = False
        self.profile.save(update_fields=["track_device_scans"])

        with patch(_ENQUEUE), self.captureOnCommitCallbacks(execute=True):
            self.client.post(reverse(_UPLOAD_URL), _valid_payload(), content_type="application/json", **_bearer(self.write_key))

        upload = DeviceScanUpload.objects.get()
        self.assertIsNone(upload.profile_id)
        # Still fully processed/stored - the setting only affects attribution.
        self.assertEqual(DeviceScanEntry.objects.count(), 1)


class NearbyDeviceMarkersVisibilityTests(_DeviceScanApiTestCase):
    """The privacy-critical invariant: markers on an undiscovered wiki never leak."""

    def setUp(self) -> None:
        super().setUp()
        self.location = Location.objects.create(latitude=0.0, longitude=0.0)
        self.wiki = baker.make(Wiki, location=self.location)
        self.device, _created = ScannedDevice.objects.get_or_create_for_mac("AA:BB:CC:DD:EE:FF")
        self.marker = WikiDeviceMarker.objects.create(
            wiki=self.wiki,
            device=self.device,
            status=MarkerStatus.ACTIVE,
            centroid=Point(0.0, 0.0, srid=4326),
            radius_meters=25.0,
            confidence=0.5,
            first_observed_at=timezone.now(),
            last_observed_at=timezone.now(),
        )

    def test_marker_hidden_when_caller_has_not_discovered_the_wiki(self) -> None:
        response = self.client.get(reverse(_NEARBY_URL), {"latitude": 0.0, "longitude": 0.0, "radius_meters": 1000}, **_bearer(self.read_key))
        self.assertEqual(response.status_code, HTTPStatus.OK)
        self.assertEqual(response.json()["markers"], [])

    def test_marker_visible_once_caller_has_a_pin_at_the_location(self) -> None:
        baker.make(Pin, profile=self.profile, location=self.location)

        response = self.client.get(reverse(_NEARBY_URL), {"latitude": 0.0, "longitude": 0.0, "radius_meters": 1000}, **_bearer(self.read_key))

        self.assertEqual(response.status_code, HTTPStatus.OK)
        markers = response.json()["markers"]
        self.assertEqual(len(markers), 1)
        self.assertEqual(markers[0]["marker_uuid"], str(self.marker.uuid))
        self.assertEqual(markers[0]["device"]["mac_address"], "AA:BB:CC:DD:EE:FF")
        self.assertEqual(markers[0]["latitude"], 0.0)
        self.assertEqual(markers[0]["longitude"], 0.0)

    def test_another_profiles_pin_does_not_grant_visibility(self) -> None:
        other_user = baker.make(User)
        baker.make(Pin, profile=other_user.profile, location=self.location)

        response = self.client.get(reverse(_NEARBY_URL), {"latitude": 0.0, "longitude": 0.0, "radius_meters": 1000}, **_bearer(self.read_key))

        self.assertEqual(response.json()["markers"], [])


class NearbyDeviceMarkersFilteringTests(_DeviceScanApiTestCase):
    """Radius and status filtering, once the wiki is visible."""

    def setUp(self) -> None:
        super().setUp()
        self.location = Location.objects.create(latitude=0.0, longitude=0.0)
        self.wiki = baker.make(Wiki, location=self.location)
        baker.make(Pin, profile=self.profile, location=self.location)
        self.device, _created = ScannedDevice.objects.get_or_create_for_mac("AA:BB:CC:DD:EE:FF")

    def _make_marker(self, *, status: str, lng: float = 0.0) -> WikiDeviceMarker:
        return WikiDeviceMarker.objects.create(
            wiki=self.wiki,
            device=self.device,
            status=status,
            centroid=Point(lng, 0.0, srid=4326),
            first_observed_at=timezone.now(),
            last_observed_at=timezone.now(),
        )

    def test_marker_outside_radius_is_excluded(self) -> None:
        self._make_marker(status=MarkerStatus.ACTIVE, lng=10.0)
        response = self.client.get(reverse(_NEARBY_URL), {"latitude": 0.0, "longitude": 0.0, "radius_meters": 500}, **_bearer(self.read_key))
        self.assertEqual(response.json()["markers"], [])

    def test_presumed_removed_and_dismissed_are_excluded(self) -> None:
        self._make_marker(status=MarkerStatus.PRESUMED_REMOVED)
        self._make_marker(status=MarkerStatus.DISMISSED)
        response = self.client.get(reverse(_NEARBY_URL), {"latitude": 0.0, "longitude": 0.0, "radius_meters": 1000}, **_bearer(self.read_key))
        self.assertEqual(response.json()["markers"], [])

    def test_stale_marker_is_still_included(self) -> None:
        marker = self._make_marker(status=MarkerStatus.STALE)
        response = self.client.get(reverse(_NEARBY_URL), {"latitude": 0.0, "longitude": 0.0, "radius_meters": 1000}, **_bearer(self.read_key))
        markers = response.json()["markers"]
        self.assertEqual([m["marker_uuid"] for m in markers], [str(marker.uuid)])

    def test_missing_required_query_params_is_rejected(self) -> None:
        response = self.client.get(reverse(_NEARBY_URL), {}, **_bearer(self.read_key))
        self.assertEqual(response.status_code, HTTPStatus.BAD_REQUEST)

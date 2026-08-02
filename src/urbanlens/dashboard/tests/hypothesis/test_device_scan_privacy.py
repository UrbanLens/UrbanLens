"""Regression guard: the API surface never exposes per-scan or per-uploader
device-scan data - only the cumulative, unattributed :class:`WikiDeviceMarker`.

Raw scans (``DeviceScanUpload``/``DeviceScanEntry``/``DeviceSignalReading``)
carry a ``profile`` (directly or via their upload) precisely because
attribution is needed for the privacy-preference plumbing described in
``Profile.track_device_scans``'s docstring. That is exactly why they must
never be individually readable back through any API: a client with
``device_scans:read`` could otherwise reconstruct who walked where. The
guarantee is layered, and each layer gets its own test class below so a
regression in any one of them fails loudly with a reason attached:

1. **No route reads them.** The external API registers exactly two
   device-scan routes - a write-only upload and a read-only aggregate query -
   and neither the upload view nor any internal ``/rest/`` viewset exposes a
   GET over the raw models.
2. **The one read serializer can't carry an identity.** ``WikiDeviceMarker``
   itself has no ``profile``/uploader field to leak, and
   ``NearbyDeviceMarkerSerializer``'s field set is pinned to an explicit
   allowlist.
3. **End to end, two different uploaders collapse into one silent marker.**
   The clustering pipeline is what actually anonymizes contributions -
   this proves the merge really happens, not just that the serializer omits
   a field that still exists on the object underneath.
"""

from __future__ import annotations

from http import HTTPStatus
from typing import TYPE_CHECKING
from unittest.mock import patch

from django.contrib.auth.models import User
from django.contrib.gis.geos import MultiPolygon, Polygon
from django.test import override_settings
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.external_api import urls_device_scans
from urbanlens.dashboard.external_api.serializers_device_scans import (
    DeviceScanUploadResponseSerializer,
    NearbyDeviceMarkerDeviceSerializer,
    NearbyDeviceMarkerSerializer,
)
from urbanlens.dashboard.external_api.views_device_scans import DeviceScanUploadView, NearbyDeviceMarkersView
from urbanlens.dashboard.models.account.model import ApiKeyScope
from urbanlens.dashboard.models.boundary.model import Boundary
from urbanlens.dashboard.models.device_scan.model import DeviceScanEntry, DeviceScanUpload, DeviceSignalReading, ScannedDevice, WikiDeviceMarker
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.services.auth.api_keys import generate_api_key
from urbanlens.dashboard.services.device_scan.pipeline import process_scan_upload
from urbanlens.dashboard.urls import router as internal_rest_router

if TYPE_CHECKING:
    from collections.abc import Iterable

_ENQUEUE = "urbanlens.dashboard.external_api.views_device_scans.safely_enqueue_task"
_LOCMEM_CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
_DEVICE_SCAN_MODELS = {DeviceScanUpload, DeviceScanEntry, DeviceSignalReading, ScannedDevice, WikiDeviceMarker}

#: Substrings that would flag a field as identifying a contributor, checked
#: case-insensitively against every field name a response serializer exposes.
_IDENTITY_FIELD_FRAGMENTS = ("profile", "uploader", "uploaded_by", "contributor", "owner", "user")


def _bearer(raw_key: str) -> dict:
    return {"HTTP_AUTHORIZATION": f"Bearer {raw_key}"}


def _key_with_scopes(user: User, scopes: Iterable[ApiKeyScope]) -> str:
    api_key, raw_key = generate_api_key(user, "Test Key")
    api_key.scopes = [scope.value for scope in scopes]
    api_key.save(update_fields=["scopes"])
    return raw_key


def _square(lng: float, lat: float, delta: float) -> MultiPolygon:
    ring = (
        (lng - delta, lat - delta),
        (lng + delta, lat - delta),
        (lng + delta, lat + delta),
        (lng - delta, lat + delta),
        (lng - delta, lat - delta),
    )
    return MultiPolygon(Polygon(ring, srid=4326), srid=4326)


class WikiDeviceMarkerModelHasNoIdentityFieldTests(TestCase):
    """The aggregate model itself has nothing to leak, even if a serializer misbehaved."""

    def test_wiki_device_marker_has_no_profile_or_uploader_field(self) -> None:
        field_names = {field.name.lower() for field in WikiDeviceMarker._meta.get_fields()}
        leaking = {name for name in field_names if any(fragment in name for fragment in _IDENTITY_FIELD_FRAGMENTS)}
        self.assertEqual(leaking, set(), f"WikiDeviceMarker gained field(s) that look identity-related: {leaking}. This model backs the cumulative nearby/ endpoint and must never carry a contributor reference.")


class RouteSurfaceTests(TestCase):
    """Exactly two device-scan routes exist, each supporting exactly one HTTP verb."""

    def test_exactly_two_device_scan_routes_are_registered(self) -> None:
        names = sorted(entry.name for entry in urls_device_scans.urlpatterns)
        self.assertEqual(
            names,
            ["device_scans.nearby", "device_scans.upload"],
            "A new route was added under the device-scans feature - re-review it against the no-individual-scan-data invariant this file guards before adding it here.",
        )

    def test_upload_view_only_declares_a_post_scope(self) -> None:
        """No GET entry means HasApiKeyScope fails closed on GET, whatever scopes a key holds."""
        self.assertEqual(set(DeviceScanUploadView.required_scopes_by_method), {"POST"})

    def test_nearby_view_only_declares_a_get_scope(self) -> None:
        self.assertEqual(set(NearbyDeviceMarkersView.required_scopes_by_method), {"GET"})


class InternalRestRouterNeverExposesDeviceScanModelsTests(TestCase):
    """The internal, session-authenticated /rest/ surface is a second potential pathway - it must stay empty of these models too."""

    def test_no_registered_viewset_serves_a_device_scan_model(self) -> None:
        for prefix, viewset, _basename in internal_rest_router.registry:
            model = getattr(getattr(viewset, "queryset", None), "model", None)
            self.assertNotIn(
                model,
                _DEVICE_SCAN_MODELS,
                f"Internal /rest/ viewset registered at {prefix!r} serves {model} - individual scan data must only ever be reachable, in aggregate form, through the external API's device-scans/nearby/ endpoint.",
            )


@override_settings(CACHES=_LOCMEM_CACHES)
class WrongMethodIsRefusedTests(TestCase):
    """A key holding both device-scan scopes still can't read via the write endpoint or write via the read one."""

    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.full_key = _key_with_scopes(self.user, [ApiKeyScope.DEVICE_SCANS_READ, ApiKeyScope.DEVICE_SCANS_WRITE])

    def test_get_on_the_upload_endpoint_is_refused_even_with_both_scopes(self) -> None:
        response = self.client.get(reverse("external_api:device_scans.upload"), **_bearer(self.full_key))
        self.assertEqual(response.status_code, HTTPStatus.FORBIDDEN)

    def test_post_on_the_nearby_endpoint_is_refused_even_with_both_scopes(self) -> None:
        response = self.client.post(reverse("external_api:device_scans.nearby"), {}, content_type="application/json", **_bearer(self.full_key))
        self.assertEqual(response.status_code, HTTPStatus.FORBIDDEN)


class SerializerFieldAllowlistTests(TestCase):
    """The two response serializers are pinned to an explicit, identity-free field set."""

    def test_nearby_marker_serializer_has_no_identity_looking_field(self) -> None:
        field_names = {name.lower() for name in NearbyDeviceMarkerSerializer().fields}
        leaking = {name for name in field_names if any(fragment in name for fragment in _IDENTITY_FIELD_FRAGMENTS)}
        self.assertEqual(leaking, set(), f"NearbyDeviceMarkerSerializer exposes field(s) that look identity-related: {leaking}.")

    def test_nearby_marker_serializer_fields_are_exactly_the_allowed_set(self) -> None:
        self.assertEqual(
            set(NearbyDeviceMarkerSerializer().fields),
            {"marker_uuid", "device", "latitude", "longitude", "radius_meters", "confidence", "avg_signal_strength", "last_observed_at", "status"},
        )

    def test_marker_device_field_is_exactly_the_allowed_set(self) -> None:
        self.assertEqual(set(NearbyDeviceMarkerDeviceSerializer().fields), {"mac_address", "device_type", "display_name"})

    def test_upload_response_serializer_returns_only_the_upload_uuid(self) -> None:
        self.assertEqual(set(DeviceScanUploadResponseSerializer().fields), {"upload_uuid"})


@override_settings(CACHES=_LOCMEM_CACHES)
class AggregateOnlyEndToEndTests(TestCase):
    """Two different uploaders' scans of the same device collapse into one unattributed marker."""

    def setUp(self) -> None:
        super().setUp()
        self.location = Location.objects.create(latitude=0.0, longitude=0.0)
        Boundary.objects.create(location=self.location, generated_polygon=_square(0.0, 0.0, 0.01))
        self.wiki = baker.make(Wiki, location=self.location)

        self.uploader_one = baker.make(User, username="contributor-one")
        self.uploader_two = baker.make(User, username="contributor-two")
        self.viewer = baker.make(User, username="viewer")
        baker.make(Pin, profile=self.viewer.profile, location=self.location)

        self.write_key_one = _key_with_scopes(self.uploader_one, [ApiKeyScope.DEVICE_SCANS_WRITE])
        self.write_key_two = _key_with_scopes(self.uploader_two, [ApiKeyScope.DEVICE_SCANS_WRITE])
        self.read_key = _key_with_scopes(self.viewer, [ApiKeyScope.DEVICE_SCANS_READ])

    def _upload(self, raw_key: str):
        payload = {
            "devices": [
                {
                    "mac_address": "AA:BB:CC:DD:EE:FF",
                    "device_type_guess": "camera",
                    "detected": True,
                    "estimated_latitude": 0.0,
                    "estimated_longitude": 0.0,
                },
            ],
        }
        with patch(_ENQUEUE):
            return self.client.post(reverse("external_api:device_scans.upload"), payload, content_type="application/json", **_bearer(raw_key))

    def test_marker_from_two_uploaders_carries_no_trace_of_either(self) -> None:
        response_one = self._upload(self.write_key_one)
        response_two = self._upload(self.write_key_two)
        self.assertEqual(response_one.status_code, HTTPStatus.ACCEPTED)
        self.assertEqual(response_two.status_code, HTTPStatus.ACCEPTED)

        # Runs the background pipeline inline - production defers this to
        # Celery via transaction.on_commit, but the classification/clustering
        # logic under test here is identical either way.
        for upload in DeviceScanUpload.objects.all():
            process_scan_upload(upload)

        # Proves the merge actually happened, not merely that the serializer
        # hides a field: one marker, corroborated by both uploads.
        marker = WikiDeviceMarker.objects.get()
        self.assertEqual(marker.observation_count, 2)

        response = self.client.get(reverse("external_api:device_scans.nearby"), {"latitude": 0.0, "longitude": 0.0, "radius_meters": 1000}, **_bearer(self.read_key))
        self.assertEqual(response.status_code, HTTPStatus.OK)
        markers = response.json()["markers"]
        self.assertEqual(len(markers), 1)

        # Deliberately checks distinctive strings only (username, uuid) - a
        # raw pk is too short/common to assert against meaningfully (it would
        # false-positive against digits in the timestamp or confidence
        # value), and the field-allowlist tests above already guarantee no
        # "profile_id"-shaped field could appear in the first place.
        body_text = response.content.decode()
        for identity in (
            self.uploader_one.username,
            self.uploader_two.username,
            str(self.uploader_one.profile.uuid),
            str(self.uploader_two.profile.uuid),
        ):
            self.assertNotIn(identity, body_text, f"nearby/ response leaked {identity!r} - it must only ever describe the cumulative marker, never a contributor.")

    def test_upload_responses_never_echo_the_uploader_either(self) -> None:
        response = self._upload(self.write_key_one)
        body_text = response.content.decode()
        self.assertNotIn(self.uploader_one.username, body_text)
        self.assertNotIn(str(self.uploader_one.profile.uuid), body_text)

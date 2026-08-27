"""Resolving expected markers must not cost one query per device.

`ingest_scan_upload` runs synchronously inside the upload request and accepts up
to `MAX_DEVICES_PER_UPLOAD` (200) devices. It used to look up each device's
`expected_marker_uuid` individually, so a full upload spent up to 200 round-trips
on marker resolution alone, on top of the per-device device/entry writes.

The scaling assertion is written as "marker resolution adds no more than a
constant" rather than a fixed query count: the rest of the loop is legitimately
per-device (a `get_or_create` per MAC, one entry insert, one bulk_create of
readings), so pinning a total would break on unrelated changes and tell nobody
anything. Comparing an upload *with* markers against the same upload *without*
isolates the part under test.
"""

from __future__ import annotations

from datetime import UTC, datetime

from django.contrib.gis.geos import Point
from django.db import connection
from django.test.utils import CaptureQueriesContext
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.device_scan.model import DeviceScanEntry
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.services.device_scan.ingestion import ingest_scan_upload

_LAT, _LNG = 42.6526, -73.7562


def _device(index: int, *, marker_uuid: str | None = None) -> dict:
    device = {
        "mac_address": f"AA:BB:CC:{index // 256:02X}:{index % 256:02X}:01",
        "estimated_latitude": _LAT,
        "estimated_longitude": _LNG,
        "detected": True,
        "readings": [
            {
                "latitude": _LAT,
                "longitude": _LNG,
                "signal_strength": -60,
                "observed_at": datetime(2026, 6, 1, 12, 0, tzinfo=UTC),
            },
        ],
    }
    if marker_uuid:
        device["expected_marker_uuid"] = marker_uuid
    return device


class DeviceScanIngestQueryTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.profile = Profile.objects.get(user=baker.make("auth.User"))
        self.marker = baker.make("dashboard.WikiDeviceMarker", centroid=Point(_LNG, _LAT, srid=4326))

    def _ingest(self, devices: list[dict]) -> int:
        with CaptureQueriesContext(connection) as queries:
            ingest_scan_upload(self.profile, client_session_uuid="", devices=devices)
        return len(queries.captured_queries)

    def test_marker_resolution_does_not_scale_with_device_count(self) -> None:
        """The property: markers cost a constant, not one query per device."""
        marker_uuid = str(self.marker.uuid)
        # Distinct MAC ranges so neither run is cheapened by devices the other created.
        without_markers = self._ingest([_device(i) for i in range(20)])
        with_markers = self._ingest([_device(1000 + i, marker_uuid=marker_uuid) for i in range(20)])

        self.assertLessEqual(
            with_markers - without_markers,
            5,
            f"marker resolution looks per-device: {without_markers} -> {with_markers} queries for 20 devices",
        )

    def test_the_marker_is_actually_attached(self) -> None:
        """Batching must not quietly stop resolving them."""
        ingest_scan_upload(self.profile, client_session_uuid="", devices=[_device(1, marker_uuid=str(self.marker.uuid))])

        self.assertEqual(DeviceScanEntry.objects.get().expected_marker_id, self.marker.pk)

    def test_an_unknown_marker_uuid_leaves_it_unset(self) -> None:
        """Same as the per-device `.first()` did - an unknown uuid must not fail the upload."""
        import uuid as uuid_module

        ingest_scan_upload(self.profile, client_session_uuid="", devices=[_device(2, marker_uuid=str(uuid_module.uuid4()))])

        self.assertIsNone(DeviceScanEntry.objects.get().expected_marker_id)

    def test_devices_without_a_marker_are_unaffected(self) -> None:
        ingest_scan_upload(self.profile, client_session_uuid="", devices=[_device(3)])

        self.assertIsNone(DeviceScanEntry.objects.get().expected_marker_id)

"""Tests for the device-scan Celery task and its pipeline.

``process_device_scan_upload`` (dashboard.tasks) is covered for its
always-PROCESSED-or-FAILED contract; ``process_scan_upload``
(services.device_scan.pipeline) is covered directly for the routing logic it
implements - type resolution, security-relevant-type gating, multi-wiki
fan-out, and absence-report routing.
"""

from __future__ import annotations

from unittest.mock import patch

from django.contrib.gis.geos import MultiPolygon, Point, Polygon
from django.utils import timezone
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.boundary.model import Boundary
from urbanlens.dashboard.models.device_scan.model import (
    DeviceScanUpload,
    DeviceType,
    DeviceTypeSource,
    ScannedDevice,
    ScanUploadStatus,
    WikiDeviceMarker,
)
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.services.device_scan.ingestion import ingest_scan_upload
from urbanlens.dashboard.services.device_scan.pipeline import process_scan_upload
from urbanlens.dashboard.tasks import process_device_scan_upload

from .place_helpers import official_geometry


def _square(lng: float, lat: float, delta: float) -> MultiPolygon:
    ring = (
        (lng - delta, lat - delta),
        (lng + delta, lat - delta),
        (lng + delta, lat + delta),
        (lng - delta, lat + delta),
        (lng - delta, lat - delta),
    )
    return MultiPolygon(Polygon(ring, srid=4326), srid=4326)


def _device_dict(**overrides) -> dict:
    device = {
        "mac_address": "AA:BB:CC:DD:EE:FF",
        "device_name": "Some Camera",
        "device_type_guess": DeviceType.CAMERA,
        "detected": True,
        "estimated_latitude": 0.0,
        "estimated_longitude": 0.0,
        "expected_marker_uuid": None,
        "readings": [],
    }
    device.update(overrides)
    return device


class _DeviceScanWikiTestCase(TestCase):
    """Shared fixture: a wiki whose boundary contains (0, 0)."""

    def setUp(self) -> None:
        super().setUp()
        self.location = Location.objects.create(latitude=0.0, longitude=0.0)
        official_geometry(self.location, _square(0.0, 0.0, 0.01))
        self.wiki = baker.make(Wiki, location=self.location)


class ProcessDeviceScanUploadTaskTests(_DeviceScanWikiTestCase):
    """The Celery task's always-PROCESSED-or-FAILED contract."""

    def test_returns_false_for_a_missing_upload(self) -> None:
        self.assertFalse(process_device_scan_upload(10_000_000))

    def test_marks_the_upload_processed_and_creates_a_marker(self) -> None:
        upload = ingest_scan_upload(None, client_session_uuid="", devices=[_device_dict()])

        # Calling a bound task directly (not via .delay()/.apply()) leaves
        # self.request.id unset, so update_task_progress's update_state()
        # would otherwise hit the real (Redis) result backend with an empty
        # task_id - same reasoning as test_process_media_upload_dispatch.py.
        with patch("urbanlens.dashboard.tasks.update_task_progress"):
            result = process_device_scan_upload(upload.pk)

        self.assertTrue(result)
        upload.refresh_from_db()
        self.assertEqual(upload.status, ScanUploadStatus.PROCESSED)
        self.assertEqual(WikiDeviceMarker.objects.filter(wiki=self.wiki).count(), 1)

    def test_marks_the_upload_failed_on_an_unexpected_error(self) -> None:
        upload = ingest_scan_upload(None, client_session_uuid="", devices=[_device_dict()])

        with (
            patch("urbanlens.dashboard.tasks.update_task_progress"),
            patch("urbanlens.dashboard.services.device_scan.clustering.recompute_wiki_device_markers", side_effect=RuntimeError("boom")),
        ):
            result = process_device_scan_upload(upload.pk)

        self.assertTrue(result)
        upload.refresh_from_db()
        self.assertEqual(upload.status, ScanUploadStatus.FAILED)
        self.assertIn("boom", upload.error)


class ProcessScanUploadTypeRoutingTests(_DeviceScanWikiTestCase):
    """Which device types raise a marker, and how classification is resolved."""

    def _run(self, **device_overrides) -> DeviceScanUpload:
        upload = ingest_scan_upload(None, client_session_uuid="", devices=[_device_dict(**device_overrides)])
        process_scan_upload(upload)
        return upload

    def test_non_security_relevant_type_creates_no_marker(self) -> None:
        self._run(device_type_guess=DeviceType.PHONE)
        self.assertEqual(WikiDeviceMarker.objects.count(), 0)

    def test_camera_guess_creates_a_marker_and_persists_the_classification(self) -> None:
        self._run(device_type_guess=DeviceType.CAMERA)
        device = ScannedDevice.objects.get(mac_address="AA:BB:CC:DD:EE:FF")
        self.assertEqual(device.device_type, DeviceType.CAMERA)
        self.assertEqual(device.device_type_source, DeviceTypeSource.CLIENT)
        self.assertEqual(WikiDeviceMarker.objects.filter(wiki=self.wiki, device=device).count(), 1)

    def test_no_client_guess_falls_back_to_the_heuristic(self) -> None:
        self._run(mac_address="8C:C8:F4:11:22:33", device_type_guess=None, device_name="")
        device = ScannedDevice.objects.get(mac_address="8C:C8:F4:11:22:33")
        self.assertEqual(device.device_type, DeviceType.CAMERA)
        self.assertEqual(device.device_type_source, DeviceTypeSource.HEURISTIC)
        self.assertEqual(WikiDeviceMarker.objects.count(), 1)

    def test_point_outside_every_wiki_boundary_creates_no_marker(self) -> None:
        self._run(estimated_latitude=50.0, estimated_longitude=50.0)
        self.assertEqual(WikiDeviceMarker.objects.count(), 0)

    def test_multiple_overlapping_wikis_each_get_their_own_marker(self) -> None:
        """Two unrelated places whose county geometry overlaps - the rare real case."""
        from urbanlens.dashboard.models.place.model import PlaceKind

        from .place_helpers import make_place

        other_place = make_place(PlaceKind.PARCEL, _square(0.0, 0.0, 0.02))
        other_location = Location.objects.create(latitude=0.0002, longitude=0.0002)
        other_wiki = baker.make(Wiki, location=other_location, place=other_place)

        self._run(device_type_guess=DeviceType.CAMERA)

        device = ScannedDevice.objects.get(mac_address="AA:BB:CC:DD:EE:FF")
        wiki_ids = set(WikiDeviceMarker.objects.filter(device=device).values_list("wiki_id", flat=True))
        self.assertEqual(wiki_ids, {self.wiki.pk, other_wiki.pk})


class ProcessScanUploadAbsenceRoutingTests(_DeviceScanWikiTestCase):
    """detected=False entries route to record_absence_report."""

    def setUp(self) -> None:
        super().setUp()
        self.device, _created = ScannedDevice.objects.get_or_create_for_mac("AA:BB:CC:DD:EE:FF")
        self.marker = WikiDeviceMarker.objects.create(
            wiki=self.wiki,
            device=self.device,
            centroid=Point(0.0, 0.0, srid=4326),
            first_observed_at=timezone.now(),
            last_observed_at=timezone.now(),
        )

    def test_absence_report_via_expected_marker_uuid(self) -> None:
        upload = ingest_scan_upload(
            None,
            client_session_uuid="",
            devices=[_device_dict(detected=False, expected_marker_uuid=self.marker.uuid)],
        )

        process_scan_upload(upload)

        self.marker.refresh_from_db()
        self.assertEqual(self.marker.absence_streak, 1)

    def test_absence_report_falls_back_to_nearest_marker_without_an_expected_uuid(self) -> None:
        upload = ingest_scan_upload(
            None,
            client_session_uuid="",
            devices=[_device_dict(detected=False, expected_marker_uuid=None)],
        )

        process_scan_upload(upload)

        self.marker.refresh_from_db()
        self.assertEqual(self.marker.absence_streak, 1)

    def test_absence_report_with_no_nearby_marker_does_nothing(self) -> None:
        upload = ingest_scan_upload(
            None,
            client_session_uuid="",
            devices=[_device_dict(detected=False, expected_marker_uuid=None, estimated_latitude=50.0, estimated_longitude=50.0)],
        )

        process_scan_upload(upload)

        self.marker.refresh_from_db()
        self.assertEqual(self.marker.absence_streak, 0)

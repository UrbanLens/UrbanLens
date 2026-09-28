"""Tests for the device-scan Celery task and its pipeline."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

from django.contrib.gis.geos import MultiPolygon, Point, Polygon
from django.utils import timezone
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
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
from urbanlens.dashboard.tasks import (
    MAX_SCAN_UPLOAD_ATTEMPTS,
    STALLED_SCAN_PENDING_AGE,
    process_device_scan_upload,
    requeue_stalled_device_scans,
    stalled_scan_claim_age,
)

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
        upload, _created = ingest_scan_upload(None, client_session_uuid="", devices=[_device_dict()])

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
        upload, _created = ingest_scan_upload(None, client_session_uuid="", devices=[_device_dict()])

        with (
            patch("urbanlens.dashboard.tasks.update_task_progress"),
            patch(
                "urbanlens.dashboard.services.device_scan.clustering.recompute_wiki_device_markers",
                side_effect=RuntimeError("boom"),
            ),
        ):
            result = process_device_scan_upload(upload.pk)

        self.assertTrue(result)
        upload.refresh_from_db()
        self.assertEqual(upload.status, ScanUploadStatus.FAILED)
        self.assertIn("boom", upload.error)


class DeviceScanClaimTests(_DeviceScanWikiTestCase):
    """A claimed upload's work commits with its PROCESSED flip, and a stalled one is handed on."""

    def _absent_upload(self) -> tuple[DeviceScanUpload, WikiDeviceMarker]:
        device, _ = ScannedDevice.objects.get_or_create_for_mac("AA:BB:CC:DD:EE:01")
        marker = WikiDeviceMarker.objects.create(
            wiki=self.wiki,
            device=device,
            centroid=Point(0.0, 0.0, srid=4326),
            first_observed_at=timezone.now(),
            last_observed_at=timezone.now(),
        )
        upload, _created = ingest_scan_upload(
            None,
            client_session_uuid="",
            devices=[_device_dict(mac_address=device.mac_address, detected=False, expected_marker_uuid=marker.uuid)],
        )
        return upload, marker

    def test_a_failure_after_an_absence_report_rolls_the_report_back(self) -> None:
        from urbanlens.dashboard.services.device_scan import clustering

        upload, marker = self._absent_upload()
        real = clustering.record_absence_report

        def report_then_fail(target):
            real(target)
            raise RuntimeError("worker died")

        with (
            patch("urbanlens.dashboard.tasks.update_task_progress"),
            patch.object(clustering, "record_absence_report", side_effect=report_then_fail),
        ):
            process_device_scan_upload(upload.pk)

        marker.refresh_from_db()
        upload.refresh_from_db()
        self.assertEqual(marker.absence_streak, 0)
        self.assertEqual(upload.status, ScanUploadStatus.FAILED)

    def test_a_second_delivery_does_not_count_the_absence_twice(self) -> None:
        upload, marker = self._absent_upload()

        with patch("urbanlens.dashboard.tasks.update_task_progress"):
            self.assertTrue(process_device_scan_upload(upload.pk))
            self.assertFalse(process_device_scan_upload(upload.pk))

        marker.refresh_from_db()
        self.assertEqual(marker.absence_streak, 1)

    def test_a_claim_records_when_and_how_often(self) -> None:
        upload, _created = ingest_scan_upload(None, client_session_uuid="", devices=[_device_dict()])

        with patch("urbanlens.dashboard.tasks.update_task_progress"):
            process_device_scan_upload(upload.pk)

        upload.refresh_from_db()
        self.assertIsNotNone(upload.claimed_at)
        self.assertEqual(upload.attempts, 1)

    def _age(self, upload: DeviceScanUpload, **fields) -> None:
        DeviceScanUpload.objects.filter(pk=upload.pk).update(**fields)

    def test_a_pending_upload_whose_enqueue_was_lost_is_requeued(self) -> None:
        upload, _created = ingest_scan_upload(None, client_session_uuid="", devices=[_device_dict()])
        self._age(upload, created=timezone.now() - STALLED_SCAN_PENDING_AGE - timedelta(minutes=1))

        with patch("urbanlens.dashboard.services.core.celery.safely_enqueue_task") as enqueue:
            self.assertEqual(requeue_stalled_device_scans(), 1)

        enqueue.assert_called_once_with(process_device_scan_upload, upload.pk, durable=False)

    def test_a_fresh_pending_upload_is_left_to_its_own_enqueue(self) -> None:
        ingest_scan_upload(None, client_session_uuid="", devices=[_device_dict()])

        with patch("urbanlens.dashboard.services.core.celery.safely_enqueue_task") as enqueue:
            self.assertEqual(requeue_stalled_device_scans(), 0)

        enqueue.assert_not_called()

    def test_an_upload_whose_worker_died_goes_back_to_pending_and_is_requeued(self) -> None:
        upload, _created = ingest_scan_upload(None, client_session_uuid="", devices=[_device_dict()])
        stale = timezone.now() - stalled_scan_claim_age() - timedelta(minutes=1)
        self._age(upload, status=ScanUploadStatus.PROCESSING, claimed_at=stale, attempts=1)

        with patch("urbanlens.dashboard.services.core.celery.safely_enqueue_task") as enqueue:
            self.assertEqual(requeue_stalled_device_scans(), 1)

        upload.refresh_from_db()
        self.assertEqual(upload.status, ScanUploadStatus.PENDING)
        enqueue.assert_called_once()

    def test_an_upload_still_being_worked_is_left_alone(self) -> None:
        upload, _created = ingest_scan_upload(None, client_session_uuid="", devices=[_device_dict()])
        self._age(
            upload,
            status=ScanUploadStatus.PROCESSING,
            claimed_at=timezone.now(),
            attempts=1,
            created=timezone.now() - timedelta(days=1),
        )

        with patch("urbanlens.dashboard.services.core.celery.safely_enqueue_task") as enqueue:
            requeue_stalled_device_scans()

        upload.refresh_from_db()
        self.assertEqual(upload.status, ScanUploadStatus.PROCESSING)
        enqueue.assert_not_called()

    def test_an_upload_that_keeps_killing_its_worker_is_failed(self) -> None:
        upload, _created = ingest_scan_upload(None, client_session_uuid="", devices=[_device_dict()])
        stale = timezone.now() - stalled_scan_claim_age() - timedelta(minutes=1)
        self._age(upload, status=ScanUploadStatus.PROCESSING, claimed_at=stale, attempts=MAX_SCAN_UPLOAD_ATTEMPTS)

        with patch("urbanlens.dashboard.services.core.celery.safely_enqueue_task") as enqueue:
            self.assertEqual(requeue_stalled_device_scans(), 0)

        upload.refresh_from_db()
        self.assertEqual(upload.status, ScanUploadStatus.FAILED)
        enqueue.assert_not_called()

    def test_a_claim_is_honoured_past_the_tasks_hard_limit(self) -> None:
        self.assertGreater(stalled_scan_claim_age().total_seconds(), process_device_scan_upload.time_limit)

    def test_the_sweep_is_on_the_beat_schedule(self) -> None:
        from django.conf import settings

        self.assertIn(
            requeue_stalled_device_scans.name, {entry["task"] for entry in settings.CELERY_BEAT_SCHEDULE.values()}
        )


class ProcessScanUploadTypeRoutingTests(_DeviceScanWikiTestCase):
    """Which device types raise a marker, and how classification is resolved."""

    def _run(self, **device_overrides) -> DeviceScanUpload:
        upload, _created = ingest_scan_upload(None, client_session_uuid="", devices=[_device_dict(**device_overrides)])
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
        upload, _created = ingest_scan_upload(
            None,
            client_session_uuid="",
            devices=[_device_dict(detected=False, expected_marker_uuid=self.marker.uuid)],
        )

        process_scan_upload(upload)

        self.marker.refresh_from_db()
        self.assertEqual(self.marker.absence_streak, 1)

    def test_absence_report_falls_back_to_nearest_marker_without_an_expected_uuid(self) -> None:
        upload, _created = ingest_scan_upload(
            None,
            client_session_uuid="",
            devices=[_device_dict(detected=False, expected_marker_uuid=None)],
        )

        process_scan_upload(upload)

        self.marker.refresh_from_db()
        self.assertEqual(self.marker.absence_streak, 1)

    def test_absence_report_with_no_nearby_marker_does_nothing(self) -> None:
        upload, _created = ingest_scan_upload(
            None,
            client_session_uuid="",
            devices=[
                _device_dict(
                    detected=False, expected_marker_uuid=None, estimated_latitude=50.0, estimated_longitude=50.0
                )
            ],
        )

        process_scan_upload(upload)

        self.marker.refresh_from_db()
        self.assertEqual(self.marker.absence_streak, 0)

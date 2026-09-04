"""Tests for the device_scan model/queryset layer itself.

Behavioral coverage of the custom managers/querysets - not the clustering
pipeline (see test_device_scan_clustering.py) or the external API (see
test_device_scan_views.py).
"""

from __future__ import annotations

from django.contrib.gis.geos import Point
from django.test import SimpleTestCase
from django.utils import timezone
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.device_scan.model import (
    DeviceScanEntry,
    DeviceScanUpload,
    MarkerStatus,
    ScannedDevice,
    ScanUploadStatus,
    WikiDeviceMarker,
)
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.wiki.model import Wiki


class ScannedDeviceManagerTests(TestCase):
    """get_or_create_for_mac is the sole entry point for resolving a device by MAC."""

    def test_creates_a_new_device_normalized(self) -> None:
        device, created = ScannedDevice.objects.get_or_create_for_mac("aa:bb:cc:dd:ee:ff")
        self.assertTrue(created)
        self.assertEqual(device.mac_address, "AA:BB:CC:DD:EE:FF")

    def test_different_separator_styles_resolve_to_the_same_device(self) -> None:
        first, first_created = ScannedDevice.objects.get_or_create_for_mac("aa:bb:cc:dd:ee:ff")
        second, second_created = ScannedDevice.objects.get_or_create_for_mac("AA-BB-CC-DD-EE-FF")

        self.assertTrue(first_created)
        self.assertFalse(second_created)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(ScannedDevice.objects.count(), 1)


class DeviceScanUploadQuerySetTests(TestCase):
    """pending() surfaces only unprocessed uploads."""

    def test_pending_excludes_processed_and_failed(self) -> None:
        pending = DeviceScanUpload.objects.create(status=ScanUploadStatus.PENDING)
        DeviceScanUpload.objects.create(status=ScanUploadStatus.PROCESSED)
        DeviceScanUpload.objects.create(status=ScanUploadStatus.FAILED)

        self.assertEqual(list(DeviceScanUpload.objects.pending()), [pending])


class DeviceScanEntryQuerySetTests(TestCase):
    """for_device() spans every upload, not just the most recent one."""

    def test_for_device_spans_multiple_uploads(self) -> None:
        device, _created = ScannedDevice.objects.get_or_create_for_mac("AA:BB:CC:DD:EE:FF")
        other_device, _created = ScannedDevice.objects.get_or_create_for_mac("11:22:33:44:55:66")
        upload_one = DeviceScanUpload.objects.create()
        upload_two = DeviceScanUpload.objects.create()
        entry_one = DeviceScanEntry.objects.create(
            upload=upload_one, device=device, location=Point(0.0, 0.0, srid=4326)
        )
        entry_two = DeviceScanEntry.objects.create(
            upload=upload_two, device=device, location=Point(0.0, 0.0, srid=4326)
        )
        DeviceScanEntry.objects.create(upload=upload_one, device=other_device, location=Point(0.0, 0.0, srid=4326))

        result = set(DeviceScanEntry.objects.for_device(device).values_list("pk", flat=True))
        self.assertEqual(result, {entry_one.pk, entry_two.pk})


class WikiDeviceMarkerQuerySetTests(TestCase):
    """visible()/for_wiki_and_device()/near() status- and distance-filtering."""

    def setUp(self) -> None:
        super().setUp()
        self.location = Location.objects.create(latitude=0.0, longitude=0.0)
        self.wiki = baker.make(Wiki, location=self.location)
        self.device, _created = ScannedDevice.objects.get_or_create_for_mac("AA:BB:CC:DD:EE:FF")

    def _make_marker(self, *, status: str, lng: float = 0.0, lat: float = 0.0) -> WikiDeviceMarker:
        return WikiDeviceMarker.objects.create(
            wiki=self.wiki,
            device=self.device,
            status=status,
            centroid=Point(lng, lat, srid=4326),
            first_observed_at=timezone.now(),
            last_observed_at=timezone.now(),
        )

    def test_visible_includes_only_active_and_stale(self) -> None:
        active = self._make_marker(status=MarkerStatus.ACTIVE)
        stale = self._make_marker(status=MarkerStatus.STALE)
        self._make_marker(status=MarkerStatus.PRESUMED_REMOVED)
        self._make_marker(status=MarkerStatus.DISMISSED)

        self.assertEqual(set(WikiDeviceMarker.objects.visible().values_list("pk", flat=True)), {active.pk, stale.pk})

    def test_for_wiki_and_device_excludes_only_dismissed(self) -> None:
        active = self._make_marker(status=MarkerStatus.ACTIVE)
        presumed_removed = self._make_marker(status=MarkerStatus.PRESUMED_REMOVED)
        self._make_marker(status=MarkerStatus.DISMISSED)

        result = set(WikiDeviceMarker.objects.for_wiki_and_device(self.wiki, self.device).values_list("pk", flat=True))
        self.assertEqual(result, {active.pk, presumed_removed.pk})

    def test_near_excludes_markers_outside_the_radius(self) -> None:
        nearby = self._make_marker(status=MarkerStatus.ACTIVE, lng=0.0001, lat=0.0)
        self._make_marker(status=MarkerStatus.ACTIVE, lng=1.0, lat=0.0)

        result = list(WikiDeviceMarker.objects.near(Point(0.0, 0.0, srid=4326), 100.0))
        self.assertEqual([marker.pk for marker in result], [nearby.pk])


class ProfileTrackDeviceScansTests(SimpleTestCase):
    """Profile.track_device_scans defaults on, matching its sibling toggles."""

    def test_field_defaults_to_true(self) -> None:
        field = Profile._meta.get_field("track_device_scans")
        self.assertTrue(field.default)


class FrontendModelIdentityTests(TestCase):
    """Upload/device/marker rows expose an opaque uuid, never a raw pk, externally."""

    def test_scanned_device_has_a_uuid(self) -> None:
        device, _created = ScannedDevice.objects.get_or_create_for_mac("AA:BB:CC:DD:EE:FF")
        self.assertIsNotNone(device.uuid)

    def test_device_scan_upload_has_a_uuid(self) -> None:
        upload = DeviceScanUpload.objects.create()
        self.assertIsNotNone(upload.uuid)

    def test_wiki_device_marker_has_a_uuid(self) -> None:
        location = Location.objects.create(latitude=0.0, longitude=0.0)
        wiki = baker.make(Wiki, location=location)
        device, _created = ScannedDevice.objects.get_or_create_for_mac("AA:BB:CC:DD:EE:FF")
        marker = WikiDeviceMarker.objects.create(
            wiki=wiki,
            device=device,
            centroid=Point(0.0, 0.0, srid=4326),
            first_observed_at=timezone.now(),
            last_observed_at=timezone.now(),
        )
        self.assertIsNotNone(marker.uuid)

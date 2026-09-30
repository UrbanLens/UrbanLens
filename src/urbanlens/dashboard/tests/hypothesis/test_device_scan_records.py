"""Each scan is its own record; a device's type, name and markers are a summary of all of them (Jess, 2026-09-30).

One account can add evidence but cannot overwrite anyone else's, and repeating itself counts once.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from django.contrib.gis.geos import MultiPolygon, Point, Polygon
from django.utils import timezone
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.device_scan.model import (
    DeviceScanEntry,
    DeviceScanUpload,
    DeviceType,
    DeviceTypeSource,
    MarkerStatus,
    ScannedDevice,
    WikiDeviceMarker,
)
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.services.device_scan.clustering import ABSENCE_REPORTERS_THRESHOLD, confidence_for_weight
from urbanlens.dashboard.services.device_scan.ingestion import ingest_scan_upload
from urbanlens.dashboard.services.device_scan.pipeline import process_scan_upload

from .place_helpers import official_geometry

_MAC = "AA:BB:CC:DD:EE:FF"


def _square(lng: float, lat: float, delta: float) -> MultiPolygon:
    ring = (
        (lng - delta, lat - delta),
        (lng + delta, lat - delta),
        (lng + delta, lat + delta),
        (lng - delta, lat + delta),
        (lng - delta, lat - delta),
    )
    return MultiPolygon(Polygon(ring, srid=4326), srid=4326)


class _ScanCase(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.location = Location.objects.create(latitude=0.0, longitude=0.0)
        official_geometry(self.location, _square(0.0, 0.0, 0.01))
        self.wiki = baker.make(Wiki, location=self.location)

    def _account(self, *, sees_the_wiki: bool = True) -> Profile:
        profile = baker.make(User).profile
        if sees_the_wiki:
            baker.make(Pin, profile=profile, location=self.location, parent_pin=None)
        return profile

    def _scan(
        self,
        uploader: Profile,
        *,
        guess: str | None = DeviceType.CAMERA,
        name: str = "",
        detected: bool = True,
        attribute: bool = True,
        expected_marker: WikiDeviceMarker | None = None,
    ) -> DeviceScanUpload:
        upload, _created = ingest_scan_upload(
            uploader,
            attribute=attribute,
            client_session_uuid="",
            devices=[
                {
                    "mac_address": _MAC,
                    "device_name": name,
                    "device_type_guess": guess,
                    "detected": detected,
                    "estimated_latitude": 0.0,
                    "estimated_longitude": 0.0,
                    "expected_marker_uuid": expected_marker.uuid if expected_marker else None,
                    "readings": [],
                }
            ],
        )
        process_scan_upload(upload)
        return upload

    def _device(self) -> ScannedDevice:
        return ScannedDevice.objects.get(mac_address=_MAC)


class TheTypeIsASummaryTests(_ScanCase):
    def test_one_account_repeating_itself_cannot_outvote_two_others(self) -> None:
        spammer, first, second = self._account(), self._account(), self._account()
        self._scan(first, guess=DeviceType.CAMERA)
        self._scan(second, guess=DeviceType.CAMERA)
        for _ in range(5):
            self._scan(spammer, guess=DeviceType.SENSOR)

        self.assertEqual(
            (self._device().device_type, self._device().device_type_source),
            (DeviceType.CAMERA, DeviceTypeSource.CLIENT),
        )

    def test_a_later_guess_does_not_overwrite_what_others_reported(self) -> None:
        for account in (self._account(), self._account()):
            self._scan(account, guess=DeviceType.CAMERA)
        self._scan(self._account(), guess=DeviceType.PHONE)

        self.assertEqual(self._device().device_type, DeviceType.CAMERA)

    def test_unknown_is_not_a_vote(self) -> None:
        self._scan(self._account(), guess=DeviceType.CAMERA)
        for account in (self._account(), self._account()):
            self._scan(account, guess=DeviceType.UNKNOWN)

        self.assertEqual(self._device().device_type, DeviceType.CAMERA)

    def test_each_scan_keeps_what_it_reported(self) -> None:
        self._scan(self._account(), guess=DeviceType.CAMERA, name="Front door cam")
        self._scan(self._account(), guess=DeviceType.SENSOR, name="Garage")

        self.assertEqual(
            sorted(DeviceScanEntry.objects.values_list("device_type_guess", "device_name")),
            [(DeviceType.CAMERA, "Front door cam"), (DeviceType.SENSOR, "Garage")],
        )

    def test_the_name_shown_is_the_one_most_people_reported(self) -> None:
        self._scan(self._account(), name="Front Door Cam")
        self._scan(self._account(), name="Front Door Cam")
        self._scan(self._account(), name="totally not a camera")

        self.assertEqual(self._device().display_name, "Front Door Cam")

    def test_the_heuristic_applies_only_when_nobody_has_said(self) -> None:
        self._scan(self._account(), guess=None, name="Reolink doorbell")
        self.assertEqual(
            (self._device().device_type, self._device().device_type_source),
            (DeviceType.CAMERA, DeviceTypeSource.HEURISTIC),
        )

        self._scan(self._account(), guess=DeviceType.PHONE)
        self.assertEqual(
            (self._device().device_type, self._device().device_type_source), (DeviceType.PHONE, DeviceTypeSource.CLIENT)
        )


class MarkerEvidenceTests(_ScanCase):
    def _marker(self) -> WikiDeviceMarker:
        return WikiDeviceMarker.objects.get(wiki=self.wiki, device=self._device())

    def test_repeated_scans_from_one_account_count_once(self) -> None:
        account = self._account()
        for _ in range(5):
            self._scan(account)

        self.assertAlmostEqual(self._marker().confidence, confidence_for_weight(1.0), places=2)

    def test_a_second_account_raises_the_confidence(self) -> None:
        self._scan(self._account())
        self._scan(self._account())

        self.assertAlmostEqual(self._marker().confidence, confidence_for_weight(2.0), places=2)

    def test_no_marker_lands_on_a_wiki_the_uploader_cannot_see(self) -> None:
        self._scan(self._account(sees_the_wiki=False))

        self.assertFalse(WikiDeviceMarker.objects.exists())

    def test_an_unattributed_scan_still_routes_where_its_uploader_could_see(self) -> None:
        upload = self._scan(self._account(), attribute=False)

        self.assertIsNone(DeviceScanUpload.objects.get(pk=upload.pk).profile_id)
        self.assertTrue(WikiDeviceMarker.objects.filter(wiki=self.wiki).exists())

    def test_scans_from_someone_who_cannot_see_the_wiki_add_nothing_to_its_marker(self) -> None:
        self._scan(self._account())
        for _ in range(3):
            self._scan(self._account(sees_the_wiki=False))

        self.assertAlmostEqual(self._marker().confidence, confidence_for_weight(1.0), places=2)


class AbsenceReportTests(_ScanCase):
    def setUp(self) -> None:
        super().setUp()
        self._scan(self._account())
        self.marker = WikiDeviceMarker.objects.get(wiki=self.wiki)
        WikiDeviceMarker.objects.filter(pk=self.marker.pk).update(last_observed_at=timezone.now())

    def test_one_account_reporting_absence_over_and_over_does_not_remove_a_marker(self) -> None:
        account = self._account()
        for _ in range(12):
            self._scan(account, detected=False, expected_marker=self.marker)

        self.marker.refresh_from_db()
        self.assertEqual((self.marker.status, self.marker.absence_streak), (MarkerStatus.ACTIVE, 1))

    def test_enough_different_accounts_do(self) -> None:
        for _ in range(ABSENCE_REPORTERS_THRESHOLD):
            self._scan(self._account(), detected=False, expected_marker=self.marker)

        self.marker.refresh_from_db()
        self.assertEqual(self.marker.status, MarkerStatus.PRESUMED_REMOVED)

    def test_a_report_without_a_marker_uuid_finds_the_one_nearby(self) -> None:
        self._scan(self._account(), detected=False)

        self.marker.refresh_from_db()
        self.assertEqual(self.marker.absence_streak, 1)

    def test_an_absence_report_about_a_wiki_the_reporter_cannot_see_is_ignored(self) -> None:
        for _ in range(ABSENCE_REPORTERS_THRESHOLD):
            self._scan(self._account(sees_the_wiki=False), detected=False, expected_marker=self.marker)

        self.marker.refresh_from_db()
        self.assertEqual((self.marker.status, self.marker.absence_streak), (MarkerStatus.ACTIVE, 0))

    def test_a_fresh_sighting_clears_earlier_absence_reports(self) -> None:
        self._scan(self._account(), detected=False, expected_marker=self.marker)
        self._scan(self._account())

        self.marker.refresh_from_db()
        self.assertEqual((self.marker.status, self.marker.absence_streak), (MarkerStatus.ACTIVE, 0))
        self.assertEqual(Point(0.0, 0.0, srid=4326).distance(self.marker.centroid) < 0.001, True)

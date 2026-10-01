"""Deleting an account keeps the device scans it uploaded, detached from it: device scans are never deleted (Jess, 2026-09-29)."""

from __future__ import annotations

import datetime

from django.contrib.auth.models import User
from django.contrib.gis.geos import Point
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.device_scan.model import (
    DeviceScanEntry,
    DeviceScanUpload,
    DeviceSignalReading,
    ScannedDevice,
    WikiDeviceMarker,
)
from urbanlens.dashboard.services.profile.account_deletion import hard_delete_profile

_WHEN = datetime.datetime(2026, 5, 1, 12, tzinfo=datetime.UTC)


def _trail(profile, device: ScannedDevice) -> DeviceScanUpload:
    upload = baker.make(DeviceScanUpload, profile=profile)
    entry = baker.make(DeviceScanEntry, upload=upload, device=device, location=Point(-73.75, 42.65, srid=4326))
    baker.make(
        DeviceSignalReading,
        entry=entry,
        point=Point(-73.7501, 42.6501, srid=4326),
        signal_strength=-60,
        observed_at=_WHEN,
    )
    return upload


class DeviceScanAccountDeletionTests(TestCase):
    def test_the_deleted_accounts_scans_stay_without_it(self) -> None:
        baker.make(User)
        leaving = baker.make(User).profile
        staying = baker.make(User).profile
        device = baker.make(ScannedDevice)
        wiki = baker.make_recipe("dashboard.wiki")
        marker = baker.make(
            WikiDeviceMarker,
            wiki=wiki,
            device=device,
            centroid=Point(-73.75, 42.65, srid=4326),
            first_observed_at=_WHEN,
            last_observed_at=_WHEN,
        )
        detached = _trail(leaving, device)
        kept = _trail(staying, device)

        hard_delete_profile(leaving)

        detached.refresh_from_db()
        self.assertIsNone(detached.profile_id)
        self.assertTrue(DeviceSignalReading.objects.filter(entry__upload_id=detached.pk).exists())
        self.assertTrue(DeviceScanUpload.objects.filter(pk=kept.pk, profile=staying).exists())
        self.assertTrue(WikiDeviceMarker.objects.filter(pk=marker.pk).exists())

    def test_nothing_left_on_the_scans_ties_them_to_the_person(self) -> None:
        """Jess, 2026-09-30: fully anonymised. Times and routes may stay; the "who" may not."""
        baker.make(User)
        leaving = baker.make(User).profile
        device = baker.make(ScannedDevice)
        upload = _trail(leaving, device)
        DeviceScanUpload.objects.filter(pk=upload.pk).update(client_session_uuid="phone-install-7f3a")

        hard_delete_profile(leaving)

        upload.refresh_from_db()
        self.assertEqual((upload.profile_id, upload.client_session_uuid), (None, ""))
        self.assertTrue(DeviceSignalReading.objects.filter(entry__upload_id=upload.pk).exists())

    def test_any_way_of_deleting_the_account_anonymises_them(self) -> None:
        baker.make(User)
        user = baker.make(User)
        upload = _trail(user.profile, baker.make(ScannedDevice))
        DeviceScanUpload.objects.filter(pk=upload.pk).update(client_session_uuid="phone-install-7f3a")

        user.delete()

        upload.refresh_from_db()
        self.assertEqual((upload.profile_id, upload.client_session_uuid), (None, ""))

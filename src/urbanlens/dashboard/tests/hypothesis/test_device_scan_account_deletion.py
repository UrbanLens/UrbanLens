"""Deleting an account deletes the device-scan trails it uploaded (N29 G4 incidental).

``DeviceScanUpload.profile`` was SET_NULL, so a deleted user's timestamped readings along their route stayed behind,
merely unattributed. The community markers derived from them are aggregates and stay.
"""

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
    def test_the_deleted_accounts_trails_go_and_everyone_elses_stay(self) -> None:
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
        gone = _trail(leaving, device)
        kept = _trail(staying, device)
        anonymous = _trail(None, device)

        hard_delete_profile(leaving)

        self.assertFalse(DeviceScanUpload.objects.filter(pk=gone.pk).exists())
        self.assertFalse(DeviceScanEntry.objects.filter(upload_id=gone.pk).exists())
        self.assertFalse(DeviceSignalReading.objects.filter(entry__upload_id=gone.pk).exists())
        self.assertEqual(set(DeviceScanUpload.objects.values_list("pk", flat=True)), {kept.pk, anonymous.pk})
        self.assertEqual(DeviceSignalReading.objects.count(), 2)
        self.assertTrue(
            WikiDeviceMarker.objects.filter(pk=marker.pk).exists(), "the community marker is not personal data"
        )
        self.assertTrue(ScannedDevice.objects.filter(pk=device.pk).exists())

"""Nightly retention sweeps delete what SiteSettings no longer keeps, and nothing else."""

from __future__ import annotations

from datetime import timedelta
from importlib import import_module

from django.conf import settings
from django.contrib.auth.models import User
from django.contrib.gis.geos import Point
from django.utils import timezone
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard import tasks
from urbanlens.dashboard.models.device_scan.model import (
    DeviceScanEntry,
    DeviceScanUpload,
    DeviceSignalReading,
    ScannedDevice,
)
from urbanlens.dashboard.models.notifications.meta import Status
from urbanlens.dashboard.models.notifications.model import NotificationLog
from urbanlens.dashboard.models.site_settings import SiteSettings


def _set(**fields) -> None:
    SiteSettings.objects.filter(pk=SiteSettings.get_current().pk).update(**fields)


class SessionPruningTests(TestCase):
    def test_expired_sessions_go_and_live_ones_stay(self) -> None:
        from django.contrib.sessions.models import Session

        store_class = import_module(settings.SESSION_ENGINE).SessionStore
        expired, live = store_class(), store_class()
        expired.set_expiry(1)
        expired.create()
        live.create()
        Session.objects.filter(session_key=expired.session_key).update(expire_date=timezone.now() - timedelta(days=1))

        tasks.prune_expired_sessions()

        self.assertFalse(Session.objects.filter(session_key=expired.session_key).exists())
        self.assertTrue(Session.objects.filter(session_key=live.session_key).exists())


class ReadNotificationPruningTests(TestCase):
    def setUp(self) -> None:
        self.profile = baker.make(User).profile

    def _notification(self, *, status: str, age_days: int) -> NotificationLog:
        notification = NotificationLog.objects.create(profile=self.profile, status=status, title="t")
        NotificationLog.objects.filter(pk=notification.pk).update(created=timezone.now() - timedelta(days=age_days))
        return notification

    def test_old_read_notifications_go_and_everything_else_stays(self) -> None:
        _set(notification_retention_days=30)
        old_read = self._notification(status=Status.READ, age_days=31)
        new_read = self._notification(status=Status.READ, age_days=29)
        old_unread = self._notification(status=Status.UNREAD, age_days=400)

        self.assertEqual(tasks.prune_read_notifications(), 1)

        remaining = set(NotificationLog.objects.values_list("pk", flat=True))
        self.assertNotIn(old_read.pk, remaining)
        self.assertTrue({new_read.pk, old_unread.pk} <= remaining)

    def test_zero_keeps_them_forever(self) -> None:
        _set(notification_retention_days=0)
        self._notification(status=Status.READ, age_days=10_000)

        self.assertEqual(tasks.prune_read_notifications(), 0)

    def test_the_default_is_a_year(self) -> None:
        self.assertEqual(SiteSettings._meta.get_field("notification_retention_days").default, 365)


class DeviceScanPruningTests(TestCase):
    def _upload(self, *, age_days: int) -> DeviceScanUpload:
        upload = DeviceScanUpload.objects.create()
        device, _ = ScannedDevice.objects.get_or_create_for_mac(f"AA:BB:CC:DD:EE:{upload.pk % 100:02d}")
        entry = DeviceScanEntry.objects.create(upload=upload, device=device, location=Point(0.0, 0.0, srid=4326))
        for _ in range(3):
            DeviceSignalReading.objects.create(
                entry=entry, point=Point(0.0, 0.0, srid=4326), observed_at=timezone.now()
            )
        DeviceScanUpload.objects.filter(pk=upload.pk).update(created=timezone.now() - timedelta(days=age_days))
        return upload

    def test_old_uploads_go_with_their_entries_and_readings(self) -> None:
        _set(device_scan_retention_days=100)
        old = self._upload(age_days=101)
        new = self._upload(age_days=99)

        self.assertEqual(tasks.prune_device_scan_uploads(), 1)

        self.assertFalse(DeviceScanUpload.objects.filter(pk=old.pk).exists())
        self.assertFalse(DeviceScanEntry.objects.filter(upload_id=old.pk).exists())
        self.assertFalse(DeviceSignalReading.objects.filter(entry__upload_id=old.pk).exists())
        self.assertEqual(DeviceSignalReading.objects.filter(entry__upload=new).count(), 3)

    def test_zero_keeps_them_forever(self) -> None:
        _set(device_scan_retention_days=0)
        self._upload(age_days=10_000)

        self.assertEqual(tasks.prune_device_scan_uploads(), 0)

    def test_the_default_outlasts_the_clustering_lookback(self) -> None:
        from urbanlens.dashboard.services.device_scan.clustering import LOOKBACK_DAYS

        self.assertGreater(SiteSettings._meta.get_field("device_scan_retention_days").default, LOOKBACK_DAYS)


class RetentionScheduleTests(TestCase):
    def test_every_sweep_is_on_the_beat_schedule(self) -> None:
        names = {entry["task"] for entry in settings.CELERY_BEAT_SCHEDULE.values()}
        for task in (tasks.prune_expired_sessions, tasks.prune_read_notifications, tasks.prune_device_scan_uploads):
            self.assertIn(task.name, names)

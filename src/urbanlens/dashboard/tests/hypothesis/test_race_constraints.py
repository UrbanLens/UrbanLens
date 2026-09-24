"""Check-then-insert paths with a unique constraint behind them (G5-4, G4-15, G2-35, G4-13 dedupe half)."""

from __future__ import annotations

import importlib
from unittest import mock
import uuid

from django.contrib.auth.models import User
from django.db import IntegrityError, transaction
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.models.device_scan.model import DeviceScanUpload
from urbanlens.dashboard.models.markup.share import MarkupMapShare
from urbanlens.dashboard.models.notifications.meta import DeliveryPreference, NotificationType
from urbanlens.dashboard.models.notifications.model import NotificationLog
from urbanlens.dashboard.models.pin_suggestions.model import PinSuggestion, PinSuggestionOrigin
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.public_pins.model import PublicPinCandidate, PublicPinCandidateStatus
from urbanlens.dashboard.models.visits.model import PinVisit, VisitSource
from urbanlens.dashboard.services.pins.pin_creation import create_pin_for_profile
from urbanlens.dashboard.services.visits import visits as visit_service


def _profile() -> Profile:
    return Profile.objects.get(user=baker.make(User))


class GeolocationVisitDedupeTests(TestCase):
    """G5-4: two overlapping pings both passed the "visited today?" read and both inserted."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)
        self.profile = _profile()
        self.pin = create_pin_for_profile(self.profile, name="Old Mill", latitude=42.65, longitude=-73.76).pin
        patch = mock.patch.object(visit_service, "geolocation_tracking_allowed", return_value=True)
        patch.start()
        self.addCleanup(patch.stop)

    def _ping(self) -> list[PinVisit]:
        return visit_service.record_geolocation_pin_visits(self.profile, latitude=42.65, longitude=-73.76)

    def test_a_ping_overlapping_another_records_one_visit(self) -> None:
        nested: list[list[PinVisit]] = []
        started: list[bool] = []

        def contains_then_overlap(pin, point):  # noqa: ANN001, ANN202
            if not started:
                started.append(True)
                # The second ping arrives after this one read "not visited today".
                nested.append(self._ping())
            return True

        with mock.patch.object(visit_service, "_pin_contains_point", side_effect=contains_then_overlap):
            outer = self._ping()

        self.assertEqual(PinVisit.objects.filter(pin=self.pin, source=VisitSource.GEOLOCATION).count(), 1)
        self.assertEqual(len(outer) + len(nested[0]), 1, "both pings reported a new visit")

    def test_the_database_refuses_a_second_automatic_visit_the_same_day(self) -> None:
        from django.utils import timezone

        now = timezone.now()
        PinVisit.objects.create(pin=self.pin, visited_at=now, source=VisitSource.GEOLOCATION)
        with self.assertRaises(IntegrityError), transaction.atomic():
            PinVisit.objects.create(pin=self.pin, visited_at=now, source=VisitSource.GEOLOCATION)

    def test_manual_visits_are_not_limited(self) -> None:
        from django.utils import timezone

        now = timezone.now()
        for _ in range(2):
            PinVisit.objects.create(pin=self.pin, visited_at=now, source=VisitSource.MANUAL)
        self.assertEqual(PinVisit.objects.filter(pin=self.pin).count(), 2)


class PublicPinSuggestionDedupeTests(TestCase):
    """G4-15: overlapping sweeps both read "not yet suggested" and both inserted."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)
        self.location = baker.make("dashboard.Location")
        baker.make("dashboard.Wiki", location=self.location, name="Old Mill")
        baker.make(PublicPinCandidate, location=self.location, status=PublicPinCandidateStatus.PASSED)
        self.recipient = _profile()

    def test_a_stale_already_suggested_read_does_not_duplicate(self) -> None:
        from urbanlens.dashboard.services.pins import public_pins

        public_pins.sync_public_pin_suggestions()
        with mock.patch.object(public_pins.PinSuggestion.objects, "filter", return_value=PinSuggestion.objects.none()):
            public_pins.sync_public_pin_suggestions()

        rows = PinSuggestion.objects.filter(
            profile=self.recipient, location=self.location, origin=PinSuggestionOrigin.COMMUNITY
        )
        self.assertEqual(rows.count(), 1)

    def test_the_task_skips_while_a_run_holds_the_lock(self) -> None:
        from urbanlens.dashboard import tasks
        from urbanlens.dashboard.services.core.locks import acquire_lock, release_lock

        token = acquire_lock(tasks.PUBLIC_PIN_EVALUATION_LOCK_KEY, 60)
        self.addCleanup(release_lock, tasks.PUBLIC_PIN_EVALUATION_LOCK_KEY, token)
        with mock.patch("urbanlens.dashboard.services.pins.public_pins.evaluate_public_pin_candidates") as run:
            self.assertEqual(tasks.evaluate_public_pin_candidates(), {})
        run.assert_not_called()


class MapShareDedupeTests(TestCase):
    """G2-35: every submit inserted another share and another notification, ignoring preferences."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)
        self.sender = _profile()
        self.recipient = _profile()
        self.map = baker.make("dashboard.MarkupMap", profile=self.sender)
        self.client.force_login(self.sender.user)
        connected = mock.patch("urbanlens.dashboard.services.social.connections.are_connections", return_value=True)
        connected.start()
        self.addCleanup(connected.stop)

    def _send(self, message: str = "") -> int:
        url = reverse("markup_map.share.send", kwargs={"map_uuid": self.map.uuid})
        return self.client.post(url, {"profile_id": self.recipient.pk, "message": message}).status_code

    def _notifications(self) -> int:
        return NotificationLog.objects.filter(
            profile=self.recipient, notification_type=NotificationType.MAP_SHARED
        ).count()

    def test_sending_twice_keeps_one_share_and_one_notification(self) -> None:
        self.assertEqual(self._send("first"), 200)
        self.assertEqual(self._send("second"), 200)
        shares = MarkupMapShare.objects.filter(markup_map=self.map, to_profile=self.recipient)
        self.assertEqual(shares.count(), 1)
        self.assertEqual(shares.get().message, "second")
        self.assertEqual(self._notifications(), 1)

    def test_a_recipient_who_turned_share_notices_off_gets_none(self) -> None:
        from urbanlens.dashboard.models.notifications.model import NotificationPreference

        NotificationPreference.objects.update_or_create(
            profile=self.recipient, defaults={"pin_shared": DeliveryPreference.NONE}
        )
        self.assertEqual(self._send(), 200)
        self.assertEqual(MarkupMapShare.objects.filter(markup_map=self.map).count(), 1)
        self.assertEqual(self._notifications(), 0)

    def test_someone_elses_map_is_refused(self) -> None:
        from urbanlens.dashboard.services.sharing.map_sharing import MapSharePermissionError, share_markup_map

        with self.assertRaises(MapSharePermissionError):
            share_markup_map(self.recipient, self.sender, self.map)


class DeviceScanReplayTests(TestCase):
    """G4-13 (dedupe half): a retried upload stored every entry and reading a second time."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)
        self.profile = _profile()

    def _upload(self, token: str):  # noqa: ANN202
        from urbanlens.dashboard.services.device_scan.ingestion import ingest_scan_upload

        device = {
            "mac_address": "AA:BB:CC:DD:EE:FF",
            "detected": True,
            "estimated_latitude": 42.65,
            "estimated_longitude": -73.76,
            "readings": [],
        }
        return ingest_scan_upload(self.profile, client_session_uuid=token, devices=[device])

    def test_a_replay_returns_the_original_upload(self) -> None:
        token = str(uuid.uuid4())
        first, created = self._upload(token)
        again, replayed = self._upload(token)
        self.assertTrue(created)
        self.assertFalse(replayed)
        self.assertEqual(first.pk, again.pk)
        self.assertEqual(DeviceScanUpload.objects.filter(client_session_uuid=token).count(), 1)

    def test_a_replay_that_loses_the_race_returns_the_winner(self) -> None:
        from urbanlens.dashboard.services.device_scan import ingestion

        token = str(uuid.uuid4())
        winner, _created = self._upload(token)
        with mock.patch.object(
            ingestion.DeviceScanUpload.objects, "filter", return_value=DeviceScanUpload.objects.none()
        ):
            loser, created = self._upload(token)
        self.assertFalse(created)
        self.assertEqual(loser.pk, winner.pk)

    def test_uploads_without_a_token_are_never_merged(self) -> None:
        self._upload("")
        self._upload("")
        self.assertEqual(DeviceScanUpload.objects.filter(client_session_uuid="").count(), 2)


class TheDedupeMigrationKeepsTheFirstTests(SimpleTestCase):
    def test_later_rows_of_each_key_are_marked(self) -> None:
        migration = importlib.import_module("urbanlens.dashboard.migrations.0073_dedupe_rows_before_race_constraints")
        rows = [(1, "a", 1), (2, "a", 1), (3, "a", 2), (4, "b", 1), (5, "b", 1), (6, "b", 1)]
        self.assertEqual(migration._later_duplicates(iter(rows)), [2, 5, 6])

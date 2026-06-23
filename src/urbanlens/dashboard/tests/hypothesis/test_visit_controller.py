"""Tests for visit-history HTMX controller behavior."""

from __future__ import annotations

from datetime import UTC, datetime

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.badges.model import KIND_STATUS, Badge
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.visits.model import PinVisit, VisitSource


class VisitHistoryControllerTests(TestCase):
    """Manual visit logging must preserve user-owned visit history correctly."""

    def setUp(self) -> None:
        super().setUp()
        self.user: User = baker.make(User)
        self.profile = self.user.profile
        self.pin: Pin = baker.make(Pin, profile=self.profile)
        self.client.force_login(self.user)

    def _visits_url(self, pin: Pin | None = None) -> str:
        return reverse("pin.visits", kwargs={"pin_uuid": (pin or self.pin).uuid})

    def _delete_url(self, visit: PinVisit, pin: Pin | None = None) -> str:
        return reverse("pin.visit.delete", kwargs={"pin_uuid": (pin or self.pin).uuid, "visit_id": visit.pk})

    def test_post_creates_manual_visit_and_syncs_pin_state(self) -> None:
        visited_badge = baker.make(Badge, profile=self.profile, kind=KIND_STATUS, name="Visited")

        response = self.client.post(
            self._visits_url(),
            {
                "visited_date": "2026-06-20",
                "visited_time": "14:30",
                "notes": "Checked the north entrance.",
            },
        )

        self.assertEqual(response.status_code, 200)
        visit = PinVisit.objects.get(pin=self.pin)
        self.assertEqual(visit.source, VisitSource.MANUAL)
        self.assertEqual(visit.visited_at, datetime(2026, 6, 20, 14, 30, tzinfo=UTC))
        self.assertEqual(visit.notes, "Checked the north entrance.")

        self.pin.refresh_from_db()
        self.assertEqual(self.pin.last_visited, visit.visited_at)
        self.assertTrue(self.pin.statuses.filter(pk=visited_badge.pk).exists())

    def test_post_without_date_returns_400_without_creating_visit(self) -> None:
        response = self.client.post(self._visits_url(), {"visited_time": "14:30"})

        self.assertEqual(response.status_code, 400)
        self.assertFalse(PinVisit.objects.filter(pin=self.pin).exists())

    def test_post_for_another_users_pin_returns_404_without_creating_visit(self) -> None:
        other_user: User = baker.make(User)
        other_pin: Pin = baker.make(Pin, profile=other_user.profile)

        response = self.client.post(
            self._visits_url(other_pin),
            {"visited_date": "2026-06-20", "visited_time": "14:30"},
        )

        self.assertEqual(response.status_code, 404)
        self.assertFalse(PinVisit.objects.filter(pin=other_pin).exists())

    def test_delete_visit_resyncs_last_visited_to_latest_remaining_visit(self) -> None:
        older_visit = baker.make(
            PinVisit,
            pin=self.pin,
            visited_at=datetime(2026, 6, 1, 9, 0, tzinfo=UTC),
            source=VisitSource.MANUAL,
        )
        newer_visit = baker.make(
            PinVisit,
            pin=self.pin,
            visited_at=datetime(2026, 6, 20, 14, 30, tzinfo=UTC),
            source=VisitSource.MANUAL,
        )
        self.pin.last_visited = newer_visit.visited_at
        self.pin.save(update_fields=["last_visited"])

        response = self.client.post(self._delete_url(newer_visit))

        self.assertEqual(response.status_code, 200)
        self.assertFalse(PinVisit.objects.filter(pk=newer_visit.pk).exists())
        self.pin.refresh_from_db()
        self.assertEqual(self.pin.last_visited, older_visit.visited_at)

    def test_delete_for_another_users_pin_returns_404_without_deleting_visit(self) -> None:
        other_user: User = baker.make(User)
        other_pin: Pin = baker.make(Pin, profile=other_user.profile)
        visit = baker.make(
            PinVisit,
            pin=other_pin,
            visited_at=datetime(2026, 6, 20, 14, 30, tzinfo=UTC),
            source=VisitSource.MANUAL,
        )

        response = self.client.post(self._delete_url(visit, other_pin))

        self.assertEqual(response.status_code, 404)
        self.assertTrue(PinVisit.objects.filter(pk=visit.pk).exists())

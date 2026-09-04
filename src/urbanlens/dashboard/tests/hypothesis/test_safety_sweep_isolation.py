"""Tests that one bad safety check-in cannot suppress everyone else's escalation.

The three safety beat tasks each loop over a queryset and call a per-check-in
service. An exception from any one of them used to abort the whole run, and
``SafetyCheckin`` has a deterministic ``ordering``, so a row that fails
repeatably - corrupt contact data, a template that won't render, an address the
mail backend rejects - would fail at the same position on every tick and every
check-in behind it would never escalate.

That failure mode is silent and unbounded: the sweep just returns early, and the
people whose emergency contacts were never called have no way to know.
"""

from __future__ import annotations

from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import User
from django.test import override_settings
from django.utils import timezone
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.safety.model import SafetyCheckin, SafetyCheckinStatus
from urbanlens.dashboard.tasks import escalate_overdue_checkins, send_due_checkin_reminders, send_final_checkin_warnings


@override_settings(CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}})
class SafetySweepIsolationTests(TestCase):
    """Each sweep takes a distributed lock through the cache, so the cache is pinned to
    locmem here - the default backend is Redis-backed and the suite's network guard only
    permits localhost."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.profile = baker.make(User).profile
        self.now = timezone.now()

    def _checkin(self, *, title: str, checkin_by, status: str, **extra) -> SafetyCheckin:
        return baker.make(
            SafetyCheckin,
            profile=self.profile,
            title=title,
            checkin_by=checkin_by,
            grace_period=timedelta(hours=1),
            status=status,
            notify_community_wiki=False,
            **extra,
        )

    def test_one_failing_escalation_does_not_suppress_the_others(self) -> None:
        # Ordering is "-checkin_by", so the newer row is visited first - make that one fail.
        self._checkin(
            title="poisoned", checkin_by=self.now - timedelta(hours=2), status=SafetyCheckinStatus.AWAITING_CHECKIN
        )
        healthy = self._checkin(
            title="healthy", checkin_by=self.now - timedelta(hours=3), status=SafetyCheckinStatus.AWAITING_CHECKIN
        )

        escalated: list[str] = []

        def escalate(checkin: SafetyCheckin) -> None:
            if checkin.title == "poisoned":
                raise ValueError("corrupt contact row")
            escalated.append(checkin.title)
            checkin.status = SafetyCheckinStatus.OVERDUE
            checkin.escalated_at = timezone.now()
            checkin.save(update_fields=["status", "escalated_at", "updated"])

        with mock.patch("urbanlens.dashboard.services.visits.safety.escalate_checkin", side_effect=escalate):
            escalate_overdue_checkins()

        self.assertEqual(escalated, ["healthy"])
        healthy.refresh_from_db()
        self.assertEqual(healthy.status, SafetyCheckinStatus.OVERDUE)

    def test_one_failing_reminder_does_not_suppress_the_others(self) -> None:
        self._checkin(
            title="poisoned", checkin_by=self.now - timedelta(minutes=10), status=SafetyCheckinStatus.SCHEDULED
        )
        self._checkin(
            title="healthy", checkin_by=self.now - timedelta(minutes=20), status=SafetyCheckinStatus.SCHEDULED
        )

        reminded: list[str] = []

        def remind(checkin: SafetyCheckin) -> None:
            if checkin.title == "poisoned":
                raise ValueError("bad email address")
            reminded.append(checkin.title)

        with mock.patch("urbanlens.dashboard.services.visits.safety.send_checkin_reminder", side_effect=remind):
            send_due_checkin_reminders()

        self.assertEqual(reminded, ["healthy"])

    def test_one_failing_final_warning_does_not_suppress_the_others(self) -> None:
        # due_for_final_warning wants overdue_at (checkin_by + grace_period) inside the next
        # FINAL_WARNING_LEAD_TIME (5 minutes), so with a 1-hour grace these sit just under an
        # hour ago. Ordering is "-checkin_by", so the later one is visited first.
        nearly_due = self.now - timedelta(minutes=58)
        self._checkin(
            title="poisoned", checkin_by=nearly_due + timedelta(minutes=1), status=SafetyCheckinStatus.AWAITING_CHECKIN
        )
        self._checkin(title="healthy", checkin_by=nearly_due, status=SafetyCheckinStatus.AWAITING_CHECKIN)

        warned: list[str] = []

        def warn(checkin: SafetyCheckin) -> None:
            if checkin.title == "poisoned":
                raise ValueError("template blew up")
            warned.append(checkin.title)

        with mock.patch("urbanlens.dashboard.services.visits.safety.send_final_warning", side_effect=warn):
            send_final_checkin_warnings()

        self.assertEqual(warned, ["healthy"])

    def test_the_sweep_still_reports_only_what_actually_succeeded(self) -> None:
        """A run that swallowed a failure must not report the failed one as done."""
        self._checkin(
            title="poisoned", checkin_by=self.now - timedelta(hours=2), status=SafetyCheckinStatus.AWAITING_CHECKIN
        )
        self._checkin(
            title="healthy", checkin_by=self.now - timedelta(hours=3), status=SafetyCheckinStatus.AWAITING_CHECKIN
        )

        def escalate(checkin: SafetyCheckin) -> None:
            if checkin.title == "poisoned":
                raise ValueError("corrupt contact row")

        with mock.patch("urbanlens.dashboard.services.visits.safety.escalate_checkin", side_effect=escalate):
            self.assertEqual(escalate_overdue_checkins(), 1)

"""Migration 0067 raises ``google_calendar`` to 120 calls a minute in a row nobody edited, and leaves an edited row alone.

It also clears push marks on links without auto-sync, which no earlier release would have delivered and this one would.

``get_limit_config`` never rewrites a row holding a service's own defaults, so without the migration a deployment
that upgraded would keep 30 a minute, and a trip with 30 or more scheduled activities would need a push to finish
every export (P334).
"""

from __future__ import annotations

import datetime
import importlib
from typing import Any

from django.apps import apps
from django.contrib.auth.models import User
from django.utils import timezone

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.api_rate_limit import ApiRateLimit
from urbanlens.dashboard.models.calendar_sync.model import CalendarSyncDirection, TripCalendarLink
from urbanlens.dashboard.models.trips.model import Trip
from urbanlens.dashboard.plugins.builtin.google_calendar import GoogleCalendarPlugin
from urbanlens.dashboard.services.core import rate_limiter

_MIGRATION = importlib.import_module("urbanlens.dashboard.migrations.0067_calendar_export_resumable")
_SERVICE = "google_calendar"


def _row(**overrides: Any) -> ApiRateLimit:
    return ApiRateLimit.objects.create(service=_SERVICE, **{**_MIGRATION._OLD, **overrides})


def _values(row: ApiRateLimit) -> dict[str, Any]:
    row.refresh_from_db()
    return {field: getattr(row, field) for field in _MIGRATION._FIELDS}


def _forward() -> None:
    _MIGRATION.raise_calendar_minute_limit(apps, None)


class CalendarMinuteLimitMigrationTests(TestCase):
    def test_the_migration_writes_what_the_plugin_now_registers(self) -> None:
        defaults = GoogleCalendarPlugin().get_service_defaults()[_SERVICE]
        self.assertEqual(
            {"calls_per_minute": defaults.calls_per_minute, "notes": defaults.notes},
            _MIGRATION._NEW,
        )
        self.assertEqual(defaults.calls_per_day, _MIGRATION._OLD["calls_per_day"])

    def test_an_untouched_row_takes_120_a_minute_and_keeps_its_daily_limit(self) -> None:
        row = _row()

        _forward()

        self.assertEqual(_values(row), {**_MIGRATION._OLD, **_MIGRATION._NEW})
        self.assertEqual(_values(row)["calls_per_day"], 2000)

    def test_an_edited_row_keeps_every_value_and_is_logged(self) -> None:
        for edit in ({"calls_per_minute": 10}, {"calls_per_day": 500}, {"notes": "Held low on purpose - Jess"}):
            with self.subTest(edit=edit):
                ApiRateLimit.objects.filter(service=_SERVICE).delete()
                row = _row(**edit)

                with self.assertLogs(_MIGRATION.logger, "WARNING"):
                    _forward()

                self.assertEqual(_values(row), {**_MIGRATION._OLD, **edit})

    def test_a_disabled_row_takes_the_limit_and_stays_disabled(self) -> None:
        row = _row(enabled=False)

        _forward()

        row.refresh_from_db()
        self.assertFalse(row.enabled)
        self.assertEqual(row.calls_per_minute, 120)

    def test_a_deployment_with_no_row_gets_120_from_get_limit_config(self) -> None:
        _forward()

        self.assertFalse(ApiRateLimit.objects.filter(service=_SERVICE).exists())
        self.assertEqual(rate_limiter.get_limit_config(_SERVICE).calls_per_minute, 120)


class StrandedPushMarkTests(TestCase):
    def test_a_mark_on_a_link_without_auto_sync_is_cleared_and_one_with_it_is_kept(self) -> None:
        profile = User.objects.create_user(username="stranded-mark").profile
        marked = timezone.now() - datetime.timedelta(days=3)
        links = {}
        for auto_sync in (False, True):
            trip = Trip.objects.create(name=f"auto_sync={auto_sync}", creator=profile)
            links[auto_sync] = TripCalendarLink.objects.create(
                trip=trip,
                profile=profile,
                google_event_id=f"evt-{auto_sync}",
                direction=CalendarSyncDirection.EXPORTED,
                auto_sync=auto_sync,
                push_requested_at=marked,
                push_attempts=2,
            )

        _MIGRATION.settle_marks_nothing_delivers(apps, None)

        for link in links.values():
            link.refresh_from_db()
        self.assertEqual((links[False].push_requested_at, links[False].push_attempts), (None, 0))
        self.assertEqual((links[True].push_requested_at, links[True].push_attempts), (marked, 2))

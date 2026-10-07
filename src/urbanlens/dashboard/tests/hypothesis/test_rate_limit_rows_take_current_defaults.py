"""Migration 0068 gives a rate-limit row still holding any earlier release's defaults 0.9.0's, and leaves an edited row alone.

`get_limit_config` writes a service's defaults into its row only when it creates it. 0064 moved rows from 0.8.0's
values to 0.9.0's; a row created by an older release kept whatever that release wrote. Overpass rows from v0.3.0b0 and
v0.4.0b3 still allow two calls a minute and 500 a day, where 0.9.0 allows 240 and 24,000. Jess, 2026-10-07: bring them
up to the current defaults.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import importlib
import re
from typing import Any

from django.apps import apps
from django.test import override_settings

from hypothesis import assume, given, settings, strategies as st
from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.api_call_log import ApiCallLog
from urbanlens.dashboard.models.api_rate_limit import ApiRateLimit
from urbanlens.dashboard.services.core import rate_limiter

_MIGRATION = importlib.import_module(
    "urbanlens.dashboard.migrations.0068_rate_limit_rows_from_any_release_take_0_9_0_defaults"
)
_FIELDS = _MIGRATION._FIELDS
_CURRENT: dict[str, dict[str, Any]] = {service: current for service, current, _earlier in _MIGRATION.DEFAULTS}
_EARLIER: dict[str, tuple[dict[str, Any], ...]] = {
    service: earlier for service, _current, earlier in _MIGRATION.DEFAULTS
}
_CASES = [(service, old) for service, earlier in _EARLIER.items() for old in earlier]
_OVERPASS = "overpass"
#: What v0.3.0b0 and v0.4.0b3 wrote for Overpass: the case that prompted the migration.
_OVERPASS_V030 = next(old for old in _EARLIER[_OVERPASS] if old["calls_per_minute"] == 2)


#: A value an admin might set for each written field, unlike every default Overpass has had.
_EDITED: dict[str, Any] = {
    "display_name": "Overpass (self-hosted)",
    "calls_per_minute": 5,
    "calls_per_day": 1_000,
    "calls_per_30_days": 10_000,
    "min_interval_seconds": 2.0,
    "usa_only": True,
    "notes": "Held down on purpose: the instance is shared with REData - Jess",
}


def _row(service: str, values: dict[str, Any], **overrides: Any) -> ApiRateLimit:
    return ApiRateLimit.objects.create(service=service, **{**values, **overrides})


def _values(row: ApiRateLimit) -> dict[str, Any]:
    row.refresh_from_db()
    return {field: getattr(row, field) for field in _FIELDS}


def _forward() -> None:
    _MIGRATION.bring_earlier_defaults_up_to_0_9_0(apps, None)


def _reverse() -> None:
    _MIGRATION.Migration.operations[0].reverse_code(apps, None)


class TheFrozenDataTests(TestCase):
    def test_every_snapshot_names_exactly_the_fields_get_limit_config_writes(self) -> None:
        for service, current, earlier in _MIGRATION.DEFAULTS:
            for values in (current, *earlier):
                with self.subTest(service=service, values=values):
                    self.assertEqual(tuple(values), _FIELDS)

    def test_no_earlier_snapshot_is_the_current_one_or_repeated(self) -> None:
        for service, current, earlier in _MIGRATION.DEFAULTS:
            with self.subTest(service=service):
                self.assertNotIn(current, earlier)
                self.assertEqual(len({tuple(old.items()) for old in earlier}), len(earlier))

    def test_the_0_9_0_values_are_what_the_registry_declares(self) -> None:
        """The migration writes frozen values, so a default changed after it would leave old rows a stale one, silently.

        A change to one of these services' defaults reaches no existing row by itself. If this fails, the change needs
        a migration of its own that moves rows from these values to the new ones, as this one does from earlier
        releases'; then compare that migration's values here instead.
        """
        declared = rate_limiter.all_service_defaults()
        for service, current in _CURRENT.items():
            with self.subTest(service=service):
                self.assertEqual({field: getattr(declared[service], field) for field in _FIELDS}, current)

    def test_overpass_v0_3_0s_two_a_minute_is_among_them(self) -> None:
        self.assertEqual((_OVERPASS_V030["calls_per_minute"], _OVERPASS_V030["calls_per_day"]), (2, 500))
        self.assertEqual((_CURRENT[_OVERPASS]["calls_per_minute"], _CURRENT[_OVERPASS]["calls_per_day"]), (240, 24_000))


class EveryEarlierDefaultTests(TestCase):
    def test_a_row_holding_any_earlier_default_takes_0_9_0s(self) -> None:
        self.assertEqual(len(_CASES), 25)
        for service, old in _CASES:
            with self.subTest(service=service, old=old):
                ApiRateLimit.objects.filter(service=service).delete()
                row = _row(service, old)

                _forward()

                self.assertEqual(_values(row), _CURRENT[service])

    def test_one_row_per_service_all_at_once(self) -> None:
        rows = {service: _row(service, earlier[0]) for service, earlier in _EARLIER.items()}

        _forward()

        for service, row in rows.items():
            with self.subTest(service=service):
                self.assertEqual(_values(row), _CURRENT[service])

    def test_a_disabled_row_takes_0_9_0s_values_and_stays_disabled(self) -> None:
        row = _row(_OVERPASS, _OVERPASS_V030, enabled=False)

        _forward()

        row.refresh_from_db()
        self.assertFalse(row.enabled)
        self.assertEqual(_values(row), _CURRENT[_OVERPASS])

    def test_a_row_brought_up_is_not_warned_about(self) -> None:
        _row(_OVERPASS, _OVERPASS_V030)
        with self.assertNoLogs(_MIGRATION.logger, "WARNING"):
            _forward()


@override_settings(TESTING=True)
class OverpassTests(TestCase):
    def test_get_limit_config_alone_leaves_a_v0_3_0_row_at_two_a_minute(self) -> None:
        row = _row(_OVERPASS, _OVERPASS_V030)

        rate_limiter.get_limit_config(_OVERPASS)

        self.assertEqual(_values(row)["calls_per_minute"], 2)

    def test_a_third_call_in_a_minute_is_refused_before_and_permitted_after(self) -> None:
        # Overpass is our own host: every environment holds all of its budget, so the row alone decides.
        self.assertEqual(rate_limiter._service_share(_OVERPASS), 1.0)
        _row(_OVERPASS, _OVERPASS_V030)
        just_now = datetime.now(UTC) - timedelta(seconds=5)
        ApiCallLog.objects.bulk_create([ApiCallLog(service=_OVERPASS, success=True) for _ in range(2)])
        ApiCallLog.objects.filter(service=_OVERPASS).update(created=just_now)
        self.assertFalse(rate_limiter.check_rate_limit(_OVERPASS))

        _forward()

        self.assertTrue(rate_limiter.check_rate_limit(_OVERPASS))


class LeftAloneTests(TestCase):
    def test_a_row_already_at_0_9_0_is_not_rewritten(self) -> None:
        last_week = datetime.now(UTC) - timedelta(days=7)
        for service, current in _CURRENT.items():
            with self.subTest(service=service):
                row = _row(service, current)
                ApiRateLimit.objects.filter(pk=row.pk).update(updated=last_week)

                _forward()

                row.refresh_from_db()
                self.assertEqual(_values(row), current)
                self.assertEqual(row.updated, last_week)

    def test_a_row_already_at_0_9_0_is_not_warned_about(self) -> None:
        _row(_OVERPASS, _CURRENT[_OVERPASS])
        with self.assertNoLogs(_MIGRATION.logger, "WARNING"):
            _forward()

    def test_a_row_an_admin_edited_keeps_every_value_and_is_logged(self) -> None:
        edits: list[dict[str, Any]] = [
            {"calls_per_minute": 5},
            {"calls_per_day": 1_000},
            {"calls_per_30_days": 10_000},
            {"min_interval_seconds": 2.0},
            {"usa_only": True},
            {"display_name": "Overpass (self-hosted)"},
            {"notes": "Two a minute on purpose: the mirror is shared with REData - Jess"},
        ]
        for edit in edits:
            with self.subTest(edit=edit):
                ApiRateLimit.objects.filter(service=_OVERPASS).delete()
                row = _row(_OVERPASS, _OVERPASS_V030, **edit)

                with self.assertLogs(_MIGRATION.logger, "WARNING") as logs:
                    _forward()

                self.assertEqual(_values(row), {**_OVERPASS_V030, **edit})
                self.assertTrue(any(_OVERPASS in line for line in logs.output), logs.output)

    def test_the_log_names_exactly_the_fields_that_differ_from_0_9_0s(self) -> None:
        """Every limit is printed with its value, so the field list is the only part that says which ones an admin changed."""
        for field in _FIELDS:
            edit = {field: _EDITED[field]}
            with self.subTest(edit=edit):
                ApiRateLimit.objects.filter(service=_OVERPASS).delete()
                row = _row(_OVERPASS, _CURRENT[_OVERPASS], **edit)

                with self.assertLogs(_MIGRATION.logger, "WARNING") as logs:
                    _forward()

                self.assertEqual(_values(row), {**_CURRENT[_OVERPASS], **edit})
                named = [
                    match.group(1)
                    for line in logs.output
                    if (match := re.search(r"overpass keeps its own (.+?) \(limits", line))
                ]
                self.assertEqual(named, [field])

    def test_an_edited_note_is_named_but_not_quoted_in_the_log(self) -> None:
        mine = "Billing alert at 2,000 - Jess"
        _row("google_places", _EARLIER["google_places"][0], notes=mine)

        with self.assertLogs(_MIGRATION.logger, "WARNING") as logs:
            _forward()

        self.assertTrue(any("google_places" in line and "notes" in line for line in logs.output), logs.output)
        self.assertFalse(any(mine in line for line in logs.output), logs.output)

    def test_a_deployment_with_no_row_gets_none(self) -> None:
        _forward()

        self.assertFalse(ApiRateLimit.objects.filter(service__in=list(_EARLIER)).exists())

    def test_a_service_no_longer_registered_is_untouched(self) -> None:
        """LoopNet left the registry; its row keeps v0.3.0's values, and nothing calls it."""
        loopnet = {
            "display_name": "LoopNet",
            "calls_per_minute": 5,
            "calls_per_day": 100,
            "calls_per_30_days": None,
            "min_interval_seconds": None,
            "usa_only": True,
            "notes": "US commercial real estate. Scraped - be conservative to avoid blocking.",
        }
        row = _row("loopnet", loopnet)

        _forward()

        self.assertEqual(_values(row), loopnet)

    def test_a_service_whose_defaults_never_changed_is_untouched(self) -> None:
        row = ApiRateLimit.objects.create(
            service="wikipedia", display_name="Wikipedia", calls_per_minute=7, calls_per_day=None
        )

        _forward()

        row.refresh_from_db()
        self.assertEqual((row.calls_per_minute, row.calls_per_day), (7, None))


_CALENDAR_MIGRATION = importlib.import_module("urbanlens.dashboard.migrations.0067_calendar_export_resumable")
_CALENDAR = "google_calendar"


class AfterTheCalendarMigrationTests(TestCase):
    """0067 already moves google_calendar's 30-a-minute rows to 120; run after it, this migration must agree with it."""

    def test_0067s_old_and_new_are_this_migrations_earlier_and_current(self) -> None:
        self.assertEqual(_EARLIER[_CALENDAR], (_CALENDAR_MIGRATION._OLD,))
        self.assertEqual(_CURRENT[_CALENDAR], {**_CALENDAR_MIGRATION._OLD, **_CALENDAR_MIGRATION._NEW})

    def test_a_row_0067_raised_ends_at_0_9_0s_values_and_is_not_warned_about(self) -> None:
        row = _row(_CALENDAR, _CALENDAR_MIGRATION._OLD)
        _CALENDAR_MIGRATION.raise_calendar_minute_limit(apps, None)

        with self.assertNoLogs(_MIGRATION.logger, "WARNING"):
            _forward()

        self.assertEqual(_values(row), _CURRENT[_CALENDAR])

    def test_every_earlier_calendar_row_ends_at_0_9_0s_values_after_both(self) -> None:
        for old in _EARLIER[_CALENDAR]:
            with self.subTest(old=old):
                ApiRateLimit.objects.filter(service=_CALENDAR).delete()
                row = _row(_CALENDAR, old)

                _CALENDAR_MIGRATION.raise_calendar_minute_limit(apps, None)
                _forward()

                self.assertEqual(_values(row), _CURRENT[_CALENDAR])


class ReverseTests(TestCase):
    def test_reverse_leaves_every_row_as_it_is(self) -> None:
        """A row at 0.9.0's values may be an admin's own choice, so reverse never puts an older default back."""
        rows = {service: _row(service, earlier[0]) for service, earlier in _EARLIER.items()}
        _forward()

        _reverse()

        for service, row in rows.items():
            with self.subTest(service=service):
                self.assertEqual(_values(row), _CURRENT[service])


#: Postgres text refuses NUL, and neither admin form could send one.
_TEXT = st.characters(exclude_characters="\x00")
_EDITS: dict[str, st.SearchStrategy[Any]] = {
    "display_name": st.text(_TEXT, min_size=1, max_size=40),
    "calls_per_minute": st.one_of(st.none(), st.integers(min_value=1, max_value=60_000)),
    "calls_per_day": st.one_of(st.none(), st.integers(min_value=1, max_value=10_000_000)),
    "calls_per_30_days": st.one_of(st.none(), st.integers(min_value=1, max_value=100_000_000)),
    "min_interval_seconds": st.one_of(st.none(), st.floats(min_value=0.0, max_value=3600.0)),
    "usa_only": st.booleans(),
    "notes": st.text(_TEXT, max_size=80),
}


@st.composite
def _edited_rows(draw: st.DrawFn) -> tuple[str, dict[str, Any]]:
    """An earlier default with at least one written field changed, so it is no default of its service any more."""
    service, old = draw(st.sampled_from(_CASES))
    fields = draw(st.lists(st.sampled_from(_FIELDS), min_size=1, unique=True))
    edited = {**old, **{field: draw(_EDITS[field].filter(lambda value, f=field: value != old[f])) for field in fields}}
    return service, edited


class AnyEditedRowTests(TestCase):
    @settings(max_examples=60, deadline=None)
    @given(case=_edited_rows())
    def test_a_row_off_every_default_in_any_field_is_never_rewritten(self, case: tuple[str, dict[str, Any]]) -> None:
        service, edited = case
        assume(edited != _CURRENT[service] and edited not in _EARLIER[service])
        ApiRateLimit.objects.filter(service=service).delete()
        row = _row(service, edited)
        before = _values(row)

        _forward()

        self.assertEqual(_values(row), before)

    @settings(max_examples=30, deadline=None)
    @given(case=st.sampled_from(_CASES), enabled=st.booleans())
    def test_any_earlier_default_takes_0_9_0s_and_keeps_its_switch(
        self, case: tuple[str, dict[str, Any]], enabled: bool
    ) -> None:
        service, old = case
        ApiRateLimit.objects.filter(service=service).delete()
        row = _row(service, old, enabled=enabled)

        _forward()

        row.refresh_from_db()
        self.assertEqual(_values(row), _CURRENT[service])
        self.assertEqual(row.enabled, enabled)

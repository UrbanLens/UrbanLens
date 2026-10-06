"""Migration 0064 gives each rate-limit row nobody edited the defaults 0.9.0 changed, and leaves an edited row alone.

`get_limit_config` writes a service's defaults into its row only when it creates it, and afterwards rewrites only the
generic fallback. So production's `redata_places` row, which 0.8.0 created with no daily cap, would have stayed
uncapped after 0.9.0 set REData Places to 40 a day.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import importlib
from typing import Any
from unittest import mock

from django.apps import apps

from hypothesis import given, settings, strategies as st
from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.api_call_log import ApiCallLog
from urbanlens.dashboard.models.api_rate_limit import ApiRateLimit
from urbanlens.dashboard.services.core import rate_limiter

_MIGRATION = importlib.import_module("urbanlens.dashboard.migrations.0064_rate_limit_rows_take_0_9_0_defaults")
_FIELDS = _MIGRATION._FIELDS
_V080 = {service: old for service, old, _changes in _MIGRATION.CHANGED_DEFAULTS}
_V090 = {service: {**old, **changes} for service, old, changes in _MIGRATION.CHANGED_DEFAULTS}
_PLACES = "redata_places"


def _row(service: str, **overrides: Any) -> ApiRateLimit:
    return ApiRateLimit.objects.create(service=service, **{**_V080[service], **overrides})


def _values(row: ApiRateLimit) -> dict[str, Any]:
    row.refresh_from_db()
    return {field: getattr(row, field) for field in _FIELDS}


def _forward() -> None:
    _MIGRATION.apply_0_9_0_defaults(apps, None)


def _reverse() -> None:
    _MIGRATION.Migration.operations[0].reverse_code(apps, None)


class RedataPlacesCapTests(TestCase):
    def test_get_limit_config_alone_leaves_a_row_0_8_0_created_uncapped(self) -> None:
        row = _row(_PLACES)

        rate_limiter.get_limit_config(_PLACES)

        self.assertIsNone(_values(row)["calls_per_day"])

    def test_a_row_0_8_0_created_takes_the_40_a_day_cap_and_nothing_else_changes(self) -> None:
        row = _row(_PLACES)

        _forward()

        self.assertEqual(_values(row), {**_V080[_PLACES], "calls_per_day": 40})

    def test_the_migrated_row_refuses_the_41st_call_of_the_day(self) -> None:
        _row(_PLACES)
        noon = datetime.now(UTC).replace(hour=12, minute=0, second=0, microsecond=0)
        with mock.patch("django.utils.timezone.now", return_value=noon):
            ApiCallLog.objects.bulk_create([ApiCallLog(service=_PLACES, success=True) for _ in range(40)])
            # An hour ago, so the 20-a-minute window is clear and only the daily one can refuse.
            ApiCallLog.objects.filter(service=_PLACES).update(created=noon - timedelta(hours=1))
            self.assertTrue(rate_limiter.check_rate_limit(_PLACES))

            _forward()

            self.assertFalse(rate_limiter.check_rate_limit(_PLACES))

    def test_a_row_an_admin_edited_keeps_every_value(self) -> None:
        edits: list[dict[str, Any]] = [
            {"calls_per_day": 100},
            {"calls_per_minute": 5},
            {"min_interval_seconds": 2.0},
            {"notes": "Uncapped on purpose: REData agreed a larger share for this deployment."},
        ]
        for edit in edits:
            with self.subTest(edit=edit):
                ApiRateLimit.objects.filter(service=_PLACES).delete()
                row = _row(_PLACES, **edit)

                _forward()

                self.assertEqual(_values(row), {**_V080[_PLACES], **edit})

    def test_an_edited_row_left_without_the_cap_is_logged_and_an_untouched_one_is_not(self) -> None:
        _row(_PLACES, calls_per_minute=5)
        with self.assertLogs(_MIGRATION.logger, "WARNING") as logs:
            _forward()
        self.assertTrue(any(_PLACES in line for line in logs.output), logs.output)

        ApiRateLimit.objects.filter(service=_PLACES).delete()
        _row(_PLACES)
        with self.assertNoLogs(_MIGRATION.logger, "WARNING"):
            _forward()

    def test_a_disabled_row_takes_the_cap_and_stays_disabled(self) -> None:
        row = _row(_PLACES, enabled=False)

        _forward()

        row.refresh_from_db()
        self.assertFalse(row.enabled)
        self.assertEqual(row.calls_per_day, 40)

    def test_a_deployment_with_no_row_gets_none_from_the_migration_and_the_cap_from_get_limit_config(self) -> None:
        _forward()

        self.assertFalse(ApiRateLimit.objects.filter(service=_PLACES).exists())
        self.assertEqual(rate_limiter.get_limit_config(_PLACES).calls_per_day, 40)


class NotesOnlyServicesTests(TestCase):
    def test_each_row_0_8_0_created_takes_0_9_0_notes_and_keeps_its_limits(self) -> None:
        rows = {service: _row(service) for service in _V080 if service != _PLACES}

        _forward()

        for service, row in rows.items():
            with self.subTest(service=service):
                self.assertEqual(_values(row), _V090[service])
                self.assertEqual(
                    {k: v for k, v in _V090[service].items() if k != "notes"},
                    {k: v for k, v in _V080[service].items() if k != "notes"},
                )

    def test_a_note_an_admin_wrote_is_kept(self) -> None:
        mine = "Billing alert at 2,000 - Jess"
        row = _row("google_places", notes=mine)

        _forward()

        self.assertEqual(_values(row)["notes"], mine)

    def test_a_service_0_9_0_did_not_change_is_untouched(self) -> None:
        row = ApiRateLimit.objects.create(
            service="nominatim", display_name="Nominatim", calls_per_minute=60, calls_per_day=None
        )

        _forward()

        row.refresh_from_db()
        self.assertEqual((row.calls_per_minute, row.calls_per_day), (60, None))


class ReverseTests(TestCase):
    def test_reverse_leaves_every_row_as_it_is(self) -> None:
        """A row at 0.9.0's values may be an admin's own choice, so reverse never lifts the cap (0.8.0 runs with it)."""
        rows = {service: _row(service) for service in _V080}
        _forward()

        _reverse()

        for service, row in rows.items():
            with self.subTest(service=service):
                self.assertEqual(_values(row), _V090[service])


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
    """A changed service and an edit that leaves at least one written field off its 0.8.0 value."""
    service = draw(st.sampled_from(sorted(_V080)))
    fields = draw(st.lists(st.sampled_from(_FIELDS), min_size=1, unique=True))
    edit = {field: draw(_EDITS[field].filter(lambda value, f=field: value != _V080[service][f])) for field in fields}
    return service, edit


class AnyEditedRowTests(TestCase):
    @settings(max_examples=40, deadline=None)
    @given(case=_edited_rows())
    def test_a_row_off_its_0_8_0_values_in_any_field_is_never_rewritten(self, case: tuple[str, dict[str, Any]]) -> None:
        service, edit = case
        row = _row(service, **edit)
        before = _values(row)

        _forward()

        self.assertEqual(_values(row), before)

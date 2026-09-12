"""The outbound-API daily budget check must be able to use its index.

``check_rate_limit`` runs on every outbound call, and its daily window counted
rows with ``created__date = <today>``. Wrapping the column in a date extraction
makes the predicate unusable by ``idxdb_apilog_svc_cdt`` (service, created), so
the count degraded to a scan of a 400-day append-only log that every user's
activity makes longer - N21 H57. The site's own traffic sets what the next call
costs, which is the coupling the availability requirement forbids.

A half-open range over the stored column is the same question asked in a form
the index can answer. TIME_ZONE is UTC and USE_TZ is on, so the boundaries are
the same instants the date extraction compared against - the tests below pin
that rather than assume it.
"""

from __future__ import annotations

from datetime import timedelta

from django.utils import timezone
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.api_call_log import ApiCallLog

SERVICE = "daily_window_probe"


class DailyWindowBoundariesTests(TestCase):
    """The window must still mean exactly the UTC calendar day."""

    def _log_at(self, moment) -> ApiCallLog:
        entry = baker.make(ApiCallLog, service=SERVICE)
        # `created` is auto_now_add, so it has to be written past the model.
        ApiCallLog.objects.filter(pk=entry.pk).update(created=moment)
        return entry

    def _counted(self) -> set[int]:
        return set(ApiCallLog.objects.for_service(SERVICE).today().values_list("pk", flat=True))

    def test_the_first_and_last_instants_of_today_are_both_counted(self) -> None:
        start = timezone.now().replace(hour=0, minute=0, second=0, microsecond=0)
        first = self._log_at(start)
        last = self._log_at(start + timedelta(days=1) - timedelta(microseconds=1))

        self.assertEqual(self._counted(), {first.pk, last.pk})

    def test_neither_neighbouring_day_is_counted(self) -> None:
        start = timezone.now().replace(hour=0, minute=0, second=0, microsecond=0)
        self._log_at(start - timedelta(microseconds=1))
        self._log_at(start + timedelta(days=1))
        today = self._log_at(start + timedelta(hours=12))

        self.assertEqual(self._counted(), {today.pk})

    def test_another_service_is_never_counted(self) -> None:
        mine = self._log_at(timezone.now())
        baker.make(ApiCallLog, service="someone_else")

        self.assertEqual(self._counted(), {mine.pk})


class DailyWindowIsIndexableTests(TestCase):
    """The predicate must compare the stored column, not a function of it."""

    def _sql(self, queryset) -> str:
        return str(queryset.query).lower()

    def test_the_window_does_not_extract_a_date_from_the_column(self) -> None:
        sql = self._sql(ApiCallLog.objects.for_service(SERVICE).today())

        self.assertNotIn("::date", sql, f"the daily window still casts the column: {sql}")
        self.assertNotIn("date(", sql, f"the daily window still extracts from the column: {sql}")

    def test_the_check_can_fail(self) -> None:
        """The form this replaced must trip the assertion above, or it proves nothing."""
        old_form = ApiCallLog.objects.for_service(SERVICE).filter(created__date=timezone.now().date())
        sql = self._sql(old_form)

        self.assertTrue(
            "::date" in sql or "date(" in sql,
            f"the old form no longer looks like a cast, so the guard above is vacuous: {sql}",
        )

    def test_it_is_still_a_bounded_window_and_not_simply_everything(self) -> None:
        sql = self._sql(ApiCallLog.objects.for_service(SERVICE).today())

        self.assertIn("created", sql)
        self.assertIn(">=", sql)
        self.assertIn("<", sql)

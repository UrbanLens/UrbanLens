"""The API usage ledger records each call's units and outcome, and the site-admin usage report reads them."""

from __future__ import annotations

from unittest.mock import Mock, patch

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.api_call_log.model import ApiCallLog
from urbanlens.dashboard.services.admin.site_admin import add_user_to_site_admin_group
from urbanlens.dashboard.services.core.rate_limiter import (
    CallLedger,
    ServiceDefaults,
    _RateLimitedSession,
    api_call_slot,
    observe_calls,
)


class SummaryByServiceTests(TestCase):
    def test_counts_calls_rather_than_rows(self) -> None:
        ApiCallLog.objects.create(service="svc", success=True, calls=5)
        ApiCallLog.objects.create(service="svc", success=True)

        summary = {row["service"]: row for row in ApiCallLog.objects.summary_by_service()}

        self.assertEqual(summary["svc"]["total"], 6)

    def test_reports_no_money(self) -> None:
        ApiCallLog.objects.create(service="svc", success=True)

        summary = {row["service"]: row for row in ApiCallLog.objects.summary_by_service()}

        self.assertEqual({key for key in summary["svc"] if "cost" in key}, set())


class SessionRecordsEachCallTests(TestCase):
    """_RateLimitedSession logs one row per request, successful or not."""

    def _mock_session(self, service_key: str, *, ok: bool) -> _RateLimitedSession:
        session = _RateLimitedSession(service_key)
        session._session = Mock()
        session._session.request.return_value = Mock(ok=ok, status_code=200 if ok else 500)
        return session

    def _get(self, service_key: str, *, ok: bool) -> None:
        defaults = {service_key: ServiceDefaults(display_name="Svc")}
        with (
            patch("urbanlens.dashboard.services.core.rate_limiter.check_rate_limit", return_value=True),
            patch("urbanlens.dashboard.services.core.rate_limiter.service_is_enabled", return_value=True),
            patch("urbanlens.dashboard.services.core.rate_limiter.all_service_defaults", return_value=defaults),
        ):
            self._mock_session(service_key, ok=ok).get("https://example.com/api")

    def test_a_successful_call_is_logged_as_a_success(self) -> None:
        self._get("some_svc", ok=True)

        entry = ApiCallLog.objects.get(service="some_svc")
        self.assertTrue(entry.success)
        self.assertEqual(entry.status_code, 200)

    def test_a_failed_response_is_logged_as_a_failure(self) -> None:
        self._get("some_svc", ok=False)

        entry = ApiCallLog.objects.get(service="some_svc")
        self.assertFalse(entry.success)
        self.assertEqual(entry.status_code, 500)


class ObserveCallsTests(TestCase):
    """``observe_calls`` hands each logged call's row pk and response to its observer."""

    _SERVICE = "observed_svc"

    def _session(self, response: Mock) -> _RateLimitedSession:
        session = _RateLimitedSession(self._SERVICE)
        session._session = Mock()
        session._session.request.return_value = response
        return session

    def _patches(self, ledger: CallLedger = CallLedger.ROW_PER_CALL):
        defaults = {self._SERVICE: ServiceDefaults(display_name="Observed", ledger=ledger)}
        return (
            patch("urbanlens.dashboard.services.core.rate_limiter.check_rate_limit", return_value=True),
            patch("urbanlens.dashboard.services.core.rate_limiter.service_is_enabled", return_value=True),
            patch("urbanlens.dashboard.services.core.rate_limiter.all_service_defaults", return_value=defaults),
        )

    def test_observer_receives_each_calls_row_pk_and_response(self) -> None:
        response = Mock(ok=True, status_code=200)
        seen: list[tuple[int, Mock]] = []
        first, second, third = self._patches()
        with first, second, third, observe_calls(lambda pk, resp: seen.append((pk, resp))):
            session = self._session(response)
            session.get("https://example.com/a")
            session.get("https://example.com/b")

        rows = list(ApiCallLog.objects.filter(service=self._SERVICE).order_by("pk"))
        self.assertEqual(seen, [(rows[0].pk, response), (rows[1].pk, response)])

    def test_an_observed_tallied_service_is_logged_per_row(self) -> None:
        seen: list[int] = []
        first, second, third = self._patches(CallLedger.TALLIED)
        with first, second, third, observe_calls(lambda pk, _resp: seen.append(pk)):
            self._session(Mock(ok=True, status_code=200)).get("https://example.com/a")

        entry = ApiCallLog.objects.get(service=self._SERVICE)
        self.assertEqual(seen, [entry.pk])

    def test_a_failing_observer_is_logged_and_the_call_still_returns(self) -> None:
        response = Mock(ok=True, status_code=200)

        def boom(_pk: int, _resp: Mock) -> None:
            raise ValueError("observer broke")

        first, second, third = self._patches()
        with (
            first,
            second,
            third,
            observe_calls(boom),
            self.assertLogs("urbanlens.dashboard.services.core.rate_limiter", level="ERROR"),
        ):
            returned = self._session(response).get("https://example.com/a")

        self.assertIs(returned, response)
        self.assertTrue(ApiCallLog.objects.get(service=self._SERVICE).success)

    def test_nested_observers_are_all_told_outermost_first(self) -> None:
        seen: list[str] = []
        first, second, third = self._patches()
        with first, second, third, observe_calls(lambda _pk, _resp: seen.append("outer")):
            session = self._session(Mock(ok=True, status_code=200))
            with observe_calls(lambda _pk, _resp: seen.append("inner")):
                session.get("https://example.com/a")
            session.get("https://example.com/b")

        self.assertEqual(seen, ["outer", "inner", "outer"])

    def test_a_failing_observer_does_not_stop_the_others(self) -> None:
        seen: list[int] = []

        def boom(_pk: int, _resp: Mock | None) -> None:
            raise ValueError("observer broke")

        first, second, third = self._patches()
        with (
            first,
            second,
            third,
            observe_calls(boom),
            observe_calls(lambda pk, _resp: seen.append(pk)),
            self.assertLogs("urbanlens.dashboard.services.core.rate_limiter", level="ERROR"),
        ):
            self._session(Mock(ok=True, status_code=200)).get("https://example.com/a")

        self.assertEqual(seen, [ApiCallLog.objects.get(service=self._SERVICE).pk])

    def test_a_request_that_raises_tells_the_observer_there_was_no_response(self) -> None:
        seen: list[tuple[int, Mock | None]] = []
        session = self._session(Mock())
        session._session.request.side_effect = ConnectionError("down")
        first, second, third = self._patches()
        with (
            first,
            second,
            third,
            observe_calls(lambda pk, resp: seen.append((pk, resp))),
            self.assertRaises(ConnectionError),
        ):
            session.get("https://example.com/a")

        entry = ApiCallLog.objects.get(service=self._SERVICE)
        self.assertEqual(seen, [(entry.pk, None)])
        self.assertFalse(entry.success)

    def test_no_observer_is_called_outside_the_block(self) -> None:
        seen: list[int] = []
        first, second, third = self._patches()
        with first, second, third:
            with observe_calls(lambda pk, _resp: seen.append(pk)):
                pass
            self._session(Mock(ok=True, status_code=200)).get("https://example.com/a")

        self.assertEqual(seen, [])


class SlotEntryPkTests(TestCase):
    """An ``api_call_slot`` exposes its reserved row's pk to the code inside it."""

    def test_entry_pk_is_the_reserved_row(self) -> None:
        defaults = {"slot_svc": ServiceDefaults(display_name="Slot")}
        with (
            patch("urbanlens.dashboard.services.core.rate_limiter.check_rate_limit", return_value=True),
            patch("urbanlens.dashboard.services.core.rate_limiter.service_is_enabled", return_value=True),
            patch("urbanlens.dashboard.services.core.rate_limiter.all_service_defaults", return_value=defaults),
            patch("urbanlens.dashboard.services.core.provider_health.check_admission"),
            api_call_slot("slot_svc") as slot,
        ):
            reserved = slot.entry_pk
            slot.success = True

        self.assertEqual(ApiCallLog.objects.get(service="slot_svc").pk, reserved)


class SiteAdminApiUsageIncludesPluginsTests(TestCase):
    """SiteAdminStatsApiUsagePartialView used to only iterate SERVICE_REGISTRY, silently omitting every plugin-declared service (the great majority of this app's integrations)."""

    def setUp(self) -> None:
        super().setUp()
        self.admin = baker.make(User)
        add_user_to_site_admin_group(self.admin)
        self.client.force_login(self.admin)

    def test_plugin_only_service_appears_in_the_report(self) -> None:
        defaults = {"a_plugin_service": ServiceDefaults(display_name="A Plugin Service")}
        with patch("urbanlens.dashboard.services.core.rate_limiter.all_service_defaults", return_value=defaults):
            response = self.client.get(reverse("site_admin_stats_api"))

        self.assertContains(response, "A Plugin Service")

    def test_a_services_calls_are_reported(self) -> None:
        defaults = {"used_svc": ServiceDefaults(display_name="Used Svc")}
        ApiCallLog.objects.create(service="used_svc", success=True, calls=7)
        with patch("urbanlens.dashboard.services.core.rate_limiter.all_service_defaults", return_value=defaults):
            response = self.client.get(reverse("site_admin_stats_api"))

        self.assertContains(response, "Used Svc")
        self.assertContains(response, '<td style="text-align:right">7</td>', html=False)

    def test_the_report_shows_no_dollar_figure(self) -> None:
        defaults = {"used_svc": ServiceDefaults(display_name="Used Svc")}
        ApiCallLog.objects.create(service="used_svc", success=True)
        with patch("urbanlens.dashboard.services.core.rate_limiter.all_service_defaults", return_value=defaults):
            response = self.client.get(reverse("site_admin_stats_api"))

        self.assertNotContains(response, "$")

    def test_requires_site_admin_permission_before_and_after_promotion(self) -> None:
        """Gated by ``dashboard.view_site_admin`` - not automatic for any logged-in user."""
        user = baker.make(User)
        self.client.force_login(user)

        response = self.client.get(reverse("site_admin_stats_api"))
        self.assertEqual(response.status_code, 403)

        add_user_to_site_admin_group(user)
        response = self.client.get(reverse("site_admin_stats_api"))
        self.assertEqual(response.status_code, 200)

"""The egress policy is applied where every external call passes (D26), and a refusal is neither failure nor deferral.

``_reserve_call``, ``api_call_slot`` and the rate-limited session refuse with ``EnvironmentRefusedError`` before
anything is sent or recorded; ``service_is_enabled`` answers the callers that ask first.
"""

from __future__ import annotations

from collections.abc import Iterator
import contextlib
import logging
from unittest import mock

from django.test import override_settings
import requests

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.models.api_call_log.model import ApiCallLog
from urbanlens.dashboard.models.api_rate_limit.model import ApiRateLimit
from urbanlens.dashboard.services.core import egress, rate_limiter
from urbanlens.dashboard.services.core.gateway import GatewayRequestError, is_source_outage
from urbanlens.dashboard.services.core.rate_limiter import (
    EnvironmentRefusedError,
    ServiceDisabledError,
    _RateLimitedSession,
    _reserve_call,
    api_call_slot,
    check_rate_limit,
    service_is_enabled,
)

QUOTA = "nominatim"
BILLED = "google_geocoding"
REDATA = "redata_geocode"
AI = "trivia_moderation"
MESSAGING = "sms"
PUBLIC_WRITE = "wayback_save"


@contextlib.contextmanager
def deployment(
    environment: str, *, share: float | None = None, overrides: dict[str, float] | None = None
) -> Iterator[None]:
    """Pretend to be one deployment: its ``UL_ENVIRONMENT``, ``UL_ENVIRONMENT_SHARE`` and overrides."""
    with (
        override_settings(ENVIRONMENT_NAME=environment, TESTING=False),
        mock.patch("urbanlens.UrbanLens.settings.app.settings.environment_share", share),
        mock.patch("urbanlens.UrbanLens.settings.app.settings.environment_share_overrides", overrides or {}),
    ):
        yield


class TheRealSettingsExistTests(SimpleTestCase):
    """``override_settings`` and ``mock.patch`` invent names; these must be real."""

    def test_settings(self) -> None:
        from django.conf import settings

        from urbanlens.UrbanLens.settings.app import settings as app_settings

        self.assertTrue(hasattr(settings, "ENVIRONMENT_NAME"))
        self.assertTrue(settings.TESTING)
        for name in (
            "environment_share",
            "environment_share_overrides",
            "background_tasks_allowlist",
            "email_send_outside_production",
        ):
            self.assertTrue(hasattr(app_settings, name), name)

    def test_the_retired_switches_are_gone(self) -> None:
        from urbanlens.UrbanLens.settings.app import settings as app_settings

        self.assertFalse(hasattr(app_settings, "allow_outbound_apis"))
        self.assertFalse(hasattr(app_settings, "billed_api_share"))
        self.assertFalse(hasattr(rate_limiter, "outbound_calls_permitted"))


class TheRefusalIsNotAnOutageTests(SimpleTestCase):
    def test_it_is_a_disabled_service_so_existing_callers_degrade(self) -> None:
        exc = EnvironmentRefusedError(QUOTA, category="quota", environment="development")
        self.assertIsInstance(exc, ServiceDisabledError)
        self.assertIsInstance(exc, GatewayRequestError)

    def test_it_is_never_retried_or_recorded_as_unanswered(self) -> None:
        self.assertFalse(EnvironmentRefusedError.transient)

    def test_it_is_never_stored_as_an_empty_answer(self) -> None:
        """``is_outage`` means "nothing was learned", which is what keeps a cache from storing it."""
        self.assertTrue(is_source_outage(EnvironmentRefusedError(QUOTA, category="quota", environment="development")))

    def test_it_says_which_service_and_environment(self) -> None:
        self.assertIn(
            "not available in the staging environment",
            str(EnvironmentRefusedError(QUOTA, category="quota", environment="staging")),
        )


class ChokePointTests(TestCase):
    def test_development_refuses_quota_and_billed_before_anything_is_recorded(self) -> None:
        with deployment("development"):
            for service in (QUOTA, BILLED):
                with self.subTest(service=service), self.assertRaises(EnvironmentRefusedError):
                    _reserve_call(service, endpoint="x")
        self.assertFalse(ApiCallLog.objects.exists())
        self.assertFalse(ApiRateLimit.objects.filter(service__in=(QUOTA, BILLED)).exists())

    def test_development_still_calls_redata_and_ai(self) -> None:
        with deployment("development"):
            _reserve_call(REDATA, endpoint="x")
            _reserve_call(AI, endpoint="x")
        self.assertEqual(ApiCallLog.objects.count(), 2)

    def test_staging_and_development_refuse_messaging_and_public_writes(self) -> None:
        for environment in ("staging", "development"):
            with deployment(environment):
                for service in (MESSAGING, PUBLIC_WRITE):
                    with (
                        self.subTest(environment=environment, service=service),
                        self.assertRaises(EnvironmentRefusedError),
                    ):
                        egress.require_egress(service)

    def test_production_calls_every_category(self) -> None:
        with deployment("production"):
            for service in (QUOTA, BILLED, REDATA, AI, MESSAGING, PUBLIC_WRITE):
                with self.subTest(service=service):
                    egress.require_egress(service)

    def test_service_is_enabled_answers_ahead_of_time_even_with_a_row_in_hand(self) -> None:
        enabled_row = mock.Mock(enabled=True)
        with deployment("development"):
            self.assertFalse(service_is_enabled(QUOTA, config=enabled_row))
            self.assertTrue(service_is_enabled(REDATA, config=enabled_row))

    def test_the_session_refuses_before_the_wire(self) -> None:
        session = _RateLimitedSession(QUOTA)
        with (
            deployment("development"),
            mock.patch.object(requests.Session, "request") as wire,
            self.assertRaises(EnvironmentRefusedError),
        ):
            session.get("https://nominatim.openstreetmap.org/search", params={"q": "x"})
        wire.assert_not_called()
        self.assertFalse(ApiCallLog.objects.exists())

    def test_the_slot_refuses_before_the_block_runs(self) -> None:
        ran = False
        with deployment("staging"), self.assertRaises(EnvironmentRefusedError), api_call_slot(MESSAGING):
            ran = True
        self.assertFalse(ran)
        self.assertFalse(ApiCallLog.objects.exists())

    def test_an_override_opts_one_provider_in(self) -> None:
        with deployment("development", overrides={QUOTA: 0.02}):
            _reserve_call(QUOTA, endpoint="x")
            with self.assertRaises(EnvironmentRefusedError):
                _reserve_call("wikipedia", endpoint="x")

    def test_ul_environment_share_zero_keeps_production_off_shared_budgets(self) -> None:
        with deployment("production", share=0.0), self.assertRaises(EnvironmentRefusedError):
            _reserve_call(BILLED, endpoint="x")

    def test_demo_mode_no_longer_decides_egress(self) -> None:
        """Its REData exemption is replaced by the policy; the login endpoint and banner are its business still."""
        with deployment("production"), mock.patch("urbanlens.UrbanLens.settings.app.settings.demo_mode", True):
            egress.require_egress(QUOTA)
        with deployment("development"), mock.patch("urbanlens.UrbanLens.settings.app.settings.demo_mode", True):
            egress.require_egress(REDATA)
            with self.assertRaises(EnvironmentRefusedError):
                egress.require_egress(QUOTA)


class RefusalLoggingTests(SimpleTestCase):
    def setUp(self) -> None:
        super().setUp()
        egress._last_logged.clear()

    def test_once_per_service_per_interval_at_info(self) -> None:
        with deployment("development"), self.assertLogs(egress.logger, logging.DEBUG) as logs:
            for _ in range(3):
                with contextlib.suppress(EnvironmentRefusedError):
                    egress.require_egress(QUOTA)
        levels = [record.levelno for record in logs.records]
        self.assertEqual(levels, [logging.INFO, logging.DEBUG, logging.DEBUG])

    def test_again_after_the_interval(self) -> None:
        with deployment("development"), self.assertLogs(egress.logger, logging.DEBUG) as logs:
            with contextlib.suppress(EnvironmentRefusedError):
                egress.require_egress(QUOTA)
            egress._last_logged[QUOTA] -= egress.LOG_INTERVAL_SECONDS + 1
            with contextlib.suppress(EnvironmentRefusedError):
                egress.require_egress(QUOTA)
        self.assertEqual([record.levelno for record in logs.records], [logging.INFO, logging.INFO])

    def test_refusals_are_collected_even_when_swallowed(self) -> None:
        with deployment("development"), egress.collect_refusals() as refused:
            with contextlib.suppress(EnvironmentRefusedError):
                egress.require_egress(QUOTA)
            egress.require_egress(REDATA)
        self.assertEqual(refused, [QUOTA])


class BudgetsAreScaledTests(TestCase):
    """Each window of a quota'd or billed service holds this deployment's share of it."""

    def setUp(self) -> None:
        super().setUp()
        ApiRateLimit.objects.create(service=QUOTA, display_name="n", calls_per_minute=600, calls_per_day=100)

    def test_staging_gets_a_twentieth_of_the_day(self) -> None:
        ApiCallLog.objects.bulk_create([ApiCallLog(service=QUOTA, success=True) for _ in range(4)])
        with deployment("staging"):
            self.assertTrue(check_rate_limit(QUOTA))
            ApiCallLog.objects.create(service=QUOTA, success=True)
            self.assertFalse(check_rate_limit(QUOTA))

    def test_production_gets_nine_tenths(self) -> None:
        ApiCallLog.objects.bulk_create([ApiCallLog(service=QUOTA, success=True) for _ in range(89)])
        with deployment("production"):
            self.assertTrue(check_rate_limit(QUOTA))
            ApiCallLog.objects.create(service=QUOTA, success=True)
            self.assertFalse(check_rate_limit(QUOTA))

    def test_redata_is_not_scaled(self) -> None:
        ApiRateLimit.objects.create(service=REDATA, display_name="r", calls_per_minute=600, calls_per_day=10)
        ApiCallLog.objects.bulk_create([ApiCallLog(service=REDATA, success=True) for _ in range(9)])
        with deployment("staging"):
            self.assertTrue(check_rate_limit(REDATA))


class StartupLogTests(SimpleTestCase):
    def test_it_names_the_environment_share_and_overrides(self) -> None:
        with (
            deployment("staging", overrides={QUOTA: 0.02}),
            mock.patch(
                "urbanlens.UrbanLens.settings.app.settings.background_tasks_allowlist", ["wayback-archive-sweep"]
            ),
        ):
            line = egress.describe_policy()
        self.assertIn("environment staging", line)
        self.assertIn("UL_ENVIRONMENT_SHARE 0.05", line)
        self.assertIn("nominatim=0.02", line)
        self.assertIn("wayback-archive-sweep", line)

    def test_it_warns_about_retired_switches_and_unknown_names(self) -> None:
        with (
            deployment("development", overrides={"no_such_service": 0.1}),
            mock.patch.dict("os.environ", {"UL_ALLOW_OUTBOUND_APIS": "true"}),
            mock.patch("urbanlens.UrbanLens.settings.app.settings.background_tasks_allowlist", ["no-such-entry"]),
            self.assertLogs(egress.logger, logging.INFO) as logs,
        ):
            egress.log_egress_policy()
        text = "\n".join(logs.output)
        self.assertIn("UL_ALLOW_OUTBOUND_APIS", text)
        self.assertIn("no_such_service", text)
        self.assertIn("no-such-entry", text)

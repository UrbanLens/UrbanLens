"""The paid-API limiter reserves before the call and refuses when it cannot count."""

from __future__ import annotations

from unittest import mock

from django.db import DatabaseError

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.api_call_log.model import ApiCallLog
from urbanlens.dashboard.models.api_rate_limit import ApiRateLimit
from urbanlens.dashboard.services.core import rate_limiter
from urbanlens.dashboard.services.core.rate_limiter import (
    ServiceDisabledError,
    api_call_slot,
    check_rate_limit,
    get_limit_config,
)


class ACountThatCannotBeReadTests(TestCase):
    """The config read fails closed for billable services; the count query beside it fails open."""

    def _unreadable_counts(self):  # noqa: ANN202
        return mock.patch.object(ApiCallLog.objects, "for_service", side_effect=DatabaseError("connection lost"))

    def test_a_billable_service_is_refused(self) -> None:
        config = get_limit_config("google_geocoding")
        with self._unreadable_counts():
            self.assertFalse(
                check_rate_limit("google_geocoding", config), "a paid API ran uncapped when its count failed"
            )

    def test_a_free_service_is_allowed(self) -> None:
        config = get_limit_config("overpass")
        with self._unreadable_counts():
            self.assertTrue(check_rate_limit("overpass", config))


class TheSlotTests(TestCase):
    def test_a_disabled_service_is_refused_before_the_block_runs(self) -> None:
        get_limit_config("trivia_moderation")
        ApiRateLimit.objects.filter(service="trivia_moderation").update(enabled=False)
        ran = []
        with self.assertRaises(ServiceDisabledError), api_call_slot("trivia_moderation"):
            ran.append(1)
        self.assertEqual(ran, [])

    def test_a_block_that_raises_is_recorded_as_failed(self) -> None:
        def failing_call() -> None:
            with api_call_slot("trivia_moderation", endpoint="model-x"):
                raise RuntimeError("provider exploded")

        self.assertRaises(RuntimeError, failing_call)
        row = ApiCallLog.objects.filter(service="trivia_moderation").get()
        self.assertFalse(row.success)
        self.assertEqual(row.endpoint, "model-x")

    def test_the_module_keeps_one_billable_rule(self) -> None:
        self.assertTrue(rate_limiter._is_billable("a-service-nobody-declared"))
        self.assertFalse(rate_limiter._is_billable("overpass"))

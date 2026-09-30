"""The sign-in parameters endpoint is limited per address, generously enough that people never meet the limit."""

from __future__ import annotations

from django.core.cache import cache
from django.urls import resolve, reverse

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.controllers.e2ee import LOGIN_PARAMS_RATE


class LoginParamsThrottleTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        cache.clear()
        self.addCleanup(cache.clear)

    def _ask(self, identifier: str, address: str) -> int:
        return self.client.get(
            reverse("e2ee.login_params"), {"identifier": identifier}, REMOTE_ADDR=address
        ).status_code

    def test_a_script_harvesting_salts_is_cut_off(self) -> None:
        statuses = [self._ask(f"user{index}", "203.0.113.7") for index in range(LOGIN_PARAMS_RATE.limit + 5)]

        self.assertEqual(statuses[: LOGIN_PARAMS_RATE.limit], [200] * LOGIN_PARAMS_RATE.limit)
        self.assertEqual(set(statuses[LOGIN_PARAMS_RATE.limit :]), {429})

    def test_another_address_is_unaffected(self) -> None:
        for index in range(LOGIN_PARAMS_RATE.limit + 1):
            self._ask(f"user{index}", "203.0.113.7")

        self.assertEqual(self._ask("jess", "198.51.100.9"), 200)

    def test_the_limit_is_generous_and_per_address(self) -> None:
        self.assertGreaterEqual(LOGIN_PARAMS_RATE.limit, 30)
        self.assertLessEqual(LOGIN_PARAMS_RATE.window_seconds, 60)
        guarded = resolve(reverse("e2ee.login_params")).func
        self.assertEqual(getattr(guarded, "throttle_scope", None), "e2ee.login_params")
        self.assertIn("GET", getattr(guarded, "throttle_methods", frozenset()))

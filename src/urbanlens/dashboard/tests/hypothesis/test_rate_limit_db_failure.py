"""A database failure must not uncap spend at a paid API."""

from __future__ import annotations

from unittest import mock

from django.db import DatabaseError

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.services.core import rate_limiter
from urbanlens.dashboard.services.core.rate_limiter import SERVICE_REGISTRY, ServiceDefaults, check_rate_limit


class RateLimitDatabaseFailureTests(SimpleTestCase):
    """What happens when the limiter cannot read its own configuration."""

    def _with_unreadable_config(self):
        return mock.patch.object(rate_limiter, "get_limit_config", side_effect=DatabaseError("connection lost"))

    def test_a_billable_service_is_refused(self) -> None:
        with self._with_unreadable_config():
            self.assertFalse(
                check_rate_limit("google_places"), "a paid API must not run uncapped when the cap cannot be read"
            )

    def test_a_service_recorded_as_free_is_allowed(self) -> None:
        with self._with_unreadable_config():
            self.assertTrue(
                check_rate_limit("overpass"), "a free service should not be taken out by a database problem"
            )

    def test_an_unknown_service_is_refused(self) -> None:
        """The default has to be the safe one: nobody has vouched for this."""
        with self._with_unreadable_config():
            self.assertFalse(check_rate_limit("a-service-nobody-declared"))

    def test_every_free_marking_is_backed_by_its_own_notes(self) -> None:
        """`billable=False` is a claim about a provider's terms, not a guess.

        Each exemption must be justified where a reader will find it - in the service's own registry entry - so
        the next person can check it against the provider rather than take this file's word for it."""
        for service, defaults in SERVICE_REGISTRY.items():
            if defaults.billable:
                continue
            with self.subTest(service=service):
                self.assertIn(
                    "free", (defaults.notes or "").lower(), f"{service} is marked free but its notes do not say so"
                )

    def test_the_default_is_billable(self) -> None:
        self.assertTrue(ServiceDefaults(display_name="Anything").billable)

    def test_most_services_are_treated_as_billable(self) -> None:
        """A guard against a sweeping edit quietly exempting everything."""
        exempt = [name for name, defaults in SERVICE_REGISTRY.items() if not defaults.billable]
        self.assertLess(
            len(exempt),
            len(SERVICE_REGISTRY) / 2,
            f"{len(exempt)} of {len(SERVICE_REGISTRY)} services are exempt: {exempt}",
        )

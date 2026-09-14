"""Every registered service must carry a rate limit of some kind."""

from __future__ import annotations

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.plugins.registry import plugin_registry
from urbanlens.dashboard.services.core.rate_limiter import all_service_defaults

_LIMIT_FIELDS = ("calls_per_minute", "calls_per_day", "calls_per_30_days")


class PluginRateLimitCoverageTests(SimpleTestCase):
    def setUp(self) -> None:
        super().setUp()
        plugin_registry.discover()

    def test_the_registry_actually_loaded(self) -> None:
        """Guards the check below from passing on an empty registry."""
        self.assertGreater(len(all_service_defaults()), 20)

    def test_every_service_declares_at_least_one_limit(self) -> None:
        unlimited = sorted(
            name
            for name, defaults in all_service_defaults().items()
            if not any(getattr(defaults, field, None) for field in _LIMIT_FIELDS)
        )

        self.assertEqual(unlimited, [], "these services would call out with no rate limit at all")

    def test_every_service_has_a_display_name(self) -> None:
        """Site-admin cost and usage screens list services by this."""
        nameless = sorted(
            name
            for name, defaults in all_service_defaults().items()
            if not (getattr(defaults, "display_name", "") or "").strip()
        )

        self.assertEqual(nameless, [])

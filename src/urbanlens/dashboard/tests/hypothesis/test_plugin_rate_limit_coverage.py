"""Every registered service must carry a rate limit of some kind.

The limiter enforces whatever `ApiRateLimit` says, and those rows are seeded from
each plugin's declared defaults. A plugin that declares none therefore does not
get a lenient limit - it gets *no* limit, and its gateway calls out as fast as the
code asks, with nothing between a retry loop and someone else's API.

All 46 services currently declare at least one of per-minute, per-day or
per-30-day. This pins that, because the failure is invisible: an unthrottled
service works perfectly until it is throttled or billed by the provider instead.

Deliberately weak on purpose - it asserts *a* limit exists, not that the numbers
are right. Those are per-provider judgements that belong with each plugin.
"""

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

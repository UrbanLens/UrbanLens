"""A rate-limit row created from the generic fallback takes the service's defaults once they exist (P93).

`get_limit_config` creates a missing row from the registered defaults, or from a generic 20/min, 500/day fallback
when there are none. Registering defaults later changed nothing for a row that already existed, so a deployment kept
the limit nobody chose - for the REData historical-map tile proxy, a 500-tile daily cap.
"""

from __future__ import annotations

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.api_rate_limit import ApiRateLimit
from urbanlens.dashboard.services.core import rate_limiter

_SERVICE = "redata_historical_maps"


def _fallback_row(**overrides: object) -> ApiRateLimit:
    values: dict[str, object] = {
        "service": _SERVICE,
        "display_name": "Redata Historical Maps",
        "calls_per_minute": 20,
        "calls_per_day": 500,
    }
    values.update(overrides)
    return ApiRateLimit.objects.create(**values)


class FallbackRowAdoptsDefaultsTests(TestCase):
    def test_a_row_still_at_the_fallback_takes_the_registered_defaults(self) -> None:
        _fallback_row()
        defaults = rate_limiter.all_service_defaults()[_SERVICE]

        config = rate_limiter.get_limit_config(_SERVICE)

        self.assertEqual(config.display_name, defaults.display_name)
        self.assertEqual(config.calls_per_minute, defaults.calls_per_minute)
        self.assertEqual(config.calls_per_day, defaults.calls_per_day)
        self.assertEqual(config.notes, defaults.notes)
        config.refresh_from_db()
        self.assertEqual(config.calls_per_day, defaults.calls_per_day)

    def test_a_row_an_admin_changed_is_kept(self) -> None:
        _fallback_row(calls_per_day=800)

        config = rate_limiter.get_limit_config(_SERVICE)

        self.assertEqual(config.calls_per_day, 800)
        self.assertEqual(config.notes, "")

    def test_adopting_the_defaults_leaves_a_disabled_service_disabled(self) -> None:
        _fallback_row(enabled=False)

        config = rate_limiter.get_limit_config(_SERVICE)

        self.assertFalse(config.enabled)
        self.assertNotEqual(config.notes, "")
